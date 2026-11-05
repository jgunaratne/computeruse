"""Running flows: a signal bus that routes messages along wires between node runtimes.

Every node in a run gets a runtime with an inbox. `FlowRunner.emit()` delivers a message from an output
port to every wired input: a *trigger* input queues it (the runtime handles its queue one message at a
time), a *latch* input just remembers it as the latest context from that source. Agents (computer,
model, critic) are asyncio work inside their runtime: a computer agent runs a normal session through
the orchestrator and waits for it, a model agent or critic sends one turn to its own tool-less
Antigravity conversation.

A run ends when every output node has received a result (completed — or incomplete if that result is
work a critic gave up on at its max rounds), when nothing is queued or running any more (incomplete, or
failed if a node errored), when the agent-call budget runs out (failed), or when the user stops it
(cancelled). Loops are allowed — that is how a critic sends work back — and are bounded by the critic's
max rounds, the run's agent-call budget and a hop limit for loops that contain no agent at all.

Runs are kept in memory while they are live and written to ``<data_dir>/flow_runs/<id>/run.json`` when
they start and end; the UI long-polls `wait()` on a version counter, as the chat panel does.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from computeruse.agent.antigravity import (
    CONVERSATION_TAG,
    AntigravityModel,
    Conversation,
    LanguageServer,
    image_media,
)
from computeruse.agent.model import ModelError
from computeruse.evals.suite import find_suite
from computeruse.flows.spec import (
    CRITIC,
    NODE_TYPES,
    ROLES,
    Flow,
    FlowEdge,
    FlowNode,
    node_label,
    validate_flow,
)
from computeruse.flows.spec import catalog as spec_catalog
from computeruse.flows.store import FlowStore
from computeruse.flows.templates import TEMPLATES
from computeruse.server.chat import ChatError, latest_frame
from computeruse.telemetry.events import Outcome

if TYPE_CHECKING:
    import httpx

    from computeruse.server.orchestrator import Orchestrator

log = logging.getLogger("computeruse.flows")

MAX_HOPS = 64  # a message that has crossed this many wires is dropped (a loop with no agent in it)
MAX_ACTIVE_RUNS = 4
LOG_LIMIT = 500
PREVIEW_CHARS = 600
CONTEXT_CHARS = 6000
JOURNAL_CONTEXT_CHARS = 8000
TASK_CHARS = 8000
WATCH_INTERVAL_S = 1.0
KEEP_FINISHED_S = 600
TERMINAL = frozenset({"completed", "incomplete", "failed", "cancelled", "interrupted"})
FLOW_TAG = "flow"


class FlowError(Exception):
    """A flow request that cannot be served; `status` is the HTTP status the API should return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class BudgetExhausted(Exception):
    pass


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class Signal:
    """One message on a wire (the same envelope for every node type)."""

    kind: str  # task | result | screen | feedback | findings | plan | summary | combined | journal | ...
    text: str = ""
    frame: dict[str, Any] | None = None  # screen ports: {session_id, seq, url}
    source: str = ""
    source_port: str = ""
    source_label: str = ""
    hops: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self, limit: int = PREVIEW_CHARS) -> dict[str, Any]:
        return {"kind": self.kind, "text": clip(self.text, limit), "chars": len(self.text), "frame": self.frame,
                "source": self.source, "source_port": self.source_port, "source_label": self.source_label,
                "meta": self.meta, "ts": self.ts}


@dataclass
class NodeState:
    id: str
    type: str
    label: str
    status: str = "idle"  # idle | waiting | running | done | stopped | error | muted
    detail: str = ""
    runs: int = 0
    queued: int = 0
    last_input: dict[str, Any] | None = None
    last_output: dict[str, Any] | None = None
    error: str | None = None
    session_id: str | None = None
    sessions: list[str] = field(default_factory=list)
    steps: int = 0
    frame: dict[str, Any] | None = None
    verdict: str | None = None
    rounds: int = 0
    received: int = 0
    expected: int = 0
    entries: int = 0
    result: str | None = None
    conversation_id: str | None = None
    updated_at: float = field(default_factory=time.time)


_VERDICT = re.compile(r"[*_`\"'#]*\s*verdict\s*[:\-–]?\s*[*_`\"']*\s*(pass|revise|fail|retry|reject|accept)[*_`\"'.!]*",
                      re.I)
_RESIDUE = " \t\r\n*_`\"'.:;,-–"


def parse_verdict(text: str) -> tuple[str, str]:
    """→ (pass | revise, the critic's reasons). Anything that is not clearly a pass counts as revise."""
    m = _VERDICT.search(text)
    word = m.group(1).lower() if m else ""
    verdict = "pass" if word in ("pass", "accept") else "revise"
    reasons = (text[: m.start()] + text[m.end():]).lstrip(_RESIDUE).rstrip() if m else text.strip()
    return verdict, reasons or "(no justification given)"


def frame_ref(session_id: str, seq: int) -> dict[str, Any]:
    return {"session_id": session_id, "seq": seq, "url": f"/api/sessions/{session_id}/frames/{seq}.png"}


# -- node runtimes ----------------------------------------------------------------------------------------


