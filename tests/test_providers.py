"""ModelCatalog: credential detection, model-id resolution, availability, discovery, client construction."""

from __future__ import annotations

import asyncio
import time

import pytest

from computeruse.agent.discovery import DiscoveredModel, DiscoveryResult
from computeruse.agent.gemini import GeminiModelClient
from computeruse.agent.model import AnthropicModelClient
from computeruse.agent.providers import ModelCatalog
from computeruse.agent.vertex import VertexAnthropicModelClient
from computeruse.config import Settings


def adc(usable: bool, project: str | None = "proj"):
    """Fake `detect_adc`: honours an explicit project like the real one, never touches the filesystem."""

    def probe(explicit):
        if not usable:
            return False, explicit, "no Application Default Credentials (fake)"
        return True, explicit or project, f"ADC credentials, project {explicit or project}"

    return probe


def no_antigravity(address, csrf_token):
    """Fake Antigravity probe: never touches a Language Server (the developer's shell may have a real one)."""
    return None, "no Antigravity Language Server at localhost:5387 (fake)", []


def catalog(tmp_path, *, has_adc=False, discovery_factory=None, antigravity_probe=no_antigravity, **kw) -> ModelCatalog:
    kw.setdefault("anthropic_api_key", None)
    kw.setdefault("gemini_api_key", None)
    kw.setdefault("gcp_project", None)
    settings = Settings(data_dir=tmp_path / "data", _env_file=None, **kw)  # type: ignore[call-arg]
    cat = ModelCatalog(settings, adc_probe=adc(has_adc), discovery_factory=discovery_factory,
                       antigravity_probe=antigravity_probe)
    cat.refresh()
    return cat


def available_ids(cat: ModelCatalog) -> list[str]:
    return [m.id for m in cat.models() if m.available]


def test_nothing_configured_means_nothing_available(tmp_path):
    cat = catalog(tmp_path)
    assert not cat.any_available() and cat.default_model() is None
    assert all(not m.available and m.reason for m in cat.models())
    ok, reason = cat.check("claude-sonnet-4-5")
    assert not ok and "vertex is unavailable" in reason and "Application Default Credentials" in reason
    ok, reason = cat.check("gemini-3.8-flash")
    assert not ok and "GEMINI_API_KEY is not set" in reason
    ok, reason = cat.check("gpt-9")
    assert not ok and "unknown model" in reason
    with pytest.raises(ValueError, match="unavailable"):
        cat.make("claude-sonnet-4-5")
    summary = cat.summary()
    assert set(summary["providers"]) == {"anthropic", "vertex", "gemini", "antigravity"}
    assert summary["preference"] == "auto"
    assert "fake" in cat.status["antigravity"].reason and not cat.status["antigravity"].available
    assert "antigravity:<id>" in cat.check("sonnet-high")[1]


def test_adc_only_routes_claude_to_vertex_and_gemini_through_vertex(tmp_path):
    cat = catalog(tmp_path, has_adc=True)
    assert cat.project == "proj"
    assert cat.resolve("claude-opus-5-5") == ("vertex", "claude-opus-5-5")
    assert cat.resolve("gemini-3.8-flash") == ("gemini", "gemini-3.8-flash")
    assert cat.status["anthropic"].available is False
    assert cat.status["gemini"].available and cat.status["gemini"].detail["route"] == "vertex"
    models = {m.id: m for m in cat.models()}
    # anthropic-only suggestions (opus-4-1, haiku) are not listed: claude-* resolves to vertex here
    assert set(models) == {"claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-4-5", "gemini-3.1-pro-preview",
                           "gemini-3.8-flash"}
    assert models["claude-opus-5-5"].tool == "toolset" and models["claude-sonnet-4-5"].tool == "builtin"
    assert models["gemini-3.8-flash"].tool == "schema"
    assert all(m.available and not m.verified and m.source == "suggested" for m in models.values())
    assert cat.default_model() == "claude-sonnet-4-5"  # the configured default is available


