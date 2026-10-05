"""Antigravity provider: Language Server detection, model catalogue, JSON reply protocol, the stateful
conversation client, and how it plugs into discovery and the model catalog.

Everything runs against `FakeLS`, an in-memory Language Server behind `httpx.MockTransport`
that mirrors the Connect-JSON shapes observed live (2026-10): no socket is ever opened, and the
`ANTIGRAVITY_*` variables a real Antigravity environment exports are ignored (`env={}`). Model names are
fictional.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from computeruse.agent.antigravity import (
    CSRF_HEADER,
    AntigravityEndpoint,
    AntigravityModelClient,
    LanguageServer,
    detect_antigravity,
    extract_actions,
    find_model,
    parse_model_configs,
    parse_reply,
    render_delta,
    resolve_address,
    resolve_token,
    response_format_section,
    token_from_index_page,
)
from computeruse.agent.discovery import DiscoveredModel, ModelDiscovery, rank
from computeruse.agent.model import ModelError
from computeruse.agent.providers import ModelCatalog
from computeruse.config import Settings
from computeruse.telemetry.events import Usage, estimate_cost_usd

TOKEN = "tok-123"
ENDPOINT = AntigravityEndpoint(address="localhost:5387", csrf_token=TOKEN, token_source="server")
TOOLS = [{"type": "computer_20250124", "name": "computer", "display_width_px": 1024, "display_height_px": 768}]
PNG = base64.b64encode(b"\x89PNG not really").decode()

# The shape `GetCascadeModelConfigData` returns (trimmed to five models; the real list has ~25).
MODELS: dict[str, Any] = {
    "clientModelConfigs": [
        {"label": "Gemini Flash Lite", "modelId": "gemini-flash-lite",
         "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M1"}, "supportsImages": True,
         "quotaInfo": {"remainingFraction": 0.93, "resetTime": "2026-10-06T03:00:00Z"}},
        {"label": "Gemini Pro (High)", "modelId": "gemini-pro-high",
         "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M2"}, "supportsImages": True,
         "quotaInfo": {"remainingFraction": 0.5}},
        {"label": "Claude Sonnet (High)", "modelId": "sonnet-high",
         "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M3"}, "supportsImages": True,
         "quotaInfo": {"remainingFraction": 0.0, "resetTime": "2026-10-05T20:00:00Z"}},
        {"label": "Text Only", "modelId": "text-only", "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M999"},
         "supportsImages": False},
        {"label": "Retired", "modelId": "retired", "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M0"},
         "supportsImages": True, "disabled": True},
        {"label": "No enum", "modelId": "no-enum"},  # not addressable: skipped
    ],
    "clientModelSorts": [
        {"name": "Recommended", "groups": [{"modelLabels": ["Gemini Pro (High)", "Claude Sonnet (High)"]},
                                           {"modelLabels": ["Gemini Flash Lite"]}]},
        {"name": "Alphabetical", "groups": [{"modelLabels": ["Claude Sonnet (High)"]}]},
    ],
}


def planner(response: str, *, thinking: str = "", stop: str = "STOP_REASON_STOP_PATTERN",
            usage: dict[str, str] | None = None, model: str = "MODEL_PLACEHOLDER_M1") -> dict[str, Any]:
    return {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE",
            "plannerResponse": {"response": response, "thinking": thinking, "stopReason": stop},
            "metadata": {"generatorModel": model, "modelUsage": usage or {
                "inputTokens": "2380", "outputTokens": "495", "thinkingOutputTokens": "400",
                "responseOutputTokens": "95"}}}


def error_step(message: str) -> dict[str, Any]:
    return {"type": "CORTEX_STEP_TYPE_ERROR_MESSAGE", "status": "CORTEX_STEP_STATUS_DONE",
            "errorMessage": {"error": {"userErrorMessage": message, "shortError": "boom"}}}


class FakeLS:
    """In-memory Antigravity Language Server: CSRF check, model list, scripted planner replies per message."""

    def __init__(self, *, token: str = TOKEN, models: dict[str, Any] | None = None,
                 replies: list[list[dict[str, Any]]] | None = None, page_size: int = 100,
                 index_token: bool = True) -> None:
        self.token = token
        self.models = MODELS if models is None else models
        self.replies = list(replies or [])  # planner steps appended after each user message
        self.page_size = page_size
        self.index_token = index_token
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gets = 0
        self.steps: dict[str, list[dict[str, Any]]] = {}
        self.annotations: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/":
            self.gets += 1
            page = f'<script>window.cfg = {{"csrfToken":"{self.token}","x":1}}</script>' if self.index_token else "<p/>"
            return httpx.Response(200, text=page)
        if request.headers.get(CSRF_HEADER) != self.token:
            return httpx.Response(401, json={"code": "unauthenticated", "message": "invalid CSRF token"})
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        handler = getattr(self, f"rpc_{method}", None)
        if handler is None:
            return httpx.Response(404, json={"code": "unimplemented", "message": method})
        return handler(body)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def bodies(self, method: str) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]

    # -- RPCs ---------------------------------------------------------------------

    def rpc_GetCascadeModelConfigData(self, body):  # noqa: N802 - mirrors the wire method name
        return httpx.Response(200, json=self.models)

    def rpc_StartCascade(self, body):  # noqa: N802
        self._n += 1
        cid = f"casc-{self._n}"
        self.steps[cid] = []
        return httpx.Response(200, json={"cascadeId": cid})

    def rpc_UpdateConversationAnnotations(self, body):  # noqa: N802
        for cid in body.get("cascadeIds", []):
            self.annotations.setdefault(cid, {}).update(body.get("annotations", {}))
        return httpx.Response(200, json={})

    def rpc_SendUserCascadeMessage(self, body):  # noqa: N802
        cid = body["cascadeId"]
        if cid not in self.steps:
            return self._not_found(cid)
        self.steps[cid].append({"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": "CORTEX_STEP_STATUS_DONE",
                                "userInput": {"items": body.get("items", [])}})
        self.steps[cid].extend(self.replies.pop(0) if self.replies else [])
        return httpx.Response(200, json={})

    def rpc_GetCascadeTrajectory(self, body):  # noqa: N802
        cid = body["cascadeId"]
        if cid not in self.steps:
            return self._not_found(cid)
        steps = self.steps[cid]
        return httpx.Response(200, json={"trajectory": {"steps": steps[:self.page_size]},
                                         "status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": len(steps)})

    def rpc_GetCascadeTrajectorySteps(self, body):  # noqa: N802
        steps = self.steps[body["cascadeId"]]
        off = int(body.get("stepOffset", 0))
        return httpx.Response(200, json={"steps": steps[off:off + self.page_size]})

    def rpc_CancelCascadeInvocation(self, body):  # noqa: N802
        self.cancelled.append(body["cascadeId"])
        return httpx.Response(200, json={})

    @staticmethod
    def _not_found(cid: str) -> httpx.Response:
        return httpx.Response(500, json={"code": "unknown", "message": f"trajectory not found: {cid}"})


def image(data: str = PNG) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def client_for(ls: FakeLS, ref: str = "antigravity:gemini-flash-lite", **kw) -> AntigravityModelClient:
    return AntigravityModelClient(ref, endpoint=ENDPOINT, transport=ls.transport(), **kw)


async def one_turn(client: AntigravityModelClient, messages: list[dict[str, Any]] | None = None):
    messages = messages or [{"role": "user", "content": [{"type": "text", "text": "Find the release year"}, image()]}]
    return await client.create(system="SYSTEM PROMPT", messages=messages, tools=TOOLS, max_tokens=4096)


# -- catalogue -----------------------------------------------------------------------


def test_model_configs_are_parsed_in_recommended_order_with_quota():
    models = parse_model_configs(MODELS)
    assert [m.id for m in models] == ["gemini-pro-high", "sonnet-high", "gemini-flash-lite",
                                      "retired", "text-only"]  # recommended order, then the rest by label
    pro, sonnet, lite, retired, text = models
    assert pro.enum == "MODEL_PLACEHOLDER_M2" and pro.label == "Gemini Pro (High)" and pro.order == 0
    assert pro.quota_remaining == 0.5 and pro.quota_reset is None and not pro.quota_exhausted
    assert pro.status_note() == "listed by Antigravity · quota 50% left"
    assert lite.status_note() == "listed by Antigravity · quota 93% left, resets 2026-10-06 03:00:00 UTC"
    assert sonnet.quota_exhausted and sonnet.quota_reset == "2026-10-05T20:00:00Z"
    assert retired.disabled and not text.supports_images and text.quota_remaining is None
    assert text.status_note() == "listed by Antigravity"
    with pytest.raises(ValueError, match="clientModelConfigs"):
        parse_model_configs({"foo": 1})


def test_find_model_matches_id_label_or_enum():
    models = parse_model_configs(MODELS)
    assert find_model(models, "gemini-pro-high").enum == "MODEL_PLACEHOLDER_M2"
    assert find_model(models, "antigravity:Gemini Pro (high)").id == "gemini-pro-high"
    assert find_model(models, " model_placeholder_m1 ").id == "gemini-flash-lite"
    assert find_model(models, "gemini-4") is None


# -- detection ---------------------------------------------------------------------------


def test_address_and_token_resolution_order():
    assert resolve_address(None, {}) == ("localhost:5387", "default")
    assert resolve_address(None, {"ANTIGRAVITY_LS_ADDRESS": " 127.0.0.1:6000 "}) == ("127.0.0.1:6000",
                                                                                      "environment (ANTIGRAVITY_LS_ADDRESS)")
    assert resolve_address("ls.corp:9", {"ANTIGRAVITY_LS_ADDRESS": "x"}) == ("ls.corp:9", "configured")
    assert resolve_token(None, {}) == (None, "server")
    assert resolve_token(None, {"ANTIGRAVITY_CSRF_TOKEN": "e"}) == ("e", "environment")
    assert resolve_token("c", {"ANTIGRAVITY_CSRF_TOKEN": "e"}) == ("c", "configured")
    assert token_from_index_page('{"csrfToken":"abc"}') == "abc" and token_from_index_page("<p/>") is None
    assert AntigravityEndpoint("http://ls.corp:9/", "t", "configured").base_url == "http://ls.corp:9"
    assert AntigravityEndpoint("ls.corp:9", "t", "configured").public() == {"address": "ls.corp:9",
                                                                      "token_source": "configured"}


def test_detect_antigravity_fetches_the_token_from_the_index_page():
    ls = FakeLS()
    endpoint, reason, models = detect_antigravity(env={}, transport=ls.transport())
    assert endpoint == ENDPOINT and ls.gets == 1 and ls.methods() == ["GetCascadeModelConfigData"]
    assert reason == "Antigravity Language Server at localhost:5387 (4 models, token from server, address default)"
    assert [m.id for m in models] == ["gemini-pro-high", "sonnet-high", "gemini-flash-lite",
                                      "text-only"]  # disabled entries dropped
    assert TOKEN not in json.dumps(endpoint.public())


def test_detect_antigravity_uses_configured_or_environment_values_without_touching_the_index():
    ls = FakeLS(token="env-tok")
    endpoint, reason, _ = detect_antigravity(env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:6000",
                                             "ANTIGRAVITY_CSRF_TOKEN": "env-tok"}, transport=ls.transport())
    assert endpoint == AntigravityEndpoint("127.0.0.1:6000", "env-tok", "environment") and ls.gets == 0
    assert "token from environment, address environment (ANTIGRAVITY_LS_ADDRESS)" in reason
    endpoint, reason, _ = detect_antigravity("http://ls.corp:9/", "env-tok", env={"ANTIGRAVITY_LS_ADDRESS": "x"},
                                        transport=ls.transport())
    assert endpoint.address == "http://ls.corp:9/" and endpoint.token_source == "configured"
    assert reason.endswith("(4 models, token from configured)")


def test_detect_antigravity_failure_modes_never_raise():
    ls = FakeLS(index_token=False)
    endpoint, reason, models = detect_antigravity(env={}, transport=ls.transport())
    assert endpoint is None and models == [] and "did not hand out a CSRF token (HTTP 200)" in reason
    assert "COMPUTERUSE_ANTIGRAVITY_CSRF_TOKEN" in reason

    endpoint, reason, _ = detect_antigravity(None, "bad", env={}, transport=FakeLS().transport())
    assert endpoint is None and reason == "Antigravity Language Server at localhost:5387 rejected the CSRF token (configured)"

    def refused(request):
        raise httpx.ConnectError("connection refused")

    endpoint, reason, _ = detect_antigravity(env={}, transport=httpx.MockTransport(refused))
    assert endpoint is None and reason.startswith("no Antigravity Language Server at localhost:5387: ConnectError")
    assert "start Antigravity, or point COMPUTERUSE_ANTIGRAVITY_ADDRESS" in reason
    endpoint, reason, _ = detect_antigravity("ls.corp:9", env={}, transport=httpx.MockTransport(refused))
    assert "start Antigravity" not in reason  # a configured address is not a "Antigravity is not running" hint

    endpoint, reason, _ = detect_antigravity(env={}, transport=FakeLS(models={"nope": 1}).transport())
    assert endpoint is None and "unexpected model list: clientModelConfigs missing" in reason

    def broken(request):
        if request.method == "GET":
            return httpx.Response(200, text='"csrfToken":"t"')
        return httpx.Response(503, json={"code": "unavailable", "message": "warming up"})

    endpoint, reason, _ = detect_antigravity(env={}, transport=httpx.MockTransport(broken))
    assert endpoint is None and reason.endswith("answered 503: unavailable: warming up")


# -- reply protocol -------------------------------------------------------------------------


def test_response_format_section_carries_the_schema_and_action_names():
    section = response_format_section((1280, 800))
    assert "left_click" in section and "1280x800" in section and '"action":{' in section
    assert "plain text only" in section


@pytest.mark.parametrize("text, narration, actions", [
    ('I see a form.\n{"action": "left_click", "coordinate": [412, 300]}', "I see a form.",
     [{"action": "left_click", "coordinate": [412, 300]}]),
    ('```json\n{"action": "type", "text": "hello {world}"}\n```', "", [{"action": "type", "text": "hello {world}"}]),
    ('{"action": "key", "text": "ctrl+l"} then {"action": "type", "text": "x"}', "",
     [{"action": "key", "text": "ctrl+l"}, {"action": "type", "text": "x"}]),
    ("All done: the year is 1991.", "All done: the year is 1991.", []),
    ('{"action": "fly", "to": "moon"} and {"not": "an action"}', '{"action": "fly", "to": "moon"} and {"not": "an action"}', []),
    ("Prose with {braces} that are not JSON", "Prose with {braces} that are not JSON", []),
])
def test_extract_actions(text, narration, actions):
    assert extract_actions(text) == (narration, actions)


def test_parse_reply_builds_canonical_blocks():
    blocks, stop = parse_reply('Clicking the field.\n{"action": "left_click", "coordinate": [1, 2]}', thinking="t" * 2000)
    assert stop == "tool_use" and [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert len(blocks[0]["thinking"]) == 1500 and blocks[1]["text"] == "Clicking the field."
    assert blocks[2]["name"] == "computer" and blocks[2]["input"] == {"action": "left_click", "coordinate": [1, 2]}
    assert blocks[2]["id"].startswith("toolu_")

    blocks, stop = parse_reply('{"action": "scroll", "coordinate": [5, 5], "scroll_direction": "down", "scroll_amount": 3, "note": "see more"}')
    assert stop == "tool_use" and [b["type"] for b in blocks] == ["text", "tool_use"]
    assert blocks[0]["text"] == "see more" and "note" not in blocks[1]["input"]

    blocks, stop = parse_reply("```\nThe answer is 42.\n```")
    assert stop == "end_turn" and blocks == [{"type": "text", "text": "The answer is 42."}]
    assert parse_reply("") == ([], "end_turn")


def test_render_delta_sends_only_user_messages_with_inline_media():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Do the thing"}, image()]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "toolu_1",
                                                                             "name": "computer", "input": {"action": "left_click"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
             "content": [{"type": "text", "text": "click failed: out of bounds"}, image("QUJD")]},
            {"type": "text", "text": "Operator guidance: slow down"},
            {"type": "text", "text": "(earlier screenshot omitted to save context)"},
        ]},
        {"role": "user", "content": "plain string content"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "done"}]},
    ]
    text, media = render_delta(messages, {"toolu_1": "left_click"})
    assert text.split("\n") == [
        "Do the thing", "[screenshot attached as image 1]",
        "Result of `left_click`: failed — click failed: out of bounds", "[screenshot after left_click attached as image 2]",
        "Operator guidance: slow down", "(earlier screenshot omitted to save context)",
        "plain string content", "Result of `action`: done — done",
    ]
    assert [m["inlineData"] for m in media] == [PNG, "QUJD"]
    assert media[1] == {"mimeType": "image/png", "inlineData": "QUJD", "description": "screenshot after left_click",
                        "displayName": "screenshot-after-left_click.png"}
    assert render_delta([], {}) == ("", [])


# -- client ----------------------------------------------------------------------------------


async def test_client_runs_a_session_through_one_antigravity_conversation():
    ls = FakeLS(replies=[
        [planner('I see a form.\n{"action": "left_click", "coordinate": [412, 300]}', thinking="Click the field.")],
        [planner("Done. The release year is 1991.", usage={"inputTokens": "3000", "outputTokens": "20",
                                                           "cacheReadTokens": "1000"})],
    ])
    ticks = iter([10.0, 10.5, 20.0, 20.25])
    client = client_for(ls, clock=lambda: next(ticks))
    assert client.name == "antigravity:gemini-flash-lite"
    messages = [{"role": "user", "content": [{"type": "text", "text": "Find the release year"}, image()]}]
    turn = await one_turn(client, messages)

    # conversation: tool-less custom agent, our prompt + the reply protocol, model pinned by enum
    start, = ls.bodies("StartCascade")
    agent = start["customAgentSpec"]["customAgent"]
    assert agent["toolNames"] == [] and agent["excludeDefaultComponents"] is True
    assert start["customAgentSpec"]["commandExecutionPolicy"] == "off"
    assert start["customAgentSpec"]["cascadeConfig"]["plannerConfig"] == {
        "planModel": "MODEL_PLACEHOLDER_M1", "requestedModel": {"model": "MODEL_PLACEHOLDER_M1"}}
    assert [s["title"] for s in agent["systemPromptSections"]] == ["COMPUTER_USE", "RESPONSE_FORMAT"]
    assert agent["systemPromptSections"][0]["content"] == "SYSTEM PROMPT"
    assert "1024x768" in agent["systemPromptSections"][1]["content"]
    assert start["source"] == "CORTEX_TRAJECTORY_SOURCE_SDK" and start["tags"] == ["computeruse"]
    assert ls.annotations["casc-1"] == {"title": "computeruse · Find the release year"}

    # first message: task text + the screenshot as inline media, blocking send
    send, = ls.bodies("SendUserCascadeMessage")
    assert send["cascadeId"] == "casc-1" and send["blocking"] is True
    assert send["items"] == [{"text": "Find the release year\n[screenshot attached as image 1]"}]
    assert send["media"][0]["inlineData"] == PNG and send["media"][0]["mimeType"] == "image/png"
    assert send["cascadeConfig"] == {"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_M1"}}
    assert send["messageOrigin"] == "AGENT_MESSAGE_ORIGIN_SDK_EXECUTABLE"

    # the turn, in canonical form
    assert turn.stop_reason == "tool_use" and turn.model == "gemini-flash-lite" and turn.latency_ms == 500.0
    assert [b["type"] for b in turn.content] == ["thinking", "text", "tool_use"]
    assert turn.content[0]["thinking"] == "Click the field." and turn.texts == ["I see a form."]
    tool_use = turn.tool_uses[0]
    assert tool_use["input"] == {"action": "left_click", "coordinate": [412, 300]}
    assert turn.usage == Usage(input_tokens=2380, output_tokens=495)
    described = client.describe()
    assert described["conversation_id"] == "casc-1" and described["model"] == "gemini-flash-lite"
    assert described["label"] == "Gemini Flash Lite" and TOKEN not in json.dumps(described)

    # second turn: only the new user message travels (assistant content already lives in Antigravity)
    messages.append({"role": "assistant", "content": turn.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use["id"],
                                                  "content": [{"type": "text", "text": "clicked"}, image("QUJD")]}]})
    turn2 = await one_turn(client, messages)
    sends = ls.bodies("SendUserCascadeMessage")
    assert len(sends) == 2 and len(ls.bodies("StartCascade")) == 1
    assert sends[1]["items"] == [{"text": "Result of `left_click`: done — clicked\n"
                                          "[screenshot after left_click attached as image 1]"}]
    assert sends[1]["media"][0]["inlineData"] == "QUJD"
    assert sends[0]["metadata"]["sessionId"] == sends[1]["metadata"]["sessionId"]
    assert turn2.stop_reason == "end_turn" and turn2.texts == ["Done. The release year is 1991."]
    assert turn2.usage == Usage(input_tokens=3000, output_tokens=20, cache_read_input_tokens=1000)
    assert turn2.latency_ms == 250.0

    await client.aclose()
    assert ls.annotations["casc-1"]["archived"] is True and ls.cancelled == []


async def test_client_pages_long_trajectories_and_merges_split_replies():
    ls = FakeLS(page_size=1, replies=[[planner("Looking…", usage={"inputTokens": "10", "outputTokens": "1"}),
                                       planner('{"action": "wait", "duration": 1}',
                                               usage={"inputTokens": "20", "outputTokens": "2"})]])
    client = client_for(ls)
    turn = await one_turn(client)
    assert ls.methods().count("GetCascadeTrajectorySteps") >= 2  # 3 steps fetched one page at a time
    assert turn.texts == ["Looking…"] and turn.tool_uses[0]["input"] == {"action": "wait", "duration": 1}
    assert turn.usage == Usage(input_tokens=30, output_tokens=3)
    await client.aclose()


async def test_client_nudges_once_on_an_empty_reply_then_fails():
    ls = FakeLS(replies=[[planner("", thinking="…")], [planner('{"action": "screenshot"}')], [planner("")], []])
    client = client_for(ls)
    turn = await one_turn(client)
    sends = ls.bodies("SendUserCascadeMessage")
    assert len(sends) == 2 and sends[1]["items"][0]["text"].startswith("Your reply was empty") and "media" not in sends[1]
    assert turn.tool_uses[0]["input"] == {"action": "screenshot"} and turn.stop_reason == "tool_use"
    with pytest.raises(ModelError, match="returned no reply"):
        await one_turn(client, [{"role": "user", "content": [{"type": "text", "text": "again"}]}] * 1)
    assert len(ls.bodies("SendUserCascadeMessage")) == 4
    await client.aclose()


async def test_client_surfaces_server_side_errors_and_max_tokens():
    ls = FakeLS(replies=[[error_step("Model quota exceeded for Gemini Flash Lite")]])
    client = client_for(ls)
    with pytest.raises(ModelError, match=r"Antigravity \(Gemini Flash Lite\): Model quota exceeded"):
        await one_turn(client)
    await client.aclose()

    ls = FakeLS(replies=[[planner("I was about to", stop="STOP_REASON_MAX_TOKENS")]])
    client = client_for(ls)
    turn = await one_turn(client)
    assert turn.stop_reason == "max_tokens" and turn.texts == ["I was about to"]
    await client.aclose()


async def test_client_validates_the_model_against_the_live_catalogue():
    ls = FakeLS()
    for ref, message in [("antigravity:gemini-4", "does not offer model 'gemini-4'; available: gemini-pro-high"),
                         ("retired", "does not offer"), ("text-only", "does not accept images"),
                         ("sonnet-high", "quota for 'Claude Sonnet \\(High\\)' is exhausted until 2026-10-05T20:00:00Z")]:
        client = client_for(ls, ref)
        with pytest.raises(ModelError, match=message):
            await one_turn(client)
        await client.aclose()
    assert "StartCascade" not in ls.methods()

    ls = FakeLS(replies=[[planner("done")]])
    client = client_for(ls, "MODEL_PLACEHOLDER_M2")  # enum and label references resolve to the catalogue entry
    assert client.name == "antigravity:MODEL_PLACEHOLDER_M2"
    await one_turn(client)
    assert client.describe()["model"] == "gemini-pro-high"
    assert ls.bodies("StartCascade")[0]["customAgentSpec"]["cascadeConfig"]["plannerConfig"]["planModel"] == "MODEL_PLACEHOLDER_M2"
    await client.aclose()


async def test_client_close_cancels_in_flight_work_and_respects_archive_flag():
    ls = FakeLS(replies=[[planner("done")]])
    await client_for(ls).aclose()  # nothing started: no RPCs at all
    assert ls.calls == []
    client = client_for(ls, archive=False)
    await one_turn(client)
    client._in_flight = True  # as if aclose() raced a blocking send
    await client.aclose()
    assert ls.cancelled == ["casc-1"] and ls.annotations["casc-1"] == {"title": "computeruse · Find the release year"}


async def test_language_server_errors_are_model_errors_with_hints():
    client = client_for(FakeLS(token="rotated"))
    with pytest.raises(ModelError, match="401.*invalid CSRF token.*stale"):
        await one_turn(client)
    await client.aclose()

    def refused(request):
        raise httpx.ConnectError("refused")

    ls = LanguageServer(ENDPOINT, transport=httpx.MockTransport(refused))
    with pytest.raises(ModelError, match="unreachable at localhost:5387 \\(GetCascadeModelConfigData\\)"):
        await ls.models()
    await ls.aclose()
    ls = LanguageServer(ENDPOINT, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(ModelError, match="non-JSON"):
        await ls.call("StartCascade", {})
    await ls.aclose()
    ls = LanguageServer(ENDPOINT, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"")))
    assert await ls.call("CancelCascadeInvocation", {}) == {}
    await ls.aclose()


def test_antigravity_usage_has_no_usd_price():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd("antigravity:gemini-pro-high", usage) is None
    assert estimate_cost_usd("antigravity:claude-opus-5-5", usage) is None  # even when a public price exists for the name
    assert estimate_cost_usd("claude-opus-5-5", usage) is not None


# -- discovery + catalog ----------------------------------------------------------------------


async def test_discovery_lists_antigravity_models_without_probing():
    ls = FakeLS()
    disc = ModelDiscovery(projects=[], antigravity=ENDPOINT, transport=ls.transport())
    try:
        res = await disc.run(["antigravity", "vertex"])  # vertex has no project: skipped
    finally:
        await disc.aclose()
    assert ls.methods() == ["GetCascadeModelConfigData"] and res.errors == {}
    assert [m.id for m in res.models] == ["gemini-pro-high", "sonnet-high", "gemini-flash-lite",
                                          "text-only"]
    by = {m.id: m for m in res.models}
    assert all(m.provider == "antigravity" and m.verified and m.source == "antigravity" for m in res.models)
    pro = by["gemini-pro-high"]
    assert pro.available and pro.reason == "listed by Antigravity · quota 50% left" and pro.version == "MODEL_PLACEHOLDER_M2"
    assert pro.label == "Gemini Pro (High)" and pro.order == 0
    assert not by["sonnet-high"].available and by["sonnet-high"].reason == "quota exhausted until 2026-10-05T20:00:00Z"
    assert not by["text-only"].available and "text-only" in by["text-only"].reason
    # Antigravity entries sort after the public-API providers, in Antigravity's own order
    public = DiscoveredModel(id="gemini-3.8-flash", provider="gemini", label="", available=True, reason="")
    assert rank(public) < rank(pro) < rank(by["sonnet-high"])


async def test_discovery_reports_a_dead_language_server_as_a_provider_error():
    def refused(request):
        raise httpx.ConnectError("refused")

    disc = ModelDiscovery(projects=[], antigravity=ENDPOINT, transport=httpx.MockTransport(refused))
    try:
        res = await disc.run(["antigravity"])
    finally:
        await disc.aclose()
    assert res.models == [] and "unreachable" in res.errors["antigravity"]


def catalog_with(tmp_path, ls: FakeLS, **kw) -> ModelCatalog:
    kw.setdefault("anthropic_api_key", None)
    kw.setdefault("gemini_api_key", None)
    kw.setdefault("gcp_project", None)
    factory = kw.pop("discovery_factory", None)
    settings = Settings(data_dir=tmp_path / "data", _env_file=None, **kw)  # type: ignore[call-arg]

    def probe(address, token):
        return detect_antigravity(address, token, env={}, transport=ls.transport())

    cat = ModelCatalog(settings, adc_probe=lambda explicit: (False, explicit, "no ADC (fake)"),
                       discovery_factory=factory, antigravity_probe=probe)
    cat.refresh()
    return cat


def test_catalog_offers_antigravity_models_before_discovery_runs(tmp_path):
    ls = FakeLS()
    cat = catalog_with(tmp_path, ls)
    st = cat.status["antigravity"]
    assert st.available and st.detail == {"address": "localhost:5387", "token_source": "server"}
    assert st.reason.startswith("Antigravity Language Server at localhost:5387 (4 models")
    assert TOKEN not in json.dumps(cat.summary())

    infos = {m.id: m for m in cat.models() if m.provider == "antigravity"}
    assert list(infos) == ["antigravity:gemini-pro-high", "antigravity:gemini-flash-lite",
                           "antigravity:sonnet-high", "antigravity:text-only"]  # available first, Antigravity's order kept
    pro = infos["antigravity:gemini-pro-high"]
    assert pro.available and pro.tool == "json" and pro.source == "antigravity" and not pro.verified
    assert pro.label == "Gemini Pro (High)" and pro.version == "MODEL_PLACEHOLDER_M2"
    assert pro.reason == "listed by Antigravity · quota 50% left"
    assert not infos["antigravity:sonnet-high"].available
    assert infos["antigravity:sonnet-high"].reason == "Antigravity quota for 'Claude Sonnet (High)' is exhausted until 2026-10-05T20:00:00Z"
    assert "cannot take screenshots" in infos["antigravity:text-only"].reason
    assert cat.default_model() == "antigravity:gemini-pro-high"  # configured default is unavailable here

    assert cat.resolve("MODEL_PLACEHOLDER_M2") == ("antigravity", "MODEL_PLACEHOLDER_M2")
    assert cat.check("MODEL_PLACEHOLDER_M2") == (True, "listed by Antigravity · quota 50% left")
    assert cat.check("antigravity:Gemini Pro (High)")[0] is True
    ok, reason = cat.check("antigravity:gemini-4")
    assert not ok and reason.startswith("Antigravity does not list model 'gemini-4'; it offers: gemini-pro-high")
    ok, reason = cat.check("sonnet-high")  # bare Antigravity ids are unknown: they need the prefix
    assert not ok and "unknown model" in reason and "antigravity:<id>" in reason

    client = cat.make("MODEL_PLACEHOLDER_M2")
    assert isinstance(client, AntigravityModelClient) and client.name == "antigravity:gemini-pro-high"
    assert client.archive is True and client.endpoint == cat.antigravity_endpoint
    with pytest.raises(ValueError, match="quota .* is exhausted"):
        cat.make("antigravity:sonnet-high")
    with pytest.raises(ValueError, match="text-only"):
        cat.make("antigravity:text-only")

    cat = catalog_with(tmp_path, ls, antigravity_archive=False)
    assert cat.make("antigravity:gemini-flash-lite").archive is False


def test_provider_preference_gates_the_antigravity_probe(tmp_path):
    ls = FakeLS()
    cat = catalog_with(tmp_path, ls, model_provider="antigravity", anthropic_api_key="sk")
    assert cat.status["antigravity"].available and not cat.status["anthropic"].available
    assert "disabled by COMPUTERUSE_MODEL_PROVIDER=antigravity" in cat.status["anthropic"].reason
    assert {m.provider for m in cat.models() if m.available} == {"antigravity"}

    ls = FakeLS()
    cat = catalog_with(tmp_path, ls, model_provider="gemini", gemini_api_key="g")
    assert ls.calls == [] and ls.gets == 0  # never contacted
    assert not cat.status["antigravity"].available and "disabled by COMPUTERUSE_MODEL_PROVIDER=gemini" in cat.status["antigravity"].reason
    assert cat.check("antigravity:gemini-flash-lite")[0] is False

    cat = catalog_with(tmp_path, FakeLS(index_token=False))
    assert not cat.status["antigravity"].available and cat.status["antigravity"].detail == {}
    assert cat.antigravity_models == [] and not [m for m in cat.models() if m.provider == "antigravity"]
    ok, reason = cat.check("antigravity:gemini-flash-lite")
    assert not ok and reason.startswith("antigravity is unavailable for 'gemini-flash-lite'")


async def test_discovery_receives_the_endpoint_and_takes_over_the_listing(tmp_path):
    ls = FakeLS()
    captured: dict[str, Any] = {}

    class Discovery:
        def __init__(self, **kw):
            captured.update(kw)

        async def run(self, providers):
            real = ModelDiscovery(projects=[], antigravity=captured["antigravity"], transport=ls.transport())
            try:
                return await real.run(providers)
            finally:
                await real.aclose()

        async def aclose(self):
            pass

    cat = catalog_with(tmp_path, ls, discovery_factory=Discovery)
    res = await cat.discover()
    assert captured["antigravity"] == cat.antigravity_endpoint and captured["candidates"] == {"antigravity": []}
    assert res is not None and [m.provider for m in res.models] == ["antigravity"] * 4
    infos = {m.id: m for m in cat.models() if m.provider == "antigravity"}
    assert infos["antigravity:gemini-pro-high"].verified and infos["antigravity:gemini-pro-high"].tool == "json"
    assert cat.discovery_state()["verified"] == 2
    ok, reason = cat.check("antigravity:sonnet-high")
    assert not ok and "not available via antigravity: quota exhausted" in reason
    client = cat.make("antigravity:gemini-flash-lite")
    assert isinstance(client, AntigravityModelClient) and client.name == "antigravity:gemini-flash-lite"