class NodeRuntime:
    def __init__(self, runner: FlowRunner, node: FlowNode) -> None:
        self.runner = runner
        self.node = node
        self.spec = NODE_TYPES[node.type]
        self.cfg = node.config
        self.label = node_label(node)
        self.state = NodeState(id=node.id, type=node.type, label=self.label,
                               status="muted" if node.muted else "idle")
        self.inbox: asyncio.Queue[tuple[str, Signal]] = asyncio.Queue()
        self.latched: dict[str, dict[tuple[str, str], Signal]] = {}
        self.task: asyncio.Task[None] | None = None

    @property
    def orch(self) -> Orchestrator:
        return self.runner.orch

    # -- wiring --

    def deliver(self, port: str, sig: Signal) -> bool:
        if self.node.muted:
            return False
        spec_port = self.spec.input(port)
        if spec_port is None:
            return False
        msg = replace(sig, meta=dict(sig.meta))
        if spec_port.trigger:
            self.runner.busy += 1
            self.inbox.put_nowait((port, msg))
            self.state.queued = self.inbox.qsize()
            if self.state.status in ("idle", "done", "stopped"):
                self.state.status = "waiting"
        else:
            self.latched.setdefault(port, {})[(sig.source, sig.source_port)] = msg
        return True

    def emit(self, port: str, sig: Signal) -> None:
        self.runner.emit(self.node, port, sig)

    def latched_text(self, port: str = "context", limit: int = CONTEXT_CHARS) -> str:
        msgs = self.latched.get(port) or {}
        parts = []
        for e in self.runner.incoming.get(self.node.id, []):
            s = msgs.get((e.source, e.source_port)) if e.target_port == port else None
            if s is not None and s.text.strip():
                parts.append(f"From {s.source_label}:\n{s.text.strip()}")
        text = "\n\n".join(parts)
        return text if len(text) <= limit else "…" + text[-(limit - 1):]  # journals grow at the end: keep it

    def latched_frame(self, port: str = "screen") -> dict[str, Any] | None:
        frames = [s for s in (self.latched.get(port) or {}).values() if s.frame]
        return max(frames, key=lambda s: s.ts).frame if frames else None

    # -- state --

    def set(self, **changes: Any) -> None:
        for k, v in changes.items():
            setattr(self.state, k, v)
        self.state.updated_at = time.time()
        self.runner.touch()

    def note(self, text: str, **extra: Any) -> None:
        self.runner.record("node", node=self.node.id, text=text, **extra)

    def fail(self, message: str) -> None:
        self.set(status="error", error=message, detail="")
        self.note(message, status="error")

    # -- lifecycle --

    async def loop(self) -> None:
        while True:
            port, sig = await self.inbox.get()
            self.state.queued = self.inbox.qsize()
            try:
                await self.handle(port, sig)
            except asyncio.CancelledError:
                raise
            except BudgetExhausted as e:
                self.fail(str(e))
                self.runner.request_finish("failed", str(e))
            except (FlowError, ModelError, ChatError, ValueError, KeyError, OSError) as e:
                self.fail(str(e) or type(e).__name__)
            except Exception as e:  # noqa: BLE001 - surfaced on the node, never lost in a task
                log.exception("flow %s: node %s failed", self.runner.id, self.node.id)
                self.fail(f"{type(e).__name__}: {e}")
            finally:
                self.runner.busy -= 1
                self.runner.touch()
                self.runner.check_idle()

    async def start(self) -> None:
        """Called once when the run starts, before any message is delivered."""

    async def handle(self, port: str, sig: Signal) -> None:
        """Process one message that arrived on trigger input `port`."""

    async def stop(self) -> None:
        """Tell the outside world to stop work in flight (a session, a server-side turn) before the
        node's task is cancelled — once the task is cancelled the client no longer knows what is running."""

    async def close(self) -> None:
        """Release what the node holds, after its task has ended."""

    async def drain(self) -> None:
        """Handle what is still queued, after the node's task has been cancelled (the run is ending, so
        anything it emits is dropped). Only memory nodes drain: their file should hold every message the
        run delivered to them."""
        while not self.inbox.empty():
            port, sig = self.inbox.get_nowait()
            try:
                await self.handle(port, sig)
            except Exception:  # noqa: BLE001 - best effort at the end of a run
                log.warning("flow %s: draining node %s failed", self.runner.id, self.node.id, exc_info=True)


class GoalRuntime(NodeRuntime):
    async def start(self) -> None:
        text = (self.runner.goal or str(self.cfg.get("text") or "")).strip()
        out = Signal(kind="task", text=text)
        self.emit("task", out)
        self.set(status="done", runs=1, last_output=out.to_json(), detail="sent the request")


