"""Which models can run here, and how to build a client for one.

Resolution (first match wins):

* explicit prefix `anthropic:` / `vertex:` / `gemini:` / `antigravity:`  → that provider
* `claude-*`  → `anthropic` when an Anthropic key is set (and the preference
                 is not `vertex`), otherwise `vertex` (ADC + GCP project)
* `gemini-*`  → `gemini` via `GEMINI_API_KEY`, else via Vertex (ADC + project)
* `MODEL_PLACEHOLDER_*` (an Antigravity model enum) → `antigravity`

Antigravity ids are always shown and passed with the `antigravity:` prefix: bare, a
`gemini-*` id would route to the Gemini API and anything else nowhere.

Availability has two layers:

* **provider status** — are the credentials there? Synchronous (`refresh()`),
  computed once at startup. For Antigravity this is a reachability check of the
  local Language Server, which also returns its model list.
* **discovery** — which model ids those credentials can *actually* call.
  Asynchronous (`await discover()`), runs in the background after startup and
  on demand (UI refresh button, `computeruse models`); see `discovery.py` for
  why a listing alone is not enough. Until it has run, `models()` returns the
  suggested ids with provider-level availability only.

`scripted` and `replay:<id>` never reach this module; the orchestrator builds
those offline clients itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from computeruse.agent.antigravity import (
    AntigravityDetection,
    AntigravityEndpoint,
    AntigravityModel,
    detect_antigravity,
    find_model,
)
from computeruse.agent.discovery import DiscoveryResult, ModelDiscovery
from computeruse.agent.model import AnthropicModelClient, ModelClient
from computeruse.agent.vertex import default_tool_mode, detect_adc
from computeruse.config import Settings

log = logging.getLogger("computeruse.providers")

PROVIDERS = ("anthropic", "vertex", "gemini", "antigravity")
_PREFIX_RE = re.compile(r"^(anthropic|vertex|gemini|antigravity):(.+)$")
_ANTIGRAVITY_ENUM_RE = re.compile(r"^MODEL_[A-Z0-9_]+$")  # Antigravity's wire-level model names (MODEL_PLACEHOLDER_M<n>)

# Suggested ids per provider: what the picker shows before discovery has run, and what discovery always
# probes even when a listing omits them (claude-sonnet-4-5 works on Vertex but is no longer catalogued).
# Antigravity has no fixed list: its models come from the Language Server at detection time.
SUGGESTED: dict[str, list[tuple[str, str]]] = {
    "vertex": [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5", "Claude Sonnet 5"),
               ("claude-sonnet-4-5", "Claude Sonnet 4.5")],
    "anthropic": [("claude-sonnet-4-5", "Claude Sonnet 4.5"), ("claude-opus-4-1", "Claude Opus 4.1"),
                  ("claude-haiku-4-5", "Claude Haiku 4.5")],
    "gemini": [("gemini-3.1-pro-preview", "Gemini 3.1 Pro"), ("gemini-3.8-flash", "Gemini 3.8 Flash")],
    "antigravity": [],
}


@dataclass
class ProviderStatus:
    available: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelInfo:
    id: str  # what to pass to make(): the bare id, or provider-prefixed when the bare id would route elsewhere
    label: str
    provider: str
    available: bool
    reason: str = ""
    tool: str = ""  # builtin | toolset | schema | json — how the computer tool reaches the model
    verified: bool = False  # availability confirmed by a live request (discovery), not just inferred
    source: str = "suggested"  # suggested | configured | vertex-catalog | gemini-api | anthropic-api | antigravity
    project: str | None = None  # vertex routes: the GCP project that serves the model
    version: str = ""
    stage: str = ""


AdcProbe = Callable[[str | None], tuple[bool, str | None, str]]
# (address, csrf token) → (endpoint or None, reason, listed models). Injectable so tests never touch a server.
AntigravityProbe = Callable[[str | None, str | None], AntigravityDetection]
DiscoveryFactory = Callable[..., Any]  # ModelDiscovery-like: .run(providers) and .aclose()


def probe_antigravity(address: str | None, csrf_token: str | None) -> AntigravityDetection:
    """Default Antigravity probe: reachability + the server's model list (see `antigravity.detect_antigravity`)."""
    return detect_antigravity(address, csrf_token)


