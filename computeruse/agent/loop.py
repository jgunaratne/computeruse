"""The agent loop: observe → think → act, wrapped in the reliability machinery
that real-world usage needs.

Reliability features (each one maps to a failure class we measure):
* Budgets: max steps / turns / wall-clock / USD → `max_steps`, `timeout`, `budget_exceeded`.
* Stuck detection: identical action on an identical screen → nudge, then `stuck`.
* Screen-settle capture: screenshots are taken after the UI stops changing, so the
  model never reasons about half-rendered frames.
* Coordinate scaling: large displays are presented to the model at a supported
  resolution; actions are mapped back to physical pixels.
* Context pruning: only the N most recent screenshots stay in the prompt.
* Guardrails with human-in-the-loop approval and hard blocks.
* Operator controls: pause, resume, single-step, cancel, inject guidance, and
  manual takeover (the loop is told what the human changed).
* Everything emits structured telemetry via a `Recorder`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any, Protocol

from computeruse.agent.guardrails import Decision, GuardrailContext, GuardrailPolicy
from computeruse.agent.model import ModelClient, ModelError
from computeruse.agent.prompts import computer_tool, system_prompt
from computeruse.computer.actions import (
    Action,
    ActionError,
    ActionResult,
    CursorPosition,
    KeyPress,
    Screenshot,
    Wait,
    Zoom,
    action_coordinates,
    describe_action,
    parse_action,
)
from computeruse.computer.base import Computer, ComputerError, Frame
from computeruse.telemetry.events import EventType, Outcome, Usage, estimate_cost_usd

# Resolutions Claude handles best (long edge <= 1568 px, ~1.15 MP max).
MAX_PIXELS = 1_150_000
MAX_LONG_EDGE = 1568
TARGETS = {4 / 3: (1024, 768), 16 / 10: (1280, 800), 16 / 9: (1366, 768)}
# How many times a turn cut off by max_tokens is asked to continue before giving up.
MAX_TRUNCATED_TURNS = 2


@dataclass
class RunConfig:
    task: str
    max_steps: int = 40
    max_turns: int | None = None
    max_duration_s: float = 900.0
    max_cost_usd: float | None = None
    screenshot_history: int = 3
    settle_ms: int = 350
    settle_checks: int = 3
    settle_check_ms: int = 150
    stuck_threshold: int = 3
    max_tokens: int = 4096
    tool_version: str = "computer_20250124"
    system_prompt_extra: str | None = None
    auto_approve: bool = False
    approval_timeout_s: float = 600.0
    start_paused: bool = False


@dataclass
class LoopResult:
    outcome: Outcome
    reason: str | None
    final_text: str | None
    steps: int
    turns: int
    usage: Usage
    cost_usd: float | None
    duration_ms: float
    last_frame_sha1: str | None = None


class Recorder(Protocol):
    """Where the loop sends telemetry. Implemented by the orchestrator (and tests)."""

    async def emit(self, type: EventType, data: dict[str, Any]) -> None: ...

    async def save_frame(self, frame: Frame, meta: dict[str, Any]) -> dict[str, Any]:
        """Persist a frame; return a JSON-able reference (seq, url, width, height, sha1)."""
        ...


class SessionCancelled(Exception):
    pass


class SessionController:
    """Thread of control shared between the loop and the API/UI."""

    def __init__(self, start_paused: bool = False) -> None:
        self._paused = start_paused
        self._cancelled = False
        self._step_allowance = 0
        self._wake = asyncio.Event()
        self._approval: tuple[str, asyncio.Future[bool]] | None = None
        self._instructions: list[str] = []
        self._manual_notes: list[str] = []
        self.state_changed = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def pending_approval_id(self) -> str | None:
        return self._approval[0] if self._approval else None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._step_allowance = 0
        self._wake.set()

    def step(self) -> None:
        self._paused = True
        self._step_allowance = 1
        self._wake.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._wake.set()
        if self._approval and not self._approval[1].done():
            self._approval[1].set_result(False)

    def instruct(self, text: str) -> None:
        if text.strip():
            self._instructions.append(text.strip())

    def note_manual_action(self, description: str) -> None:
        self._manual_notes.append(description)

    def drain_notes(self) -> tuple[list[str], list[str]]:
        inst, notes = self._instructions, self._manual_notes
        self._instructions, self._manual_notes = [], []
        return inst, notes

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        if not self._approval or self._approval[0] != approval_id or self._approval[1].done():
            return False
        self._approval[1].set_result(approved)
        return True

    async def await_approval(self, approval_id: str, timeout: float) -> bool:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._approval = (approval_id, fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            return False
        finally:
            self._approval = None

    async def checkpoint(self, on_pause, on_resume, consume: bool = True) -> None:
        """Block here while paused. `consume=True` marks an *action* boundary: a
        single-step allowance is spent only at those, so "step" means exactly one
        executed action regardless of how many turn-level checkpoints precede it."""
        if self._cancelled:
            raise SessionCancelled()
        if not self._paused:
            return
        if self._step_allowance > 0:
            if consume:
                self._step_allowance -= 1
            return
        await on_pause()
        while self._paused and self._step_allowance == 0 and not self._cancelled:
            self._wake.clear()
            await self._wake.wait()
        if self._cancelled:
            raise SessionCancelled()
        if self._step_allowance > 0 and consume:
            self._step_allowance -= 1
        await on_resume()


@dataclass
class CoordinateScaler:
    physical: tuple[int, int]
    model: tuple[int, int]

    @classmethod
    def for_display(cls, w: int, h: int) -> CoordinateScaler:
        if w * h <= MAX_PIXELS and max(w, h) <= MAX_LONG_EDGE:
            return cls((w, h), (w, h))
        ratio = w / h
        for r, (tw, th) in TARGETS.items():
            if abs(ratio - r) < 0.02:
                return cls((w, h), (tw, th))
        scale = min((MAX_PIXELS / (w * h)) ** 0.5, MAX_LONG_EDGE / max(w, h))
        return cls((w, h), (int(w * scale), int(h * scale)))

    @property
    def active(self) -> bool:
        return self.physical != self.model

    def to_physical(self, x: int, y: int) -> tuple[int, int]:
        if not self.active:
            return x, y
        return (round(x * self.physical[0] / self.model[0]), round(y * self.physical[1] / self.model[1]))

    def to_model(self, x: int, y: int) -> tuple[int, int]:
        if not self.active:
            return x, y
        return (round(x * self.model[0] / self.physical[0]), round(y * self.model[1] / self.physical[1]))

    _COORD_RE = re.compile(r"\((\d+),\s*(\d+)\)")

    def scale_output(self, output: str | None) -> str | None:
        """Map "(x, y)" tuples in tool output (cursor_position) back to model space."""
        if not output or not self.active:
            return output
        def _rewrite(m: re.Match[str]) -> str:
            x, y = self.to_model(int(m.group(1)), int(m.group(2)))
            return f"({x}, {y})"

        return self._COORD_RE.sub(_rewrite, output)

    def scale_action_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.active:
            return payload
        out = dict(payload)
        for key in ("coordinate", "start_coordinate"):
            c = out.get(key)
            if isinstance(c, (list, tuple)) and len(c) == 2:
                out[key] = list(self.to_physical(int(c[0]), int(c[1])))
        region = out.get("region")
        if isinstance(region, (list, tuple)) and len(region) == 4:
            x0, y0 = self.to_physical(int(region[0]), int(region[1]))
            x1, y1 = self.to_physical(int(region[2]), int(region[3]))
            out["region"] = [x0, y0, x1, y1]
        return out

    def frame_png_for_model(self, frame: Frame) -> bytes:
        if not self.active:
            return frame.png
        img = frame.image().resize(self.model)
        return Frame.from_image(img).png


class AgentLoop:
    def __init__(
        self,
        *,
        session_id: str,
        computer: Computer,
        model: ModelClient,
        config: RunConfig,
        recorder: Recorder,
        policy: GuardrailPolicy | None = None,
        controller: SessionController | None = None,
        backend: str = "unknown",
        isolated: bool = True,
        model_name: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.computer = computer
        self.model = model
        self.config = config
        self.rec = recorder
        self.policy = policy or GuardrailPolicy()
        self.controller = controller or SessionController(start_paused=config.start_paused)
        self.backend = backend
        self.isolated = isolated
        self.model_name = model_name or getattr(model, "name", "model")
        self.steps = 0
        self.turns = 0
        self.usage = Usage()
        self.cost_usd: float | None = None
        self.last_frame: Frame | None = None
        self.final_text: str | None = None
        self._outcome: Outcome | None = None
        self._reason: str | None = None
        self._stuck_key: str | None = None
        self._stuck_repeats = 0
        self._stuck_nudged = False
        self._consecutive_blocks = 0
        self._truncated = 0
        self._recent: list[str] = []
        w, h = computer.display.size
        self.scaler = CoordinateScaler.for_display(w, h)

    # -- public ------------------------------------------------------------

    async def run(self) -> LoopResult:
        started = time.monotonic()
        cfg = self.config
        await self.rec.emit(EventType.SESSION_STARTED, {
            "task": cfg.task, "backend": self.backend, "model": self.model_name,
            "display": {"width": self.scaler.physical[0], "height": self.scaler.physical[1]},
            "model_display": {"width": self.scaler.model[0], "height": self.scaler.model[1]},
            "config": {"max_steps": cfg.max_steps, "max_duration_s": cfg.max_duration_s,
                       "max_cost_usd": cfg.max_cost_usd, "screenshot_history": cfg.screenshot_history,
                       "stuck_threshold": cfg.stuck_threshold},
            "live_view_url": self.computer.live_view_url(),
        })
        messages: list[dict[str, Any]] = []
        try:
            frame = await self._capture(retries=3)
            ref = await self.rec.save_frame(frame, {"kind": "initial", "step": None})
            await self.rec.emit(EventType.FRAME, ref)
            messages.append({"role": "user", "content": [
                {"type": "text", "text": cfg.task}, self._image_block(frame)]})
            await self._main(messages, started)
        except SessionCancelled:
            self._end(Outcome.CANCELLED, "cancelled by user")
        except asyncio.CancelledError:
            self._end(Outcome.CANCELLED, "cancelled by user" if self.controller.cancelled else "task cancelled")
            if not self.controller.cancelled:
                raise
        except ComputerError as e:
            await self.rec.emit(EventType.ERROR, {"where": "computer", "message": str(e)})
            self._end(Outcome.COMPUTER_ERROR, str(e))
        except ModelError as e:
            await self.rec.emit(EventType.ERROR, {"where": "model", "message": str(e)})
            self._end(Outcome.MODEL_ERROR, str(e))
        except Exception as e:  # noqa: BLE001 - last line of defence, always recorded
            await self.rec.emit(EventType.ERROR, {"where": "loop", "message": f"{type(e).__name__}: {e}",
                                                  "traceback": traceback.format_exc()[-4000:]})
            self._end(Outcome.INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        duration_ms = (time.monotonic() - started) * 1000
        result = LoopResult(
            outcome=self._outcome or Outcome.INTERNAL_ERROR, reason=self._reason, final_text=self.final_text,
            steps=self.steps, turns=self.turns, usage=self.usage, cost_usd=self.cost_usd,
            duration_ms=duration_ms, last_frame_sha1=self.last_frame.sha1 if self.last_frame else None,
        )
        await self.rec.emit(EventType.SESSION_ENDED, {
            "outcome": result.outcome.value, "reason": result.reason, "final_text": result.final_text,
            "steps": result.steps, "turns": result.turns, "duration_ms": round(duration_ms),
            "usage": self.usage.model_dump(), "cost_usd": self.cost_usd,
        })
        return result

    # -- main loop ---------------------------------------------------------

    async def _main(self, messages: list[dict[str, Any]], started: float) -> None:
        cfg = self.config
        system = system_prompt(self.backend, self.scaler.model, cfg.system_prompt_extra)
        tools = [computer_tool(cfg.tool_version, self.scaler.model)]
        while self._outcome is None:
            await self._checkpoint(consume=False)  # pause point; a "step" is spent at the action boundary
            if cfg.max_turns is not None and self.turns >= cfg.max_turns:
                return self._end(Outcome.MAX_STEPS, f"reached max_turns={cfg.max_turns}")
            if self.steps >= cfg.max_steps:
                return self._end(Outcome.MAX_STEPS, f"reached max_steps={cfg.max_steps}")
            elapsed = time.monotonic() - started
            if elapsed > cfg.max_duration_s:
                return self._end(Outcome.TIMEOUT, f"exceeded {cfg.max_duration_s:.0f}s")
            if cfg.max_cost_usd is not None and self.cost_usd is not None and self.cost_usd > cfg.max_cost_usd:
                return self._end(Outcome.BUDGET_EXCEEDED, f"spent ${self.cost_usd:.2f} > ${cfg.max_cost_usd:.2f}")

            await self._inject_operator_context(messages)
            self._prune_images(messages)

            turn_idx = self.turns
            self.turns += 1
            await self.rec.emit(EventType.TURN_STARTED, {"turn": turn_idx, "step": self.steps})
            turn = await self.model.create(system=system, messages=messages, tools=tools, max_tokens=cfg.max_tokens)
            self.usage.add(turn.usage)
            self.cost_usd = estimate_cost_usd(self.model_name, self.usage)
            await self.rec.emit(EventType.MODEL_CALLED, {
                "turn": turn_idx, "latency_ms": round(turn.latency_ms, 1), "retries": turn.retries,
                "stop_reason": turn.stop_reason, "usage": turn.usage.model_dump(), "model": turn.model,
                "n_tool_uses": len(turn.tool_uses), "content": turn.content,
                "cumulative_cost_usd": self.cost_usd,
            })
            for text in turn.texts:
                await self.rec.emit(EventType.ASSISTANT_TEXT, {"turn": turn_idx, "text": text})
            messages.append({"role": "assistant", "content": turn.content})

            if not turn.tool_uses:
                if turn.stop_reason == "max_tokens" and self._truncated < MAX_TRUNCATED_TURNS:
                    self._truncated += 1
                    await self.rec.emit(EventType.ERROR, {"where": "model", "recoverable": True,
                                                           "message": "turn cut off by max_tokens; asking to continue"})
                    messages.append({"role": "user", "content": [{"type": "text", "text": (
                        "Your previous reply was cut off by the output token limit. Continue with the task: "
                        "keep reasoning brief and call the computer tool for the next action.")}]})
                    continue
                self.final_text = "\n\n".join(turn.texts) or None
                return self._end(Outcome.COMPLETED, "model ended its turn")
            self._truncated = 0

            results: list[dict[str, Any]] = []
            for tu in turn.tool_uses:
                if self._outcome is not None:
                    break
                if self.steps >= cfg.max_steps:
                    self._end(Outcome.MAX_STEPS, f"reached max_steps={cfg.max_steps}")
                    break
                await self._checkpoint()
                results.append(await self._handle_tool_use(tu, turn_idx))
            if results:
                # Pair every tool_use with a result even when we stop early, keeping the transcript valid.
                answered = {r["tool_use_id"] for r in results}
                for tu in turn.tool_uses:
                    if tu["id"] not in answered:
                        results.append({"type": "tool_result", "tool_use_id": tu["id"], "is_error": True,
                                        "content": [{"type": "text", "text": "Session ended before this action ran."}]})
                messages.append({"role": "user", "content": results})

    async def _handle_tool_use(self, tu: dict[str, Any], turn_idx: int) -> dict[str, Any]:
        step_idx = self.steps
        self.steps += 1
        tool_use_id = tu["id"]
        if tu.get("name") != "computer":
            return self._tool_result(tool_use_id, error=f"unknown tool {tu.get('name')!r}")
        raw_input = tu.get("input") or {}
        try:
            action = parse_action(self.scaler.scale_action_input(raw_input))
        except ActionError as e:
            await self.rec.emit(EventType.ACTION_EXECUTED, {
                "step": step_idx, "turn": turn_idx, "kind": raw_input.get("action"), "ok": False,
                "error": str(e), "duration_ms": 0, "description": "invalid action"})
            return self._tool_result(tool_use_id, error=f"Invalid action: {e}")

        description = describe_action(action)
        coords = [list(c) for c in action_coordinates(action)]
        await self.rec.emit(EventType.ACTION_PROPOSED, {
            "step": step_idx, "turn": turn_idx, "tool_use_id": tool_use_id, "kind": action.action,
            "action": action.model_dump(exclude_none=True), "model_input": raw_input,
            "description": description, "coordinates": coords,
        })

        # Guardrails -------------------------------------------------------
        ctx = GuardrailContext(backend=self.backend, task=self.config.task, step=step_idx,
                               isolated=self.isolated, recent_actions=self._recent[-10:])
        verdict = self.policy.evaluate(action, ctx)
        await self.rec.emit(EventType.GUARDRAIL_DECISION, {
            "step": step_idx, "decision": verdict.decision.value, "rule": verdict.rule, "reason": verdict.reason})
        if verdict.decision == Decision.BLOCK:
            self._consecutive_blocks += 1
            await self.rec.emit(EventType.ACTION_EXECUTED, {
                "step": step_idx, "turn": turn_idx, "kind": action.action, "ok": False, "blocked": True,
                "error": verdict.reason, "duration_ms": 0, "description": description, "coordinates": coords})
            if self._consecutive_blocks >= 4:
                self._end(Outcome.GUARDRAIL_BLOCKED, f"repeatedly blocked: {verdict.reason}")
            return self._tool_result(tool_use_id, error=f"Action blocked by policy: {verdict.reason}. "
                                                        "Choose a different approach or stop and explain.")
        self._consecutive_blocks = 0
        if verdict.decision == Decision.REQUIRE_APPROVAL and not self.config.auto_approve:
            approval_id = f"{self.session_id}:{step_idx}"
            await self.rec.emit(EventType.APPROVAL_REQUESTED, {
                "step": step_idx, "approval_id": approval_id, "kind": action.action, "description": description,
                "action": action.model_dump(exclude_none=True), "rule": verdict.rule, "reason": verdict.reason})
            approved = await self.controller.await_approval(approval_id, self.config.approval_timeout_s)
            await self.rec.emit(EventType.APPROVAL_RESOLVED, {
                "step": step_idx, "approval_id": approval_id, "approved": approved})
            if self.controller.cancelled:
                raise SessionCancelled()
            if not approved:
                await self.rec.emit(EventType.ACTION_EXECUTED, {
                    "step": step_idx, "turn": turn_idx, "kind": action.action, "ok": False, "rejected": True,
                    "error": "rejected by user", "duration_ms": 0, "description": description, "coordinates": coords})
                return self._tool_result(tool_use_id, error=f"The user declined this action ({verdict.reason}). "
                                                            "Choose a different approach or stop and explain.")

        # Stuck detection --------------------------------------------------
        before_sha = self.last_frame.sha1 if self.last_frame else ""
        key = f"{json.dumps(action.model_dump(exclude_none=True), sort_keys=True)}|{before_sha}"
        self._stuck_repeats = self._stuck_repeats + 1 if key == self._stuck_key else 1
        self._stuck_key = key
        nudge: str | None = None
        if self._stuck_repeats >= self.config.stuck_threshold:
            if self._stuck_repeats >= self.config.stuck_threshold * 2:
                self._end(Outcome.STUCK, f"'{description}' repeated {self._stuck_repeats}x with no screen change")
                return self._tool_result(tool_use_id, error="Session stopped: the same action keeps producing no change.")
            if not self._stuck_nudged:
                self._stuck_nudged = True
                await self.rec.emit(EventType.STUCK_NUDGED, {"step": step_idx, "repeats": self._stuck_repeats,
                                                             "description": description})
                nudge = (f"Note from the harness: you have performed '{description}' {self._stuck_repeats} times "
                         "on an unchanged screen. It is not working. Re-read the screenshot, consider that the "
                         "target may be elsewhere or needs a different interaction (scroll, keyboard, wait), "
                         "or explain why the task cannot be completed.")
        else:
            self._stuck_nudged = False

        # Execute ------------------------------------------------------------
        t0 = time.perf_counter()
        if isinstance(action, Zoom):
            return await self._zoom(action, tool_use_id, step_idx, turn_idx, description, coords, nudge, t0)
        result = await self._execute_with_retry(action)
        frame = await self._capture_after(action)
        exec_ms = (time.perf_counter() - t0) * 1000
        ref = await self.rec.save_frame(frame, {"kind": "after_action", "step": step_idx})
        await self.rec.emit(EventType.FRAME, ref)
        changed = frame.sha1 != before_sha
        self.last_frame = frame
        self._recent.append(description)
        await self.rec.emit(EventType.ACTION_EXECUTED, {
            "step": step_idx, "turn": turn_idx, "kind": action.action, "ok": result.ok, "error": result.error,
            "output": result.output, "duration_ms": round(result.duration_ms or exec_ms, 1),
            "total_ms": round(exec_ms, 1), "description": description, "coordinates": coords,
            "frame_seq": ref.get("seq"), "screen_changed": changed,
        })
        text_parts: list[str] = []
        if result.output:
            text_parts.append(self.scaler.scale_output(result.output) or "")
        if not result.ok and result.error:
            text_parts.append(f"Action failed: {result.error}")
        if nudge:
            text_parts.append(nudge)
        return self._tool_result(tool_use_id, text="\n".join(text_parts) or None, frame=frame,
                                 is_error=not result.ok)

    async def _zoom(self, action: Zoom, tool_use_id: str, step_idx: int, turn_idx: int, description: str,
                    coords: list[list[int]], nudge: str | None, t0: float) -> dict[str, Any]:
        """Harness-level observation: crop a fresh capture to the region and upscale it."""
        full = await self._capture(retries=2)
        self.last_frame = full
        pw, ph = full.width, full.height
        x0, y0, x1, y1 = action.region  # already physical pixels (scaled with the action input)
        x0, y0, x1, y1 = max(0, min(x0, pw - 1)), max(0, min(y0, ph - 1)), max(1, min(x1, pw)), max(1, min(y1, ph))
        if x1 - x0 < 4 or y1 - y0 < 4:
            await self.rec.emit(EventType.ACTION_EXECUTED, {
                "step": step_idx, "turn": turn_idx, "kind": action.action, "ok": False, "duration_ms": 0,
                "error": "zoom region is empty after clamping", "description": description, "coordinates": coords})
            return self._tool_result(tool_use_id, error="Zoom region is outside the screen or too small.")
        crop = full.image().crop((x0, y0, x1, y1))
        mw, mh = self.scaler.model
        scale = min(mw / crop.width, mh / crop.height)
        if scale > 1:  # upscale small regions so text becomes legible; never exceed the model display
            crop = crop.resize((round(crop.width * scale), round(crop.height * scale)))
        zoomed = Frame.from_image(crop)
        exec_ms = (time.perf_counter() - t0) * 1000
        ref = await self.rec.save_frame(zoomed, {"kind": "zoom", "step": step_idx, "region": [x0, y0, x1, y1]})
        await self.rec.emit(EventType.FRAME, ref)
        self._recent.append(description)
        await self.rec.emit(EventType.ACTION_EXECUTED, {
            "step": step_idx, "turn": turn_idx, "kind": action.action, "ok": True, "error": None, "output": None,
            "duration_ms": round(exec_ms, 1), "total_ms": round(exec_ms, 1), "description": description,
            "coordinates": coords, "frame_seq": ref.get("seq"), "screen_changed": False,
        })
        mx0, my0 = self.scaler.to_model(x0, y0)
        mx1, my1 = self.scaler.to_model(x1, y1)
        # Magnification relative to the model's view of the screen, per axis.
        sx = zoomed.width / max(1, mx1 - mx0)
        sy = zoomed.height / max(1, my1 - my0)
        note = (f"Zoomed view of screen region ({mx0}, {my0})–({mx1}, {my1}) at {sx:.1f}x. The pixel "
                f"coordinates in this image are NOT screen coordinates: map them back into that region before "
                f"clicking (screen_x = {mx0} + image_x / {sx:.2f}, screen_y = {my0} + image_y / {sy:.2f}).")
        text = note + (f"\n{nudge}" if nudge else "")
        content: list[dict[str, Any]] = [
            {"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(zoomed.png).decode()}},
        ]
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}

    # -- helpers -------------------------------------------------------------

    def _end(self, outcome: Outcome, reason: str | None) -> None:
        if self._outcome is None:
            self._outcome, self._reason = outcome, reason

    async def _checkpoint(self, consume: bool = True) -> None:
        async def on_pause():
            await self.rec.emit(EventType.SESSION_PAUSED, {"step": self.steps, "turn": self.turns})

        async def on_resume():
            await self.rec.emit(EventType.SESSION_RESUMED, {"step": self.steps, "turn": self.turns})

        await self.controller.checkpoint(on_pause, on_resume, consume=consume)

    async def _execute_with_retry(self, action: Action) -> ActionResult:
        if isinstance(action, KeyPress) and action.repeat > 1:
            single = action.model_copy(update={"repeat": 1})
            total = 0.0
            for _ in range(action.repeat):
                result = await self._execute_once(single)
                total += result.duration_ms
                if not result.ok:
                    return result
            return ActionResult(ok=True, duration_ms=total)
        return await self._execute_once(action)

    async def _execute_once(self, action: Action) -> ActionResult:
        try:
            return await self.computer.execute(action)
        except ComputerError as e:
            await self.rec.emit(EventType.ERROR, {"where": "computer", "message": f"retrying after: {e}"})
            await asyncio.sleep(1.0)
            health = await self.computer.health()
            if not health.ok:
                raise ComputerError(f"computer unhealthy: {health.detail}") from e
            return await self.computer.execute(action)

    async def _capture(self, retries: int = 1) -> Frame:
        last: Exception | None = None
        for i in range(retries):
            try:
                return await self.computer.screenshot()
            except ComputerError as e:
                last = e
                await asyncio.sleep(0.5 * (i + 1))
        raise ComputerError(f"screenshot failed: {last}")

    async def _capture_after(self, action: Action) -> Frame:
        """Wait for the screen to settle (bounded), then capture."""
        if isinstance(action, (Screenshot, CursorPosition, Wait)):
            return await self._capture(retries=2)
        settle = self.config.settle_ms / 1000
        if settle:
            await asyncio.sleep(settle)
        frame = await self._capture(retries=2)
        for _ in range(self.config.settle_checks):  # screen still animating / loading? re-check
            await asyncio.sleep(self.config.settle_check_ms / 1000)
            nxt = await self._capture(retries=2)
            if nxt.sha1 == frame.sha1:
                return frame
            frame = nxt
        return frame

    def _image_block(self, frame: Frame) -> dict[str, Any]:
        png = self.scaler.frame_png_for_model(frame)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(png).decode()}}

    def _tool_result(self, tool_use_id: str, *, text: str | None = None, frame: Frame | None = None,
                     error: str | None = None, is_error: bool = False) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if error:
            content.append({"type": "text", "text": error})
            is_error = True
        if text:
            content.append({"type": "text", "text": text})
        if frame is not None:
            content.append(self._image_block(frame))
        if not content:
            content.append({"type": "text", "text": "ok"})
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return block

    def _prune_images(self, messages: list[dict[str, Any]]) -> None:
        keep = self.config.screenshot_history
        if keep <= 0:
            return
        images: list[tuple[list, int]] = []  # (container list, index)
        for msg in messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for i, block in enumerate(msg["content"]):
                if block.get("type") == "image":
                    images.append((msg["content"], i))
                elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    for j, inner in enumerate(block["content"]):
                        if inner.get("type") == "image":
                            images.append((block["content"], j))
        for container, idx in images[:-keep]:
            container[idx] = {"type": "text", "text": "(earlier screenshot omitted to save context)"}

    async def _inject_operator_context(self, messages: list[dict[str, Any]]) -> None:
        """Fold operator guidance / manual actions into the next model call."""
        instructions, notes = self.controller.drain_notes()
        if not instructions and not notes:
            return
        parts: list[str] = []
        if notes:
            parts.append("While the session was paused the operator manually performed: " + "; ".join(notes)
                         + ". The screen may have changed; a fresh screenshot is attached.")
        for text in instructions:
            await self.rec.emit(EventType.USER_INSTRUCTION, {"text": text})
            parts.append(f"Operator guidance: {text}")
        blocks: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(parts)}]
        if notes:
            frame = await self._capture(retries=2)
            ref = await self.rec.save_frame(frame, {"kind": "after_manual", "step": None})
            await self.rec.emit(EventType.FRAME, ref)
            self.last_frame = frame
            blocks.append(self._image_block(frame))
        if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": "user", "content": blocks})
