"""Chat panel backend: free-form conversations with Antigravity models, optionally grounded in a session.

Every chat is one **tool-less** Antigravity conversation (`agent.antigravity.Conversation`): the
model can answer questions but can never click, type, run commands or touch the machine hosting the
Language Server. When the console shows a session, the panel can attach that session's timeline to
each message — only the part the chat has not seen yet — plus the latest screenshot, so the user can
ask "why did it click there?" or "what is on the screen now?" and get a grounded answer.

Chats live in memory for the server's lifetime (the transcript itself is kept by Antigravity, in the
conversation). Generation is non-blocking: `send()` records the user message and a pending assistant
message, runs the model turn in a task, and `wait()` long-polls until the turn finishes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from computeruse.agent.antigravity import (
    CONVERSATION_TAG,
    AntigravityModel,
    Conversation,
    LanguageServer,
    find_model,
    image_media,
)
from computeruse.agent.model import ModelError
from computeruse.computer.actions import describe_action, parse_action
from computeruse.telemetry.events import Event, EventType, SessionRecord, Usage

if TYPE_CHECKING:
    import httpx

    from computeruse.server.orchestrator import Orchestrator

log = logging.getLogger("computeruse.chat")

CHAT_TAG = "chat"
MAX_CONTEXT_LINES = 60  # newest timeline entries attached per message
MAX_CONTEXT_CHARS = 6000
MAX_LINE_CHARS = 300
MAX_THINKING_CHARS = 4000
MAX_TOOL_ROUNDS = 4
CONTEXT_MARKER = "[Session context"

CONTROL_COMMANDS = {
    "pause": "pause",
    "pause_session": "pause",
    "resume": "resume",
    "resume_session": "resume",
    "step": "step",
    "cancel": "cancel",
    "stop": "cancel",
    "stop_session": "cancel",
    "instruct": "instruct",
    "instruction": "instruct",
    "budget": "budget",
    "approve": "approve",
    "reject": "reject",
    "take_control": "take_control",
    "release_control": "release_control",
    "start_session": "start_session",
    "create_session": "start_session",
    "create_schedule": "create_schedule",
    "schedule": "create_schedule",
}

COMPUTER_ACTIONS = {
    "click": "left_click",
    "left_click": "left_click",
    "right_click": "right_click",
    "middle_click": "middle_click",
    "double_click": "double_click",
    "triple_click": "triple_click",
    "mouse_move": "mouse_move",
    "type": "type",
    "key": "key",
    "press_hotkey": "key",
    "hotkey": "key",
    "scroll": "scroll",
    "scroll_at": "scroll",
    "drag": "left_click_drag",
    "left_click_drag": "left_click_drag",
    "left_mouse_down": "left_mouse_down",
    "left_mouse_up": "left_mouse_up",
    "wait": "wait",
    "screenshot": "screenshot",
}

CHAT_SYSTEM = """\
You are the assistant built into the computeruse console, a tool for running and supervising a \
computer-use agent: a model that operates a browser or a desktop by looking at screenshots and \
clicking, typing and scrolling, one step at a time, under guardrails and budgets (max steps, \
duration, cost), with a human who can pause, instruct, approve, take over the mouse and keyboard, \
or cancel.

You can answer questions, give advice, supervise the running agent, and directly control the \
computer and sessions on the user's behalf.

To perform actions, include one or more JSON command or action blocks in your reply. You may \
include a brief sentence of narration before or alongside the blocks.

### Available Controls:

