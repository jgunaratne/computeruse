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
CONTEXT_MARKER = "[Session context"

CHAT_SYSTEM = """\
You are the assistant built into the computeruse console, a tool for running and supervising a \
computer-use agent: a model that operates a browser or a desktop by looking at screenshots and \
clicking, typing and scrolling, one step at a time, under guardrails and budgets (max steps, \
duration, cost), with a human who can pause, instruct, approve, take over the mouse and keyboard, \
or cancel.

You are a chat participant only. You have no tools: you cannot click, type, browse, run commands, \
read files, or change a session. If asked to act, say so plainly and point to what the user can do \
in the console (pause/resume, instruct the agent, raise the step budget, take control, cancel, start \
a new session with a better task description).

Some user messages end with a block that starts with "[Session context". The console appends it \
automatically — the user did not write it. It contains the session's timeline entries that are new \
since your previous reply (steps taken, what the agent said, guardrail decisions, operator actions, \
errors, the outcome) and sometimes the latest screenshot. Treat it as ground truth about the session: \
refer to step numbers, quote error messages, describe what the screenshot shows. If the context does \
not contain what the user asks about, say that instead of guessing. Without any context block, \
answer from general knowledge.

Be concise and concrete. Use Markdown: short paragraphs, bullet lists where they help, fenced code \
blocks for code, commands or exact text. No preamble, no sign-off.
"""


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

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "role": self.role, "text": self.text, "created_at": self.created_at,
            "model": self.model, "model_label": self.model_label,
            "thinking": self.thinking[:MAX_THINKING_CHARS], "usage": self.usage.model_dump() if self.usage else None,
            "latency_ms": self.latency_ms, "stop_reason": self.stop_reason, "error": self.error,
            "pending": self.pending, "context": self.context,
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

    async def _generate(self, chat: Chat, assistant: ChatMessage, text: str, media: list[dict[str, Any]],
                        model: AntigravityModel, session_id: str | None, last_seq: int | None) -> None:
        t0 = time.perf_counter()
        try:
            if chat.conversation is None:
                await self._start(chat, model, text)
            assert chat.conversation is not None
            reply = await chat.conversation.send(text, media or None, model=model)
            if reply.empty:
                log.warning("chat %s: empty reply from %s; nudging once", chat.id, model.label)
                reply = reply.merged(await chat.conversation.send(
                    "Your reply was empty. Please answer the previous message.", model=model))
            if reply.empty:
                raise ModelError(f"Antigravity ({model.label}) returned no reply"
                                 f"{' (stop reason ' + reply.stop_reason + ')' if reply.stop_reason else ''}")
            assistant.text, assistant.thinking = reply.text.strip(), reply.thinking.strip()
            assistant.usage, assistant.stop_reason = reply.usage, reply.stop_reason
            if reply.generator and reply.generator != model.enum:
                actual = find_model(self.models(), reply.generator)
                assistant.model = actual.id if actual else reply.generator
                assistant.model_label = actual.label if actual else reply.generator
            if session_id is not None and last_seq is not None:
                chat.seen_seq[session_id] = last_seq  # only once the model has actually seen it
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