def test_anthropic_key_wins_for_claude_ids(tmp_path):
    cat = catalog(tmp_path, anthropic_api_key="sk-test")
    assert cat.resolve("claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")
    assert cat.check("claude-sonnet-4-5") == (True, "API key set")
    ids = [m.id for m in cat.models()]
    assert ids[0] == "claude-sonnet-4-5" and "claude-opus-4-1" in ids and "claude-opus-5-5" not in ids
    # explicit prefixes bypass auto-routing (and report that provider's availability)
    assert cat.resolve("vertex:claude-opus-5-5") == ("vertex", "claude-opus-5-5")
    assert cat.check("vertex:claude-opus-5-5")[0] is False
    assert cat.models()[0].tool == "builtin"


def test_hard_provider_preference_disables_the_others(tmp_path):
    cat = catalog(tmp_path, has_adc=True, anthropic_api_key="sk-test", gemini_api_key="g", model_provider="vertex")
    assert cat.resolve("claude-sonnet-4-5") == ("vertex", "claude-sonnet-4-5")
    assert cat.status["anthropic"].available is False and "disabled by" in cat.status["anthropic"].reason
    assert cat.status["gemini"].available is False
    assert available_ids(cat) == ["claude-sonnet-4-5", "claude-opus-5-5", "claude-sonnet-5"]  # default id sorts first

    cat = catalog(tmp_path, has_adc=True, anthropic_api_key="sk-test", model_provider="gemini")
    ok, reason = cat.check("claude-sonnet-4-5")
    assert not ok and "disabled by COMPUTERUSE_MODEL_PROVIDER=gemini" in reason
    assert available_ids(cat) == ["gemini-3.1-pro-preview", "gemini-3.8-flash"]
    assert cat.default_model() == "gemini-3.1-pro-preview"  # configured default unavailable → first available

    cat = catalog(tmp_path, has_adc=True, anthropic_api_key="sk-test", model_provider="none")
    assert not cat.any_available() and all("none" in st.reason for st in cat.status.values())


def test_gemini_key_takes_precedence_over_vertex_for_gemini(tmp_path):
    cat = catalog(tmp_path, has_adc=True, gemini_api_key="g")
    assert cat.status["gemini"].detail["route"] == "api_key"
    assert cat.status["gemini"].reason == "API key set"


async def test_make_builds_the_right_client(tmp_path):
    cat = catalog(tmp_path, has_adc=True, gemini_api_key="g", anthropic_api_key="sk-test", gcp_location="us-east5",
                  thinking_effort="low")
    anthropic = cat.make("claude-sonnet-4-5")
    assert isinstance(anthropic, AnthropicModelClient) and anthropic.model == "claude-sonnet-4-5"

    vertex = cat.make("vertex:claude-opus-5-5")
    assert isinstance(vertex, VertexAnthropicModelClient)
    assert vertex.describe() == {"provider": "vertex", "model": "claude-opus-5-5", "project": "proj",
                                 "projects": ["proj"],
                                 "locations": ["us-east5", "global", "us-central1", "europe-west1", "europe-west4"],
                                 "tool_mode": "toolset"}
    assert vertex.thinking_effort == "low"
    await vertex.aclose()

    gemini = cat.make("gemini-3.8-flash")
    assert isinstance(gemini, GeminiModelClient) and gemini.route == "api_key" and gemini.thinking_level == "low"
    await gemini.aclose()

    cat = catalog(tmp_path, has_adc=True, vertex_tool_mode="custom")
    via_vertex = cat.make("gemini-3.8-flash")
    assert isinstance(via_vertex, GeminiModelClient) and via_vertex.route == "vertex" and via_vertex.project == "proj"
    await via_vertex.aclose()
    forced = cat.make("claude-opus-5-5")
    assert isinstance(forced, VertexAnthropicModelClient) and forced.tool_mode == "custom"
    assert cat.models()[0].tool == "schema"
    await forced.aclose()


# -- multiple projects + discovery ------------------------------------------------------------


