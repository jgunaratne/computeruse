"""VertexAnthropicModelClient: wire shape per tool mode, 400 adaptation, location rotation, retries, parsing.

Every test runs against `httpx.MockTransport`; no network, no credentials.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import HttpRecorder, fake_token, no_backoff

from computeruse.agent.model import ModelError
from computeruse.agent.prompts import computer_tool
from computeruse.agent.vertex import (
    DEFAULT_LOCATIONS,
    TOOLSET_TYPE,
    VertexAnthropicModelClient,
    default_tool_mode,
    detect_adc,
    from_toolset_block,
    to_toolset_messages,
    vertex_host,
)

TOOLS = [computer_tool("computer_20250124", (1024, 768))]
# A canonical transcript with provider-private bookkeeping (`thought_signature`) and a thinking block.
MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "Open Notes"}]},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "computer",
         "input": {"action": "left_click", "coordinate": [10, 20]}, "thought_signature": "gemini-only"},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "ok"}]},
    ]},
]


def ok(content=None, stop_reason="end_turn", usage=None, model="claude-x"):
    return httpx.Response(200, json={
        "content": content or [{"type": "text", "text": "done"}], "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5}, "model": model,
    })


def err(status, message):
    return httpx.Response(status, json={"error": {"type": "invalid_request_error", "message": message}})


def make_client(responses, **kw):
    rec = HttpRecorder(responses)
    client = VertexAnthropicModelClient(kw.pop("model", "claude-sonnet-4-5"), project="proj",
                                        token_source=fake_token, transport=httpx.MockTransport(rec), **kw)
    client._backoff = no_backoff  # type: ignore[method-assign]
    return rec, client


async def call(client):
    return await client.create(system="sys", messages=MESSAGES, tools=TOOLS, max_tokens=100)


# -- pure helpers --------------------------------------------------------------


def test_tool_mode_defaults_follow_the_model_family():
    assert default_tool_mode("claude-sonnet-4-5") == "builtin"
    assert default_tool_mode("claude-opus-4-1@20250805") == "builtin"
    assert default_tool_mode("claude-opus-5-5") == "toolset"
    assert default_tool_mode("claude-sonnet-5") == "toolset"
    assert default_tool_mode("claude-fable-5-1") == "toolset"  # new families: the major version decides
    assert default_tool_mode("mystery-model") == "builtin"


def test_vertex_host_global_vs_regional():
    assert vertex_host("global") == "aiplatform.googleapis.com"
    assert vertex_host("us-east5") == "us-east5-aiplatform.googleapis.com"


def test_toolset_translation_round_trip():
    msgs = to_toolset_messages(MESSAGES)
    assert msgs[0] == MESSAGES[0]  # plain user turn untouched
    thinking, tool_use = msgs[1]["content"]
    assert thinking["type"] == "thinking"
    assert tool_use == {"type": "tool_use", "id": "toolu_1", "name": "left_click", "input": {"coordinate": [10, 20]},
                        "toolset_name": "computer"}
    result = msgs[2]["content"][0]
    assert result["toolset_name"] == "computer" and result["tool_use_id"] == "toolu_1"
    assert "toolset_name" not in MESSAGES[2]["content"][0], "input must not be mutated"

    member = {"type": "tool_use", "id": "x", "name": "zoom", "input": {"region": [0, 0, 5, 5]}, "toolset_name": "computer"}
    assert from_toolset_block(member) == {"type": "tool_use", "id": "x", "name": "computer",
                                          "input": {"action": "zoom", "region": [0, 0, 5, 5]}}
    assert from_toolset_block({"type": "text", "text": "hi"}) == {"type": "text", "text": "hi"}


def test_rejects_unknown_tool_mode():
    with pytest.raises(ValueError, match="tool_mode"):
        VertexAnthropicModelClient("claude-sonnet-4-5", project="p", tool_mode="magic", token_source=fake_token)


# -- request shaping -------------------------------------------------------------


async def test_builtin_mode_sends_beta_header_and_canonical_messages():
    rec, client = make_client([ok()])
    turn = await call(client)
    req = rec.requests[0]
    assert req.url.host == "aiplatform.googleapis.com"
    assert req.url.path == ("/v1/projects/proj/locations/global/publishers/anthropic/models/"
                            "claude-sonnet-4-5:rawPredict")
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["anthropic-beta"] == "computer-use-2025-01-24"
    body = rec.body()
    assert body["anthropic_version"] == "vertex-2023-10-16" and "anthropic_beta" not in body
    assert body["tools"] == TOOLS
    assert body["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    thinking, tool_use = body["messages"][1]["content"]
    assert thinking == {"type": "thinking", "thinking": "", "signature": "sig"}  # passed back verbatim
    assert tool_use["name"] == "computer" and "thought_signature" not in tool_use  # private key stripped
    assert turn.stop_reason == "end_turn" and turn.model == "claude-x"
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (10, 5)
    await client.aclose()


async def test_toolset_mode_wire_shape_and_canonical_response():
    response = [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "tool_use", "id": "toolu_9", "name": "zoom", "input": {"region": [1, 2, 3, 4]},
         "toolset_name": "computer"},
    ]
    usage = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}
    rec, client = make_client([ok(response, stop_reason="tool_use", usage=usage)], model="claude-opus-5-5")
    assert client.tool_mode == "toolset"
    turn = await call(client)
    assert "anthropic-beta" not in rec.requests[0].headers
    body = rec.body()
    assert body["tools"] == [{"type": TOOLSET_TYPE}]  # no name / display fields: the API rejects them
    tool_use = body["messages"][1]["content"][1]
    assert tool_use["name"] == "left_click" and tool_use["toolset_name"] == "computer"
    assert tool_use["input"] == {"coordinate": [10, 20]}
    assert body["messages"][2]["content"][0]["toolset_name"] == "computer"
    # ...and the loop only ever sees canonical `computer` calls
    assert turn.tool_uses == [{"type": "tool_use", "id": "toolu_9", "name": "computer",
                               "input": {"action": "zoom", "region": [1, 2, 3, 4]}}]
    assert turn.content[0]["type"] == "thinking"
    assert (turn.usage.cache_read_input_tokens, turn.usage.cache_creation_input_tokens) == (3, 4)


async def test_custom_mode_sends_a_json_schema_tool():
    rec, client = make_client([ok()], tool_mode="custom")
    await call(client)
    (tool,) = rec.body()["tools"]
    assert tool["name"] == "computer" and "type" not in tool
    assert tool["input_schema"]["properties"]["action"]["enum"] and "1024x768" in tool["description"]
    assert "anthropic-beta" not in rec.requests[0].headers


async def test_thinking_effort_is_sent_as_adaptive_thinking():
    rec, client = make_client([ok()], thinking_effort="medium")
    await call(client)
    body = rec.body()
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "medium"}


# -- 400 adaptation --------------------------------------------------------------


async def test_tool_definition_400s_fall_back_through_modes():
    rec, client = make_client([
        err(400, "tools.0: Input tag 'computer_20250124' found using 'type' does not match any expected tags"),
        err(400, "toolset computer_toolset_20260801 is not supported for this model"),
        ok(),
    ])
    await call(client)
    sent = [rec.body(i)["tools"][0] for i in range(3)]
    assert sent[0] == TOOLS[0]
    assert sent[1] == {"type": TOOLSET_TYPE}
    assert "input_schema" in sent[2]
    assert client.tool_mode == "custom"


async def test_toolset_first_models_fall_back_to_builtin_then_custom():
    rec, client = make_client([err(400, "toolset not available"), err(400, "anthropic-beta not supported"), ok()],
                              model="claude-opus-5-5")
    await call(client)
    assert rec.body(0)["tools"] == [{"type": TOOLSET_TYPE}]
    assert rec.body(1)["tools"] == TOOLS and rec.requests[1].headers["anthropic-beta"]
    assert "input_schema" in rec.body(2)["tools"][0]


async def test_cache_control_400_disables_prompt_caching():
    rec, client = make_client([err(400, "system.0.cache_control: Extra inputs are not permitted"), ok()])
    await call(client)
    assert "cache_control" in rec.body(0)["system"][0]
    assert "cache_control" not in rec.body(1)["system"][0]
    assert client.prompt_caching is False


async def test_thinking_400_drops_the_thinking_config():
    rec, client = make_client([err(400, "thinking: not supported on this model"), ok()], thinking_effort="high")
    await call(client)
    assert "thinking" in rec.body(0) and "thinking" not in rec.body(1) and "output_config" not in rec.body(1)


async def test_unrelated_400_raises_with_the_api_message():
    _, client = make_client([err(400, "messages.3: unexpected role")])
    with pytest.raises(ModelError, match="unexpected role"):
        await call(client)


async def test_tool_400_with_no_modes_left_raises():
    _, client = make_client([err(400, "tools.0: bad")] * 3)
    with pytest.raises(ModelError, match="tools.0"):
        await call(client)


# -- locations / retries ---------------------------------------------------------------


async def test_404_rotates_locations_and_remembers_dead_ones():
    rec, client = make_client([err(404, "Publisher Model not found"), ok(), ok()], location="us-east5")
    assert client.locations[0] == "us-east5" and "global" in client.locations
    await call(client)
    assert rec.requests[0].url.host == "us-east5-aiplatform.googleapis.com"
    assert rec.requests[1].url.host == "aiplatform.googleapis.com"
    await call(client)  # dead location is skipped on the next call
    assert rec.requests[2].url.host == "aiplatform.googleapis.com" and len(rec.requests) == 3


async def test_404_everywhere_is_a_clear_error():
    _, client = make_client([err(404, "not found")] * len(DEFAULT_LOCATIONS))
    with pytest.raises(ModelError, match="not served in any of"):
        await call(client)


async def test_multiple_projects_rotate_location_major_and_remember_dead_targets():
    rec = HttpRecorder([err(404, "Publisher Model not found"), ok(), ok()])
    client = VertexAnthropicModelClient("claude-sonnet-5", projects=["a", "b"], token_source=fake_token,
                                        transport=httpx.MockTransport(rec))
    client._backoff = no_backoff  # type: ignore[method-assign]
    assert client.project == "a" and client.projects == ["a", "b"]
    assert client.targets[:3] == [("a", "global"), ("b", "global"), ("a", "us-east5")]
    await call(client)
    assert "/projects/a/locations/global/" in str(rec.requests[0].url)
    assert "/projects/b/locations/global/" in str(rec.requests[1].url)  # b's global before a's regions
    await call(client)
    assert "/projects/b/" in str(rec.requests[2].url) and len(rec.requests) == 3  # (a, global) stays dead
    assert client.describe()["projects"] == ["a", "b"]
    await client.aclose()


async def test_403_is_project_scoped_and_falls_through_to_the_next_project():
    rec = HttpRecorder([err(403, "requires data sharing to be enabled for publisher anthropic"), ok()])
    client = VertexAnthropicModelClient("claude-fable-5", projects=["a", "b"], token_source=fake_token,
                                        transport=httpx.MockTransport(rec))
    await call(client)
    assert "/projects/a/" in str(rec.requests[0].url) and "/projects/b/" in str(rec.requests[1].url)
    assert all(t in client._dead for t in client.targets if t[0] == "a")  # every a-target, not just global

    _, single = make_client([err(403, "Permission denied on resource project proj")])
    with pytest.raises(ModelError, match="project proj may use"):
        await call(single)

    rec = HttpRecorder([err(404, "nf")] * len(DEFAULT_LOCATIONS) * 2)
    both = VertexAnthropicModelClient("claude-x", projects=["a", "b"], token_source=fake_token,
                                      transport=httpx.MockTransport(rec))
    with pytest.raises(ModelError, match=r"any of \['a', 'b'\]"):
        await call(both)


def test_client_requires_a_project():
    with pytest.raises(ValueError, match="at least one GCP project"):
        VertexAnthropicModelClient("claude-sonnet-4-5", token_source=fake_token)
    client = VertexAnthropicModelClient("claude-sonnet-4-5", project="p", projects=["p", "q"], token_source=fake_token)
    assert client.projects == ["p", "q"]  # `project` is folded into the list, de-duplicated


async def test_retryable_statuses_retry_then_give_up():
    rec, client = make_client([err(429, "rate limited"), err(503, "overloaded"), ok()], max_retries=3)
    turn = await call(client)
    assert turn.retries == 2 and len(rec.requests) == 3

    _, client = make_client([err(529, "overloaded")] * 3, max_retries=2)
    with pytest.raises(ModelError, match="529 after 2 retries"):
        await call(client)


async def test_transport_errors_are_retried():
    rec, client = make_client([httpx.ConnectError("boom"), ok()])
    turn = await call(client)
    assert turn.retries == 1 and len(rec.requests) == 2


async def test_permission_denied_carries_an_actionable_hint():
    _, client = make_client([err(403, "Permission denied on resource project proj")])
    with pytest.raises(ModelError, match="gcloud auth application-default login"):
        await call(client)


# -- ADC detection ---------------------------------------------------------------------


def test_detect_adc_reports_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    usable, project, reason = detect_adc("proj")
    assert usable is False and project == "proj" and "Application Default Credentials" in reason


def test_detect_adc_project_resolution(monkeypatch):
    import google.auth

    for var in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "cloudtop-example"))
    # cloudtop ADC projects cannot host the AI APIs, so they are never used implicitly
    assert detect_adc(None) == (False, None, "ADC found but no GCP project; set GOOGLE_CLOUD_PROJECT")
    assert detect_adc("my-project") == (True, "my-project", "ADC credentials, project my-project")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    assert detect_adc(None)[1] == "from-env"
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "real-project"))
    assert detect_adc(None) == (True, "real-project", "ADC credentials, project real-project")
