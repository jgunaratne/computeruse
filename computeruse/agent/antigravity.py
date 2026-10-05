"""Antigravity as a computer-use model provider.

The models an Antigravity installation offers — Gemini and Claude tiers,
including builds that are not on the public APIs — are reachable through its
local **Language Server** (the process behind the Antigravity UI and IDE
extensions). The server speaks Connect RPC over plain HTTP on localhost and
accepts JSON bodies, so this module talks to it directly: no SDK, no CLI
subprocess. Verified live against `localhost:5387` (2026-10).

How a session maps onto Antigravity:

* One **conversation per session**, created lazily on the first `create()`
  with a *tool-less custom agent*: our system prompt is the whole system prompt
  (`exclude_default_components`), no tools are registered, command execution is
  off. The model can therefore only answer — it can never run commands or touch
  files on the machine hosting the Language Server.
* The canonical transcript is **stateful** on the Antigravity side. Each `create()`
  sends only the messages appended since the previous call — the task (first
  turn) or the tool results — as one user message whose screenshots travel as
  inline PNG `media`. `blocking: true` makes the RPC return when the model turn
  is complete; the new trajectory steps carry the reply, thinking and token
  usage.
* No tool is wired to the Antigravity agent, so the `computer` tool is driven through
  a **JSON reply protocol**: an action is a JSON object `{"action": ..., ...}`
  using the `computer_tool_schema` parameters (optionally preceded by one
  sentence of narration); a plain-text reply with no action object is the final
  answer. `parse_reply()` turns that back into canonical `text` / `tool_use`
  blocks, so the loop never sees the difference.
* Usage comes from `modelUsage` on the planner steps. Antigravity is not billed in
  USD (it draws on the user's Antigravity quota), so no cost is attributed.

Connection: address and CSRF token come from the settings
(`COMPUTERUSE_ANTIGRAVITY_ADDRESS` / `COMPUTERUSE_ANTIGRAVITY_CSRF_TOKEN`), else from the
environment an Antigravity shell provides (`ANTIGRAVITY_LS_ADDRESS` /
`ANTIGRAVITY_CSRF_TOKEN`), else the default `localhost:5387` with the token
fetched from the server's own index page. The token is never logged or exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from computeruse.agent.model import ModelError, ModelTurn
from computeruse.agent.prompts import ACTION_NAMES, computer_tool_schema
from computeruse.agent.vertex import display_from_tools
from computeruse.telemetry.events import Usage

log = logging.getLogger("computeruse.antigravity")

PROVIDER = "antigravity"
SERVICE = "exa.language_server_pb.LanguageServerService"
CSRF_HEADER = "x-codeium-csrf-token"
DEFAULT_ADDRESS = "localhost:5387"
ADDRESS_ENV = ("ANTIGRAVITY_LS_ADDRESS", "LANGUAGE_SERVER_ADDRESS")
TOKEN_ENV = ("ANTIGRAVITY_CSRF_TOKEN", "CSRF_TOKEN")
CONVERSATION_TAG = "computeruse"
MAX_THINKING_CHARS = 1500  # kept on the canonical turn for the event log; the rest stays in Antigravity
_CSRF_IN_PAGE = re.compile(r'"?csrfToken"?\s*:\s*"([^"]+)"')
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$", re.M)
_TERMINAL_STATUSES = {"CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_ERROR", "CORTEX_STEP_STATUS_CANCELED",
                      "CORTEX_STEP_STATUS_INTERRUPTED", "CORTEX_STEP_STATUS_INVALID"}


# -- endpoint ------------------------------------------------------------------------


@dataclass(frozen=True)
class AntigravityEndpoint:
    address: str  # host:port (or a full http(s):// URL)
    csrf_token: str
    token_source: str  # configured | environment | server

    @property
    def base_url(self) -> str:
        return base_url_of(self.address)

    def public(self) -> dict[str, Any]:
        """What may be shown to users: never the token itself."""
        return {"address": self.address, "token_source": self.token_source}


def base_url_of(address: str) -> str:
    a = address.strip().rstrip("/")
    return a if a.startswith(("http://", "https://")) else f"http://{a}"


def resolve_address(configured: str | None, env: dict[str, str] | None = None) -> tuple[str, str]:
    """→ (address, where it came from): settings, else the Antigravity shell environment, else the default port."""
    env = os.environ if env is None else env
    if configured:
        return configured.strip(), "configured"
    for key in ADDRESS_ENV:
        if env.get(key):
            return env[key].strip(), f"environment ({key})"
    return DEFAULT_ADDRESS, "default"


def resolve_token(configured: str | None, env: dict[str, str] | None = None) -> tuple[str | None, str]:
    env = os.environ if env is None else env
    if configured:
        return configured.strip(), "configured"
    for key in TOKEN_ENV:
        if env.get(key):
            return env[key].strip(), "environment"
    return None, "server"


def token_from_index_page(body: str) -> str | None:
    """The Language Server's index page embeds the CSRF token when Jetbox (the web UI) is enabled."""
    m = _CSRF_IN_PAGE.search(body)
    return m.group(1) if m else None