class ComputerRuntime(NodeRuntime):
    def __init__(self, runner: FlowRunner, node: FlowNode) -> None:
        super().__init__(runner, node)
        self.active: str | None = None
        self._scan_after = -1
        self._frame: dict[str, Any] | None = None

    async def handle(self, port: str, sig: Signal) -> None:
        backend, model, script = self.runner.service.resolve_computer(self.node)
        task = (sig.text.strip() or "(no task text)")
        context = self.latched_text()
        if context:
            task = f"{task}\n\nContext from the rest of the team:\n{context}"
        task = clip(task, TASK_CHARS)
        self.runner.charge()
        self.set(status="running", runs=self.state.runs + 1, last_input=sig.to_json(), error=None,
                 detail="starting a session")
        overrides: dict[str, Any] = {"max_steps": int(self.cfg.get("max_steps") or 20)}
        if str(self.cfg.get("instructions") or "").strip():
            overrides["system_prompt_extra"] = str(self.cfg["instructions"]).strip()
        lock = self.runner.service.exclusive(backend)
        async with lock if lock is not None else contextlib.nullcontext():
            rec = await self.orch.create_session(
                task=task, backend=backend, model=model, overrides=overrides, script=script, tags=[FLOW_TAG],
                metadata={"flow": {"flow_id": self.runner.flow.id, "flow_name": self.runner.flow.name,
                                   "run_id": self.runner.id, "node_id": self.node.id, "node_label": self.label}},
            )
            self.active, self._scan_after, self._frame = rec.id, -1, None
            self.set(session_id=rec.id, sessions=[*self.state.sessions, rec.id], steps=0,
                     detail=f"session {rec.id[:8]} starting")
            self.note(f"started session {rec.id}", session_id=rec.id)
            watcher = asyncio.create_task(self._watch(rec.id), name=f"flow-watch-{rec.id}")
            try:
                rec = await self.orch.wait(rec.id)
            finally:
                watcher.cancel()
                self.active = None
        frame = self._scan(rec.id)
        outcome = rec.outcome.value if rec.outcome else "unknown"
        final = str(rec.metadata.get("final_text") or "").strip()
        ok = rec.outcome == Outcome.COMPLETED
        if ok:
            text = final or "The agent reported the task as done."
        else:
            text = f"The computer agent stopped ({outcome}): {rec.outcome_reason or 'no reason given'}."
            if final:
                text += f"\nIts last message: {final}"
        meta = {"session_id": rec.id, "outcome": outcome, "steps": rec.steps}
        if frame:
            self.emit("screen", Signal(kind="screen", text=f"Final screen of session {rec.id}", frame=frame, meta=meta))
        out = Signal(kind="result", text=text, meta=meta)
        self.emit("result", out)
        self.set(status="done" if ok else "stopped", steps=rec.steps, frame=frame or self.state.frame,
                 last_output=out.to_json(), detail=f"{outcome} · {rec.steps} steps")
        self.note(f"session {rec.id[:8]} ended: {outcome} after {rec.steps} steps", session_id=rec.id)

    def _scan(self, session_id: str) -> dict[str, Any] | None:
        events = self.orch.events(session_id, after_seq=self._scan_after)
        if events:
            self._scan_after = events[-1].seq
            seq = latest_frame(events)
            if seq is not None:
                self._frame = frame_ref(session_id, seq)
        return self._frame

    async def _watch(self, session_id: str) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            rec = self.orch.get_session(session_id)
            if rec is None:
                continue
            frame = self._scan(session_id)
            detail = f"session {session_id[:8]} · {rec.status.value} · {rec.steps} steps"
            changes: dict[str, Any] = {}
            if rec.steps != self.state.steps:
                changes["steps"] = rec.steps
            if frame and frame != self.state.frame:
                changes["frame"] = frame
            if detail != self.state.detail:
                changes["detail"] = detail
            if changes:
                self.set(**changes)

    async def stop(self) -> None:
        if self.active:
            with contextlib.suppress(Exception):
                await self.orch.control(self.active, "cancel", {})


class ModelRuntime(NodeRuntime):
    def __init__(self, runner: FlowRunner, node: FlowNode) -> None:
        super().__init__(runner, node)
        self.conversation: Conversation | None = None
        self.conversations: list[Conversation] = []

    def system_prompt(self) -> str:
        return (str(self.cfg.get("instructions") or "").strip()
                or "You are one agent in a multi-agent flow. Reply concisely to the message you are given.")

    def keeps_memory(self) -> bool:
        return self.cfg.get("memory", "keep") != "fresh"

    def kind(self) -> str:
        return ROLES.get(str(self.cfg.get("role")), ROLES["custom"])[1]

    async def _conversation(self, model: AntigravityModel) -> Conversation:
        if self.conversation is not None and self.keeps_memory():
            return self.conversation
        conv = Conversation(await self.runner.language_server(), model)
        await conv.start([("ROLE", self.system_prompt())],
                         title=f"computeruse flow · {self.runner.flow.name} · {self.label}",
                         tags=(CONVERSATION_TAG, FLOW_TAG))
        self.conversation = conv
        self.conversations.append(conv)
        self.set(conversation_id=conv.id)
        return conv

    async def converse(self, text: str, frame: dict[str, Any] | None):
        model = self.runner.service.resolve_model(str(self.cfg.get("model") or ""))
        conv = await self._conversation(model)
        media: list[dict[str, Any]] = []
        if frame and model.supports_images:
            path = self.orch.store.frame_path(frame["session_id"], int(frame["seq"]))
            if path is not None:
                png = await asyncio.to_thread(path.read_bytes)
                caption = f"the final screen of session {frame['session_id']}"
                media.append(image_media(base64.b64encode(png).decode(), caption))
                text += f"\n\n[screenshot attached: {caption}]"
        reply = await conv.send(text, media or None, model=model)
        if reply.empty:
            reply = reply.merged(await conv.send("Your reply was empty. Please answer the previous message.",
                                                 model=model))
        if reply.empty:
            raise ModelError(f"Antigravity ({model.label}) returned no reply")
        return reply

    def compose(self, sig: Signal) -> str:
        parts = []
        context = self.latched_text()
        if context:
            parts.append(f"[Context]\n{context}")
        parts.append(f"[Message from {sig.source_label or 'the flow'}]\n{sig.text.strip()}")
        return "\n\n".join(parts)

    async def handle(self, port: str, sig: Signal) -> None:
        self.runner.charge()
        self.set(status="running", runs=self.state.runs + 1, last_input=sig.to_json(), error=None, detail="thinking")
        reply = await self.converse(self.compose(sig), self.latched_frame())
        out = Signal(kind=self.kind(), text=reply.text.strip(),
                     meta={"output_tokens": reply.usage.output_tokens, "input_tokens": reply.usage.input_tokens})
        self.emit("out", out)
        self.set(status="done", last_output=out.to_json(), detail=f"replied · {reply.usage.output_tokens} tokens")

    async def stop(self) -> None:
        for conv in self.conversations:
            with contextlib.suppress(Exception):
                await conv.cancel()  # a no-op unless a turn is in flight

    async def close(self) -> None:
        if self.orch.settings.antigravity_archive:
            for conv in self.conversations:
                with contextlib.suppress(Exception):
                    await conv.archive()


