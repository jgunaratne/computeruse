"""ModelDiscovery against `httpx.MockTransport`: listings ∪ candidates, per-(model, project) probes, verdicts.

No network, no credentials: a small URL router plays the provider endpoints.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import pytest
from conftest import fake_token

from computeruse.agent.discovery import (
    DiscoveredModel,
    DiscoveryResult,
    ModelDiscovery,
    classify,
    claude_label,
    gemini_label,
    is_text_gemini,
    rank,
)


class Router:
    """MockTransport handler: first matching rule answers; everything else is a loud 500."""

    def __init__(self) -> None:
        self.rules: list[tuple[Callable[[httpx.Request], bool], Callable[[httpx.Request], httpx.Response]]] = []
        self.requests: list[httpx.Request] = []

    def get(self, path_part: str, payload, status: int = 200) -> None:
        self.rules.append((lambda r, p=path_part: r.method == "GET" and p in str(r.url),
                           lambda r, s=status, pl=payload: httpx.Response(s, json=pl)))

    def post(self, path_part: str, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.rules.append((lambda r, p=path_part: r.method == "POST" and p in str(r.url), responder))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for pred, responder in self.rules:
            if pred(request):
                return responder(request)
        return httpx.Response(500, json={"error": {"message": f"unrouted {request.method} {request.url}"}})

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def err(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


def ok() -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}], "usage": {"output_tokens": 1}})


def vertex_probe(table: dict[tuple[str, str], int | Exception]):
    """rawPredict/generateContent responder keyed by (model, project)."""

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        project = path.split("/projects/")[1].split("/")[0]
        model = path.split("/models/")[1].split(":")[0]
        outcome = table.get((model, project), 404)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == 200:
            return ok()
        if outcome == 403:
            return err(403, f"requires data sharing to be enabled for publisher 'anthropic' in {project}")
        return err(outcome, f"{model} not found in {project}" if outcome == 404 else f"status {outcome}")

    return respond


def discovery(router: Router, **kw) -> ModelDiscovery:
    kw.setdefault("projects", ["a", "b"])
    kw.setdefault("token_source", fake_token)
    return ModelDiscovery(transport=httpx.MockTransport(router), timeout=5, **kw)


def publisher_listing(*ids: str, stage: str = "GA") -> dict:
    return {"publisherModels": [{"name": f"publishers/x/models/{mid}", "versionId": mid.rsplit("-", 1)[-1],
                                 "launchStage": stage} for mid in ids]}


def by_id(models: list[DiscoveredModel]) -> dict[str, DiscoveredModel]:
    return {m.id: m for m in models}


# -- pure helpers ---------------------------------------------------------------------------


def test_labels():
    assert claude_label("claude-opus-5-5") == "Claude Opus 5.5"
    assert claude_label("claude-sonnet-5") == "Claude Sonnet 5"
    assert claude_label("claude-sonnet-5@default") == "Claude Sonnet 5"
    assert claude_label("claude-sonnet-4-5@20250929") == "Claude Sonnet 4.5 (20250929)"
    assert claude_label("claude-fable-5-1") == "Claude Fable 5.1"
    assert claude_label("weird") == "weird"
    assert gemini_label("gemini-3.8-flash") == "Gemini 3.8 Flash"
    assert gemini_label("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview") == "Gemini 3.1 Pro Preview"
    assert gemini_label("gemini-3.1-pro-preview", "Nano Banana") == "Gemini 3.1 Pro Preview"  # codenames ignored


def test_text_gemini_filter():
    assert is_text_gemini("gemini-3.8-flash")
    assert is_text_gemini("gemini-2.5-pro", ["generateContent"])
    assert is_text_gemini("gemini-pro-latest")
    assert not is_text_gemini("gemini-2.0-flash")  # too old for screenshot-driven control
    assert not is_text_gemini("gemini-2.5-flash-image")
    assert not is_text_gemini("gemini-3.5-flash-tts")
    assert not is_text_gemini("gemini-2.5-computer-use-preview-10-2025")  # different protocol
    assert not is_text_gemini("gemini-3.8-flash", ["embedContent"])
    assert not is_text_gemini("embedding-001")
    assert not is_text_gemini("gemini-embedding-001")


def test_rank_puts_newest_strongest_first():
    ids = ["gemini-3.8-flash", "claude-sonnet-4-5", "claude-opus-5-5", "gemini-3.1-pro-preview", "claude-sonnet-5",
           "claude-haiku-4-5", "gemini-3.8-flash-lite", "mystery", "claude-fable-5"]
    models = [DiscoveredModel(id=i, provider="x", label=i, available=True, reason="") for i in ids]
    assert [m.id for m in sorted(models, key=rank)] == [
        "claude-opus-5-5", "claude-sonnet-5", "claude-fable-5", "claude-sonnet-4-5", "claude-haiku-4-5",
        "gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-3.1-pro-preview", "mystery"]


def test_classify_table():
    assert classify(200, "", 120.0, "p") == (True, True, "verified in project p (120 ms)")
    assert classify(429, "quota", 5.0) == (True, True, "served, but quota was exhausted at probe time (429)")
    assert classify(400, "bad body", 5.0)[:2] == (True, True)
    assert classify(404, "nf", 5.0, "p") == (False, True, "not served in project p (404)")
    assert classify(403, "denied", 5.0, "p") == (False, True, "access denied in project p: denied")
    assert classify(401, "", 5.0) == (False, True, "credentials rejected (401)")
    available, verified, reason = classify(599, "ConnectError: boom", 5.0)
    assert available and not verified and reason.startswith("unverified")


def test_find_matches_version_suffixes():
    res = DiscoveryResult(models=[DiscoveredModel(id="claude-sonnet-5", provider="vertex", label="", available=True,
                                                  reason="")], projects=["a"], started_at=0, finished_at=1,
                          probed=True)
    assert res.find("vertex", "claude-sonnet-5@default") is res.models[0]
    assert res.find("vertex", "claude-sonnet-5@20260101") is res.models[0]
    assert res.find("anthropic", "claude-sonnet-5") is None
    assert res.find("vertex", "claude-opus-5-5") is None
    assert res.duration_ms == 1000 and res.age_s > 0


# -- Claude on Vertex ------------------------------------------------------------------------


async def test_vertex_claude_unions_catalogue_with_candidates_and_picks_the_serving_project():
    router = Router()
    router.get("/publishers/anthropic/models",
               publisher_listing("claude-opus-5-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5"))
    router.post(":rawPredict", vertex_probe({
        ("claude-opus-5-5", "a"): 200, ("claude-sonnet-5", "b"): 200, ("claude-sonnet-4-5", "a"): 200,
        ("claude-fable-5", "a"): 403, ("claude-fable-5", "b"): 403,
    }))
    disc = discovery(router, candidates={"vertex": ["claude-sonnet-4-5", "claude-opus-5-5"]})
    res = await disc.run(["vertex"])
    await disc.aclose()

    listing = next(r for r in router.requests if r.method == "GET")
    assert listing.headers["x-goog-user-project"] == "a" and listing.headers["Authorization"] == "Bearer tok"
    assert listing.url.params["listAllVersions"] == "false"
    probe = json.loads(router.posts()[0].content)
    assert probe["max_tokens"] == 1 and probe["anthropic_version"] == "vertex-2023-10-16"
    assert len(router.posts()) == 5 * 2  # every (model, project) pair, once

    assert res.errors == {} and res.probed and res.projects == ["a", "b"]
    assert [m.id for m in res.models] == ["claude-opus-5-5", "claude-sonnet-5", "claude-fable-5", "claude-sonnet-4-5",
                                          "claude-haiku-4-5"]
    m = by_id(res.models)
    opus = m["claude-opus-5-5"]
    assert opus.available and opus.verified and opus.project == "a" and opus.source == "vertex-catalog"
    assert opus.label == "Claude Opus 5.5" and opus.stage == "GA" and opus.probe_ms is not None
    assert opus.reason.startswith("verified in project a")
    sonnet5 = m["claude-sonnet-5"]
    assert sonnet5.available and sonnet5.project == "b"  # a 404s, b serves → b
    s45 = m["claude-sonnet-4-5"]
    assert s45.available and s45.project == "a" and s45.source == "suggested"  # not catalogued, still probed
    fable = m["claude-fable-5"]
    assert not fable.available and fable.verified
    assert "access denied in project a" in fable.reason and "access denied in project b" in fable.reason
    haiku = m["claude-haiku-4-5"]
    assert not haiku.available and haiku.reason == "not served in projects a, b (404)"  # identical verdicts collapse


async def test_vertex_listing_failure_still_probes_candidates():
    router = Router()
    router.get("/publishers/anthropic/models", {"error": {"message": "Permission denied"}}, status=403)
    router.post(":rawPredict", vertex_probe({("claude-opus-5-5", "a"): 200}))
    disc = discovery(router, projects=["a"], candidates={"vertex": ["claude-opus-5-5", "claude-sonnet-5"]})
    res = await disc.run(["vertex"])
    await disc.aclose()
    assert "catalogue unavailable" in res.errors["vertex"] and "403" in res.errors["vertex"]
    m = by_id(res.models)
    assert m["claude-opus-5-5"].available and m["claude-opus-5-5"].project == "a"
    assert not m["claude-sonnet-5"].available and m["claude-sonnet-5"].reason == "not served in project a (404)"


async def test_list_mode_skips_probes_and_marks_everything_unverified():
    router = Router()
    router.get("/publishers/anthropic/models", publisher_listing("claude-opus-5-5"))
    disc = discovery(router, probe=False, candidates={"vertex": ["claude-sonnet-4-5"]})
    res = await disc.run(["vertex"])
    await disc.aclose()
    assert not router.posts() and not res.probed
    m = by_id(res.models)
    assert m["claude-opus-5-5"].available and not m["claude-opus-5-5"].verified
    assert m["claude-opus-5-5"].reason == "listed (not verified)" and m["claude-opus-5-5"].project == "a"
    assert m["claude-sonnet-4-5"].reason == "suggested (not verified)"


async def test_transport_errors_make_a_model_unverified_not_unavailable():
    router = Router()
    router.get("/publishers/anthropic/models", publisher_listing("claude-opus-5-5"))
    router.post(":rawPredict", vertex_probe({("claude-opus-5-5", "a"): httpx.ConnectError("boom")}))
    disc = discovery(router, projects=["a"])
    res = await disc.run(["vertex"])
    await disc.aclose()
    m = res.models[0]
    assert m.available and not m.verified and "unverified" in m.reason and "ConnectError" in m.reason


async def test_provider_job_crash_is_reported_not_raised():
    async def broken_token() -> str:
        raise RuntimeError("no ADC here")

    router = Router()
    disc = discovery(router, projects=["a"], token_source=broken_token, candidates={"vertex": ["claude-opus-5-5"]})
    res = await disc.run(["vertex", "gemini"])  # gemini via vertex too: both jobs fail on the token
    await disc.aclose()
    assert res.models == [] and "no ADC here" in res.errors["vertex"] and "no ADC here" in res.errors["gemini"]


async def test_nothing_to_do_without_credentials():
    disc = discovery(Router(), projects=[])
    res = await disc.run(["vertex", "gemini", "anthropic"])
    await disc.aclose()
    assert res.models == [] and res.errors == {}


# -- Gemini -----------------------------------------------------------------------------


GEMINI_LISTING = {"models": [
    {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "version": "3.8",
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.1-pro-preview", "displayName": "Gemini 3.1 Pro Preview", "version": "3.1",
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/gemini-2.5-computer-use-preview-10-2025", "supportedGenerationMethods": ["generateContent"]},
]}


async def test_gemini_api_key_route_lists_filters_and_probes():
    router = Router()
    router.get("generativelanguage.googleapis.com/v1beta/models?", GEMINI_LISTING)

    def respond(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        return ok() if model == "gemini-3.8-flash" else err(429, "quota exceeded")

    router.post(":generateContent", respond)
    disc = discovery(router, projects=[], gemini_key="g-key", candidates={"gemini": ["gemini-3.1-pro-preview"]})
    res = await disc.run(["gemini"])
    await disc.aclose()

    listing = next(r for r in router.requests if r.method == "GET")
    assert listing.headers["x-goog-api-key"] == "g-key" and "Authorization" not in listing.headers
    assert json.loads(router.posts()[0].content)["generationConfig"] == {"maxOutputTokens": 1}
    assert [m.id for m in res.models] == ["gemini-3.8-flash", "gemini-3.1-pro-preview"]  # filtered + ranked
    m = by_id(res.models)
    flash = m["gemini-3.8-flash"]
    assert flash.available and flash.verified and flash.route == "api_key" and flash.project is None
    assert flash.label == "Gemini 3.8 Flash" and flash.source == "gemini-api" and flash.version == "3.8"
    pro = m["gemini-3.1-pro-preview"]
    assert pro.available and pro.verified and "quota" in pro.reason


async def test_gemini_via_vertex_when_no_api_key():
    router = Router()
    router.get("/publishers/google/models", publisher_listing("gemini-3.8-flash", "gemini-2.5-flash-image",
                                                                "gemini-2.0-flash"))
    router.post(":generateContent", vertex_probe({("gemini-3.8-flash", "b"): 200}))
    disc = discovery(router, candidates={"gemini": []})
    res = await disc.run(["gemini"])
    await disc.aclose()
    assert [m.id for m in res.models] == ["gemini-3.8-flash"]
    m = res.models[0]
    assert m.available and m.route == "vertex" and m.project == "b" and m.source == "vertex-catalog"
    assert all("/publishers/google/models/gemini-3.8-flash:generateContent" in str(r.url) for r in router.posts())


# -- Anthropic API --------------------------------------------------------------------------


async def test_anthropic_api_paginates_listing_and_probes_messages():
    router = Router()
    pages = {
        None: {"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}], "has_more": True,
               "last_id": "claude-sonnet-4-5"},
        "claude-sonnet-4-5": {"data": [{"id": "claude-opus-4-1", "display_name": "Claude Opus 4.1"}],
                              "has_more": False},
    }
    router.rules.append((lambda r: r.method == "GET" and "api.anthropic.com/v1/models" in str(r.url),
                         lambda r: httpx.Response(200, json=pages[r.url.params.get("after_id")])))

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 1 and request.headers["x-api-key"] == "sk-test"
        return ok() if body["model"] != "claude-haiku-4-5" else err(404, "model: claude-haiku-4-5")

    router.post("/v1/messages", respond)
    disc = discovery(router, projects=[], anthropic_key="sk-test", candidates={"anthropic": ["claude-haiku-4-5"]})
    res = await disc.run(["anthropic"])
    await disc.aclose()
    assert [m.id for m in res.models] == ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"]  # 4.5 > 4.1
    m = by_id(res.models)
    assert m["claude-sonnet-4-5"].available and m["claude-sonnet-4-5"].source == "anthropic-api"
    assert m["claude-sonnet-4-5"].label == "Claude Sonnet 4.5" and m["claude-sonnet-4-5"].project is None
    assert not m["claude-haiku-4-5"].available and m["claude-haiku-4-5"].reason == "not served (404)"


async def test_run_only_touches_requested_providers():
    router = Router()
    router.get("/publishers/anthropic/models", publisher_listing("claude-opus-5-5"))
    router.post(":rawPredict", vertex_probe({("claude-opus-5-5", "a"): 200}))
    disc = discovery(router, projects=["a"], gemini_key="g", anthropic_key="sk")
    t0 = time.time()
    res = await disc.run(["vertex"])
    await disc.aclose()
    assert {m.provider for m in res.models} == {"vertex"}
    assert all("aiplatform.googleapis.com" in str(r.url) for r in router.requests)
    assert res.started_at >= t0 - 1 and res.finished_at >= res.started_at


@pytest.mark.parametrize("bad", ["gemini-3.8-flash-image", "claude"])
def test_candidate_filters_apply_to_suggestions_too(bad):
    disc = ModelDiscovery(projects=["a"], token_source=fake_token, candidates={"vertex": [bad], "gemini": [bad]})
    assert disc._merge_candidates("gemini", {}, is_text_gemini) == {}
    assert "claude" not in disc._merge_candidates("vertex", {}, lambda mid: mid.startswith("claude-"))