def detect_antigravity(address: str | None = None, csrf_token: str | None = None, *, timeout: float = 3.0,
                  env: dict[str, str] | None = None,
                  transport: httpx.BaseTransport | None = None) -> AntigravityDetection:
    """Synchronous availability probe for `ModelCatalog.refresh()`.

    → (endpoint or None, human reason, the enabled models the server lists for this user). Reaches the
    server twice at most: the index page when no token is known, then the model-list RPC, which also
    proves the token works. Never raises; failures come back as (None, reason, []).
    """
    addr, addr_from = resolve_address(address, env)
    token, token_from = resolve_token(csrf_token, env)
    base = base_url_of(addr)
    hint = ("" if addr_from != "default"
            else " (start Antigravity, or point COMPUTERUSE_ANTIGRAVITY_ADDRESS at its Language Server)")
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)), transport=transport) as http:
            if not token:
                page = http.get(f"{base}/")
                token = token_from_index_page(page.text) if page.status_code == 200 else None
                if not token:
                    return None, (f"Antigravity Language Server at {addr} did not hand out a CSRF token (HTTP "
                                  f"{page.status_code}); set COMPUTERUSE_ANTIGRAVITY_CSRF_TOKEN"), []
            resp = http.post(f"{base}/{SERVICE}/GetCascadeModelConfigData", json={},
                             headers={CSRF_HEADER: token, "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return None, f"no Antigravity Language Server at {addr}: {type(e).__name__}{hint}", []
    if resp.status_code == 401:
        return None, f"Antigravity Language Server at {addr} rejected the CSRF token ({token_from})", []
    if resp.status_code != 200:
        return None, f"Antigravity Language Server at {addr} answered {resp.status_code}: {connect_error(resp)}", []
    try:
        models = [m for m in parse_model_configs(resp.json()) if not m.disabled]
    except ValueError as e:
        return None, f"Antigravity Language Server at {addr} returned an unexpected model list: {e}", []
    endpoint = AntigravityEndpoint(address=addr, csrf_token=token, token_source=token_from)
    return endpoint, (f"Antigravity Language Server at {addr} ({len(models)} models, token from {token_from}"
                      f"{'' if addr_from == 'configured' else ', address ' + addr_from})"), models


def connect_error(resp: httpx.Response) -> str:
    """Connect RPC errors are `{"code": ..., "message": ...}`; fall back to the raw body."""
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            return f"{data.get('code', 'error')}: {data['message']}"
    except ValueError:
        pass
    return (resp.text or f"HTTP {resp.status_code}")[:300]


# -- model catalogue ---------------------------------------------------------------------


@dataclass(frozen=True)
class AntigravityModel:
    enum: str  # MODEL_PLACEHOLDER_M<n> — what the RPCs take
    id: str  # e.g. gemini-flash-lite — what users type
    label: str  # e.g. Gemini Flash Lite — what the Antigravity picker shows
    supports_images: bool = True
    disabled: bool = False
    quota_remaining: float | None = None  # fraction of the user's quota left, when reported
    quota_reset: str | None = None  # RFC 3339
    order: int = 10_000  # position in Antigravity's recommended ordering

    @property
    def quota_exhausted(self) -> bool:
        return self.quota_remaining is not None and self.quota_remaining <= 0.0

    def status_note(self) -> str:
        parts = ["listed by Antigravity"]
        if self.quota_remaining is not None:
            parts.append(f"quota {self.quota_remaining * 100:.0f}% left")
            if self.quota_reset:
                parts[-1] += f", resets {self.quota_reset.replace('T', ' ').replace('Z', ' UTC')}"
        return " · ".join(parts)


# What `detect_antigravity` (and any stand-in for it) returns: reachable endpoint or None, a human reason,
# and the enabled models listed for this user (empty when unreachable).
AntigravityDetection = tuple[AntigravityEndpoint | None, str, list[AntigravityModel]]


def parse_model_configs(data: dict[str, Any]) -> list[AntigravityModel]:
    """`GetCascadeModelConfigData` response → models in Antigravity's recommended order."""
    configs = data.get("clientModelConfigs")
    if not isinstance(configs, list):
        raise ValueError("clientModelConfigs missing")
    order: dict[str, int] = {}
    for sort in data.get("clientModelSorts") or []:
        for group in sort.get("groups") or []:
            for label in group.get("modelLabels") or []:
                order.setdefault(label.lower(), len(order))
        break  # the first sort is the recommended one
    out: list[AntigravityModel] = []
    for c in configs:
        enum = ((c.get("modelOrAlias") or {}).get("model")) or ""
        label = c.get("label") or enum
        if not enum:
            continue
        quota = c.get("quotaInfo") or {}
        out.append(AntigravityModel(
            enum=enum, id=c.get("modelId") or enum.lower(), label=label,
            supports_images=bool(c.get("supportsImages", False)), disabled=bool(c.get("disabled", False)),
            quota_remaining=float(quota["remainingFraction"]) if "remainingFraction" in quota else None,
            quota_reset=quota.get("resetTime"), order=order.get(label.lower(), 10_000),
        ))
    out.sort(key=lambda m: (m.order, m.label))
    return out


def find_model(models: list[AntigravityModel], ref: str) -> AntigravityModel | None:
    """Match a user reference against model id, display label or enum name (case-insensitive)."""
    want = ref.strip().lower()
    if want.startswith(f"{PROVIDER}:"):
        want = want[len(PROVIDER) + 1:]
    for m in models:
        if want in (m.id.lower(), m.label.lower(), m.enum.lower()):
            return m
    return None


# -- Language Server client -------------------------------------------------------------------


class LanguageServer:
    """Minimal async Connect-JSON client for the Antigravity Language Server."""

    def __init__(self, endpoint: AntigravityEndpoint, *, timeout: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.endpoint = endpoint
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), transport=transport,
                                       headers={CSRF_HEADER: endpoint.csrf_token})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.endpoint.base_url}/{SERVICE}/{method}"
        try:
            resp = await self._http.post(url, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise ModelError(f"Antigravity Language Server unreachable at {self.endpoint.address} "
                             f"({method}): {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            detail = connect_error(resp)
            if resp.status_code == 401:
                detail += " — the CSRF token is stale; restart the server from an Antigravity shell or set " \
                          "COMPUTERUSE_ANTIGRAVITY_CSRF_TOKEN"
            raise ModelError(f"Antigravity {method} failed ({resp.status_code}): {detail}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise ModelError(f"Antigravity {method} returned non-JSON: {resp.text[:200]!r}") from e

    async def models(self) -> list[AntigravityModel]:
        return parse_model_configs(await self.call("GetCascadeModelConfigData", {}, timeout=30.0))

    async def trajectory(self, cascade_id: str) -> tuple[list[dict[str, Any]], str]:
        """All steps of a conversation (paged) and the run status."""
        data = await self.call("GetCascadeTrajectory", {"cascadeId": cascade_id}, timeout=60.0)
        steps = list((data.get("trajectory") or {}).get("steps") or [])
        total = int(data.get("numTotalSteps") or 0)
        while len(steps) < total:
            more = await self.call("GetCascadeTrajectorySteps", {"cascadeId": cascade_id, "stepOffset": len(steps)},
                                   timeout=60.0)
            got = list(more.get("steps") or [])
            if not got:
                break
            steps.extend(got)
        return steps, str(data.get("status") or "")


# -- reply protocol -----------------------------------------------------------------------


def response_format_section(display: tuple[int, int]) -> str:
    """System-prompt section that replaces tool calling with JSON replies."""
    schema = computer_tool_schema(display)
    return (
        "This conversation has no callable tools. The `computer` tool described above is driven through your "
        "replies instead:\n"
        "* To perform an action, reply with exactly one JSON object using the `computer` parameters below, e.g. "
        '{"action": "left_click", "coordinate": [412, 300]} or {"action": "type", "text": "hello"}. You may '
        "put one short sentence of narration (what you see, what you will do) before the object, nothing after it. "
        "One action per reply.\n"
        "* The next message brings that action's result and a fresh screenshot.\n"
        "* When the task is complete, or impossible/unsafe, reply with plain text only — no JSON object — giving "
        "your final summary (include any requested information verbatim).\n"
        "* Never reply with an empty message or with a question; the user is not watching live.\n\n"
        f"`computer` tool: {schema['description']}\n\n"
        f"`computer` parameters (JSON schema; `action` is one of {', '.join(ACTION_NAMES)}):\n"
        f"{json.dumps(schema['input_schema'], separators=(',', ':'))}"
    )


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text)


def extract_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split a reply into (narration, action objects).

    Every JSON object in the text whose `action` is a known action name counts; text between/after
    objects is dropped (the protocol asks for narration *before* the object).
    """
    decoder = json.JSONDecoder()
    actions: list[dict[str, Any]] = []
    narration_end: int | None = None
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("action"), str) and obj["action"] in ACTION_NAMES:
            if narration_end is None:
                narration_end = i
            actions.append(obj)
        i = end
    narration = text if narration_end is None else text[:narration_end]
    return _strip_fences(narration).strip(), actions


def parse_reply(text: str, thinking: str = "") -> tuple[list[dict[str, Any]], str]:
    """Antigravity reply text → (canonical content blocks, stop_reason)."""
    blocks: list[dict[str, Any]] = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": thinking[:MAX_THINKING_CHARS]})
    narration, actions = extract_actions(text)
    if not actions:
        final = _strip_fences(text).strip()
        if final:
            blocks.append({"type": "text", "text": final})
        return blocks, "end_turn"
    if narration:
        blocks.append({"type": "text", "text": narration})
    for action in actions:
        note = action.pop("note", None)  # tolerated even though the protocol asks for narration up front
        if isinstance(note, str) and note.strip() and not narration:
            blocks.append({"type": "text", "text": note.strip()})
        blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}", "name": "computer",
                       "input": action})
    return blocks, "tool_use"


# -- transcript → Antigravity message ------------------------------------------------------------


def render_delta(messages: list[dict[str, Any]], action_names: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """Canonical user messages not yet sent → (message text, inline media list)."""
    lines: list[str] = []
    media: list[dict[str, Any]] = []

    def attach(block: dict[str, Any], description: str) -> None:
        src = block.get("source") or {}
        if not src.get("data"):
            return
        media.append({"mimeType": src.get("media_type", "image/png"), "inlineData": src["data"],
                      "description": description, "displayName": f"{description.replace(' ', '-')}.png"})
        lines.append(f"[{description} attached as image {len(media)}]")

    for msg in messages:
        if msg.get("role") != "user":
            continue  # assistant turns already live in the Antigravity conversation
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                lines.append(content.strip())
            continue
        for b in content or []:
            t = b.get("type")
            if t == "text" and (b.get("text") or "").strip():
                lines.append(b["text"].strip())
            elif t == "image":
                attach(b, "screenshot")
            elif t == "tool_result":
                name = action_names.get(b.get("tool_use_id", ""), "action")
                texts: list[str] = []
                images: list[dict[str, Any]] = []
                inner = b.get("content")
                if isinstance(inner, str):
                    texts.append(inner)
                else:
                    for ib in inner or []:
                        if ib.get("type") == "text" and ib.get("text"):
                            texts.append(ib["text"])
                        elif ib.get("type") == "image":
                            images.append(ib)
                verdict = "failed" if b.get("is_error") else "done"
                detail = " ".join(x.strip() for x in texts if x.strip())
                lines.append(f"Result of `{name}`: {verdict}{' — ' + detail if detail else ''}")
                for ib in images:
                    attach(ib, f"screenshot after {name}")
    return "\n".join(lines), media


# -- conversations -----------------------------------------------------------------------------


def _usage_of(step: dict[str, Any]) -> Usage:
    mu = ((step.get("metadata") or {}).get("modelUsage")) or {}

    def n(key: str) -> int:
        try:
            return int(mu.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return Usage(input_tokens=n("inputTokens"), output_tokens=n("outputTokens"),
                 cache_read_input_tokens=n("cacheReadTokens"), cache_creation_input_tokens=n("cacheWriteTokens"))


def _step_error(step: dict[str, Any]) -> str | None:
    """Human-readable error carried by a trajectory step, if any."""
    err = step.get("error") or (step.get("errorMessage") or {}).get("error")
    if isinstance(err, dict):
        for key in ("userErrorMessage", "modelErrorMessage", "shortError", "details"):
            if err.get(key):
                return str(err[key])
        return "unspecified error"
    if step.get("status") == "CORTEX_STEP_STATUS_ERROR":
        return f"step {step.get('type', '?')} ended in error"
    return None


def _planner_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in steps if s.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
            and s.get("status") in _TERMINAL_STATUSES]


@dataclass
class Reply:
    """What one blocking exchange produced, read back from the new trajectory steps."""

    text: str  # planner responses, joined
    thinking: str  # planner thinking, joined
    usage: Usage
    stop_reason: str  # raw STOP_REASON_*, "" when the server did not say
    generator: str  # model enum the server reports as the generator, "" when absent
    steps: list[dict[str, Any]]

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @classmethod
    def of(cls, steps: list[dict[str, Any]]) -> Reply:
        texts: list[str] = []
        thinking: list[str] = []
        usage = Usage()
        stop = generator = ""
        for s in _planner_steps(steps):
            pr = s.get("plannerResponse") or {}
            if pr.get("response"):
                texts.append(str(pr["response"]))
            if pr.get("thinking"):
                thinking.append(str(pr["thinking"]))
            stop = pr.get("stopReason") or stop
            usage.add(_usage_of(s))
            generator = (s.get("metadata") or {}).get("generatorModel") or generator
        return cls(text="\n".join(texts), thinking="\n".join(thinking), usage=usage, stop_reason=stop,
                   generator=generator, steps=steps)

    def merged(self, other: Reply) -> Reply:
        usage = Usage()
        usage.add(self.usage)
        usage.add(other.usage)
        return Reply(text="\n".join(t for t in (self.text, other.text) if t),
                     thinking="\n".join(t for t in (self.thinking, other.thinking) if t), usage=usage,
                     stop_reason=other.stop_reason or self.stop_reason, generator=other.generator or self.generator,
                     steps=[*self.steps, *other.steps])


_BUSY_HINTS = ("has not processed the previous input", "already canceling", "executor is busy")


def _is_busy(e: ModelError) -> bool:
    """Transient refusal while the server is still finishing (or cancelling) the previous turn."""
    msg = str(e)
    return "(500)" in msg and any(h in msg for h in _BUSY_HINTS)


class Conversation:
    """One Antigravity conversation (a *cascade*), driven through blocking user messages.

    It is started with a **tool-less custom agent**: the given system-prompt sections are the whole
    system prompt, no tools are registered and command execution is off, so the model can only
    answer — it can never run commands or touch files on the machine hosting the Language Server.
    The transcript is stateful on the server: `send()` delivers one user message (text plus inline
    images), waits for the model turn to finish and returns what the new trajectory steps carried.
    """

    BUSY_RETRIES = 8  # SendUserCascadeMessage attempts while the server reports the executor busy
    BUSY_RETRY_DELAY = 0.5  # seconds, multiplied by the attempt number (≈18 s in total)

    def __init__(self, ls: LanguageServer, model: AntigravityModel, *, client_session_id: str | None = None) -> None:
        self.ls = ls
        self.model = model
        self.id: str | None = None
        self.in_flight = False
        self._seen_steps = 0
        self._client_session_id = client_session_id or uuid.uuid4().hex

    async def start(self, sections: list[tuple[str, str]], *, title: str, tags: tuple[str, ...] = (CONVERSATION_TAG,)
                    ) -> str:
        spec = {
            "customAgent": {
                "systemPromptSections": [{"title": t, "content": c} for t, c in sections],
                "toolNames": [],
                "excludeDefaultComponents": True,
            },
            "commandExecutionPolicy": "off",
            "cascadeConfig": {"plannerConfig": {"planModel": self.model.enum,
                                                "requestedModel": {"model": self.model.enum}}},
        }
        resp = await self.ls.call("StartCascade", {
            "source": "CORTEX_TRAJECTORY_SOURCE_SDK", "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
            "customAgentSpec": spec, "tags": list(tags),
        }, timeout=90.0)
        cid = resp.get("cascadeId")
        if not cid:
            raise ModelError(f"Antigravity StartCascade returned no conversation id: {resp}")
        self.id = cid
        try:
            await self.ls.call("UpdateConversationAnnotations", {
                "cascadeIds": [cid], "annotations": {"title": title}, "mergeAnnotations": True}, timeout=30.0)
        except ModelError as e:  # cosmetic
            log.debug("could not title Antigravity conversation %s: %s", cid, e)
        steps, _ = await self.ls.trajectory(cid)
        self._seen_steps = len(steps)
        log.info("antigravity: conversation %s on %s (%s)", cid, self.model.label, self.model.enum)
        return cid

    async def send(self, text: str, media: list[dict[str, Any]] | None = None, *,
                   model: AntigravityModel | None = None) -> Reply:
        """Deliver one user message and return the model's reply. `model` switches the planner for this turn."""
        if self.id is None:
            raise ModelError("Antigravity conversation has not been started")
        model = model or self.model
        body: dict[str, Any] = {
            "metadata": {"ideName": "computeruse", "ideVersion": "0.1", "extensionName": "computeruse",
                         "extensionVersion": "0.1", "locale": "en-US", "productName": "sdk",
                         "apiKey": "sdk-go-key", "sessionId": self._client_session_id},
            "cascadeId": self.id,
            "items": [{"text": text}],
            "cascadeConfig": {"plannerConfig": {"planModel": model.enum}},
            "blocking": True,
            "messageOrigin": "AGENT_MESSAGE_ORIGIN_SDK_EXECUTABLE",
        }
        if media:
            body["media"] = media
        self.in_flight = True
        try:
            for attempt in range(1, self.BUSY_RETRIES + 1):
                try:
                    await self.ls.call("SendUserCascadeMessage", body)
                    break
                except ModelError as e:
                    if attempt == self.BUSY_RETRIES or not _is_busy(e):
                        raise
                    # The server cancels asynchronously: right after a cancel (or a blocking send that returned
                    # early) it refuses new input for a moment. Wait it out rather than failing the turn.
                    delay = self.BUSY_RETRY_DELAY * attempt
                    log.info("antigravity: conversation %s is still busy (%s); retrying in %.1fs", self.id, e, delay)
                    await asyncio.sleep(delay)
        finally:
            self.in_flight = False
        steps, status = await self.ls.trajectory(self.id)
        new = steps[self._seen_steps:]
        self._seen_steps = len(steps)
        for s in new:
            err = _step_error(s)
            if err:
                raise ModelError(f"Antigravity ({model.label}): {err}")
        if status == "CASCADE_RUN_STATUS_RUNNING":
            log.warning("antigravity: conversation %s still running after a blocking send", self.id)
        return Reply.of(new)

    async def cancel(self) -> None:
        """Stop a generation that is still running (no-op when nothing is in flight)."""
        if self.id and self.in_flight:
            try:
                await self.ls.call("CancelCascadeInvocation", {"cascadeId": self.id}, timeout=20.0)
            except ModelError as e:
                if "already canceling" not in str(e):
                    raise
                log.debug("antigravity: conversation %s was already canceling", self.id)

    async def archive(self) -> None:
        """Hide the conversation from the Antigravity UI (its data is kept)."""
        if self.id:
            await self.ls.call("UpdateConversationAnnotations", {
                "cascadeIds": [self.id], "annotations": {"archived": True}, "mergeAnnotations": True}, timeout=20.0)


def image_media(data_b64: str, description: str, *, media_type: str = "image/png") -> dict[str, Any]:
    """Inline-image entry for `Conversation.send(media=...)`."""
    return {"mimeType": media_type, "inlineData": data_b64, "description": description,
            "displayName": f"{description.replace(' ', '-')}.{'jpg' if 'jpeg' in media_type else 'png'}"}


# -- computer-use client ----------------------------------------------------------------------------


class AntigravityModelClient:
    """Drives one Antigravity conversation per session through the Language Server's JSON RPCs."""

    provider = PROVIDER

    def __init__(self, model_ref: str, *, endpoint: AntigravityEndpoint, archive: bool = True,
                 timeout: float = 600.0, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.model_ref = model_ref.split(":", 1)[1] if model_ref.startswith(f"{PROVIDER}:") else model_ref
        self.name = f"{PROVIDER}:{self.model_ref}"
        self.model: AntigravityModel | None = None  # resolved against the live catalogue on first use
        self.endpoint = endpoint
        self.archive = archive
        self.ls = LanguageServer(endpoint, timeout=timeout, transport=transport)
        self.conversation: Conversation | None = None
        self._clock = clock
        self._sent = 0  # canonical messages already delivered to the conversation
        self._action_names: dict[str, str] = {}  # tool_use id → action name, for result labelling
        self._lock = asyncio.Lock()

    @property
    def conversation_id(self) -> str | None:
        return self.conversation.id if self.conversation else None

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model.id if self.model else self.model_ref,
                "model_enum": self.model.enum if self.model else None, "label": self.model.label if self.model else None,
                "conversation_id": self.conversation_id, **self.endpoint.public()}

    # -- lifecycle ---------------------------------------------------------------------

    async def _resolve_model(self) -> AntigravityModel:
        if self.model is None:
            models = await self.ls.models()
            found = find_model(models, self.model_ref)
            if found is None or found.disabled:
                offered = ", ".join(f"{m.id} ({m.label})" for m in models if not m.disabled)
                raise ModelError(f"Antigravity does not offer model {self.model_ref!r}; available: {offered}")
            if not found.supports_images:
                raise ModelError(f"Antigravity model {found.label!r} does not accept images, so it cannot see the screen")
            if found.quota_exhausted:
                raise ModelError(f"Antigravity quota for {found.label!r} is exhausted"
                                 f"{' until ' + found.quota_reset if found.quota_reset else ''}")
            self.model = found
        return self.model

    async def _start(self, system: str, display: tuple[int, int], title: str) -> None:
        model = await self._resolve_model()
        self.conversation = Conversation(self.ls, model)
        await self.conversation.start([("COMPUTER_USE", system), ("RESPONSE_FORMAT", response_format_section(display))],
                                      title=title)

    async def aclose(self) -> None:
        """Stop any generation still running, hide the conversation from the Antigravity UI, drop the connection."""
        try:
            if self.conversation is not None:
                try:
                    await self.conversation.cancel()
                except ModelError as e:
                    log.warning("antigravity: cancel failed for %s: %s", self.conversation_id, e)
                if self.archive:
                    try:
                        await self.conversation.archive()
                    except ModelError as e:
                        log.warning("antigravity: archive failed for %s: %s", self.conversation_id, e)
        finally:
            await self.ls.aclose()

    # -- one model turn ----------------------------------------------------------------

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        async with self._lock:
            t0 = self._clock()
            if self.conversation is None:
                task = next((b.get("text", "") for m in messages if m.get("role") == "user"
                             for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                             if b.get("type") == "text"), "")
                await self._start(system, display_from_tools(tools), f"computeruse · {task.strip()[:70] or 'session'}")
            assert self.conversation is not None
            text, media = render_delta(messages[self._sent:], self._action_names)
            if not text and not media:
                text = "(continue)"
            self._sent = len(messages)
            reply = await self.conversation.send(text, media)
            if reply.empty:
                # The server went idle without a model reply (empty response, thinking only): ask once more.
                log.warning("antigravity: empty reply from %s; nudging once", self.model.label if self.model else "?")
                reply = reply.merged(await self.conversation.send(
                    "Your reply was empty. Reply with one JSON action object, or with plain text if the task is "
                    "finished.", []))
            return self._turn(reply, t0)

    def _turn(self, reply: Reply, t0: float) -> ModelTurn:
        assert self.model is not None
        if reply.empty:
            raise ModelError(f"Antigravity ({self.model.label}) returned no reply"
                             f"{' (stop reason ' + reply.stop_reason + ')' if reply.stop_reason else ''}")
        content, stop_reason = parse_reply(reply.text, reply.thinking)
        if reply.stop_reason == "STOP_REASON_MAX_TOKENS" and stop_reason == "end_turn":
            stop_reason = "max_tokens"
        for b in content:
            if b.get("type") == "tool_use":
                self._action_names[b["id"]] = str(b["input"].get("action", "action"))
        generator = reply.generator
        model_name = self.model.id if not generator or generator == self.model.enum else generator
        return ModelTurn(content=content, stop_reason=stop_reason, usage=reply.usage,
                         latency_ms=(self._clock() - t0) * 1000, model=model_name)