class CriticRuntime(ModelRuntime):
    def system_prompt(self) -> str:
        rubric = str(self.cfg.get("rubric") or "").strip() or "Judge whether the goal is fully achieved."
        return f"{CRITIC}\n\nRubric:\n{rubric}"

    def keeps_memory(self) -> bool:
        return True

    async def handle(self, port: str, sig: Signal) -> None:
        self.runner.charge()
        rounds = self.state.rounds + 1
        max_rounds = int(self.cfg.get("max_rounds") or 3)
        self.set(status="running", runs=self.state.runs + 1, rounds=rounds, last_input=sig.to_json(), error=None,
                 detail=f"judging round {rounds} of {max_rounds}")
        parts = []
        context = self.latched_text()
        if context:
            parts.append(f"[Goal and context]\n{context}")
        parts.append(f"[Work to judge: round {rounds} of at most {max_rounds}, from {sig.source_label or 'the flow'}]"
                     f"\n{sig.text.strip()}")
        reply = await self.converse("\n\n".join(parts), self.latched_frame())
        verdict, reasons = parse_verdict(reply.text)
        meta = {"round": rounds, "max_rounds": max_rounds, "reasons": clip(reasons, 2000)}
        if verdict == "pass":
            out = Signal(kind="result", text=sig.text, meta={**meta, "verdict": "pass"})
            self.emit("pass", out)
            self.set(status="done", verdict="pass", last_output=out.to_json(), detail=f"passed in round {rounds}")
        elif rounds >= max_rounds:
            out = Signal(kind="result", text=sig.text, meta={**meta, "verdict": "max_rounds"})
            self.emit("pass", out)
            self.set(status="stopped", verdict="max_rounds", last_output=out.to_json(),
                     detail=f"stopped after {rounds} rounds")
            self.note(f"no pass after {rounds} rounds; passing the last work on, marked as stopped")
        else:
            feedback = (f"Round {rounds} was not accepted.\n\nThe critic's reasons:\n{reasons}\n\n"
                        f"The work that was judged:\n{clip(sig.text.strip(), 3000)}")
            out = Signal(kind="feedback", text=feedback, meta={**meta, "verdict": "revise"})
            self.emit("revise", out)
            self.set(status="done", verdict="revise", last_output=out.to_json(), detail=f"round {rounds}: revise")


class JoinRuntime(NodeRuntime):
    def __init__(self, runner: FlowRunner, node: FlowNode) -> None:
        super().__init__(runner, node)
        self.buffer: dict[tuple[str, str], Signal] = {}

    def expected(self) -> list[tuple[str, str]]:
        return [(e.source, e.source_port) for e in self.runner.incoming.get(self.node.id, [])
                if e.target_port == "in" and not self.runner.runtimes[e.source].node.muted]

    async def handle(self, port: str, sig: Signal) -> None:
        expected = self.expected()
        self.buffer[(sig.source, sig.source_port)] = sig
        got = [k for k in expected if k in self.buffer]
        if len(got) < len(expected):
            self.set(status="waiting", received=len(got), expected=len(expected), last_input=sig.to_json(),
                     detail=f"{len(got)} of {len(expected)} in")
            return
        parts = [f"## {self.buffer[k].source_label}\n{self.buffer[k].text.strip()}" for k in expected]
        out = Signal(kind="combined", text="\n\n".join(parts),
                     meta={"sources": [self.buffer[k].source_label for k in expected]})
        self.buffer.clear()
        self.emit("out", out)
        self.set(status="done", runs=self.state.runs + 1, received=len(expected), expected=len(expected),
                 last_input=sig.to_json(), last_output=out.to_json(), detail=f"sent {len(expected)} together")