def discovered(**kw) -> DiscoveredModel:
    base = {"label": kw.get("id", "?"), "available": True, "reason": "verified", "verified": True, "source": "suggested"}
    return DiscoveredModel(**{**base, **kw})


class FakeDiscovery:
    """Stands in for ModelDiscovery: records constructor args, returns a canned result (optionally slowly)."""

    calls: list[dict] = []

    def __init__(self, result: DiscoveryResult | Exception, delay: float = 0.0):
        self.result, self.delay = result, delay

    def factory(self):
        def make(**kw):
            FakeDiscovery.calls.append(kw)
            return self

        return make

    async def run(self, providers):
        self.providers = list(providers)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self):
        self.closed = True


def result(models, projects=("a", "b"), probed=True) -> DiscoveryResult:
    now = time.time()
    return DiscoveryResult(models=list(models), projects=list(projects), started_at=now - 1.2, finished_at=now,
                           probed=probed)


def test_multiple_projects_are_parsed_and_passed_to_the_vertex_client(tmp_path):
    cat = catalog(tmp_path, has_adc=True, gcp_project="a, b,a b")
    assert cat.projects == ["a", "b"] and cat.project == "a"
    assert cat.status["vertex"].reason == "ADC credentials, projects a, b"
    assert cat.status["vertex"].detail["projects"] == ["a", "b"]
    client = cat.make("claude-opus-5-5")
    assert isinstance(client, VertexAnthropicModelClient) and client.projects == ["a", "b"]
    assert cat.summary()["projects"] == ["a", "b"]
    assert cat.summary()["discovery"]["status"] == "idle"


async def test_discovery_drives_models_check_and_make(tmp_path):
    FakeDiscovery.calls.clear()
    fake = FakeDiscovery(result([
        discovered(id="claude-opus-5-5", provider="vertex", label="Claude Opus 5.5", project="b",
                   source="vertex-catalog", version="5.5", stage="GA"),
        discovered(id="claude-sonnet-5", provider="vertex", label="Claude Sonnet 5", available=False,
                   reason="not served in project a (404); not served in project b (404)", project="a"),
        discovered(id="claude-sonnet-4-5", provider="vertex", label="Claude Sonnet 4.5", project="a"),
        discovered(id="gemini-3.8-flash", provider="gemini", label="Gemini 3.8 Flash", route="api_key",
                   source="gemini-api"),
        discovered(id="gemini-3.1-pro-preview", provider="gemini", label="Gemini 3.1 Pro", verified=False,
                   reason="unverified: probe failed with 599", route="api_key"),
    ]))
    cat = catalog(tmp_path, has_adc=True, gemini_api_key="g", gcp_project="a,b", discovery_factory=fake.factory())
    res = await cat.discover()
    assert res is fake.result and fake.providers == ["vertex", "gemini"] and fake.closed
    call = FakeDiscovery.calls[0]
    assert call["projects"] == ["a", "b"] and call["gemini_key"] == "g" and call["anthropic_key"] is None
    assert call["probe"] is True and call["candidates"]["vertex"][:3] == ["claude-opus-5-5", "claude-sonnet-5",
                                                                         "claude-sonnet-4-5"]

    ids = [m.id for m in cat.models()]
    # available first, configured default first among those; unverified entries still offered; unavailable last
    assert ids == ["claude-sonnet-4-5", "claude-opus-5-5", "gemini-3.8-flash", "gemini-3.1-pro-preview",
                   "claude-sonnet-5"]
    by_id = {m.id: m for m in cat.models()}
    assert by_id["claude-opus-5-5"].verified and by_id["claude-opus-5-5"].project == "b"
    assert by_id["claude-opus-5-5"].source == "vertex-catalog" and by_id["claude-opus-5-5"].stage == "GA"
    assert not by_id["gemini-3.1-pro-preview"].verified and by_id["gemini-3.1-pro-preview"].available
    assert not by_id["claude-sonnet-5"].available and "404" in by_id["claude-sonnet-5"].reason

    ok, reason = cat.check("claude-sonnet-5")
    assert not ok and "not available via vertex" in reason and "refresh models" in reason
    assert cat.check("claude-sonnet-5@default")[0] is False  # version suffix is matched against the bare id
    assert cat.check("claude-opus-5-5") == (True, "verified")
    assert cat.check("claude-opus-9")[0] is True  # unknown to discovery → allowed, the request will tell
    with pytest.raises(ValueError, match="not available via vertex"):
        cat.make("claude-sonnet-5")

    client = cat.make("claude-opus-5-5")
    assert isinstance(client, VertexAnthropicModelClient) and client.projects == ["b", "a"]  # serving project first
    await client.aclose()
    client = cat.make("claude-sonnet-4-5")
    assert isinstance(client, VertexAnthropicModelClient) and client.projects == ["a", "b"]
    await client.aclose()

    state = cat.discovery_state()
    assert state["status"] == "done" and state["count"] == 5 and state["verified"] == 3
    assert state["projects"] == ["a", "b"] and state["probed"] and state["refreshed_at"] == fake.result.finished_at
    assert cat.default_model() == "claude-sonnet-4-5"