1. Direct Computer Actions (interact directly with the active session's computer):
   - {"action": "click", "coordinate": [x, y]} — click at coordinates
   - {"action": "right_click", "coordinate": [x, y]} — right click at coordinates
   - {"action": "double_click", "coordinate": [x, y]} — double click
   - {"action": "triple_click", "coordinate": [x, y]} — triple click
   - {"action": "mouse_move", "coordinate": [x, y]} — move mouse pointer
   - {"action": "type", "text": "..."} — type text
   - {"action": "key", "key": "Return"} — press a key or shortcut (e.g. "Return", "Escape", "ctrl+a", "BackSpace")
   - {"action": "scroll", "coordinate": [x, y], "delta_x": 0, "delta_y": -100} — scroll at coordinates
   - {"action": "drag", "start_coordinate": [x1, y1], "end_coordinate": [x2, y2]} — click and drag
   - {"action": "wait", "duration": 1.0} — wait specified seconds
   - {"action": "screenshot"} — take and inspect the latest screenshot
   Coordinates must be within the display resolution (e.g. 1024×768 or 1280×800) given in the session context.

2. Agent & Session Lifecycle:
   - {"command": "pause"} — pause the running agent loop
   - {"command": "resume"} — resume the agent loop
   - {"command": "step"} — step the agent forward by 1 step
   - {"command": "cancel"} — cancel/stop the session
   - {"command": "instruct", "text": "..."} — provide steering instructions for the agent's next turn
   - {"command": "budget", "max_steps": N} — adjust limits (max_steps, max_duration_s, max_cost_usd)
   - {"command": "approve"} / {"command": "reject"} — approve or reject a pending approval
   - {"command": "take_control"} / {"command": "release_control", "resume": true} — operator takeover

3. Session Launch & Scheduled Automation:
   - {"command": "start_session", "task": "...", "backend": "browser"} — launch a new session (backend: "browser" | "desktop" | "simulated")
   - {"command": "create_schedule", "title": "...", "plan": "...", "target_kind": "antigravity_chat", "frequency": "daily", "time_of_day": "09:00"} — create a recurring or one-off scheduled task (target_kind: "antigravity_chat" | "session" | "flow"; frequency: "hourly" | "daily" | "weekly" | "monthly" | "once")

### Interaction Protocol:
- If asked to act, emit the corresponding command/action JSON.
- If you execute computer actions, you will receive the execution results and updated screenshot in the next turn so you can observe the outcome.
- When all actions are complete or when asked a general question, reply with your final explanation and no further action blocks.
- Be concise, direct, and helpful. Use Markdown.
"""


def extract_control_calls(text: str) -> list[dict[str, Any]]:
    """Find command and computer action objects in the model reply."""
    decoder = json.JSONDecoder()
    calls: list[dict[str, Any]] = []
    i = 0
    while i < len(text):
        if text[i] in "{[":
            try:
                obj, end = decoder.raw_decode(text, i)
                i = end
                items = obj if isinstance(obj, list) else [obj]
                for item in items:
                    if isinstance(item, dict):
                        cmd = str(item.get("command", "")).lower()
                        act = str(item.get("action", "")).lower()
                        if cmd in CONTROL_COMMANDS:
                            calls.append(item)
                        elif act in COMPUTER_ACTIONS:
                            calls.append(item)
                continue
            except ValueError:
                pass
        i += 1
    return calls


@dataclass
class ChatActionExecution:
    type: str  # "command" | "action"
    name: str  # e.g. "click", "pause", "start_session"
    params: dict[str, Any]
    ok: bool
    summary: str
    error: str | None = None
    session_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "params": self.params,
            "ok": self.ok,
            "summary": self.summary,
            "error": self.error,
            "session_id": self.session_id,
        }


class ChatError(Exception):
    """A chat request that cannot be served; `status` is the HTTP status the API should return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# -- session context -------------------------------------------------------------------------------


def describe_event(e: Event) -> str | None:
    """One timeline line for the chat model, or None for entries that carry nothing worth telling."""
    d, t = e.data, e.type
    if t == EventType.SESSION_STARTED:
        disp = d.get("display") or {}
        size = f", display {disp['width']}×{disp['height']}" if disp.get("width") and disp.get("height") else ""
        return f"Session started on the {d.get('backend', '?')} backend with model {d.get('model', '?')}{size}"
    if t == EventType.SESSION_ENDED:
        out = f"Session ended: {d.get('outcome', '?')}"
        if d.get("reason"):
            out += f" — {d['reason']}"
        if d.get("final_text"):
            out += f". Agent's final message: {d['final_text']}"
        return out
    if t == EventType.SESSION_PAUSED:
        return f"Session paused (after step {d.get('step')})"
    if t == EventType.SESSION_RESUMED:
        return f"Session resumed (step {d.get('step')})"
    if t == EventType.TURN_STARTED:
        if d.get("phase") == "eval_check":
            return f"Eval check {'passed' if d.get('passed') else 'failed'}: {d.get('detail') or ''}".rstrip(": ")
        return None
    if t == EventType.ASSISTANT_TEXT:
        return f"Agent: {d.get('text', '')}"
    if t == EventType.GUARDRAIL_DECISION:
        if d.get("decision") in (None, "allow"):
            return None
        rule = f" (rule {d['rule']})" if d.get("rule") else ""
        return f"Guardrail {d['decision']}{rule} at step {d.get('step')}: {d.get('reason') or ''}".rstrip(": ")
    if t == EventType.APPROVAL_REQUESTED:
        return f"Approval requested for step {d.get('step')}: {d.get('description') or d.get('kind')}"
    if t == EventType.APPROVAL_RESOLVED:
        return f"Approval {'granted' if d.get('approved') else 'rejected'} for step {d.get('step')}"
    if t == EventType.ACTION_EXECUTED:
        desc = d.get("description") or d.get("kind") or "action"
        if d.get("blocked"):
            status = f"blocked: {d.get('error')}"
        elif d.get("rejected"):
            status = "rejected by the user"
        elif d.get("ok"):
            status = "ok"
            if "screen_changed" in d:
                status += ", screen changed" if d["screen_changed"] else ", screen unchanged"
        else:
            status = f"failed: {d.get('error') or 'unknown error'}"
        if d.get("output"):
            status += f"; output: {d['output']}"
        return f"Step {d.get('step')}: {desc} — {status}"
    if t == EventType.STUCK_NUDGED:
        return f"Stuck: '{d.get('description')}' repeated {d.get('repeats')}×; the agent was nudged to try something else"
    if t == EventType.USER_INSTRUCTION:
        return f"Operator instruction to the agent: {d.get('text', '')}"
    if t == EventType.MANUAL_ACTION:
        status = "ok" if d.get("ok") else f"failed: {d.get('error')}"
        return f"Operator performed: {d.get('description') or d.get('kind')} — {status}"
    if t == EventType.OPERATOR_CONTROL:
        if d.get("state") == "taken":
            return "Operator took control of the mouse and keyboard"
        return "Operator released control" + (" and resumed the agent" if d.get("resumed") else "")
    if t == EventType.BUDGET_CHANGED:
        changes = ", ".join(f"{k}={v}" for k, v in (d.get("changes") or {}).items())
        return f"Budget changed: {changes}"
    if t == EventType.ERROR:
        return f"Error ({d.get('where', '?')}): {d.get('message', '')}"
    return None  # turn.started, model.called, model.retry, action.proposed, frame, preview: noise for a reader


def latest_frame(events: list[Event]) -> int | None:
    """Sequence number of the newest full-screen frame among `events` (zoom crops only as a last resort)."""
    full = [int(e.data["seq"]) for e in events if e.type == EventType.FRAME and e.data.get("seq") is not None
            and e.data.get("kind") != "zoom"]
    if full:
        return full[-1]
    crops = [int(e.data["seq"]) for e in events if e.type == EventType.FRAME and e.data.get("seq") is not None]
    return crops[-1] if crops else None


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def session_context(rec: SessionRecord, events: list[Event], *, first: bool, screenshot: str | None) -> str:
    """The context block appended to a user message.

    `events` are the session's entries the chat has not seen yet; `first` adds the task header the first
    time the session is attached; `screenshot` is the caption of an attached image, or None.
    """
    head = f"{CONTEXT_MARKER}: session {rec.id} · {rec.status.value} · {rec.steps} steps, {rec.turns} turns]"
    lines: list[str] = []
    if first:
        lines.append(f"Task: {_clip(rec.task, 1000)}")
        lines.append(f"Backend: {rec.backend} · model: {rec.model}")
    entries = [_clip(line, MAX_LINE_CHARS) for line in (describe_event(e) for e in events) if line]
    omitted = max(0, len(entries) - MAX_CONTEXT_LINES)
    entries = entries[len(entries) - MAX_CONTEXT_LINES:] if omitted else entries
    while entries and sum(len(x) + 1 for x in entries) > MAX_CONTEXT_CHARS:
        entries.pop(0)
        omitted += 1
    if omitted:
        lines.append(f"(… {omitted} earlier entries omitted)")
    if entries:
        lines.append("Timeline" + (" since your previous reply:" if not first else ":"))
        lines.extend(f"- {x}" for x in entries)
    elif not first:
        lines.append("No new timeline entries since your previous reply.")
    else:
        lines.append("No timeline entries yet.")
    if screenshot:
        lines.append(f"[screenshot attached: {screenshot}]")
    return "\n".join([head, *lines])


# -- chats -------------------------------------------------------------------------------------------


@dataclass
class ChatMessage:
    id: str
    role: str  # user | assistant
    text: str
    created_at: float
    model: str | None = None  # Antigravity model id that answered / is answering
    model_label: str | None = None
    thinking: str = ""
    usage: Usage | None = None
    latency_ms: float | None = None
    stop_reason: str = ""
    error: str | None = None
    pending: bool = False
    context: dict[str, Any] | None = None  # what the console attached to a user message
    actions: list[ChatActionExecution] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "role": self.role, "text": self.text, "created_at": self.created_at,
            "model": self.model, "model_label": self.model_label,
            "thinking": self.thinking[:MAX_THINKING_CHARS], "usage": self.usage.model_dump() if self.usage else None,
            "latency_ms": self.latency_ms, "stop_reason": self.stop_reason, "error": self.error,
            "pending": self.pending, "context": self.context,
            "actions": [a.to_json() for a in self.actions],
        }