class JournalRuntime(NodeRuntime):
    def __init__(self, runner: FlowRunner, node: FlowNode) -> None:
        super().__init__(runner, node)
        self.content = ""
        self.path: Path = runner.dir / f"journal-{node.id}.md"

    def _tail(self) -> str:
        c = self.content
        return c if len(c) <= JOURNAL_CONTEXT_CHARS else "…" + c[-(JOURNAL_CONTEXT_CHARS - 1):]

    async def start(self) -> None:
        seed = str(self.cfg.get("seed") or "").strip()
        self.content = f"{seed}\n" if seed else ""
        await asyncio.to_thread(self.path.write_text, self.content, "utf-8")
        if seed:
            self.emit("context", Signal(kind="journal", text=self._tail()))
        self.set(detail="journal.md is empty" if not seed else "journal.md seeded")

    async def handle(self, port: str, sig: Signal) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(sig.ts))
        entry = f"\n## {stamp} · {sig.source_label} ({sig.kind})\n{clip(sig.text.strip(), 4000)}\n"
        # Everything the run can observe happens before the file write, so a run that ends while the
        # write is in flight still counts the entry; close() rewrites the file from `content` anyway.
        self.content += entry
        out = Signal(kind="journal", text=self._tail())
        self.emit("context", out)
        entries = self.state.entries + 1
        self.set(status="done", runs=self.state.runs + 1, entries=entries, last_input=sig.to_json(),
                 last_output=out.to_json(), detail=f"{entries} {'entry' if entries == 1 else 'entries'}")

        def append() -> None:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(entry)

        await asyncio.to_thread(append)

    async def close(self) -> None:
        await asyncio.to_thread(self.path.write_text, self.content, "utf-8")


class OutputRuntime(NodeRuntime):
    def deliver(self, port: str, sig: Signal) -> bool:
        ok = super().deliver(port, sig)
        if ok and port == "screen" and sig.frame:
            self.state.frame = sig.frame
        return ok

    async def handle(self, port: str, sig: Signal) -> None:
        verdict = sig.meta.get("verdict")
        self.set(status="stopped" if verdict == "max_rounds" else "done", runs=self.state.runs + 1, result=sig.text,
                 verdict=verdict, last_input=sig.to_json(), frame=self.latched_frame() or self.state.frame,
                 detail="stopped at the critic's max rounds" if verdict == "max_rounds" else "received the result")
        self.note(f"received a result from {sig.source_label}")
        self.runner.output_fired(self.node.id)


RUNTIMES: dict[str, type[NodeRuntime]] = {
    "goal": GoalRuntime, "computer": ComputerRuntime, "model": ModelRuntime, "critic": CriticRuntime,
    "join": JoinRuntime, "journal": JournalRuntime, "output": OutputRuntime, "note": NodeRuntime,
}


# -- one run -------------------------------------------------------------------------------------------------


