"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from computeruse.telemetry.events import SessionRecord

MAX_STEPS_CAP = 5000  # hard ceiling for max_steps (per session and per live adjustment)
MAX_TURNS_CAP = 10_000
MAX_DURATION_CAP_S = 24 * 3600


class RunOverrides(BaseModel):
    """Per-session overrides of the agent loop defaults (null = keep default)."""

    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS_CAP)
    max_turns: int | None = Field(default=None, ge=1, le=MAX_TURNS_CAP)
    max_duration_s: float | None = Field(default=None, gt=0, le=MAX_DURATION_CAP_S)
    max_cost_usd: float | None = Field(default=None, ge=0)
    stuck_threshold: int | None = Field(default=None, ge=2, le=20)
    screenshot_history: int | None = Field(default=None, ge=1, le=10)
    settle_ms: int | None = Field(default=None, ge=0, le=10_000)
    system_prompt_extra: str | None = None
    auto_approve: bool | None = None
    start_paused: bool | None = None


class CreateSessionRequest(BaseModel):
    task: str = ""
    backend: str | None = None  # default: demo task's suite backend, else "browser"
    model: str | None = None  # default: settings.model; "scripted"; "replay:<session_id>"
    overrides: RunOverrides | None = None
    script: list[dict[str, Any]] | None = None  # for model=scripted
    demo_task: str | None = None  # "<suite>/<task_id>": pulls instruction + reference script
    tags: list[str] = Field(default_factory=list)
    backend_options: dict[str, Any] = Field(default_factory=dict)
    guardrails: dict[str, Any] | None = None  # allowed_domains / blocked_domains / blocked_keys / max_type_length
    start_paused: bool = False


class ControlRequest(BaseModel):
    command: Literal["pause", "resume", "step", "cancel", "approve", "reject", "instruct",
                     "take_control", "release_control", "budget"]
    approval_id: str | None = None
    text: str | None = None
    resume: bool | None = None  # release_control: hand back *and* let the agent continue
    # budget: new limits for a session that is still running (any subset; takes effect at the next check)
    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS_CAP)
    max_turns: int | None = Field(default=None, ge=1, le=MAX_TURNS_CAP)
    max_duration_s: float | None = Field(default=None, gt=0, le=MAX_DURATION_CAP_S)
    max_cost_usd: float | None = Field(default=None, ge=0)


class EvalRunRequest(BaseModel):
    suite: str
    model: str = "scripted"
    backend: str | None = None
    repeats: int = Field(default=1, ge=1, le=20)
    task_ids: list[str] | None = None
    concurrency: int = Field(default=1, ge=1, le=8)


class ChatCreateRequest(BaseModel):
    model: str | None = None  # Antigravity model id / label (prefix optional); default: settings.chat_model or a Flash model
    title: str | None = Field(default=None, max_length=120)


class ChatSendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    model: str | None = None  # switch the model for this and later messages
    session_id: str | None = None  # attach the unseen part of this session's timeline (+ latest screenshot)
    screenshot: bool = True


def session_to_json(rec: SessionRecord) -> dict[str, Any]:
    d = rec.model_dump(mode="json")
    d["success"] = rec.success
    d["terminal"] = rec.status.terminal
    return d
