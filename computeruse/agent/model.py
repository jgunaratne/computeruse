"""Model clients.

Every client speaks the same *canonical transcript*: Anthropic-style messages
whose assistant content is `text` / `thinking` / `tool_use(name="computer",
input={"action": ...})` blocks and whose user content carries `tool_result`
blocks with text and base64 PNG `image` parts. Providers translate to and from
that shape inside `create()`, so the loop, the event store, replay and the UI
never see provider differences.

* `AnthropicModelClient` — Claude via the public Messages API (needs an API key).
* `VertexAnthropicModelClient` (vertex.py) — Claude on Vertex AI with ADC; the
  route for environments without an Anthropic key.
* `GeminiModelClient` (gemini.py) — Gemini via the Developer API key or Vertex.
* `ScriptedModelClient` — plays a fixed plan (no credentials; evals/tests/demos).
* `ReplayModelClient` — re-emits the model turns recorded in a previous session.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from computeruse.telemetry.events import Usage


@dataclass
class ModelTurn:
    content: list[dict[str, Any]]  # canonical content blocks (text / thinking / tool_use)
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    retries: int = 0
    model: str = ""

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def texts(self) -> list[str]:
        return [b["text"] for b in self.content if b.get("type") == "text" and b.get("text")]


class ModelError(RuntimeError):
    """Model call failed after retries (or non-retryable)."""


class ModelClient(Protocol):
    name: str

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn: ...


# Fields the Anthropic Messages API accepts back per block type. Anything else on
# a canonical block is provider bookkeeping (e.g. Gemini `thought_signature`).
_ANTHROPIC_BLOCK_FIELDS = {
    "text": {"type", "text", "cache_control"},
    "tool_use": {"type", "id", "name", "input"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "image": {"type", "source", "cache_control"},
    "tool_result": {"type", "tool_use_id", "content", "is_error", "cache_control"},
}


def sanitize_for_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip provider-private keys from canonical blocks before an Anthropic-shaped request."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        blocks = []
        for b in content:
            allowed = _ANTHROPIC_BLOCK_FIELDS.get(b.get("type"))
            blocks.append({k: v for k, v in b.items() if k in allowed} if allowed else b)
        out.append({**msg, "content": blocks})
    return out


# ---------------------------------------------------------------------------


class AnthropicModelClient:
    """Claude via the Messages API with the `computer_*` tool (beta)."""

    def __init__(self, model: str, api_key: str | None = None, beta_flag: str = "computer-use-2025-01-24",
                 max_retries: int = 4, timeout: float = 180.0) -> None:
        import anthropic

        self.name = model
        self.model = model
        self.beta_flag = beta_flag
        self.max_retries = max_retries
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        anthropic = self._anthropic
        attempt = 0
        t0 = time.perf_counter()
        while True:
            try:
                resp = await self._client.beta.messages.create(
                    model=self.model, max_tokens=max_tokens, system=system,
                    messages=sanitize_for_anthropic(messages), tools=tools, betas=[self.beta_flag],
                )
                content = [b.model_dump(exclude_none=True) for b in resp.content]
                content = [{k: v for k, v in b.items() if k in _ANTHROPIC_BLOCK_FIELDS.get(b.get("type"), b)}
                           for b in content]  # keep only the fields the API accepts back
                u = resp.usage
                usage = Usage(
                    input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                    cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                    cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                )
                return ModelTurn(content=content, stop_reason=resp.stop_reason or "end_turn", usage=usage,
                                 latency_ms=(time.perf_counter() - t0) * 1000, retries=attempt, model=resp.model)
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                    anthropic.APITimeoutError) as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise ModelError(f"{type(e).__name__} after {attempt - 1} retries: {e}") from e
                await asyncio.sleep(min(30.0, (2 ** attempt) * 0.5 + random.random()))
            except anthropic.APIStatusError as e:
                if e.status_code in (529, 503) and attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(min(30.0, (2 ** attempt) * 0.5 + random.random()))
                    continue
                raise ModelError(f"API error {e.status_code}: {e.message}") from e


# ---------------------------------------------------------------------------


@dataclass
class ScriptStep:
    """One scripted model turn: optional reasoning text + zero or more actions.

    Actions may use `target` (a symbolic UI element name resolved by the
    computer, e.g. "dock.notes") instead of pixel coordinates.
    """

    text: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


class ScriptedModelClient:
    name = "scripted"

    def __init__(self, steps: list[ScriptStep] | list[dict[str, Any]],
                 locator: Callable[[str], tuple[int, int]] | None = None,
                 latency_ms: float = 0.0, final_text: str = "Task complete.") -> None:
        self.steps = [s if isinstance(s, ScriptStep) else ScriptStep(**s) for s in steps]
        self.locator = locator
        self.latency_ms = latency_ms
        self.final_text = final_text
        self.cursor = 0

    def _resolve(self, action: dict[str, Any]) -> dict[str, Any]:
        out = dict(action)
        for key, src in (("coordinate", "target"), ("start_coordinate", "start_target")):
            if src in out:
                name = out.pop(src)
                if not self.locator:
                    raise ModelError(f"script uses target {name!r} but no locator is configured")
                try:
                    out[key] = list(self.locator(name))
                except Exception as e:
                    raise ModelError(f"script target {name!r} could not be resolved: {e}") from e
        return out

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        t0 = time.perf_counter()
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.cursor >= len(self.steps):
            content = [{"type": "text", "text": self.final_text}]
            return ModelTurn(content=content, stop_reason="end_turn", latency_ms=(time.perf_counter() - t0) * 1000,
                             model=self.name)
        step = self.steps[self.cursor]
        self.cursor += 1
        content: list[dict[str, Any]] = []
        if step.text:
            content.append({"type": "text", "text": step.text})
        for action in step.actions:
            content.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}", "name": "computer",
                            "input": self._resolve(action)})
        if step.done or not step.actions:
            if not content:
                content.append({"type": "text", "text": self.final_text})
            return ModelTurn(content=content, stop_reason="end_turn", latency_ms=(time.perf_counter() - t0) * 1000,
                             model=self.name)
        return ModelTurn(content=content, stop_reason="tool_use", latency_ms=(time.perf_counter() - t0) * 1000,
                         usage=Usage(input_tokens=1500, output_tokens=80), model=self.name)


# ---------------------------------------------------------------------------


class ReplayModelClient:
    """Replays `model.called` events captured from an earlier session."""

    name = "replay"

    def __init__(self, turns: list[list[dict[str, Any]]], source_session: str) -> None:
        self.turns = turns
        self.source_session = source_session
        self.name = f"replay:{source_session[:8]}"
        self.cursor = 0

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        if self.cursor >= len(self.turns):
            return ModelTurn(content=[{"type": "text", "text": "(replay exhausted)"}], stop_reason="end_turn",
                             model=self.name)
        content = [dict(b) for b in self.turns[self.cursor]]
        self.cursor += 1
        for b in content:  # fresh ids keep tool_use/tool_result pairing valid
            if b.get("type") == "tool_use":
                b["id"] = f"toolu_{uuid.uuid4().hex[:16]}"
        has_tool = any(b.get("type") == "tool_use" for b in content)
        return ModelTurn(content=content, stop_reason="tool_use" if has_tool else "end_turn", model=self.name)
