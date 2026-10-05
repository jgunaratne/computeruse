"""Gemini as a computer-use model.

General Gemini models have no Anthropic-style built-in computer tool, so the
`computer` tool is declared as a function (`computer_tool_schema`) and the
canonical transcript is translated part by part:

    user text / image          → user parts: text / inlineData
    assistant text             → model part: text (+ thoughtSignature)
    assistant tool_use         → model part: functionCall (+ thoughtSignature)
    user tool_result           → user parts: functionResponse, then the screenshot
                                  as an inlineData part

Gemini 3 requires the `thoughtSignature` returned on a functionCall part to be
echoed on the next request; it is kept on the canonical block as
`thought_signature` (stripped by every Anthropic-shaped provider).

Two routes share the client — the Gemini Developer API (`GEMINI_API_KEY`) and
Vertex AI (`publishers/google/models/*` with ADC) — because the request and
response bodies are identical.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from typing import Any

import httpx

from computeruse.agent.model import ModelError, ModelTurn
from computeruse.agent.prompts import computer_tool_schema
from computeruse.agent.vertex import (
    AdcTokenSource,
    TokenSource,
    display_from_tools,
    error_message,
    vertex_host,
)
from computeruse.telemetry.events import Usage

log = logging.getLogger("computeruse.gemini")

DEVELOPER_API = "https://generativelanguage.googleapis.com/v1beta"
TOOL_NAME = "computer"
RETRYABLE = {408, 429, 500, 502, 503, 504}
_THINKING_ERROR = re.compile(r"thinking", re.I)


# -- transcript shaping ----------------------------------------------------------


def _inline(block: dict[str, Any]) -> dict[str, Any]:
    src = block.get("source") or {}
    return {"inlineData": {"mimeType": src.get("media_type", "image/png"), "data": src.get("data", "")}}


def to_gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical messages → Gemini `contents` (consecutive same-role turns are merged)."""
    call_names: dict[str, str] = {}
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        content = msg.get("content")
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        else:
            responses: list[dict[str, Any]] = []
            images: list[dict[str, Any]] = []
            for b in content or []:
                t = b.get("type")
                if t == "text":
                    part: dict[str, Any] = {"text": b.get("text") or ""}
                    if role == "model" and b.get("thought_signature"):
                        part["thoughtSignature"] = b["thought_signature"]
                    parts.append(part)
                elif t == "image":
                    parts.append(_inline(b))
                elif t == "tool_use":
                    name = b.get("name") or TOOL_NAME
                    call_names[b.get("id", "")] = name
                    part = {"functionCall": {"name": name, "args": b.get("input") or {}}}
                    if b.get("thought_signature"):
                        part["thoughtSignature"] = b["thought_signature"]
                    parts.append(part)
                elif t == "tool_result":
                    texts: list[str] = []
                    n_before = len(images)
                    inner = b.get("content")
                    if isinstance(inner, str):
                        texts.append(inner)
                    else:
                        for ib in inner or []:
                            if ib.get("type") == "text":
                                texts.append(ib.get("text") or "")
                            elif ib.get("type") == "image":
                                images.append(_inline(ib))
                    response: dict[str, Any] = {"status": "error" if b.get("is_error") else "ok"}
                    if texts:
                        response["output"] = "\n".join(texts)
                    if len(images) > n_before:
                        response["screenshot"] = f"attached below as image {len(images)}"
                    responses.append({"functionResponse": {"name": call_names.get(b.get("tool_use_id", ""),
                                                                                  TOOL_NAME),
                                                           "response": response}})
                # thinking / redacted_thinking blocks are provider-private: dropped
            # All function responses first, then their screenshots, so the response count
            # matches the previous turn's calls regardless of interleaving.
            parts = responses + images + parts
        if not parts:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return contents


