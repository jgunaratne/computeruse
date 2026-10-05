"""Model discovery: which model ids can *this* deployment actually call?

Listing endpoints say what exists; only a request says what a project may use.
Probed 2026-10: the Vertex publisher catalogue lists twelve Claude models, yet
one project served exactly two of them (`claude-opus-5-5` and
`claude-sonnet-4-5` — the latter not even in the catalogue any more), the
`claude-fable-*` entries answered 403 (data-sharing opt-in) and everything else
404. A second project served `claude-sonnet-5` and nothing else. A per-model
GET is no help: it 404s for served and unserved models alike.

So discovery is two-phase:

1. **Candidates** — union of the provider's listing (if it can be fetched),
   the suggested ids and the configured default, so a model that works but is
   missing from the listing is still offered.
2. **Verification** — one `max_tokens=1` request per (model, project) with a
   small concurrency cap. 200/429 → served (429 = quota is the only problem),
   404 → not served to that project, 403 → access blocked, anything else →
   unverified (offered, but flagged). Cost is negligible (~10 tokens each).

The Gemini Developer API and the Anthropic API scope their listings to the
key, so for those the listing alone is treated as authoritative unless probing
is enabled for them too (it is, by default — the badge then means the same
thing everywhere).

Antigravity (via its local Language Server) is listed, never probed: its
per-user model list already carries entitlement and remaining quota, and every
probe would leave a conversation in the user's Antigravity history.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from computeruse.agent.vertex import (
    ANTHROPIC_VERSION,
    AdcTokenSource,
    TokenSource,
    error_message,
    vertex_host,
)

log = logging.getLogger("computeruse.discovery")

VERTEX_CATALOG = "https://aiplatform.googleapis.com/v1beta1/publishers/{publisher}/models"
GEMINI_LIST = "https://generativelanguage.googleapis.com/v1beta/models"
ANTHROPIC_LIST = "https://api.anthropic.com/v1/models"
ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"

# Gemini variants that are not screenshot-in / function-call-out text models.
_GEMINI_EXCLUDE = ("image", "imagen", "veo", "tts", "audio", "live", "embedding", "transcribe", "translate",
                   "omni", "robotics", "computer-use", "windsurf", "1p-", "exp-", "aqa")
_GEMINI_VERSION = re.compile(r"^gemini-(\d+(?:\.\d+)?)")
_CLAUDE_ID = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?(?:@(\w+))?$")
_CLAUDE_FAMILY_RANK = {"opus": 0, "sonnet": 1, "haiku": 2}
_GEMINI_TIER_RANK = (("pro", 0), ("flash-lite", 2), ("flash", 1))


# -- results -----------------------------------------------------------------------


@dataclass
class DiscoveredModel:
    id: str
    provider: str  # anthropic | vertex | gemini | antigravity
    label: str
    available: bool
    reason: str
    verified: bool = False  # confirmed by a live request
    source: str = "listed"  # vertex-catalog | gemini-api | anthropic-api | antigravity | suggested | configured
    project: str | None = None  # vertex routes: the project that serves it
    route: str | None = None  # gemini: api_key | vertex
    version: str = ""
    stage: str = ""
    probe_ms: float | None = None
    order: int | None = None  # provider-supplied ordering (Antigravity's recommended list), when there is one


@dataclass
class DiscoveryResult:
    models: list[DiscoveredModel]
    projects: list[str]
    started_at: float
    finished_at: float
    probed: bool
    errors: dict[str, str] = field(default_factory=dict)  # provider → listing/probe failure

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.finished_at)

    def find(self, provider: str, model_id: str) -> DiscoveredModel | None:
        for m in self.models:
            if m.provider == provider and m.id == model_id:
                return m
        bare = model_id.split("@", 1)[0]  # claude-sonnet-5@default ≡ claude-sonnet-5
        if bare != model_id:
            return self.find(provider, bare)
        return None


# -- naming / filtering ------------------------------------------------------------------


def claude_label(model_id: str) -> str:
    m = _CLAUDE_ID.match(model_id)
    if not m:
        return model_id
    family, major, minor, suffix = m.groups()
    version = f"{major}.{minor}" if minor else major
    label = f"Claude {family.title()} {version}"
    return f"{label} ({suffix})" if suffix and suffix != "default" else label


def gemini_label(model_id: str, display_name: str | None = None) -> str:
    if display_name and not display_name.lower().startswith("nano"):
        return display_name
    parts = model_id.split("-")
    return " ".join(p.title() if p.isalpha() else p for p in parts)


def is_text_gemini(model_id: str, methods: Iterable[str] | None = None) -> bool:
    """Text/vision Gemini models usable as a computer-use policy (≥ 2.5, no media-specialised variants)."""
    if not model_id.startswith("gemini-"):
        return False
    if any(x in model_id for x in _GEMINI_EXCLUDE):
        return False
    if methods is not None and "generateContent" not in methods:
        return False
    m = _GEMINI_VERSION.match(model_id)
    if m:
        return float(m.group(1)) >= 2.5
    return model_id.endswith("-latest")


def is_claude_text(model_id: str) -> bool:
    return model_id.startswith("claude-")


def rank(model: DiscoveredModel) -> tuple:
    """Newest / strongest first within a provider; stable for unknown shapes.

    Antigravity entries keep the order Antigravity itself recommends (and come after the public-API providers).
    """
    mid = model.id
    if model.provider == "antigravity":
        return (3, model.order if model.order is not None else 10_000, mid)
    cm = _CLAUDE_ID.match(mid)
    if cm:
        family, major, minor, _ = cm.groups()
        return (0, -int(major), -int(minor or 0), _CLAUDE_FAMILY_RANK.get(family, 9), mid)
    gm = _GEMINI_VERSION.match(mid)
    if gm:
        tier = next((r for key, r in _GEMINI_TIER_RANK if key in mid), 3)
        preview = 1 if "preview" in mid else 0
        return (1, -float(gm.group(1)), tier, preview, mid)
    return (2, 0, 0, 0, mid)


def short(message: str, limit: int = 140) -> str:
    """First sentence of an API error message, capped — enough to act on, short enough for a picker tooltip."""
    m = message.strip()
    cut = m.find(". ")
    if 0 < cut < limit:
        m = m[:cut + 1]
    return m[:limit]


def classify(status: int, message: str, ms: float, project: str | None = None, *,
             where: str | None = None) -> tuple[bool, bool, str]:
    """HTTP status of a 1-token probe → (available, verified, reason). `where` overrides "project X"."""
    where = f" in {where}" if where else (f" in project {project}" if project else "")
    if status == 200:
        return True, True, f"verified{where} ({ms:.0f} ms)"
    if status == 429:
        return True, True, f"served{where}, but quota was exhausted at probe time (429)"
    if status == 400:  # auth and routing worked; only the probe body was rejected
        return True, True, f"reachable{where} (probe body rejected: {short(message, 80)})"
    if status == 404:
        return False, True, f"not served{where} (404)"
    if status == 403:
        return False, True, f"access denied{where}: {short(message)}"
    if status == 401:
        return False, True, "credentials rejected (401)"
    return True, False, f"unverified: probe failed with {status} {short(message, 80)}".rstrip()


# -- discovery ---------------------------------------------------------------------------


class ModelDiscovery:
    """Lists and verifies models for the configured providers. One instance per run."""

    def __init__(self, *, projects: list[str], location: str | None = None, anthropic_key: str | None = None,
                 gemini_key: str | None = None, candidates: dict[str, list[str]] | None = None,
                 probe: bool = True, concurrency: int = 6, timeout: float = 30.0,
                 token_source: TokenSource | None = None, antigravity: Any | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.projects = projects
        self.location = location or "global"
        self.anthropic_key = anthropic_key
        self.gemini_key = gemini_key
        self.candidates = {k: list(v) for k, v in (candidates or {}).items()}
        self.probe = probe
        self.antigravity = antigravity  # AntigravityEndpoint of a reachable Language Server, or None
        self._sem = asyncio.Semaphore(concurrency)
        self._token = token_source
        self._transport = transport
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0), transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self) -> dict[str, str]:
        if self._token is None:
            self._token = AdcTokenSource()
        return {"Authorization": f"Bearer {await self._token()}"}

    # -- entry point ---------------------------------------------------------------

    async def run(self, providers: Iterable[str]) -> DiscoveryResult:
        started = time.time()
        wanted = set(providers)
        errors: dict[str, str] = {}
        models: list[DiscoveredModel] = []
        jobs = []
        if "vertex" in wanted and self.projects:
            jobs.append(("vertex", self._vertex_claude(errors)))
        if "gemini" in wanted:
            if self.gemini_key:
                jobs.append(("gemini", self._gemini_api(errors)))
            elif self.projects:
                jobs.append(("gemini", self._gemini_vertex(errors)))
        if "anthropic" in wanted and self.anthropic_key:
            jobs.append(("anthropic", self._anthropic(errors)))
        if "antigravity" in wanted and self.antigravity is not None:
            jobs.append(("antigravity", self._antigravity()))
        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        for (provider, _), res in zip(jobs, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("model discovery for %s failed: %s", provider, res)
                errors[provider] = f"{type(res).__name__}: {res}"
            else:
                models.extend(res)
        models.sort(key=rank)
        return DiscoveryResult(models=models, projects=list(self.projects), started_at=started,
                               finished_at=time.time(), probed=self.probe, errors=errors)

    # -- Antigravity -----------------------------------------------------------------------

    async def _antigravity(self) -> list[DiscoveredModel]:
        """The Language Server's per-user model list, with quota. No probe: a 1-token request would create a
        conversation per model in the user's Antigravity history, and the listing already reflects live entitlement
        and remaining quota, which is what Antigravity's own picker trusts."""
        from computeruse.agent.antigravity import LanguageServer

        ls = LanguageServer(self.antigravity, timeout=30.0, transport=self._transport)
        try:
            listed = await ls.models()
        finally:
            await ls.aclose()
        out: list[DiscoveredModel] = []
        for m in listed:
            if m.disabled:
                continue
            available, reason = True, m.status_note()
            if not m.supports_images:
                available, reason = False, "cannot take screenshots (text-only model)"
            elif m.quota_exhausted:
                available = False
                reason = f"quota exhausted{' until ' + m.quota_reset if m.quota_reset else ''}"
            out.append(DiscoveredModel(id=m.id, provider="antigravity", label=m.label, available=available, reason=reason,
                                       verified=True, source="antigravity", version=m.enum, order=m.order))
        return out

    # -- Claude on Vertex ----------------------------------------------------------------

    async def _vertex_catalog(self, publisher: str) -> list[dict[str, Any]]:
        headers = {**await self._bearer(), "x-goog-user-project": self.projects[0]}
        items: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"pageSize": 200, "listAllVersions": "false"}
            if token:
                params["pageToken"] = token
            resp = await self._http.get(VERTEX_CATALOG.format(publisher=publisher), headers=headers, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"publisher listing {resp.status_code}: {error_message(resp)[:160]}")
            data = resp.json()
            items.extend(data.get("publisherModels") or [])
            token = data.get("nextPageToken")
            if not token:
                break
        return items

    def _merge_candidates(self, provider: str, listed: dict[str, dict[str, Any]], keep,
                          labeler=lambda mid: mid) -> dict[str, DiscoveredModel]:
        """Listing ∪ suggested ∪ configured → id → template record (not yet verified)."""
        out: dict[str, DiscoveredModel] = {}
        for mid, meta in listed.items():
            if keep(mid):
                out[mid] = DiscoveredModel(id=mid, provider=provider, label=meta.get("label") or labeler(mid),
                                           available=True, reason="listed", source=meta.get("source", "listed"),
                                           version=meta.get("version", ""), stage=meta.get("stage", ""))
        for mid in self.candidates.get(provider, []):
            if mid not in out and keep(mid):
                out[mid] = DiscoveredModel(id=mid, provider=provider, label=labeler(mid), available=True,
                                           reason="suggested", source="suggested")
        return out

    async def _vertex_claude(self, errors: dict[str, str]) -> list[DiscoveredModel]:
        listed: dict[str, dict[str, Any]] = {}
        try:
            for item in await self._vertex_catalog("anthropic"):
                mid = item.get("name", "").split("/")[-1]
                listed[mid] = {"label": claude_label(mid), "source": "vertex-catalog",
                               "version": item.get("versionId", ""), "stage": item.get("launchStage", "")}
        except Exception as e:  # listing is best-effort; suggestions are still probed
            errors["vertex"] = f"catalogue unavailable ({e}); probed suggested ids only"
        templates = self._merge_candidates("vertex", listed, is_claude_text, claude_label)
        if not self.probe:
            for t in templates.values():
                t.project = self.projects[0]
                t.reason = f"{t.reason} (not verified)"
            return list(templates.values())
        return await self._verify_across_projects(templates, self._probe_claude)

    async def _verify_across_projects(self, templates: dict[str, DiscoveredModel], probe) -> list[DiscoveredModel]:
        """Probe every (model, project); keep the first project that serves the model."""
        pairs = [(mid, project) for mid in templates for project in self.projects]
        outcomes = await asyncio.gather(*(probe(mid, project) for mid, project in pairs))
        by_model: dict[str, list[tuple[str, tuple[int, str, float]]]] = {}
        for (mid, project), outcome in zip(pairs, outcomes, strict=True):
            by_model.setdefault(mid, []).append((project, outcome))
        results: list[DiscoveredModel] = []
        for mid, tmpl in templates.items():
            verdicts = [(project, *classify(status, message, ms, project), ms, status, message)
                        for project, (status, message, ms) in by_model[mid]]
            served = next((v for v in verdicts if v[1] and v[2]), None)  # available and verified
            unsure = next((v for v in verdicts if not v[2]), None)
            pick = served or unsure or verdicts[0]
            project, available, verified, reason, ms, status, message = pick
            if not served and len(verdicts) > 1 and all(v[2] for v in verdicts):
                # Same verdict everywhere → say it once. 404 bodies quote the per-project resource path, so
                # compare them by status only; 403 bodies carry the actionable detail, so include it.
                if len({(v[5], short(v[6]) if v[5] == 403 else "") for v in verdicts}) == 1:
                    names = ", ".join(v[0] for v in verdicts)
                    reason = classify(status, message, ms, where=f"projects {names}")[2]
                else:
                    reason = "; ".join(v[3] for v in verdicts)
            results.append(DiscoveredModel(id=mid, provider=tmpl.provider, label=tmpl.label, available=available,
                                           reason=reason, verified=verified, source=tmpl.source, project=project,
                                           route="vertex" if tmpl.provider == "gemini" else None,
                                           version=tmpl.version, stage=tmpl.stage, probe_ms=ms))
        return results

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, str, float]:
        async with self._sem:
            t0 = time.perf_counter()
            try:
                resp = await self._http.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                return 599, f"{type(e).__name__}: {e}", (time.perf_counter() - t0) * 1000
            ms = (time.perf_counter() - t0) * 1000
            return resp.status_code, "" if resp.status_code == 200 else error_message(resp), ms

    async def _probe_claude(self, model: str, project: str) -> tuple[int, str, float]:
        url = (f"https://{vertex_host(self.location)}/v1/projects/{project}/locations/{self.location}"
               f"/publishers/anthropic/models/{model}:rawPredict")
        body = {"anthropic_version": ANTHROPIC_VERSION, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        return await self._post(url, await self._bearer(), body)

    # -- Gemini ------------------------------------------------------------------------------

    _GEMINI_PROBE = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                     "generationConfig": {"maxOutputTokens": 1}}

    async def _probe_gemini_vertex(self, model: str, project: str) -> tuple[int, str, float]:
        url = (f"https://{vertex_host(self.location)}/v1/projects/{project}/locations/{self.location}"
               f"/publishers/google/models/{model}:generateContent")
        return await self._post(url, await self._bearer(), self._GEMINI_PROBE)

    async def _gemini_vertex(self, errors: dict[str, str]) -> list[DiscoveredModel]:
        listed: dict[str, dict[str, Any]] = {}
        try:
            for item in await self._vertex_catalog("google"):
                mid = item.get("name", "").split("/")[-1]
                listed[mid] = {"label": gemini_label(mid), "source": "vertex-catalog",
                               "version": item.get("versionId", ""), "stage": item.get("launchStage", "")}
        except Exception as e:
            errors["gemini"] = f"catalogue unavailable ({e}); probed suggested ids only"
        templates = self._merge_candidates("gemini", listed, is_text_gemini)
        if not self.probe:
            for t in templates.values():
                t.project, t.route, t.reason = self.projects[0], "vertex", f"{t.reason} (not verified)"
            return list(templates.values())
        return await self._verify_across_projects(templates, self._probe_gemini_vertex)

    async def _gemini_api(self, errors: dict[str, str]) -> list[DiscoveredModel]:
        headers = {"x-goog-api-key": self.gemini_key or ""}
        listed: dict[str, dict[str, Any]] = {}
        try:
            token: str | None = None
            for _ in range(10):
                params: dict[str, Any] = {"pageSize": 200}
                if token:
                    params["pageToken"] = token
                resp = await self._http.get(GEMINI_LIST, headers=headers, params=params)
                if resp.status_code != 200:
                    raise RuntimeError(f"models.list {resp.status_code}: {error_message(resp)[:160]}")
                data = resp.json()
                for item in data.get("models") or []:
                    mid = item.get("name", "").split("/")[-1]
                    if is_text_gemini(mid, item.get("supportedGenerationMethods")):
                        listed[mid] = {"label": gemini_label(mid, item.get("displayName")), "source": "gemini-api",
                                       "version": item.get("version", "")}
                token = data.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            errors["gemini"] = f"models.list unavailable ({e}); probed suggested ids only"
        templates = self._merge_candidates("gemini", listed, is_text_gemini)
        for t in templates.values():
            t.route = "api_key"
        if not self.probe:
            for t in templates.values():
                t.reason = "listed for this API key (not verified)"
            return list(templates.values())
        outcomes = await asyncio.gather(*(self._post(f"{GEMINI_LIST}/{mid}:generateContent", headers,
                                                     self._GEMINI_PROBE) for mid in templates))
        out = []
        for tmpl, (status, message, ms) in zip(templates.values(), outcomes, strict=True):
            available, verified, reason = classify(status, message, ms)
            out.append(DiscoveredModel(id=tmpl.id, provider="gemini", label=tmpl.label, available=available,
                                       reason=reason, verified=verified, source=tmpl.source, route="api_key",
                                       version=tmpl.version, probe_ms=ms))
        return out

    # -- Anthropic API ---------------------------------------------------------------------------

    async def _anthropic(self, errors: dict[str, str]) -> list[DiscoveredModel]:
        headers = {"x-api-key": self.anthropic_key or "", "anthropic-version": "2023-06-01"}
        listed: dict[str, dict[str, Any]] = {}
        try:
            after: str | None = None
            for _ in range(10):
                params: dict[str, Any] = {"limit": 100}
                if after:
                    params["after_id"] = after
                resp = await self._http.get(ANTHROPIC_LIST, headers=headers, params=params)
                if resp.status_code != 200:
                    raise RuntimeError(f"models list {resp.status_code}: {error_message(resp)[:160]}")
                data = resp.json()
                for item in data.get("data") or []:
                    mid = item.get("id", "")
                    if is_claude_text(mid):
                        listed[mid] = {"label": item.get("display_name") or claude_label(mid), "source": "anthropic-api"}
                if not data.get("has_more"):
                    break
                after = data.get("last_id")
        except Exception as e:
            errors["anthropic"] = f"models list unavailable ({e}); probed suggested ids only"
        templates = self._merge_candidates("anthropic", listed, is_claude_text)
        if not self.probe:
            for t in templates.values():
                t.reason = "listed for this API key (not verified)"
            return list(templates.values())
        outcomes = await asyncio.gather(*(self._post(
            ANTHROPIC_MESSAGES, {**headers, "content-type": "application/json"},
            {"model": mid, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}) for mid in templates))
        out = []
        for tmpl, (status, message, ms) in zip(templates.values(), outcomes, strict=True):
            available, verified, reason = classify(status, message, ms)
            out.append(DiscoveredModel(id=tmpl.id, provider="anthropic", label=tmpl.label, available=available,
                                       reason=reason, verified=verified, source=tmpl.source, probe_ms=ms))
        return out