@dataclass
class Chat:
    id: str
    model: AntigravityModel
    created_at: float
    title: str | None = None
    updated_at: float = 0.0
    messages: list[ChatMessage] = field(default_factory=list)
    ls: LanguageServer | None = None
    conversation: Conversation | None = None
    seen_seq: dict[str, int] = field(default_factory=dict)  # session id → last event seq already attached
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def pending(self) -> bool:
        return self.task is not None and not self.task.done()

    def to_json(self, *, messages: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "title": self.title, "model": self.model.id, "model_label": self.model.label,
            "created_at": self.created_at, "updated_at": self.updated_at, "pending": self.pending,
            "conversation_id": self.conversation.id if self.conversation else None,
            "message_count": len(self.messages), "sessions": sorted(self.seen_seq),
        }
        if messages:
            out["messages"] = [m.to_json() for m in self.messages]
        return out


class ChatService:
    """Owns the chats of one server process; see the module docstring."""

    def __init__(self, orch: Orchestrator, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_chats: int = 50) -> None:
        self.orch = orch
        self.transport = transport  # tests inject an in-memory Language Server
        self.max_chats = max_chats
        self.chats: dict[str, Chat] = {}

    # -- availability & models -----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        cat = self.orch.catalog
        st = cat.status.get("antigravity")
        if cat.antigravity_endpoint is None:
            return False, (st.reason if st and st.reason else "Antigravity Language Server not detected")
        return True, st.reason if st else ""

    def models(self) -> list[AntigravityModel]:
        return [m for m in self.orch.catalog.antigravity_models if not m.disabled]

    def default_model(self) -> AntigravityModel | None:
        models = self.models()
        usable = [m for m in models if not m.quota_exhausted]
        configured = self.orch.settings.chat_model
        if configured:
            m = find_model(models, configured)
            if m is not None:
                return m
            log.warning("chat: configured COMPUTERUSE_CHAT_MODEL=%r is not offered by Antigravity", configured)
        flash = [m for m in usable if "flash" in m.label.lower() or "flash" in m.id.lower()]
        return (flash or usable or models or [None])[0]

    def resolve_model(self, ref: str | None) -> AntigravityModel:
        if not ref:
            m = self.default_model()
            if m is None:
                raise ChatError(503, "Antigravity lists no usable model")
            return m
        m = find_model(self.models(), ref)
        if m is None:
            offered = ", ".join(x.id for x in self.models())
            raise ChatError(400, f"Antigravity does not offer model {ref!r}; available: {offered}")
        return m

    def index(self) -> dict[str, Any]:
        ok, reason = self.available()
        default = self.default_model() if ok else None
        return {
            "available": ok, "reason": reason, "default_model": default.id if default else None,
            "models": [{"id": m.id, "label": m.label, "supports_images": m.supports_images,
                        "available": not m.quota_exhausted,
                        "reason": (f"quota exhausted until {m.quota_reset}" if m.quota_exhausted and m.quota_reset
                                   else "quota exhausted" if m.quota_exhausted else m.status_note())}
                       for m in (self.models() if ok else [])],
            "chats": [c.to_json(messages=False) for c in self.list()],
        }

    # -- CRUD -----------------------------------------------------------------------

    def get(self, chat_id: str) -> Chat:
        chat = self.chats.get(chat_id)
        if chat is None:
            raise ChatError(404, "unknown chat")
        return chat

    def list(self) -> list[Chat]:
        return sorted(self.chats.values(), key=lambda c: c.updated_at, reverse=True)

    async def create(self, *, model: str | None = None, title: str | None = None) -> Chat:
        ok, reason = self.available()
        if not ok:
            raise ChatError(503, f"chat needs a running Antigravity: {reason}")
        now = time.time()
        chat = Chat(id=uuid.uuid4().hex[:12], model=self.resolve_model(model), created_at=now, updated_at=now,
                    title=(title or "").strip()[:120] or None)
        self.chats[chat.id] = chat
        await self._evict()
        return chat

    async def _evict(self) -> None:
        idle = [c for c in self.list() if not c.pending]
        while len(self.chats) > self.max_chats and idle:
            oldest = idle.pop()
            log.info("chat: evicting idle chat %s (%d chats kept)", oldest.id, self.max_chats)
            await self._dispose(oldest)

    async def delete(self, chat_id: str) -> None:
        await self._dispose(self.get(chat_id))

    async def _dispose(self, chat: Chat) -> None:
        self.chats.pop(chat.id, None)
        await self._stop(chat)
        if chat.conversation is not None and self.orch.settings.antigravity_archive:
            try:
                await chat.conversation.archive()
            except ModelError as e:
                log.warning("chat: could not archive conversation %s: %s", chat.conversation.id, e)
        if chat.ls is not None:
            await chat.ls.aclose()

    async def close(self) -> None:
        for chat in list(self.chats.values()):
            await self._dispose(chat)

    # -- turns ----------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, *, model: str | None = None, session_id: str | None = None,
                   screenshot: bool = True) -> tuple[ChatMessage, ChatMessage]:
        """Record a user message and start the reply; returns (user message, pending assistant message)."""
        chat = self.get(chat_id)
        if chat.pending:
            raise ChatError(409, "the previous reply is still being generated")
        ok, reason = self.available()
        if not ok:
            raise ChatError(503, f"chat needs a running Antigravity: {reason}")
        text = text.strip()
        if not text:
            raise ChatError(400, "message text is empty")
        target = self.resolve_model(model) if model else chat.model
        if target.quota_exhausted:
            raise ChatError(400, f"Antigravity quota for {target.label!r} is exhausted"
                                 f"{' until ' + target.quota_reset if target.quota_reset else ''}")

        wire_text, media, context, last_seq = text, [], None, None
        if session_id:
            rec = self.orch.get_session(session_id)
            if rec is None:
                raise ChatError(404, f"unknown session {session_id!r}")
            wire_text, media, context, last_seq = await self._with_context(chat, rec, text, target,
                                                                           screenshot=screenshot)
        else:
            active_runners = [r for r in self.orch.runners.values() if not r.record.status.terminal]
            if active_runners:
                active_info = ", ".join(f"session {r.record.id} ({r.record.backend}, {r.record.status.value}, {r.record.steps} steps)"
                                        for r in active_runners)
                wire_text = f"{text}\n\n[Active sessions on server: {active_info}]"

        now = time.time()
        user = ChatMessage(id=uuid.uuid4().hex[:12], role="user", text=text, created_at=now, context=context)
        assistant = ChatMessage(id=uuid.uuid4().hex[:12], role="assistant", text="", created_at=now,
                                model=target.id, model_label=target.label, pending=True)
        chat.messages.extend([user, assistant])
        chat.model = target
        chat.title = chat.title or _clip(text.splitlines()[0], 60)
        chat.updated_at = now
        chat.changed.clear()
        chat.task = asyncio.create_task(
            self._generate(chat, assistant, wire_text, media, target, session_id, last_seq),
            name=f"chat-{chat.id}")
        return user, assistant

    async def _with_context(self, chat: Chat, rec: SessionRecord, text: str, model: AntigravityModel, *,
                            screenshot: bool) -> tuple[str, list[dict[str, Any]], dict[str, Any], int]:
        seen = chat.seen_seq.get(rec.id, -1)
        events = self.orch.events(rec.id, after_seq=seen)
        last_seq = events[-1].seq if events else seen
        media: list[dict[str, Any]] = []
        caption: str | None = None
        frame_seq = latest_frame(events) if screenshot else None
        if frame_seq is not None and model.supports_images:
            path = self.orch.store.frame_path(rec.id, frame_seq)
            if path is not None:
                png = await asyncio.to_thread(path.read_bytes)
                caption = f"the latest screen of session {rec.id} (frame {frame_seq})"
                media.append(image_media(base64.b64encode(png).decode(), caption))
        block = session_context(rec, events, first=seen < 0, screenshot=caption)
        context = {"session_id": rec.id, "events": len(events), "first": seen < 0, "screenshot": bool(media),
                   "after_seq": seen, "until_seq": last_seq}
        if screenshot and frame_seq is not None and not model.supports_images:
            context["note"] = f"{model.label} does not accept images; screenshot not attached"
        return f"{text}\n\n{block}", media, context, last_seq

    async def _execute_call(self, call: dict[str, Any], session_id: str | None
                           ) -> tuple[ChatActionExecution, bytes | None, str | None]:
        cmd_raw = str(call.get("command", "")).lower()
        act_raw = str(call.get("action", "")).lower()

        if cmd_raw in CONTROL_COMMANDS:
            cmd = CONTROL_COMMANDS[cmd_raw]
            if cmd == "start_session":
                task = str(call.get("task", "")).strip()
                if not task:
                    return ChatActionExecution(
                        type="command", name="start_session", params=call, ok=False,
                        summary="start session", error="task description is required",
                    ), None, None
                backend = str(call.get("backend") or "browser").strip()
                script = call.get("script")
                model_name = call.get("model")
                if not model_name:
                    if script:
                        model_name = "scripted"
                    elif self.default_model():
                        model_name = self.default_model().id
                    else:
                        model_name = self.orch.settings.model
                overrides: dict[str, Any] = {}
                if "max_steps" in call:
                    try:
                        overrides["max_steps"] = int(call["max_steps"])
                    except (ValueError, TypeError):
                        pass
                if call.get("start_paused"):
                    overrides["start_paused"] = True
                try:
                    rec = await self.orch.create_session(
                        task=task, backend=backend, model=model_name,
                        overrides=overrides or None, script=script,
                    )
                    return ChatActionExecution(
                        type="command", name="start_session", params=call, ok=True,
                        summary=f"started session {rec.id[:8]} ({backend}) for task: {_clip(task, 60)}",
                        session_id=rec.id,
                    ), None, rec.id
                except Exception as e:
                    return ChatActionExecution(
                        type="command", name="start_session", params=call, ok=False,
                        summary=f"start session ({backend})", error=str(e),
                    ), None, None

            if cmd == "create_schedule":
                title = str(call.get("title") or call.get("task") or "Scheduled routine").strip()
                plan = str(call.get("plan") or call.get("task") or title).strip()
                target_kind = str(call.get("target_kind") or "antigravity_chat").strip()
                backend = str(call.get("backend") or "simulated").strip()
                trig = {
                    "frequency": str(call.get("frequency") or "daily").lower(),
                    "time_of_day": str(call.get("time_of_day") or "09:00"),
                    "minute_of_hour": int(call.get("minute_of_hour") or 0),
                    "days_of_week": call.get("days_of_week") or [1, 2, 3, 4, 5],
                    "day_of_month": int(call.get("day_of_month") or 1),
                    "condition": str(call.get("condition") or ""),
                }
                try:
                    sched = self.orch.schedules.create(
                        title=title, plan=plan, target_kind=target_kind, backend=backend, trigger=trig,
                    )
                    return ChatActionExecution(
                        type="command", name="create_schedule", params=call, ok=True,
                        summary=f"scheduled '{sched.title}' ({sched.to_summary()['trigger_summary']})",
                    ), None, session_id
                except Exception as e:
                    return ChatActionExecution(
                        type="command", name="create_schedule", params=call, ok=False,
                        summary=f"schedule '{title}'", error=str(e),
                    ), None, session_id

            # Other control commands target a session
            sid = call.get("session_id") or session_id
            if not sid:
                active = [r.record.id for r in self.orch.runners.values() if not r.record.status.terminal]
                if len(active) == 1:
                    sid = active[0]
            if not sid:
                return ChatActionExecution(
                    type="command", name=cmd, params=call, ok=False,
                    summary=f"{cmd} session", error="no session is active or specified",
                ), None, None

            try:
                if cmd == "instruct":
                    text = str(call.get("text", "")).strip()
                    if not text:
                        raise ValueError("instruction text is required")
                    await self.orch.control(sid, "instruct", {"text": text})
                    summary = f"instructed session {sid[:8]}: {_clip(text, 50)}"
                elif cmd == "budget":
                    budget_args = {k: call[k] for k in ("max_steps", "max_turns", "max_duration_s", "max_cost_usd") if k in call}
                    if not budget_args:
                        raise ValueError("budget: pass at least one limit (max_steps, max_cost_usd, etc.)")
                    await self.orch.control(sid, "budget", budget_args)
                    summary = f"updated budget for session {sid[:8]}: {', '.join(f'{k}={v}' for k, v in budget_args.items())}"
                elif cmd in ("approve", "reject"):
                    await self.orch.control(sid, cmd, {"approval_id": call.get("approval_id")})
                    summary = f"{cmd}d approval for session {sid[:8]}"
                elif cmd in ("pause", "resume", "step", "cancel", "take_control", "release_control"):
                    await self.orch.control(sid, cmd, call)
                    summary = f"{cmd} session {sid[:8]}"
                else:
                    raise ValueError(f"unknown command {cmd!r}")
                return ChatActionExecution(
                    type="command", name=cmd, params=call, ok=True,
                    summary=summary, session_id=sid,
                ), None, sid
            except Exception as e:
                return ChatActionExecution(
                    type="command", name=cmd, params=call, ok=False,
                    summary=f"{cmd} session {sid[:8]}", error=str(e), session_id=sid,
                ), None, sid

        if act_raw in COMPUTER_ACTIONS:
            canonical_act = COMPUTER_ACTIONS[act_raw]
            sid = call.get("session_id") or session_id
            if not sid:
                active = [r.record.id for r in self.orch.runners.values() if not r.record.status.terminal]
                if len(active) == 1:
                    sid = active[0]
            if not sid:
                return ChatActionExecution(
                    type="action", name=act_raw, params=call, ok=False,
                    summary=f"action {act_raw}", error="no session is active or specified",
                ), None, None

            runner = self.orch.runners.get(sid)
            if runner is None or runner.record.status.terminal:
                return ChatActionExecution(
                    type="action", name=act_raw, params=call, ok=False,
                    summary=f"action {act_raw}", error=f"session {sid[:8]} is not active", session_id=sid,
                ), None, sid

            action_dict = dict(call)
            action_dict.pop("session_id", None)
            action_dict.pop("command", None)
            action_dict["action"] = canonical_act
            if "hotkey" in action_dict and "key" not in action_dict:
                action_dict["key"] = action_dict.pop("hotkey")

            # Pause running agent if it is actively executing turns
            if not runner._agent_idle:
                await self.orch.control(sid, "pause", {})
                for _ in range(25):
                    if runner._agent_idle:
                        break
                    await asyncio.sleep(0.2)

            try:
                res = await self.orch.manual_action(sid, action_dict)
                desc = res.get("description")
                if not desc:
                    try:
                        desc = describe_action(parse_action(action_dict))
                    except Exception:
                        desc = canonical_act

                # Capture latest screenshot
                png_bytes: bytes | None = None
                events = self.orch.events(sid)
                fseq = latest_frame(events)
                if fseq is not None:
                    path = self.orch.store.frame_path(sid, fseq)
                    if path and path.exists():
                        png_bytes = await asyncio.to_thread(path.read_bytes)

                return ChatActionExecution(
                    type="action", name=canonical_act, params=call,
                    ok=bool(res.get("ok", True)), summary=desc, error=res.get("error"),
                    session_id=sid,
                ), png_bytes, sid
            except Exception as e:
                return ChatActionExecution(
                    type="action", name=canonical_act, params=call, ok=False,
                    summary=f"action {canonical_act}", error=str(e), session_id=sid,
                ), None, sid

        return ChatActionExecution(
            type="unknown", name="unknown", params=call, ok=False,
            summary="unrecognized command or action", error="unrecognized call",
        ), None, session_id

    async def _generate(self, chat: Chat, assistant: ChatMessage, text: str, media: list[dict[str, Any]],
                        model: AntigravityModel, session_id: str | None, last_seq: int | None) -> None:
        t0 = time.perf_counter()
        try:
            if chat.conversation is None:
                await self._start(chat, model, text)
            assert chat.conversation is not None

            current_text = text
            current_media = media
            accumulated_text: list[str] = []
            accumulated_thinking: list[str] = []
            executed_actions: list[ChatActionExecution] = []
            current_session_id = session_id

            for round_idx in range(MAX_TOOL_ROUNDS):
                reply = await chat.conversation.send(current_text, current_media or None, model=model)
                if reply.empty and round_idx == 0:
                    log.warning("chat %s: empty reply from %s; nudging once", chat.id, model.label)
                    reply = reply.merged(await chat.conversation.send(
                        "Your reply was empty. Please answer the previous message.", model=model))
                if reply.empty:
                    if not accumulated_text and not executed_actions:
                        raise ModelError(f"Antigravity ({model.label}) returned no reply"
                                         f"{' (stop reason ' + reply.stop_reason + ')' if reply.stop_reason else ''}")
                    break

                if reply.text.strip():
                    accumulated_text.append(reply.text.strip())
                if reply.thinking.strip():
                    accumulated_thinking.append(reply.thinking.strip())
                if assistant.usage is None:
                    assistant.usage = reply.usage
                else:
                    assistant.usage.add(reply.usage)
                assistant.stop_reason = reply.stop_reason
                if reply.generator and reply.generator != model.enum:
                    actual = find_model(self.models(), reply.generator)
                    assistant.model = actual.id if actual else reply.generator
                    assistant.model_label = actual.label if actual else reply.generator

                calls = extract_control_calls(reply.text)
                if not calls:
                    break

                round_results: list[str] = []
                latest_frame_png: bytes | None = None
                for call in calls:
                    exec_rec, frame_png, new_sid = await self._execute_call(call, current_session_id)
                    executed_actions.append(exec_rec)
                    if new_sid:
                        current_session_id = new_sid
                    if frame_png:
                        latest_frame_png = frame_png
                    status_text = "ok" if exec_rec.ok else f"failed: {exec_rec.error or 'error'}"
                    round_results.append(f"- {exec_rec.summary}: {status_text}")

                assistant.actions = list(executed_actions)
                chat.updated_at = time.time()
                chat.changed.set()

                if round_idx == MAX_TOOL_ROUNDS - 1:
                    break

                feedback_lines = ["[Execution results:", *round_results]
                feedback_media: list[dict[str, Any]] = []
                if latest_frame_png and model.supports_images:
                    caption = f"screenshot after action (session {current_session_id})"
                    feedback_media.append(image_media(base64.b64encode(latest_frame_png).decode(), caption))
                    feedback_lines.append(f"[screenshot attached: {caption}]")
                feedback_lines.append("]")
                current_text = "\n".join(feedback_lines)
                current_media = feedback_media

            assistant.text = "\n\n".join(accumulated_text)
            assistant.thinking = "\n\n".join(accumulated_thinking)
            assistant.actions = executed_actions

            if current_session_id is not None:
                events = self.orch.events(current_session_id)
                if events:
                    chat.seen_seq[current_session_id] = events[-1].seq
                elif last_seq is not None:
                    chat.seen_seq[current_session_id] = last_seq
        except asyncio.CancelledError:
            assistant.error = "cancelled"
            raise
        except ModelError as e:
            assistant.error = str(e)
            log.warning("chat %s: %s", chat.id, e)
        except Exception as e:  # noqa: BLE001 - surfaced to the user, never lost in a task
            assistant.error = f"{type(e).__name__}: {e}"
            log.exception("chat %s: reply failed", chat.id)
        finally:
            assistant.pending = False
            assistant.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            chat.updated_at = time.time()
            chat.changed.set()

    async def _start(self, chat: Chat, model: AntigravityModel, first_text: str) -> None:
        endpoint = self.orch.catalog.antigravity_endpoint
        if endpoint is None:
            raise ModelError("Antigravity Language Server is not available")
        if chat.ls is None:
            chat.ls = LanguageServer(endpoint, transport=self.transport)
        conversation = Conversation(chat.ls, model)
        title = chat.title or _clip(first_text.split(CONTEXT_MARKER)[0], 60)
        await conversation.start([("CHAT", CHAT_SYSTEM)], title=f"computeruse chat · {title}",
                                 tags=(CONVERSATION_TAG, CHAT_TAG))
        chat.conversation = conversation

    async def wait(self, chat_id: str, wait_s: float) -> Chat:
        """Return the chat, after waiting up to `wait_s` seconds for a pending reply to finish."""
        chat = self.get(chat_id)
        if wait_s > 0 and chat.pending:
            try:
                await asyncio.wait_for(chat.changed.wait(), timeout=wait_s)
            except TimeoutError:
                pass
        return chat

    async def cancel(self, chat_id: str) -> Chat:
        chat = self.get(chat_id)
        await self._stop(chat)
        return chat

    async def _stop(self, chat: Chat) -> None:
        if not chat.pending:
            return
        if chat.conversation is not None:
            try:
                await chat.conversation.cancel()  # tell the server first, so it stops spending tokens
            except ModelError as e:
                log.warning("chat %s: cancel failed: %s", chat.id, e)
        assert chat.task is not None
        chat.task.cancel()
        await asyncio.wait([chat.task])