class FlowRunner:
    def __init__(self, service: FlowService, flow: Flow, *, run_id: str, goal: str | None = None) -> None:
        self.service = service
        self.orch = service.orch
        self.flow = flow
        self.id = run_id
        self.goal = goal.strip() if goal and goal.strip() else None
        self.status = "running"
        self.reason = ""
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.version = 0
        self.invocations = 0
        self.max_invocations = flow.settings.max_agent_calls
        self.log: list[dict[str, Any]] = []
        self.busy = 0
        self.done = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.dir = service.store.run_dir(run_id)
        self.ls: LanguageServer | None = None
        self._log_seq = 0
        self._changed = asyncio.Event()
        self._finishing = False
        self._finish_task: asyncio.Task[None] | None = None
        self.routes: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.incoming: dict[str, list[FlowEdge]] = {}
        for e in flow.edges:
            self.routes.setdefault((e.source, e.source_port), []).append((e.target, e.target_port))
            self.incoming.setdefault(e.target, []).append(e)
        self.runtimes: dict[str, NodeRuntime] = {n.id: RUNTIMES[n.type](self, n) for n in flow.nodes}
        self.outputs = {n.id for n in flow.nodes if n.type == "output" and not n.muted}
        self.fired: set[str] = set()

    # -- bookkeeping --

    def touch(self) -> None:
        self.version += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def record(self, type_: str, **data: Any) -> None:
        self._log_seq += 1
        self.log.append({"seq": self._log_seq, "ts": time.time(), "type": type_, **data})
        if len(self.log) > LOG_LIMIT:
            del self.log[: len(self.log) - LOG_LIMIT]

    def charge(self) -> None:
        if self.invocations >= self.max_invocations:
            raise BudgetExhausted(f"the run used all {self.max_invocations} agent calls it was allowed "
                                  "(raise the limit in the flow settings)")
        self.invocations += 1

    async def language_server(self) -> LanguageServer:
        if self.ls is None:
            endpoint = self.orch.catalog.antigravity_endpoint
            if endpoint is None:
                raise FlowError(503, "the Antigravity Language Server is not available")
            self.ls = LanguageServer(endpoint, transport=self.service.transport)
        return self.ls

    # -- the bus --

    def emit(self, node: FlowNode, port: str, sig: Signal) -> None:
        if self._finishing:
            return
        sig.source, sig.source_port, sig.source_label = node.id, port, self.runtimes[node.id].label
        sig.hops += 1
        if sig.hops > MAX_HOPS:
            self.record("drop", source=node.id, source_port=port,
                        text=f"dropped after {MAX_HOPS} hops: a loop with no agent in it never stops")
            self.touch()
            return
        for target, target_port in self.routes.get((node.id, port), []):
            delivered = self.runtimes[target].deliver(target_port, sig)
            self.record("message", source=node.id, source_port=port, target=target, target_port=target_port,
                        kind=sig.kind, text=clip(sig.text, 280), frame=sig.frame is not None,
                        dropped=not delivered)
        self.touch()

    def output_fired(self, node_id: str) -> None:
        self.fired.add(node_id)
        if not self.outputs or not self.fired >= self.outputs:
            return
        # Work a critic gave up on (max rounds) reaches the output, but the run did not succeed.
        unaccepted = [self.runtimes[o].label for o in self.outputs if self.runtimes[o].state.verdict == "max_rounds"]
        if unaccepted:
            self.request_finish("incomplete", f"{', '.join(unaccepted)} received work that no critic passed "
                                "(stopped at the critic's max rounds)")
        else:
            self.request_finish("completed", "every output received a result")

    def check_idle(self) -> None:
        if self.busy == 0 and not self._finishing:
            asyncio.get_running_loop().call_soon(self._idle)

    def _idle(self) -> None:
        if self.busy != 0 or self._finishing:
            return
        errors = [rt for rt in self.runtimes.values() if rt.state.status == "error"]
        if errors:
            first = errors[0]
            self.request_finish("failed", f"{first.label}: {first.state.error}")
        elif not self.outputs:
            self.request_finish("completed", "every agent finished")
        else:
            missing = [self.runtimes[o].label for o in self.outputs if o not in self.fired]
            self.request_finish("incomplete", "the flow went quiet before " + ", ".join(missing)
                                + (" received a result" if len(missing) == 1 else " all received results"))

    # -- lifecycle --

    async def run(self) -> None:
        await asyncio.to_thread(self.dir.mkdir, parents=True, exist_ok=True)
        live = [rt for rt in self.runtimes.values() if not rt.node.muted and rt.node.type != "note"]
        self.record("run", status="running", text=f"run started: {len(live)} active nodes")
        self.persist()
        for rt in live:
            rt.task = asyncio.create_task(rt.loop(), name=f"flow-{self.id}-{rt.node.id}")
        # Memory first, so the goals' first messages land in journals that already hold their seed.
        for rt in sorted(live, key=lambda r: 0 if r.node.type == "journal" else 1):
            try:
                await rt.start()
            except Exception as e:  # noqa: BLE001
                rt.fail(f"{type(e).__name__}: {e}")
        self.touch()
        self.check_idle()
        await self.done.wait()

    def request_finish(self, status: str, reason: str) -> None:
        if self._finishing:
            return
        self._finishing = True
        self._finish_task = asyncio.create_task(self._finish(status, reason), name=f"flow-finish-{self.id}")

    async def _finish(self, status: str, reason: str) -> None:
        for rt in self.runtimes.values():
            try:
                await rt.stop()
            except Exception:  # noqa: BLE001 - best effort
                log.warning("flow %s: stopping node %s failed", self.id, rt.node.id, exc_info=True)
        tasks = [rt.task for rt in self.runtimes.values() if rt.task is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rt in self.runtimes.values():
            if rt.node.type == "journal" and not rt.node.muted:
                await rt.drain()
        for rt in self.runtimes.values():
            try:
                await rt.close()
            except Exception:  # noqa: BLE001 - best effort
                log.warning("flow %s: closing node %s failed", self.id, rt.node.id, exc_info=True)
            if rt.state.status in ("running", "waiting"):
                rt.state.status = "stopped"
                if rt.node.type != "join":  # a join keeps "1 of 2 in": it says what the run was waiting for
                    rt.state.detail = "stopped when the run ended"
        if self.ls is not None:
            with contextlib.suppress(Exception):
                await self.ls.aclose()
        self.status, self.reason, self.ended_at = status, reason, time.time()
        self.record("run", status=status, text=reason)
        self.persist()
        self.done.set()
        self.touch()

    def persist(self) -> None:
        try:
            self.service.store.save_run(self.to_json())
        except OSError as e:
            log.warning("flow %s: could not persist the run: %s", self.id, e)

    async def wait(self, after: int, wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while self.version <= after and not self.done.is_set():
            changed = self._changed
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "flow_id": self.flow.id, "flow_name": self.flow.name, "status": self.status,
                "reason": self.reason, "started_at": self.started_at, "ended_at": self.ended_at,
                "invocations": self.invocations, "max_invocations": self.max_invocations}

    def to_json(self) -> dict[str, Any]:
        results = [{"node": rt.node.id, "label": rt.label, "text": rt.state.result, "verdict": rt.state.verdict,
                    "frame": rt.state.frame}
                   for rt in self.runtimes.values() if rt.node.type == "output" and rt.state.result is not None]
        return {**self.summary(), "version": self.version, "goal": self.goal,
                "nodes": {nid: asdict(rt.state) for nid, rt in self.runtimes.items()},
                "log": list(self.log), "results": results,
                "graph": {"nodes": [n.model_dump(mode="json") for n in self.flow.nodes],
                          "edges": [e.model_dump(mode="json") for e in self.flow.edges]}}


# -- the service ------------------------------------------------------------------------------------------------


class FlowService:
    """Owns flow documents and their runs for one server process."""

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        self.store = FlowStore(orch.settings.data_dir)
        self.runs: dict[str, FlowRunner] = {}
        self._transport: httpx.AsyncBaseTransport | None = None  # tests inject an in-memory Language Server
        self._locks: dict[str, asyncio.Lock] = {}
        self._mark_interrupted()

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        return self._transport if self._transport is not None else self.orch.chat.transport

    @transport.setter
    def transport(self, value: httpx.AsyncBaseTransport | None) -> None:
        self._transport = value

    def _mark_interrupted(self) -> None:
        for run in self.store.list_runs(limit=100_000):
            if run.get("status") == "running":
                run.update(status="interrupted", reason="the server stopped during this run",
                           ended_at=run.get("ended_at") or time.time())
                with contextlib.suppress(OSError, KeyError):
                    self.store.save_run(run)

    # -- environment --

    def default_backend(self) -> str:
        infos = {b.id: b for b in self.orch.registry.list()}
        for candidate in ("browser", "simulated"):
            if candidate in infos and infos[candidate].available:
                return candidate
        return next((b.id for b in infos.values() if b.available), "simulated")

    def exclusive(self, backend: str) -> asyncio.Lock | None:
        """The live desktop takes one session at a time; flows queue for it instead of failing."""
        if backend != "desktop":
            return None
        return self._locks.setdefault(backend, asyncio.Lock())

    def resolve_model(self, ref: str) -> AntigravityModel:
        try:
            model = self.orch.chat.resolve_model(ref or None)
        except ChatError as e:
            raise FlowError(400 if e.status < 500 else e.status, str(e)) from e
        if model.quota_exhausted:
            raise FlowError(400, f"Antigravity quota for {model.label!r} is exhausted"
                                 f"{' until ' + model.quota_reset if model.quota_reset else ''}")
        return model

    def resolve_computer(self, node: FlowNode) -> tuple[str, str, list[dict[str, Any]] | None]:
        """→ (backend, model, script) for a computer agent; a scripted demo brings its own backend."""
        cfg = node.config
        demo = str(cfg.get("demo_task") or "").strip()
        if demo:
            if "/" not in demo:
                raise FlowError(400, f"{node_label(node)!r}: demo task must look like '<suite>/<task_id>'")
            suite_name, task_id = demo.split("/", 1)
            try:
                suite = find_suite(suite_name)
                spec = suite.task(task_id)
            except (FileNotFoundError, KeyError) as e:
                raise FlowError(400, f"{node_label(node)!r}: unknown demo task {demo!r}") from e
            if not spec.script:
                raise FlowError(400, f"{node_label(node)!r}: demo task {demo!r} has no reference script")
            return suite.backend, "scripted", spec.script
        backend = str(cfg.get("backend") or "browser")
        model = str(cfg.get("model") or "") or self.orch.catalog.default_model() or self.orch.settings.model
        return backend, model, None

    # -- documents --

    def catalog(self) -> dict[str, Any]:
        data = spec_catalog({"computer": {"backend": self.default_backend(),
                                     "max_steps": min(20, self.orch.settings.default_max_steps)}})
        data["templates"] = [t.summary() for t in TEMPLATES.values()]
        idx = self.orch.chat.index()
        data["antigravity"] = {k: idx[k] for k in ("available", "reason", "default_model", "models")}
        return data

    def list(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for run in self.store.list_runs(limit=1000):
            latest.setdefault(run["flow_id"], run)
        for runner in self.runs.values():
            prev = latest.get(runner.flow.id)
            if prev is None or runner.started_at >= (prev.get("started_at") or 0):
                latest[runner.flow.id] = runner.summary()
        return [{"id": f.id, "name": f.name, "template": f.template, "created_at": f.created_at,
                 "updated_at": f.updated_at, "nodes": len(f.nodes), "edges": len(f.edges),
                 "last_run": {k: latest[f.id].get(k) for k in ("id", "status", "started_at", "ended_at")}
                 if f.id in latest else None}
                for f in self.store.list()]

    def create(self, *, name: str | None = None, template: str | None = None,
               nodes: list[dict[str, Any]] | None = None, edges: list[dict[str, Any]] | None = None,
               settings: dict[str, Any] | None = None) -> Flow:
        tpl = None
        if template:
            tpl = TEMPLATES.get(template)
            if tpl is None:
                raise FlowError(404, f"unknown template {template!r}; available: {', '.join(TEMPLATES)}")
            nodes, edges = tpl.build(self.default_backend())
        flow = self._parse({"id": uuid.uuid4().hex[:10], "name": (name or (tpl.name if tpl else "") or "Untitled flow"),
                            "nodes": nodes or [], "edges": edges or [], "template": template,
                            "settings": settings or {}})
        return self.store.save(flow)

    def get(self, flow_id: str) -> Flow:
        flow = self.store.get(flow_id)
        if flow is None:
            raise FlowError(404, "unknown flow")
        return flow

    def save(self, flow_id: str, payload: dict[str, Any]) -> Flow:
        existing = self.get(flow_id)
        flow = self._parse({**payload, "id": flow_id, "created_at": existing.created_at, "updated_at": time.time(),
                            "template": existing.template})
        return self.store.save(flow)

    async def delete(self, flow_id: str) -> None:
        self.get(flow_id)
        for runner in list(self.runs.values()):
            if runner.flow.id == flow_id and not runner.done.is_set():
                runner.request_finish("cancelled", "the flow was deleted")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(runner.done.wait(), 30)
        self.store.delete(flow_id)

    @staticmethod
    def _parse(data: dict[str, Any]) -> Flow:
        try:
            flow = Flow.model_validate(data)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "flow"
            raise FlowError(400, f"invalid flow: {where}: {first['msg']}") from e
        try:
            return validate_flow(flow)
        except ValueError as e:
            raise FlowError(400, str(e)) from e

    # -- runs --

    def preflight(self, flow: Flow, goal: str | None) -> None:
        """Refuse a run that cannot work before anything starts, naming the node that is the problem."""
        live = [n for n in flow.nodes if not n.muted and n.type != "note"]
        goals = [n for n in live if n.type == "goal"]
        if not goals:
            raise FlowError(400, "add a Goal (or unmute one): goals are what start a run")
        for g in goals:
            if not (goal or str(g.config.get("text") or "")).strip():
                raise FlowError(400, f"{node_label(g)!r} is empty: write the request first")
        needs_model = [n for n in live if NODE_TYPES[n.type].needs_antigravity]
        if needs_model:
            ok, reason = self.orch.chat.available()
            if not ok:
                raise FlowError(400, f"{node_label(needs_model[0])!r} needs a running Antigravity: {reason}")
            for n in needs_model:
                try:
                    self.resolve_model(str(n.config.get("model") or ""))
                except FlowError as e:
                    raise FlowError(e.status, f"{node_label(n)!r}: {e}") from e
        for n in live:
            if n.type != "computer":
                continue
            backend, model, script = self.resolve_computer(n)
            try:
                info = self.orch.registry.info(backend)
            except KeyError as e:
                raise FlowError(400, f"{node_label(n)!r}: unknown computer {backend!r}") from e
            if not info.available:
                raise FlowError(400, f"{node_label(n)!r}: the {backend} computer is unavailable: {info.reason}")
            if script is None:
                ok, reason = self.orch.catalog.check(model)
                if not ok:
                    raise FlowError(400, f"{node_label(n)!r}: {reason}")

    async def start(self, flow_id: str, *, goal: str | None = None) -> FlowRunner:
        flow = validate_flow(self.get(flow_id))
        active = [r for r in self.runs.values() if not r.done.is_set()]
        if any(r.flow.id == flow_id for r in active):
            raise FlowError(409, "this flow is already running; stop it before starting another run")
        if len(active) >= MAX_ACTIVE_RUNS:
            raise FlowError(429, f"{len(active)} flows are already running; wait for one to finish")
        self.preflight(flow, goal)
        runner = FlowRunner(self, flow, run_id=uuid.uuid4().hex[:12], goal=goal)
        self.runs[runner.id] = runner
        runner.task = asyncio.create_task(runner.run(), name=f"flow-run-{runner.id}")
        runner.task.add_done_callback(lambda _t: self._release(runner))
        return runner

    def _release(self, runner: FlowRunner) -> None:
        asyncio.get_running_loop().call_later(KEEP_FINISHED_S, self.runs.pop, runner.id, None)

    def get_run(self, run_id: str) -> dict[str, Any]:
        runner = self.runs.get(run_id)
        if runner is not None:
            return runner.to_json()
        run = self.store.get_run(run_id)
        if run is None:
            raise FlowError(404, "unknown run")
        return run

    async def wait_run(self, run_id: str, after: int, wait_s: float) -> dict[str, Any]:
        runner = self.runs.get(run_id)
        if runner is not None and wait_s > 0:
            await runner.wait(after, wait_s)
        return self.get_run(run_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        runner = self.runs.get(run_id)
        if runner is None:
            return self.get_run(run_id)
        runner.request_finish("cancelled", "stopped by the user")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(runner.done.wait(), 30)
        return runner.to_json()

    def list_runs(self, flow_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.get(flow_id)
        live = {r.id: r.summary() for r in self.runs.values() if r.flow.id == flow_id}
        stored = [{k: run.get(k) for k in ("id", "flow_id", "flow_name", "status", "reason", "started_at", "ended_at",
                                           "invocations", "max_invocations")}
                  for run in self.store.list_runs(flow_id, limit=limit) if run.get("id") not in live]
        runs = [*live.values(), *stored]
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]

    async def close(self) -> None:
        active = [r for r in self.runs.values() if not r.done.is_set()]
        for runner in active:
            runner.request_finish("cancelled", "the server is shutting down")
        for runner in active:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(runner.done.wait(), 15)
