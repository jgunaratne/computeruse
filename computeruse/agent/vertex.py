"""Claude on Vertex AI — the Claude route for environments without an Anthropic key.

Where the public Anthropic API is unreachable (or no Anthropic key exists),
Vertex AI's Anthropic publisher endpoint serves the same models with
Application Default Credentials:

    POST https://{host}/v1/projects/{project}/locations/{loc}
         /publishers/anthropic/models/{model}:rawPredict
    Authorization: Bearer <ADC token>
    {"anthropic_version": "vertex-2023-10-16", "messages": [...], ...}

Probed behaviour (2026-10):

* `claude-sonnet-4-5` serves the built-in `computer_20250124` tool only when the
  beta flag travels as the `anthropic-beta` HTTP header (the body field is
  rejected).
* `claude-opus-5-5` rejects that beta and instead offers
  `computer_toolset_20260801`: the API serves one tool per action
  (`screenshot`, `left_click`, …, `zoom`), tags each call with
  `toolset_name="computer"`, and requires every tool_result to echo it.
* Any model accepts a plain JSON-schema `computer` tool.

`VertexAnthropicModelClient` picks a *tool mode* from the model id, falls back
along builtin → toolset → custom when the API 400s on the tool definition, and
maps all three to and from the canonical transcript so nothing else in the
system knows which wire format was used.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from computeruse.agent.model import ModelError, ModelTurn, sanitize_for_anthropic
from computeruse.agent.prompts import computer_tool_schema
from computeruse.telemetry.events import Usage

log = logging.getLogger("computeruse.vertex")

ANTHROPIC_VERSION = "vertex-2023-10-16"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_LOCATIONS = ("global", "us-east5", "us-central1", "europe-west1", "europe-west4")
TOOLSET_TYPE = "computer_toolset_20260801"
TOOL_NAME = "computer"
TOOL_MODES = ("builtin", "toolset", "custom")
RETRYABLE = {408, 429, 500, 502, 503, 504, 529}

_TOOL_ERROR = re.compile(r"tools\.\d|toolset|anthropic[-_]beta|input_schema|display_width", re.I)
_CACHE_ERROR = re.compile(r"cache_control", re.I)
_THINKING_ERROR = re.compile(r"thinking|output_config|effort", re.I)
_FAMILY_RE = re.compile(r"claude-[a-z]+-(\d+)")  # any family: the major version decides the tool API

TokenSource = Callable[[], Awaitable[str]]


def vertex_host(location: str) -> str:
    return "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"


def default_tool_mode(model: str) -> str:
    """Claude 5+ builds serve the toolset API; earlier ones the dated built-in tool."""
    m = _FAMILY_RE.search(model)
    return "toolset" if m and int(m.group(1)) >= 5 else "builtin"


# -- credentials ---------------------------------------------------------------


class AdcTokenSource:
    """Bearer tokens from Application Default Credentials, refreshed off the event loop."""

    def __init__(self) -> None:
        self._creds: Any = None
        self.project: str | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> None:
        import google.auth

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "no quota project": rawPredict bills the project in the path
            self._creds, self.project = google.auth.default(scopes=[CLOUD_SCOPE])

    def _refresh(self) -> None:
        import google.auth.transport.requests

        self._creds.refresh(google.auth.transport.requests.Request())

    async def __call__(self) -> str:
        async with self._lock:
            try:
                if self._creds is None:
                    await asyncio.to_thread(self._load)
                if not self._creds.valid:
                    await asyncio.to_thread(self._refresh)
            except ModelError:
                raise
            except Exception as e:
                raise ModelError(f"Google credentials unavailable: {e}. "
                                 "Run `gcloud auth application-default login`.") from e
            return self._creds.token


def detect_adc(explicit_project: str | None = None) -> tuple[bool, str | None, str]:
    """Synchronously check for ADC. Returns (usable, project, human reason); never raises."""
    project = (explicit_project or os.environ.get("GOOGLE_CLOUD_PROJECT")
               or os.environ.get("GOOGLE_CLOUD_PROJECT_ID") or os.environ.get("GCLOUD_PROJECT") or None)
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        return False, project, "google-auth is not installed"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _, adc_project = google.auth.default(scopes=[CLOUD_SCOPE])
    except DefaultCredentialsError as e:
        return False, project, f"no Application Default Credentials ({str(e).splitlines()[0]})"
    except Exception as e:  # pragma: no cover - environment specific
        return False, project, f"ADC error: {e}"
    if not project and adc_project and not str(adc_project).startswith("cloudtop-"):
        project = str(adc_project)  # cloudtop ADC projects cannot host the AI APIs
    if not project:
        return False, None, "ADC found but no GCP project; set GOOGLE_CLOUD_PROJECT"
    return True, project, f"ADC credentials, project {project}"


# -- transcript shaping ----------------------------------------------------------


def to_toolset_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical `computer` tool_use/tool_result blocks → toolset member calls.

    `{"name": "computer", "input": {"action": "left_click", "coordinate": [1, 2]}}`
    becomes `{"name": "left_click", "input": {"coordinate": [1, 2]}, "toolset_name": "computer"}`
    and the paired tool_result gains `toolset_name` (the API insists on it).
    """
    member_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        blocks: list[dict[str, Any]] = []
        for b in content:
            t = b.get("type")
            inp = b.get("input")
            if (msg.get("role") == "assistant" and t == "tool_use" and b.get("name") == TOOL_NAME
                    and isinstance(inp, dict) and "action" in inp):
                rest = {k: v for k, v in inp.items() if k != "action"}
                member_ids.add(b["id"])
                blocks.append({"type": "tool_use", "id": b["id"], "name": inp["action"], "input": rest,
                               "toolset_name": TOOL_NAME})
            elif t == "tool_result" and b.get("tool_use_id") in member_ids:
                blocks.append({**b, "toolset_name": TOOL_NAME})
            else:
                blocks.append(b)
        out.append({**msg, "content": blocks})
    return out