class ModelCatalog:
    """Provider detection + model discovery + model-id resolution + client construction."""

    def __init__(self, settings: Settings, adc_probe: AdcProbe = detect_adc,
                 discovery_factory: DiscoveryFactory | None = None,
                 antigravity_probe: AntigravityProbe = probe_antigravity) -> None:
        self.settings = settings
        self._adc_probe = adc_probe
        self._antigravity_probe = antigravity_probe
        self._discovery_factory: DiscoveryFactory = discovery_factory or ModelDiscovery
        self._status: dict[str, ProviderStatus] | None = None
        self.project: str | None = None  # primary GCP project (first configured / ADC default)
        self.projects: list[str] = []  # every project Vertex requests may be routed to
        self.antigravity_endpoint: AntigravityEndpoint | None = None  # reachable Antigravity Language Server, if any
        self.antigravity_models: list[AntigravityModel] = []  # its model list at detection time (shown until discovery runs)
        self.discovery: DiscoveryResult | None = None
        self.discovery_error: str | None = None
        self._discovery_task: asyncio.Task[DiscoveryResult | None] | None = None

    # -- detection ---------------------------------------------------------------

    def refresh(self) -> dict[str, ProviderStatus]:
        """Detect credentials. Synchronous (may touch the filesystem / localhost); call off the loop at startup."""
        s = self.settings
        pref = s.model_provider
        if pref == "none":
            off = ProviderStatus(False, "disabled by COMPUTERUSE_MODEL_PROVIDER=none")
            self._status = {p: off for p in PROVIDERS}
            return self._status
        anthropic_key = s.resolved_api_key()
        gemini_key = s.resolved_gemini_key()
        configured = s.gcp_projects()
        adc_ok, project, adc_reason = self._adc_probe(configured[0] if configured else None)
        self.projects = list(dict.fromkeys(p for p in [project, *configured] if p))
        self.project = self.projects[0] if self.projects else None
        if adc_ok and len(self.projects) > 1:
            adc_reason = f"ADC credentials, projects {', '.join(self.projects)}"
        if pref in ("auto", "antigravity"):
            self.antigravity_endpoint, antigravity_reason, self.antigravity_models = self._antigravity_probe(s.antigravity_address,
                                                                                         s.antigravity_csrf_token)
        else:
            self.antigravity_endpoint, antigravity_reason, self.antigravity_models = None, "", []
        status = {
            "anthropic": ProviderStatus(bool(anthropic_key), "API key set" if anthropic_key
                                        else "ANTHROPIC_API_KEY is not set"),
            "vertex": ProviderStatus(adc_ok, adc_reason, {"project": self.project, "projects": self.projects,
                                                          "location": s.gcp_location}),
            "gemini": ProviderStatus(
                bool(gemini_key) or adc_ok,
                "API key set" if gemini_key else (f"via Vertex ({adc_reason})" if adc_ok
                                                  else f"GEMINI_API_KEY is not set and {adc_reason}"),
                {"route": "api_key" if gemini_key else "vertex"}),
            "antigravity": ProviderStatus(self.antigravity_endpoint is not None, antigravity_reason,
                                     self.antigravity_endpoint.public() if self.antigravity_endpoint else {}),
        }
        if pref in PROVIDERS:  # a hard preference disables the others
            for p in PROVIDERS:
                if p != pref:
                    status[p] = ProviderStatus(False, f"disabled by COMPUTERUSE_MODEL_PROVIDER={pref}")
        self._status = status
        return status

    @property
    def status(self) -> dict[str, ProviderStatus]:
        if self._status is None:
            self.refresh()
        return self._status  # type: ignore[return-value]

    # -- discovery -------------------------------------------------------------------

    def _candidate_ids(self, provider: str) -> list[str]:
        """Suggested ids plus the configured default: probed even if the provider's listing omits them."""
        ids = [mid for mid, _ in SUGGESTED[provider]]
        resolved, bare = self.resolve(self.settings.model)
        if resolved == provider and bare not in ids:
            ids.append(bare)
        return ids

    async def discover(self, force: bool = False) -> DiscoveryResult | None:
        """Run (or reuse) model discovery. Concurrent callers share one run; `force` bypasses the cache.

        Returns None when discovery is off, or when it failed and there is no earlier result to fall back to.
        """
        if self.settings.model_discovery == "off":
            return None
        task = self._discovery_task
        if task is not None and not task.done():
            return await asyncio.shield(task)
        if self.discovery is not None and not force and self.discovery.age_s < self.settings.model_discovery_max_age_s:
            return self.discovery
        self._discovery_task = asyncio.create_task(self._run_discovery(), name="model-discovery")
        return await asyncio.shield(self._discovery_task)

    async def _run_discovery(self) -> DiscoveryResult | None:
        s = self.settings
        providers = [p for p in PROVIDERS if self.status[p].available]
        disc = self._discovery_factory(
            projects=list(self.projects), location=s.gcp_location,
            anthropic_key=s.resolved_api_key() if "anthropic" in providers else None,
            gemini_key=s.resolved_gemini_key() if "gemini" in providers else None,
            candidates={p: self._candidate_ids(p) for p in providers}, probe=s.model_discovery == "probe",
            antigravity=self.antigravity_endpoint if "antigravity" in providers else None,
        )
        try:
            result: DiscoveryResult = await disc.run(providers)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("model discovery failed")
            self.discovery_error = f"{type(e).__name__}: {e}"
            return self.discovery  # keep the previous result if there was one
        finally:
            with contextlib.suppress(Exception):
                await disc.aclose()
        self.discovery, self.discovery_error = result, None
        usable = [m for m in result.models if m.available]
        log.info("model discovery: %d usable of %d candidates (%d verified) in %.0f ms; projects=%s%s",
                 len(usable), len(result.models), sum(1 for m in usable if m.verified), result.duration_ms,
                 self.projects, f"; errors={result.errors}" if result.errors else "")
        return result

    def discovery_state(self) -> dict[str, Any]:
        """What the UI shows next to the picker: mode, whether a run is in flight, when it last finished."""
        mode = self.settings.model_discovery
        running = self._discovery_task is not None and not self._discovery_task.done()
        res = self.discovery
        if mode == "off":
            status = "off"
        elif running:
            status = "running"
        elif res is not None:
            status = "done"
        elif self.discovery_error:
            status = "error"
        else:
            status = "idle"
        errors = dict(res.errors) if res else {}
        if self.discovery_error:
            errors["discovery"] = self.discovery_error
        return {
            "mode": mode, "status": status, "probed": res.probed if res else mode == "probe",
            "refreshed_at": res.finished_at if res else None,
            "duration_ms": round(res.duration_ms) if res else None,
            "projects": list(self.projects), "errors": errors,
            "count": len(res.models) if res else 0,
            "verified": sum(1 for m in res.models if m.available and m.verified) if res else 0,
            "max_age_s": self.settings.model_discovery_max_age_s,
        }

    async def close(self) -> None:
        task = self._discovery_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- resolution ----------------------------------------------------------------

    def resolve(self, model_id: str) -> tuple[str, str]:
        """→ (provider, bare model id). provider may be 'unknown'."""
        m = _PREFIX_RE.match(model_id)
        if m:
            return m.group(1), m.group(2)
        pref = self.settings.model_provider
        if model_id.startswith("claude"):
            if pref == "vertex":
                return "vertex", model_id
            if pref == "anthropic" or self.status["anthropic"].available:
                return "anthropic", model_id
            return "vertex", model_id
        if model_id.startswith("gemini"):
            return "gemini", model_id
        if _ANTIGRAVITY_ENUM_RE.match(model_id):
            return "antigravity", model_id
        return "unknown", model_id

    def display_id(self, provider: str, bare: str) -> str:
        """The id a user passes to reach `bare` on `provider`: prefixed only when auto-routing would go elsewhere."""
        return bare if self.resolve(bare)[0] == provider else f"{provider}:{bare}"

    def check(self, model_id: str) -> tuple[bool, str]:
        provider, bare = self.resolve(model_id)
        if provider == "unknown":
            return False, (f"unknown model {model_id!r}: use a claude-* or gemini-* id, 'antigravity:<id>', 'scripted', "
                           "or 'replay:<session_id>'")
        st = self.status[provider]
        if not st.available:
            return False, f"{provider} is unavailable for {bare!r}: {st.reason}"
        rec = self.discovery.find(provider, bare) if self.discovery else None
        if rec is None:
            if provider == "antigravity":
                return self._check_antigravity(bare, st)
            return True, st.reason  # not discovered: allowed, the request itself will say if the id is wrong
        if rec.verified and not rec.available:
            return False, f"{bare!r} is not available via {provider}: {rec.reason} (refresh models if access changed)"
        return True, rec.reason

    def _check_antigravity(self, ref: str, st: ProviderStatus) -> tuple[bool, str]:
        """Validate an Antigravity reference (id, label or enum) against the server's list from detection time.

        The Language Server silently accepts unknown model enums, so "the request will tell" does not hold;
        the per-user list already encodes entitlement and remaining quota, which is what Antigravity's own
        picker trusts.
        """
        if not self.antigravity_models:  # listing failed but the server answered: let the client validate at start
            return True, st.reason
        m = find_model(self.antigravity_models, ref)
        if m is None:
            return False, (f"Antigravity does not list model {ref!r}; it offers: "
                           f"{', '.join(x.id for x in self.antigravity_models)} (refresh models if Antigravity was updated)")
        if not m.supports_images:
            return False, f"Antigravity model {m.label!r} cannot take screenshots (text-only model)"
        if m.quota_exhausted:
            return False, f"Antigravity quota for {m.label!r} is exhausted{' until ' + m.quota_reset if m.quota_reset else ''}"
        return True, m.status_note()

    def tool_for(self, provider: str, bare: str) -> str:
        if provider == "anthropic":
            return "builtin"
        if provider == "vertex":
            mode = self.settings.vertex_tool_mode
            return (default_tool_mode(bare) if mode == "auto" else mode).replace("custom", "schema")
        if provider == "antigravity":
            return "json"  # no function calling through the Language Server: actions come back as JSON replies
        return "schema"

    def models(self) -> list[ModelInfo]:
        """Discovered models (when discovery has run) plus suggestions for the providers it did not cover.

        Available ones first, the configured default first among those; otherwise newest/strongest first.
        """
        infos: list[ModelInfo] = []
        seen: set[tuple[str, str]] = set()
        covered: set[str] = set()
        if self.discovery is not None:
            for m in self.discovery.models:
                st = self.status.get(m.provider)
                if st is None:
                    continue
                covered.add(m.provider)
                seen.add((m.provider, m.id))
                available = m.available and st.available
                infos.append(ModelInfo(
                    id=self.display_id(m.provider, m.id), label=m.label, provider=m.provider, available=available,
                    reason=m.reason if st.available else st.reason, tool=self.tool_for(m.provider, m.id),
                    verified=m.verified, source=m.source, project=m.project, version=m.version, stage=m.stage,
                ))
        for provider in PROVIDERS:
            if provider in covered:
                continue
            st = self.status[provider]
            if provider == "antigravity":
                infos.extend(self._antigravity_infos(seen))
                continue
            for mid, label in SUGGESTED[provider]:
                if (provider, mid) in seen:
                    continue
                shown = self.display_id(provider, mid)
                if shown != mid and not st.available:  # routes elsewhere and this provider is off: pure noise
                    continue
                seen.add((provider, mid))
                ok, reason = self.check(shown)
                infos.append(ModelInfo(id=shown, label=label, provider=provider, available=ok,
                                       reason="" if ok else reason, tool=self.tool_for(provider, mid)))
        default = self.settings.model
        infos.sort(key=lambda i: (not i.available, i.id != default))
        return infos

    def _antigravity_infos(self, seen: set[tuple[str, str]]) -> list[ModelInfo]:
        """Antigravity's list from detection time, shown until discovery re-lists it. Ids always carry the
        `antigravity:` prefix (bare, `gemini-*` ids would route to the Gemini API and the rest nowhere)."""
        out: list[ModelInfo] = []
        for m in self.antigravity_models:
            if m.disabled or ("antigravity", m.id) in seen:
                continue
            seen.add(("antigravity", m.id))
            shown = self.display_id("antigravity", m.id)
            ok, reason = self.check(shown)
            out.append(ModelInfo(id=shown, label=m.label, provider="antigravity", available=ok, reason=reason,
                                 tool=self.tool_for("antigravity", m.id), source="antigravity", version=m.enum))
        return out

    def default_model(self) -> str | None:
        if self.check(self.settings.model)[0]:
            return self.settings.model
        return next((m.id for m in self.models() if m.available), None)

    def any_available(self) -> bool:
        return any(st.available for st in self.status.values())

    def summary(self) -> dict[str, Any]:
        return {
            "providers": {p: asdict(st) for p, st in self.status.items()},
            "project": self.project,
            "projects": list(self.projects),
            "preference": self.settings.model_provider,
            "discovery": self.discovery_state(),
        }

    # -- construction ----------------------------------------------------------------

    def make(self, model_id: str) -> ModelClient:
        ok, reason = self.check(model_id)
        if not ok:
            raise ValueError(reason)
        provider, bare = self.resolve(model_id)
        s = self.settings
        rec = self.discovery.find(provider, bare) if self.discovery else None
        if provider == "anthropic":
            return AnthropicModelClient(bare, api_key=s.resolved_api_key(), beta_flag=s.beta_flag,
                                        max_retries=s.model_max_retries)
        if provider == "vertex":
            from computeruse.agent.vertex import VertexAnthropicModelClient

            assert self.projects
            projects = list(self.projects)
            if rec is not None and rec.project in projects:  # the project discovery saw serving this model goes first
                projects = [rec.project, *[p for p in projects if p != rec.project]]
            return VertexAnthropicModelClient(
                bare, projects=projects, location=s.gcp_location, tool_mode=s.vertex_tool_mode,
                beta_flag=s.beta_flag, max_retries=s.model_max_retries, thinking_effort=s.thinking_effort,
            )
        if provider == "antigravity":
            from computeruse.agent.antigravity import AntigravityModelClient

            if self.antigravity_endpoint is None:
                raise ValueError(f"antigravity is unavailable for {bare!r}: {self.status['antigravity'].reason}")
            known = find_model(self.antigravity_models, bare)  # label / enum references → the canonical id
            return AntigravityModelClient(known.id if known else bare, endpoint=self.antigravity_endpoint,
                                     archive=s.antigravity_archive)
        from computeruse.agent.gemini import GeminiModelClient

        key = s.resolved_gemini_key()
        project = None if key else ((rec.project if rec is not None and rec.project else None) or self.project)
        return GeminiModelClient(
            bare, api_key=key, project=project, location=s.gcp_location or "global",
            max_retries=s.model_max_retries, thinking_level=s.thinking_effort,
        )