def from_gemini_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini candidate parts → canonical content blocks."""
    blocks: list[dict[str, Any]] = []
    for p in parts:
        if p.get("thought"):
            continue
        sig = p.get("thoughtSignature")
        if "functionCall" in p:
            fc = p["functionCall"] or {}
            block: dict[str, Any] = {"type": "tool_use", "id": fc.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                                     "name": fc.get("name") or TOOL_NAME, "input": fc.get("args") or {}}
            if sig:
                block["thought_signature"] = sig
            blocks.append(block)
        elif "text" in p:
            block = {"type": "text", "text": p.get("text") or ""}
            if sig:
                block["thought_signature"] = sig
            blocks.append(block)
    return blocks


# -- client ------------------------------------------------------------------------


class GeminiModelClient:
    """Gemini via the Developer API (API key) or Vertex AI (ADC), same wire format."""

    provider = "gemini"

    def __init__(self, model: str, *, api_key: str | None = None, project: str | None = None,
                 location: str | None = None, max_retries: int = 4, timeout: float = 180.0,
                 thinking_level: str | None = None, token_source: TokenSource | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not api_key and not project:
            raise ValueError("GeminiModelClient needs an API key (GEMINI_API_KEY) or a GCP project for Vertex")
        self.name = model
        self.model = model
        self.api_key = api_key
        self.project = project
        self.location = location or "global"
        self.route = "api_key" if api_key else "vertex"
        self.max_retries = max_retries
        self.thinking_level = thinking_level
        self._token = token_source or (None if api_key else AdcTokenSource())
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0), transport=transport)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "route": self.route,
                "project": self.project, "location": self.location if self.route == "vertex" else None}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self) -> str:
        if self.route == "api_key":
            return f"{DEVELOPER_API}/models/{self.model}:generateContent"
        return (f"https://{vertex_host(self.location)}/v1/projects/{self.project}/locations/{self.location}"
                f"/publishers/google/models/{self.model}:generateContent")

    async def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.route == "api_key":
            headers["x-goog-api-key"] = self.api_key or ""
        else:
            assert self._token is not None
            headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    def _body(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
              max_tokens: int) -> dict[str, Any]:
        fn = computer_tool_schema(display_from_tools(tools))
        declaration = {"name": fn["name"], "description": fn["description"], "parameters": fn["input_schema"]}
        generation: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if self.thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_gemini_contents(messages),
            "tools": [{"functionDeclarations": [declaration]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": generation,
        }

    async def create(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> ModelTurn:
        t0 = time.perf_counter()
        attempt = 0
        while True:
            body = self._body(system, messages, tools, max_tokens)
            try:
                resp = await self._http.post(self._url(), headers=await self._headers(), json=body)
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise ModelError(f"Gemini API unreachable after {attempt - 1} retries: {e}") from e
                await self._backoff(attempt)
                continue
            if resp.status_code == 200:
                return self._parse(resp.json(), t0, attempt)
            message = error_message(resp)
            if resp.status_code == 400 and self.thinking_level and _THINKING_ERROR.search(message):
                log.warning("%s: thinking config rejected (%s); disabling", self.model, message[:120])
                self.thinking_level = None
                continue
            if resp.status_code in RETRYABLE:
                attempt += 1
                if attempt > self.max_retries:
                    raise ModelError(f"Gemini API {resp.status_code} after {attempt - 1} retries: {message}")
                log.warning("%s: %s (attempt %d): %s", self.model, resp.status_code, attempt, message[:120])
                await self._backoff(attempt)
                continue
            if resp.status_code in (401, 403):
                hint = ("check GEMINI_API_KEY" if self.route == "api_key"
                        else f"check ADC and access to project {self.project}")
                raise ModelError(f"Gemini API denied the request ({resp.status_code}): {message}; {hint}")
            if resp.status_code == 404:
                raise ModelError(f"Gemini model {self.model!r} not found via {self.route}: {message}")
            raise ModelError(f"Gemini API error {resp.status_code}: {message}")

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(30.0, (2 ** attempt) * 0.5 + random.random()))

    def _parse(self, data: dict[str, Any], t0: float, attempt: int) -> ModelTurn:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ModelError(f"Gemini returned no candidates (blockReason={feedback.get('blockReason')})")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        content = from_gemini_parts(parts)
        finish = cand.get("finishReason") or "STOP"
        if not content and finish not in ("STOP", "MAX_TOKENS"):
            raise ModelError(f"Gemini stopped with finishReason={finish} and no content")
        has_tool = any(b["type"] == "tool_use" for b in content)
        stop_reason = "tool_use" if has_tool else ("max_tokens" if finish == "MAX_TOKENS" else "end_turn")
        um = data.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=int(um.get("promptTokenCount") or 0),
            output_tokens=int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0),
            cache_read_input_tokens=int(um.get("cachedContentTokenCount") or 0),
        )
        return ModelTurn(content=content, stop_reason=stop_reason, usage=usage,
                         latency_ms=(time.perf_counter() - t0) * 1000, retries=attempt,
                         model=data.get("modelVersion") or self.model)
