"""In-memory Antigravity Language Server used by the provider and chat tests.

`FakeLS` sits behind `httpx.MockTransport` and mirrors the Connect-JSON shapes observed live
(2026-10): CSRF check, model list, scripted planner replies per user message, paged trajectories,
annotations and cancellation. No socket is ever opened. Model names are fictional.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx

from computeruse.agent.antigravity import CSRF_HEADER, AntigravityEndpoint

TOKEN = "tok-123"
ENDPOINT = AntigravityEndpoint(address="localhost:5387", csrf_token=TOKEN, token_source="server")
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
        self.busy = 0  # number of upcoming user messages to refuse with the live server's "executor busy" error
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
        if self.busy > 0:
            self.busy -= 1
            return httpx.Response(500, json={"code": "unknown", "message": "executor has not processed the previous "
                                                                              "input yet (error ID: 51d3e37242f4)"})
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


class SlowLS(FakeLS):
    """A `FakeLS` whose user messages block until `release()` is called (or the client cancels).

    `httpx.MockTransport` awaits coroutine handlers under `AsyncClient`, so the blocking send can be
    parked on an `asyncio.Event` while the test pokes the server from the outside.
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()  # set once a send is parked on the gate

    def release(self) -> None:
        self.gate.set()

    async def __call__(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        if request.url.path.endswith("/SendUserCascadeMessage"):
            self.waiting.set()
            await self.gate.wait()
        return super().__call__(request)

    def rpc_CancelCascadeInvocation(self, body):  # noqa: N802
        self.gate.set()  # the real server finishes the blocking call with whatever it has
        return super().rpc_CancelCascadeInvocation(body)
