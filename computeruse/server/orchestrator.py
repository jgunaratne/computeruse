"""Session orchestration: creates computers + model clients, runs agent loops as
asyncio tasks, records telemetry, and exposes the control surface used by the API.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from computeruse.agent.guardrails import GuardrailPolicy
from computeruse.agent.loop import AgentLoop, LoopResult, RunConfig, SessionController
from computeruse.agent.model import (
    ModelClient,
    ReplayModelClient,
    ScriptedModelClient,
)
from computeruse.agent.providers import ModelCatalog
from computeruse.computer.actions import (
    Action,
    ActionError,
    TypeText,
    describe_action,
    parse_action,
)
from computeruse.computer.base import Computer, ComputerError, Frame
from computeruse.computer.registry import BackendRegistry
from computeruse.computer.remote import RemoteComputer
from computeruse.config import Settings
from computeruse.server.chat import ChatService
from computeruse.server.operator import OperatorControl
from computeruse.telemetry.bus import EventBus
from computeruse.telemetry.events import (
    Event,
    EventType,
    Outcome,
    SessionRecord,
    SessionStatus,
    set_pricing_overrides,
)
from computeruse.telemetry.store import Store

log = logging.getLogger("computeruse.orchestrator")

Checker = Callable[["SessionRunner", LoopResult], Awaitable[tuple[bool | None, str]]]


class SessionRunner:
    """One live session: computer + model + loop + telemetry plumbing."""

    def __init__(self, orch: Orchestrator, record: SessionRecord, computer: Computer, model: ModelClient,
                 config: RunConfig, policy: GuardrailPolicy, isolated: bool, checker: Checker | None = None) -> None:
        self.orch = orch
        self.record = record
        self.computer = computer
        self.model = model
        self.config = config
        self.policy = policy
        self.isolated = isolated
        self.checker = checker
        self.controller = SessionController(start_paused=config.start_paused)
        self.operator = OperatorControl()
        self.loop: AgentLoop | None = None
        self.task: asyncio.Task | None = None
        self.done = asyncio.Event()
        self.result: LoopResult | None = None
        self._seq = 0
        self._frame_seq = 0
        self._preview_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- Recorder protocol --------------------------------------------------

    async def emit(self, type: EventType, data: dict[str, Any]) -> None:
        self._seq += 1
        event = Event(session_id=self.record.id, seq=self._seq, type=type, data=data)
        self._apply(event)
        if type != EventType.PREVIEW:
            self.orch.store.append_event(event)
            self.orch.store.update_session(self.record)
        self.orch.bus.publish(event)

    async def save_frame(self, frame: Frame, meta: dict[str, Any]) -> dict[str, Any]:
        self._frame_seq += 1
        seq = self._frame_seq
        await asyncio.to_thread(self.orch.store.save_frame, self.record.id, seq, frame.png)
        return {"seq": seq, "url": f"/api/sessions/{self.record.id}/frames/{seq}.png",
                "width": frame.width, "height": frame.height, "sha1": frame.sha1, **meta}

    def _apply(self, event: Event) -> None:
        r, d = self.record, event.data
        t = event.type
        if t == EventType.SESSION_STARTED:
            r.status = SessionStatus.PAUSED if self.config.start_paused else SessionStatus.RUNNING
            r.started_at = event.ts
            disp = d.get("display") or {}
            r.display_width, r.display_height = disp.get("width"), disp.get("height")
            r.live_view_url = d.get("live_view_url")
        elif t == EventType.SESSION_PAUSED:
            r.status = SessionStatus.PAUSED
        elif t == EventType.SESSION_RESUMED:
            r.status = SessionStatus.RUNNING
        elif t == EventType.APPROVAL_REQUESTED:
            r.status = SessionStatus.AWAITING_APPROVAL
        elif t == EventType.APPROVAL_RESOLVED:
            r.status = SessionStatus.RUNNING
        elif t == EventType.MODEL_CALLED:
            r.turns = d.get("turn", r.turns) + 1
            if d.get("cumulative_cost_usd") is not None:
                r.cost_usd = d["cumulative_cost_usd"]
        elif t == EventType.ACTION_EXECUTED:
            r.steps = max(r.steps, int(d.get("step", -1)) + 1)
        elif t == EventType.SESSION_ENDED:
            outcome = Outcome(d["outcome"])
            r.outcome = outcome
            r.outcome_reason = d.get("reason")
            r.ended_at = event.ts
            r.duration_ms = d.get("duration_ms")
            r.steps, r.turns = d.get("steps", r.steps), d.get("turns", r.turns)
            r.cost_usd = d.get("cost_usd", r.cost_usd)
            if d.get("usage"):
                from computeruse.telemetry.events import Usage

                r.usage = Usage(**d["usage"])
            if d.get("final_text"):
                r.metadata["final_text"] = d["final_text"]
            r.status = (SessionStatus.CANCELLED if outcome == Outcome.CANCELLED
                        else SessionStatus.COMPLETED if outcome == Outcome.COMPLETED else SessionStatus.FAILED)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"session-{self.record.id[:8]}")

    async def _run(self) -> None:
        r = self.record
        try:
            r.status = SessionStatus.STARTING
            self.orch.store.update_session(r)
            self.orch.bus.publish(Event(session_id=r.id, seq=0, type=EventType.TURN_STARTED,
                                        data={"turn": -1, "phase": "booting computer"}))
            await self.computer.start()
            if self.computer.display.width == 0:
                raise ComputerError("computer reported a 0x0 display")
            info = getattr(self.computer, "session_info", None)
            if callable(info):
                r.metadata["computer"] = info()
            self.loop = AgentLoop(
                session_id=r.id, computer=self.computer, model=self.model, config=self.config, recorder=self,
                policy=self.policy, controller=self.controller, backend=r.backend, isolated=self.isolated,
                model_name=r.model,
            )
            self._preview_task = asyncio.create_task(self._preview_loop())
            self.result = await self.loop.run()
            if self.checker is not None:
                try:
                    passed, detail = await self.checker(self, self.result)
                except Exception as e:  # noqa: BLE001
                    passed, detail = False, f"checker crashed: {type(e).__name__}: {e}"
                r.eval_passed, r.eval_detail = passed, detail
                self.orch.store.update_session(r)
                await self.emit(EventType.ERROR if passed is False else EventType.TURN_STARTED,
                                {"phase": "eval_check", "passed": passed, "detail": detail})
        except Exception as e:  # noqa: BLE001 - boot failures end up here
            log.exception("session %s failed before/after the loop", r.id)
            if r.status not in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED):
                detail = f"{type(e).__name__}: {e}"
                diag = getattr(self.computer, "diagnostics", None)
                if callable(diag):
                    extra = diag()
                    if extra:
                        detail += "\n" + extra[-1500:]
                await self.emit(EventType.ERROR, {"where": "boot", "message": detail})
                await self.emit(EventType.SESSION_ENDED, {
                    "outcome": Outcome.COMPUTER_ERROR.value, "reason": detail[:500], "steps": r.steps,
                    "turns": r.turns, "duration_ms": 0, "usage": r.usage.model_dump(), "cost_usd": r.cost_usd})
        finally:
            if self._preview_task:
                self._preview_task.cancel()
            try:
                await asyncio.wait_for(self.computer.stop(), timeout=30)
            except Exception as e:  # noqa: BLE001
                log.warning("computer stop failed for %s: %s", r.id, e)
            await self._close_model()
            if self.operator.active:  # session ended while a human was driving
                self.operator.stop()
            r.metadata.pop("operator_control", None)
            self.orch.store.update_session(r)
            self.orch.bus.close_session(r.id)
            self.done.set()
            self.orch._release(self)

    async def _close_model(self) -> None:
        """Record what the model client knows about itself (e.g. the Antigravity conversation id), then release it."""
        describe = getattr(self.model, "describe", None)
        if callable(describe):
            try:
                self.record.metadata["model_client"] = describe()
            except Exception as e:  # noqa: BLE001 - metadata only
                log.debug("model describe failed for %s: %s", self.record.id, e)
        close = getattr(self.model, "aclose", None)
        if callable(close):
            try:
                await asyncio.wait_for(close(), timeout=30)
            except Exception as e:  # noqa: BLE001
                log.warning("model client close failed for %s: %s", self.record.id, e)

    # -- controls -----------------------------------------------------------

    async def control(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        c = self.controller
        if command == "take_control":
            return await self.take_control()
        if command == "release_control":
            return await self.release_control(resume=bool(payload.get("resume")))
        if command in ("resume", "step") and self.operator.active:
            await self.release_control(resume=False)  # the agent cannot share the mouse with a human
        if command == "pause":
            c.pause()
        elif command == "resume":
            c.resume()
        elif command == "step":
            c.step()
        elif command == "cancel":
            c.cancel()
            if self.task and self.loop is None:  # still booting: abort the boot
                self.task.cancel()
        elif command in ("approve", "reject"):
            approval_id = payload.get("approval_id") or c.pending_approval_id
            if not approval_id or not c.resolve_approval(approval_id, command == "approve"):
                raise ValueError("no matching pending approval")
        elif command == "instruct":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ValueError("instruction text is required")
            c.instruct(text)
        elif command == "budget":
            await self.set_budget(payload)
        else:
            raise ValueError(f"unknown command {command!r}")
        return self._control_payload()

    BUDGET_KEYS = ("max_steps", "max_turns", "max_duration_s", "max_cost_usd")

    def budget(self) -> dict[str, Any]:
        """The limits the loop is enforcing right now (mirrored into the session metadata)."""
        return {k: getattr(self.config, k) for k in self.BUDGET_KEYS}

    async def set_budget(self, payload: dict[str, Any]) -> None:
        """Raise or lower budgets of a session that is still running; the loop checks them before every
        turn and after every step, so a raise lets a session that would have stopped at `max_steps` carry
        on, and a lower one ends it at the next boundary."""
        changes = {k: payload[k] for k in self.BUDGET_KEYS if payload.get(k) is not None}
        if not changes:
            raise ValueError("budget: pass at least one of " + ", ".join(self.BUDGET_KEYS))
        if "max_steps" in changes and int(changes["max_steps"]) <= self.record.steps:
            raise ValueError(f"max_steps must exceed the {self.record.steps} steps already taken "
                             "(cancel the session to stop it)")
        before = {k: getattr(self.config, k) for k in changes}
        for k, v in changes.items():
            setattr(self.config, k, v)
        self.record.metadata["budget"] = self.budget()
        await self.emit(EventType.BUDGET_CHANGED, {"changes": changes, "before": before, "budget": self.budget(),
                                                   "steps": self.record.steps, "turns": self.record.turns})

    def _control_payload(self) -> dict[str, Any]:
        return {"ok": True, "status": self.record.status.value,
                "control": self.operator.snapshot() if self.operator.active else None}

    @property
    def _agent_idle(self) -> bool:
        """True when the loop is parked at a checkpoint, i.e. it will not touch the computer."""
        return self.record.status in (SessionStatus.PAUSED, SessionStatus.AWAITING_APPROVAL)

    async def take_control(self) -> dict[str, Any]:
        """Hand the mouse and keyboard to the operator. Pauses the agent first; input is accepted as
        soon as the loop reaches its next checkpoint (the UI waits for `status == paused`)."""
        if self.record.status.terminal or self.done.is_set():
            raise ValueError("session has ended")
        if self.operator.active:
            return self._control_payload()
        if not self._agent_idle:
            self.controller.pause()
        self.operator.start()
        self.record.metadata["operator_control"] = {"active": True, "since": self.operator.since}
        await self.emit(EventType.OPERATOR_CONTROL, {"state": "taken", "status": self.record.status.value,
                                                     "waiting_for_pause": not self._agent_idle})
        return self._control_payload()

    async def release_control(self, *, resume: bool = False) -> dict[str, Any]:
        """Hand control back: let go of anything still held, capture the screen the operator left
        behind, record a summary and brief the model on it before its next turn."""
        if not self.operator.active:
            raise ValueError("the operator is not in control")
        if "left" in self.operator.buttons_down:
            try:
                async with self._lock:
                    await self.computer.execute(parse_action({"action": "left_mouse_up"}))
            except Exception:  # noqa: BLE001 - best effort
                pass
        frame: Frame | None = None
        try:
            async with self._lock:
                frame = await self.computer.screenshot()
        except Exception as e:  # noqa: BLE001 - the computer may be gone; still release
            log.warning("screenshot after operator control failed for %s: %s", self.record.id, e)
        if frame is not None:
            ref = await self.save_frame(frame, {"kind": "after_control", "step": None})
            await self.emit(EventType.FRAME, ref)
            if self.loop:
                self.loop.last_frame = frame
        snap = self.operator.stop()
        self.record.metadata.pop("operator_control", None)
        if snap["inputs"]:
            self.controller.note_manual_action(snap["summary"])
        await self.emit(EventType.OPERATOR_CONTROL, {"state": "released", **snap, "resumed": resume})
        if resume:
            self.controller.resume()
        return self._control_payload()

    async def operator_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One raw input event from the console while the operator is in control.

        Deliberately light: no settle delay, no screenshot, nothing persisted per event — pointer
        moves arrive at tens of hertz and keystrokes may be passwords. The takeover is summarised
        once, when control is released.
        """
        if not self.operator.active:
            raise ValueError("take control first")
        if not self._agent_idle:
            raise ValueError("waiting for the agent to finish its current step")
        try:
            action = parse_action(payload)
        except ActionError as e:
            raise ValueError(str(e)) from e
        if action.action in ("zoom", "screenshot", "cursor_position", "wait"):
            raise ValueError(f"{action.action} is not an operator input")
        async with self._lock:
            result = await self.computer.execute(action)
        self.operator.note(action, result.ok)
        return {"ok": result.ok, "error": result.error}

    async def manual_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One discrete operator action while the agent is paused, recorded in the timeline with the
        resulting screen (used for the console's quick "type"/"key" inputs and by the API/CLI)."""
        if not self._agent_idle:
            raise ValueError("pause the session before taking manual control")
        try:
            action = parse_action(payload)
        except ActionError as e:
            raise ValueError(str(e)) from e
        async with self._lock:
            result = await self.computer.execute(action)
            await asyncio.sleep(0.3)
            frame = await self.computer.screenshot()
        ref = await self.save_frame(frame, {"kind": "manual", "step": None})
        await self.emit(EventType.FRAME, ref)
        desc = describe_operator_action(action)
        if self.operator.active:
            self.operator.note(action, result.ok)  # folded into the takeover summary
        else:
            self.controller.note_manual_action(desc)
        if self.loop:
            self.loop.last_frame = frame
        await self.emit(EventType.MANUAL_ACTION, {"description": desc, "kind": action.action, "ok": result.ok,
                                                  "error": result.error,
                                                  "coordinates": payload.get("coordinate") and [payload["coordinate"]]})
        return {"ok": result.ok, "error": result.error}

    async def _preview_loop(self) -> None:
        """Push cheap JPEG frames to live viewers while the session is active (faster while a human drives)."""
        s = self.orch.settings
        last_sha: str | None = None
        while True:
            fps = s.control_preview_fps if self.operator.active else s.preview_fps
            await asyncio.sleep(1.0 / max(0.5, float(fps)))
            if self.orch.bus.subscriber_count(self.record.id) == 0:
                continue
            try:
                async with self._lock:
                    jpeg = await self._preview_jpeg()
            except Exception:  # noqa: BLE001 - preview is best-effort
                continue
            if jpeg is None:
                continue
            sha = str(hash(jpeg))
            if sha == last_sha:
                continue
            last_sha = sha
            self._seq += 1
            self.orch.bus.publish(Event(session_id=self.record.id, seq=self._seq, type=EventType.PREVIEW,
                                        data={"jpeg_b64": base64.b64encode(jpeg).decode()}))

    async def _preview_jpeg(self) -> bytes | None:
        if isinstance(self.computer, RemoteComputer):
            return await self.computer.preview_jpeg(quality=50)
        frame = await self.computer.screenshot()
        buf = io.BytesIO()
        frame.image().save(buf, format="JPEG", quality=50)
        return buf.getvalue()


def describe_operator_action(action: Action) -> str:
    """Like `describe_action`, but never records what a human typed (it may be a password)."""
    if isinstance(action, TypeText):
        n = len(action.text)
        return f"type {n} character{'s' if n != 1 else ''}"
    return describe_action(action)


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.bus = EventBus()
        self.registry = BackendRegistry(settings)
        self.catalog = ModelCatalog(settings)
        self.chat = ChatService(self)
        self.runners: dict[str, SessionRunner] = {}
        self._desktop_lock = asyncio.Lock()
        set_pricing_overrides(settings.pricing_overrides)
        interrupted = self.store.mark_interrupted()
        if interrupted:
            log.warning("marked %d sessions interrupted by a previous run", interrupted)
        for provider, st in self.catalog.refresh().items():
            log.info("model provider %s: %s", provider, st.reason)

    # -- construction helpers --------------------------------------------------

    def make_policy(self, overrides: dict[str, Any] | None = None) -> GuardrailPolicy:
        """Guardrails for one session. Overrides can only tighten the global settings:
        extra blocked domains/keys are unioned, an allowlist replaces/narrows the global one."""
        s, o = self.settings, overrides or {}
        allowed = list(o.get("allowed_domains") or s.allowed_domains)
        if s.allowed_domains and o.get("allowed_domains"):
            allowed = [d for d in o["allowed_domains"] if d in s.allowed_domains] or list(s.allowed_domains)
        return GuardrailPolicy.default(
            blocked_keys=[*s.blocked_keys, *o.get("blocked_keys", [])],
            desktop_blocked_keys=s.desktop_blocked_keys,
            allowed_domains=allowed,
            blocked_domains=[*s.blocked_domains, *o.get("blocked_domains", [])],
            max_type_length=min(s.max_type_length, int(o.get("max_type_length") or s.max_type_length)),
        )

    def make_model(self, name: str, computer: Computer, script: list[dict[str, Any]] | None = None) -> ModelClient:
        if name == "scripted":
            if not script:
                raise ValueError("model 'scripted' requires a script (pick a demo task or provide one)")
            locator = getattr(computer, "locate", None)
            return ScriptedModelClient(script, locator=locator, latency_ms=250)
        if name.startswith("replay:"):
            source = name.split(":", 1)[1]
            events = self.store.get_events(source, types=[EventType.MODEL_CALLED.value])
            if not events:
                raise ValueError(f"no recorded model turns for session {source}")
            return ReplayModelClient([e.data.get("content", []) for e in events], source)
        return self.catalog.make(name)

    def make_config(self, task: str, overrides: dict[str, Any] | None = None, backend: str = "") -> RunConfig:
        s = self.settings
        cfg = RunConfig(
            task=task, max_steps=s.default_max_steps, max_duration_s=s.default_max_duration_s,
            max_cost_usd=s.default_max_cost_usd, screenshot_history=s.screenshot_history,
            settle_ms=s.settle_ms, stuck_threshold=s.stuck_threshold, max_tokens=s.max_tokens,
            tool_version=s.tool_version, approval_timeout_s=s.approval_timeout_s,
        )
        if backend == "simulated":
            cfg.settle_ms, cfg.settle_checks = 0, 0
        for k, v in (overrides or {}).items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    # -- sessions -----------------------------------------------------------------

    async def create_session(self, *, task: str, backend: str, model: str, overrides: dict[str, Any] | None = None,
                             script: list[dict[str, Any]] | None = None, tags: list[str] | None = None,
                             backend_options: dict[str, Any] | None = None, eval_run_id: str | None = None,
                             eval_task_id: str | None = None, checker: Checker | None = None,
                             autostart: bool = True, metadata: dict[str, Any] | None = None,
                             guardrails: dict[str, Any] | None = None) -> SessionRecord:
        info = self.registry.info(backend)
        if not info.available:
            raise ValueError(f"backend {backend!r} unavailable: {info.reason}")
        if backend == "desktop" and any(r.record.backend == "desktop" and not r.done.is_set()
                                        for r in self.runners.values()):
            raise ValueError("another session is already driving the desktop; wait for it to finish")
        computer = self.registry.create(backend, backend_options)
        model_client = self.make_model(model, computer, script)
        config = self.make_config(task, overrides, backend)
        meta = dict(metadata or {})
        if guardrails:
            meta["guardrails"] = guardrails
        meta["budget"] = {k: getattr(config, k) for k in SessionRunner.BUDGET_KEYS}
        record = SessionRecord(
            id=uuid.uuid4().hex[:12], task=task, backend=backend, model=model, tags=tags or [],
            eval_run_id=eval_run_id, eval_task_id=eval_task_id, metadata=meta,
        )
        self.store.create_session(record)
        runner = SessionRunner(self, record, computer, model_client, config, self.make_policy(guardrails),
                               isolated=info.isolated, checker=checker)
        self.runners[record.id] = runner
        if autostart:
            runner.start()
        return record

    def _release(self, runner: SessionRunner) -> None:
        # Keep finished runners around briefly so late websocket joins still get history fast.
        asyncio.get_event_loop().call_later(120, self.runners.pop, runner.record.id, None)

    def get_session(self, session_id: str) -> SessionRecord | None:
        runner = self.runners.get(session_id)
        return runner.record if runner else self.store.get_session(session_id)

    def list_sessions(self, **filters: Any) -> list[SessionRecord]:
        return self.store.list_sessions(**filters)

    def events(self, session_id: str, after_seq: int = -1) -> list[Event]:
        return self.store.get_events(session_id, after_seq=after_seq)

    async def control(self, session_id: str, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        runner = self.runners.get(session_id)
        if not runner or runner.done.is_set():
            raise ValueError("session is not active")
        return await runner.control(command, payload)

    async def manual_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        runner = self.runners.get(session_id)
        if not runner or runner.done.is_set():
            raise ValueError("session is not active")
        return await runner.manual_action(payload)

    async def operator_input(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        runner = self.runners.get(session_id)
        if not runner or runner.done.is_set():
            raise ValueError("session is not active")
        return await runner.operator_input(payload)

    async def wait(self, session_id: str, timeout: float | None = None) -> SessionRecord:
        runner = self.runners.get(session_id)
        if runner:
            await asyncio.wait_for(runner.done.wait(), timeout=timeout)
            return runner.record
        rec = self.store.get_session(session_id)
        if not rec:
            raise KeyError(session_id)
        return rec

    async def shutdown(self) -> None:
        for runner in list(self.runners.values()):
            if not runner.done.is_set():
                runner.controller.cancel()
                if runner.task:
                    runner.task.cancel()
        for runner in list(self.runners.values()):
            if runner.task:
                try:
                    await asyncio.wait_for(runner.done.wait(), timeout=15)
                except TimeoutError:
                    pass
        await self.chat.close()
        await self.catalog.close()
        self.store.close()


def now_ms() -> int:
    return int(time.time() * 1000)