def from_toolset_block(block: dict[str, Any]) -> dict[str, Any]:
    """Toolset member call → canonical `computer` tool_use."""
    if block.get("type") == "tool_use" and block.get("toolset_name") == TOOL_NAME:
        inp = block.get("input") or {}
        return {"type": "tool_use", "id": block["id"], "name": TOOL_NAME,
                "input": {"action": block.get("name"), **inp}}
    return block


def display_from_tools(tools: list[dict[str, Any]], default: tuple[int, int] = (1024, 768)) -> tuple[int, int]:
    for t in tools:
        if "display_width_px" in t and "display_height_px" in t:
            return int(t["display_width_px"]), int(t["display_height_px"])
    return default


def error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:1000]
    return str(data)[:500]


# -- client ------------------------------------------------------------------------


class VertexAnthropicModelClient:
    """Claude via `publishers/anthropic/models/*:rawPredict` on Vertex AI.

    Requests rotate over `(project, location)` targets: a 404 marks a target
    dead (the model is not served there), so a model that only one of the
    configured projects may use still works. Targets are ordered
    location-major — every project's `global` endpoint before any regional
    fallback — because `global` serves everything a project has access to.
    """

    provider = "vertex"

    def __init__(self, model: str, *, project: str | None = None, projects: list[str] | None = None,
                 location: str | None = None, tool_mode: str = "auto", beta_flag: str = "computer-use-2025-01-24",
                 max_retries: int = 4, timeout: float = 180.0, thinking_effort: str | None = None,
                 prompt_caching: bool = True, token_source: TokenSource | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if tool_mode != "auto" and tool_mode not in TOOL_MODES:
            raise ValueError(f"tool_mode must be one of {('auto', *TOOL_MODES)}, got {tool_mode!r}")
        self.projects = list(dict.fromkeys(p for p in [*(projects or []), project] if p))
        if not self.projects:
            raise ValueError("VertexAnthropicModelClient needs at least one GCP project")
        self.name = model
        self.model = model
        self.project = self.projects[0]
        locations = list(DEFAULT_LOCATIONS)
        if location:
            locations = [location, *[loc for loc in locations if loc != location]]
        self.locations = locations
        self.targets: list[tuple[str, str]] = [(p, loc) for loc in locations for p in self.projects]
        self.tool_mode = default_tool_mode(model) if tool_mode == "auto" else tool_mode
        self.beta_flag = beta_flag
        self.max_retries = max_retries
        self.thinking_effort = thinking_effort
        self.prompt_caching = prompt_caching
        self._token = token_source or AdcTokenSource()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0), transport=transport)
        self._dead: set[tuple[str, str]] = set()
        self._tried_modes: set[str] = set()

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "project": self.project, "projects": self.projects,
                "locations": self.locations, "tool_mode": self.tool_mode}

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- request shaping ---------------------------------------------------------

    def _tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.tool_mode == "builtin":
            return tools
        if self.tool_mode == "toolset":
            return [{"type": TOOLSET_TYPE}]
        return [computer_tool_schema(display_from_tools(tools))]

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        msgs = sanitize_for_anthropic(messages)
        return to_toolset_messages(msgs) if self.tool_mode == "toolset" else msgs

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if self.tool_mode == "builtin" and self.beta_flag:
            headers["anthropic-beta"] = self.beta_flag
        return headers

    def _body(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
              max_tokens: int) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if self.prompt_caching:
            system_block["cache_control"] = {"type": "ephemeral"}
        body: dict[str, Any] = {
            "anthropic_version": ANTHROPIC_VERSION, "max_tokens": max_tokens, "system": [system_block],
            "messages": self._messages(messages), "tools": self._tools(tools),
        }
        if self.thinking_effort:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": self.thinking_effort}
        return body

    def _url(self, target: tuple[str, str]) -> str:
        project, location = target
        return (f"https://{vertex_host(location)}/v1/projects/{project}/locations/{location}"
                f"/publishers/anthropic/models/{self.model}:rawPredict")

    # -- adaptation --------------------------------------------------------------

    def _next_tool_mode(self) -> str | None:
        preferred = default_tool_mode(self.model)
        order = ["toolset", "builtin", "custom"] if preferred == "toolset" else ["builtin", "toolset", "custom"]
        self._tried_modes.add(self.tool_mode)
        return next((m for m in order if m not in self._tried_modes), None)

    def _adapt(self, message: str) -> bool:
        """React to a 400: drop an optional feature or switch tool mode. True = retry."""
        if self.prompt_caching and _CACHE_ERROR.search(message):
            log.warning("%s: prompt caching rejected (%s); continuing without", self.model, message[:120])
            self.prompt_caching = False
            return True
        if _TOOL_ERROR.search(message):
            nxt = self._next_tool_mode()
            if nxt:
                log.warning("%s: tool mode %r rejected (%s); switching to %r", self.model, self.tool_mode,
                            message[:160], nxt)
                self.tool_mode = nxt
                return True
            return False
        if self.thinking_effort and _THINKING_ERROR.search(message):
            log.warning("%s: thinking config rejected (%s); disabling", self.model, message[:120])
            self.thinking_effort = None
            return True
        return False

    # -- call ----------------------------------------------------------------------

    def _live_targets(self) -> list[tuple[str, str]]:
        return [t for t in self.targets if t not in self._dead]

    def _not_served_error(self) -> ModelError:
        projects = self.projects[0] if len(self.projects) == 1 else f"any of {self.projects}"
        return ModelError(f"{self.model} is not served in any of {list(self.locations)} for project {projects} "
                          f"(or the project lacks access)")

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        t0 = time.perf_counter()
        attempt = 0
        adaptations = 0
        rotation = 0
        while True:
            live = self._live_targets()
            if not live:
                raise self._not_served_error()
            target = live[rotation % len(live)]
            project, location = target
            token = await self._token()
            body = self._body(system, messages, tools, max_tokens)
            try:
                resp = await self._http.post(self._url(target), headers=self._headers(token), json=body)
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise ModelError(f"Vertex AI unreachable after {attempt - 1} retries: {e}") from e
                await self._backoff(attempt)
                rotation += 1
                continue

            if resp.status_code == 200:
                return self._parse(resp.json(), t0, attempt)

            message = error_message(resp)
            if resp.status_code == 404:
                log.info("%s not found in %s/%s: %s", self.model, project, location, message[:120])
                self._dead.add(target)
                continue
            if resp.status_code == 400 and adaptations < 4 and self._adapt(message):
                adaptations += 1
                continue
            if resp.status_code in RETRYABLE:
                attempt += 1
                if attempt > self.max_retries:
                    raise ModelError(f"Vertex AI {resp.status_code} after {attempt - 1} retries: {message}")
                log.warning("%s: %s in %s/%s (attempt %d): %s", self.model, resp.status_code, project, location,
                            attempt, message[:120])
                await self._backoff(attempt)
                rotation += 1
                continue
            if resp.status_code == 403:  # project-scoped: permission or publisher data-sharing opt-in
                self._dead.update(t for t in self.targets if t[0] == project)
                if self._live_targets():
                    log.warning("%s: project %s denied access (%s); trying the next project", self.model, project,
                                message[:120])
                    continue
            if resp.status_code in (401, 403):
                raise ModelError(
                    f"Vertex AI denied the request ({resp.status_code}): {message}. Check "
                    f"`gcloud auth application-default login` and that project {project} may use "
                    f"publisher model {self.model}.")
            raise ModelError(f"Vertex AI error {resp.status_code}: {message}")

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(30.0, (2 ** attempt) * 0.5 + random.random()))

    def _parse(self, data: dict[str, Any], t0: float, attempt: int) -> ModelTurn:
        raw = [from_toolset_block(b) for b in data.get("content") or []]
        content = sanitize_for_anthropic([{"role": "assistant", "content": raw}])[0]["content"]
        u = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )
        return ModelTurn(content=content, stop_reason=data.get("stop_reason") or "end_turn", usage=usage,
                         latency_ms=(time.perf_counter() - t0) * 1000, retries=attempt,
                         model=data.get("model") or self.model)
