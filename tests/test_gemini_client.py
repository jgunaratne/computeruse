"""GeminiModelClient: canonical transcript ⇄ Gemini `contents`, both routes, response parsing, retries."""

from __future__ import annotations

import httpx
import pytest
from conftest import HttpRecorder, fake_token, no_backoff

from computeruse.agent.gemini import GeminiModelClient, from_gemini_parts, to_gemini_contents
from computeruse.agent.model import ModelError
from computeruse.agent.prompts import computer_tool

TOOLS = [computer_tool("computer_20250124", (1280, 800))]
PNG = {"type": "base64", "media_type": "image/png", "data": "AAA="}
MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "Open Notes"}, {"type": "image", "source": PNG}]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Clicking.", "thought_signature": "sig-text"},
        {"type": "tool_use", "id": "call_1", "name": "computer",
         "input": {"action": "left_click", "coordinate": [1, 2]}, "thought_signature": "sig-1"},
        {"type": "tool_use", "id": "call_2", "name": "computer", "input": {"action": "screenshot"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": [
            {"type": "text", "text": "clicked"}, {"type": "image", "source": {**PNG, "data": "BBB="}}]},
        {"type": "tool_result", "tool_use_id": "call_2", "is_error": True, "content": "capture failed"},
    ]},
    {"role": "user", "content": [{"type": "text", "text": "operator nudge"}]},  # merged into the previous turn
]


def gemini(parts, finish="STOP", usage=None):
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30,
                                   "cachedContentTokenCount": 40},
        "modelVersion": "gemini-test-001",
    })


def err(status, message):
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": "X"}})


def make_client(responses, **kw):
    rec = HttpRecorder(responses)
    kw.setdefault("api_key", "key")
    client = GeminiModelClient(kw.pop("model", "gemini-x"), token_source=fake_token,
                               transport=httpx.MockTransport(rec), **kw)
    client._backoff = no_backoff  # type: ignore[method-assign]
    return rec, client


async def call(client, messages=None):
    return await client.create(system="sys", messages=messages or MESSAGES, tools=TOOLS, max_tokens=64)


# -- translation -----------------------------------------------------------------


def test_contents_translation_orders_responses_before_screenshots_and_merges_roles():
    contents = to_gemini_contents(MESSAGES)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"] == [{"text": "Open Notes"}, {"inlineData": {"mimeType": "image/png", "data": "AAA="}}]

    text, fc1, fc2 = contents[1]["parts"]
    assert text == {"text": "Clicking.", "thoughtSignature": "sig-text"}
    assert fc1 == {"functionCall": {"name": "computer", "args": {"action": "left_click", "coordinate": [1, 2]}},
                   "thoughtSignature": "sig-1"}
    assert fc2 == {"functionCall": {"name": "computer", "args": {"action": "screenshot"}}}

    parts = contents[2]["parts"]
    assert [next(iter(p)) for p in parts] == ["functionResponse", "functionResponse", "inlineData", "text"]
    assert parts[0]["functionResponse"] == {"name": "computer", "response": {
        "status": "ok", "output": "clicked", "screenshot": "attached below as image 1"}}
    assert parts[1]["functionResponse"]["response"] == {"status": "error", "output": "capture failed"}
    assert parts[2]["inlineData"]["data"] == "BBB="
    assert parts[3] == {"text": "operator nudge"}


def test_string_content_and_empty_turns():
    contents = to_gemini_contents([{"role": "user", "content": "hi"}, {"role": "assistant", "content": []},
                                   {"role": "user", "content": ""}])
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_from_gemini_parts_skips_thoughts_and_keeps_signatures():
    blocks = from_gemini_parts([
        {"thought": True, "text": "private reasoning"},
        {"text": "Working.", "thoughtSignature": "s0"},
        {"functionCall": {"name": "computer", "args": {"action": "key", "text": "Return"}}, "thoughtSignature": "s1"},
        {"functionCall": {"id": "fc-7", "name": "computer", "args": {"action": "screenshot"}}},
    ])
    assert blocks[0] == {"type": "text", "text": "Working.", "thought_signature": "s0"}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["id"].startswith("call_")
    assert blocks[1]["input"] == {"action": "key", "text": "Return"} and blocks[1]["thought_signature"] == "s1"
    assert blocks[2]["id"] == "fc-7" and "thought_signature" not in blocks[2]
    assert len(blocks) == 3


