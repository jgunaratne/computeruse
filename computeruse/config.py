"""Runtime configuration (environment variables prefixed COMPUTERUSE_, or .env).

Credentials also accept their conventional un-prefixed names (ANTHROPIC_API_KEY,
GEMINI_API_KEY, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION) so one `.env` can
be shared with other tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMPUTERUSE_", env_file=".env", extra="ignore",
                                      populate_by_name=True)

    # --- server ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = Field(default=Path("data"))
    web_dist: Path = Field(default=Path("web/dist"))
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # --- model access -----------------------------------------------------
    # auto: claude-* → Anthropic API if a key is set, else Vertex AI (ADC); gemini-* → Gemini API key, else Vertex;
    # antigravity:* → the Language Server of a running Antigravity (its own model catalogue, no key).
    model_provider: Literal["auto", "anthropic", "vertex", "gemini", "antigravity", "none"] = "auto"
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("COMPUTERUSE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
    gemini_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("COMPUTERUSE_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"))
    # Comma-separated list is allowed: Vertex access to a Claude model is per *project*, so a second project
    # can unlock models the first is not entitled to. The first project is the default / billing project.
    gcp_project: str | None = Field(
        default=None, validation_alias=AliasChoices("COMPUTERUSE_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT",
                                                    "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"))
    gcp_location: str | None = Field(  # None → "global" first, then regional fallbacks
        default=None, validation_alias=AliasChoices("COMPUTERUSE_GCP_LOCATION", "GOOGLE_CLOUD_LOCATION",
                                                    "VERTEX_LOCATION"))
    # Antigravity Language Server (host:port). Unset → the address an Antigravity shell exports (ANTIGRAVITY_LS_ADDRESS),
    # else localhost:5387. The CSRF token likewise falls back to ANTIGRAVITY_CSRF_TOKEN, then to the token the
    # server publishes on its index page. Secret: never logged or returned by the API.
    antigravity_address: str | None = Field(default=None, validation_alias=AliasChoices("COMPUTERUSE_ANTIGRAVITY_ADDRESS"))
    antigravity_csrf_token: str | None = Field(default=None,
                                          validation_alias=AliasChoices("COMPUTERUSE_ANTIGRAVITY_CSRF_TOKEN"))
    antigravity_archive: bool = True  # hide finished sessions' conversations from the Antigravity UI (data is kept)
    vertex_tool_mode: Literal["auto", "builtin", "toolset", "custom"] = "auto"
    thinking_effort: Literal["low", "medium", "high"] | None = None  # None → provider default
    # Override/add USD-per-million (input, output) prices, e.g. {"claude-opus-5-5": [5, 25]}.
    pricing_overrides: dict[str, tuple[float, float]] = Field(default_factory=dict)
    # How the model picker learns what is callable: "probe" lists the provider catalogues *and* verifies each
    # model with a 1-token request per project; "list" trusts the catalogues; "off" shows suggestions only.
    model_discovery: Literal["off", "list", "probe"] = "probe"
    model_discovery_max_age_s: int = 3600  # re-run discovery on demand after this; the UI has a refresh button

    # --- model ----------------------------------------------------------
    model: str = "claude-sonnet-4-5"
    tool_version: str = "computer_20250124"
    beta_flag: str = "computer-use-2025-01-24"
    max_tokens: int = 4096
    model_max_retries: int = 4

    # --- agent defaults -------------------------------------------------
    default_max_steps: int = 40
    default_max_duration_s: float = 900.0
    default_max_cost_usd: float | None = 3.0
    screenshot_history: int = 3  # images kept in the model context
    settle_ms: int = 350  # wait after an action before capturing the result
    stuck_threshold: int = 3
    approval_timeout_s: float = 600.0

    # --- computers ------------------------------------------------------
    browser_size: str = "1280x800"
    chrome_binary: str | None = None
    chrome_start_url: str = "about:blank"
    # Where the browser backend keeps cookies, logins and SSO state between sessions (default
    # <data_dir>/chrome-profile). "ephemeral" gives every session a throwaway profile instead.
    chrome_profile_dir: Path | None = None
    chrome_profile_mode: Literal["persistent", "ephemeral"] = "persistent"
    desktop_connector: str | None = None  # gnome monitor connector; default primary
    remote_daemon_url: str | None = None
    remote_daemon_token: str | None = None
    remote_live_view_url: str | None = None
    docker_image: str = "computeruse-sandbox:latest"
    preview_fps: float = 4.0
    control_preview_fps: float = 12.0  # live-view rate while an operator is driving the computer

    # --- guardrails -----------------------------------------------------
    allowed_domains: list[str] = Field(default_factory=list)  # empty = any domain
    blocked_domains: list[str] = Field(default_factory=list)
    blocked_keys: list[str] = Field(
        default_factory=lambda: ["ctrl+alt+Delete", "ctrl+alt+BackSpace", "ctrl+alt+F1", "ctrl+alt+F2"]
    )
    desktop_blocked_keys: list[str] = Field(
        default_factory=lambda: ["super+l", "ctrl+alt+l", "alt+F4", "super+q", "ctrl+q", "alt+F2"]
    )
    max_type_length: int = 4000

    def resolved_api_key(self) -> str | None:
        return self.anthropic_api_key or None

    def resolved_gemini_key(self) -> str | None:
        return self.gemini_api_key or None

    def gcp_projects(self) -> list[str]:
        """`gcp_project` split on commas/whitespace, de-duplicated, order preserved (first = primary)."""
        raw = (self.gcp_project or "").replace(",", " ").split()
        return list(dict.fromkeys(raw))

    def browser_dims(self) -> tuple[int, int]:
        w, h = self.browser_size.lower().split("x")
        return int(w), int(h)

    def chrome_profile_path(self) -> Path:
        """Directory of the persistent browser profile (cookies, logins, SSO state)."""
        return Path(self.chrome_profile_dir) if self.chrome_profile_dir else Path(self.data_dir) / "chrome-profile"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