async def test_discovery_is_shared_cached_and_forceable(tmp_path):
    FakeDiscovery.calls.clear()
    fake = FakeDiscovery(result([discovered(id="claude-opus-5-5", provider="vertex", project="a")]), delay=0.05)
    cat = catalog(tmp_path, has_adc=True, discovery_factory=fake.factory())
    first, second = await asyncio.gather(cat.discover(), cat.discover())  # concurrent callers share one run
    assert first is second and len(FakeDiscovery.calls) == 1
    assert await cat.discover() is first and len(FakeDiscovery.calls) == 1  # fresh enough: cached
    assert await cat.discover(force=True) is first and len(FakeDiscovery.calls) == 2  # forced: re-run
    cat.settings.model_discovery_max_age_s = 0
    await cat.discover()
    assert len(FakeDiscovery.calls) == 3  # stale: re-run


async def test_discovery_off_and_failure_fall_back_to_suggestions(tmp_path):
    cat = catalog(tmp_path, has_adc=True, model_discovery="off")
    assert await cat.discover() is None and cat.discovery_state()["status"] == "off"
    assert "claude-opus-5-5" in available_ids(cat)

    fake = FakeDiscovery(RuntimeError("listing exploded"))
    cat = catalog(tmp_path, has_adc=True, discovery_factory=fake.factory())
    assert await cat.discover() is None
    state = cat.discovery_state()
    assert state["status"] == "error" and "listing exploded" in state["errors"]["discovery"]
    assert "claude-opus-5-5" in available_ids(cat)  # suggestions still offered, provider-level availability
    assert cat.check("claude-opus-5-5")[0] is True


async def test_discovered_ids_are_prefixed_when_auto_routing_goes_elsewhere(tmp_path):
    fake = FakeDiscovery(result([
        discovered(id="claude-sonnet-4-5", provider="vertex", project="proj"),
        discovered(id="claude-sonnet-4-5", provider="anthropic", source="anthropic-api"),
        discovered(id="gemini-3.8-flash", provider="gemini", route="vertex", project="proj"),
    ], projects=("proj",)))
    cat = catalog(tmp_path, has_adc=True, anthropic_api_key="sk", discovery_factory=fake.factory())
    await cat.discover()
    ids = [m.id for m in cat.models()]
    assert "claude-sonnet-4-5" in ids and "vertex:claude-sonnet-4-5" in ids and "gemini-3.8-flash" in ids
    assert isinstance(cat.make("vertex:claude-sonnet-4-5"), VertexAnthropicModelClient)
    assert isinstance(cat.make("claude-sonnet-4-5"), AnthropicModelClient)
    gem = cat.make("gemini-3.8-flash")
    assert isinstance(gem, GeminiModelClient) and gem.project == "proj"
    await gem.aclose()