# -- client -------------------------------------------------------------------------


def test_client_needs_a_key_or_a_project():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiModelClient("gemini-x")


async def test_api_key_route_request_and_parse():
    rec, client = make_client([gemini([
        {"thought": True, "text": "hmm"},
        {"functionCall": {"name": "computer", "args": {"action": "screenshot"}}, "thoughtSignature": "sig"},
    ])])
    turn = await call(client)
    req = rec.requests[0]
    assert str(req.url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    assert req.headers["x-goog-api-key"] == "key" and "authorization" not in req.headers
    body = rec.body()
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    (decl,) = body["tools"][0]["functionDeclarations"]
    assert decl["name"] == "computer" and "1280x800" in decl["description"]
    assert "zoom" in decl["parameters"]["properties"]["action"]["enum"]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    assert body["generationConfig"] == {"maxOutputTokens": 64}
    assert len(body["contents"]) == 3

    assert turn.stop_reason == "tool_use" and turn.model == "gemini-test-001"
    (tool_use,) = turn.tool_uses
    assert tool_use["input"] == {"action": "screenshot"} and tool_use["thought_signature"] == "sig"
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.cache_read_input_tokens) == (100, 50, 40)
    assert client.describe()["route"] == "api_key"
    await client.aclose()


async def test_vertex_route_uses_adc_and_the_google_publisher_path():
    rec, client = make_client([gemini([{"text": "done"}])], api_key=None, project="proj", location="us-central1")
    turn = await call(client)
    req = rec.requests[0]
    assert req.url.host == "us-central1-aiplatform.googleapis.com"
    assert req.url.path == "/v1/projects/proj/locations/us-central1/publishers/google/models/gemini-x:generateContent"
    assert req.headers["authorization"] == "Bearer tok" and "x-goog-api-key" not in req.headers
    assert turn.stop_reason == "end_turn" and turn.texts == ["done"]
    assert client.describe() == {"provider": "gemini", "model": "gemini-x", "route": "vertex", "project": "proj",
                                 "location": "us-central1"}


async def test_finish_reasons_and_blocked_prompts():
    _, client = make_client([gemini([{"text": "partial"}], finish="MAX_TOKENS")])
    assert (await call(client)).stop_reason == "max_tokens"

    _, client = make_client([httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})])
    with pytest.raises(ModelError, match="SAFETY"):
        await call(client)

    _, client = make_client([gemini([], finish="RECITATION")])
    with pytest.raises(ModelError, match="RECITATION"):
        await call(client)


async def test_thinking_level_is_sent_then_dropped_when_rejected():
    rec, client = make_client([err(400, "thinking_level is not supported for this model"), gemini([{"text": "ok"}])],
                              thinking_level="high")
    await call(client)
    assert rec.body(0)["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert "thinkingConfig" not in rec.body(1)["generationConfig"]


async def test_retries_and_denials():
    rec, client = make_client([err(429, "quota"), httpx.ReadTimeout("slow"), gemini([{"text": "ok"}])])
    turn = await call(client)
    assert turn.retries == 2 and len(rec.requests) == 3

    _, client = make_client([err(503, "busy")] * 2, max_retries=1)
    with pytest.raises(ModelError, match="503 after 1 retries"):
        await call(client)

    _, client = make_client([err(403, "API key not valid")])
    with pytest.raises(ModelError, match="GEMINI_API_KEY"):
        await call(client)

    _, client = make_client([err(404, "model not found")])
    with pytest.raises(ModelError, match="not found via api_key"):
        await call(client)
