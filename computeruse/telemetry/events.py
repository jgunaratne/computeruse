"""Structured telemetry events emitted by the agent loop and the orchestrator.

Every event is persisted (SQLite) and broadcast live (WebSocket). The web UI,
metrics and the replay model client are all derived purely from this stream,
which keeps the harness observable by construction.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    SESSION_STARTED = "session.started"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    SESSION_ENDED = "session.ended"
    TURN_STARTED = "turn.started"
    MODEL_CALLED = "model.called"
    MODEL_RETRY = "model.retry"
    ASSISTANT_TEXT = "assistant.text"
    ACTION_PROPOSED = "action.proposed"
    GUARDRAIL_DECISION = "guardrail.decision"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    ACTION_EXECUTED = "action.executed"
    FRAME = "frame"
    PREVIEW = "preview"  # transient live-view frame, not persisted
    STUCK_NUDGED = "stuck.nudged"
    USER_INSTRUCTION = "user.instruction"
    MANUAL_ACTION = "manual.action"
    OPERATOR_CONTROL = "operator.control"  # a human took over / handed back the mouse and keyboard
    BUDGET_CHANGED = "budget.changed"  # max steps / turns / duration / cost raised or lowered mid-session
    ERROR = "error"


class Event(BaseModel):
    session_id: str
    seq: int = 0
    ts: float = Field(default_factory=time.time)
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "seq": self.seq, "ts": self.ts,
                "type": self.type.value, "data": self.data}


class SessionStatus(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED)


class Outcome(str, Enum):
    """Why a session ended. This is the failure taxonomy used by metrics."""

    COMPLETED = "completed"  # the model declared the task done
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    STUCK = "stuck"
    MODEL_ERROR = "model_error"
    COMPUTER_ERROR = "computer_error"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens


# USD per million tokens (input, output), matched by longest model-id prefix.
# Cache reads are billed at 10% of input and cache writes at 125% (Anthropic);
# Gemini cached tokens are approximated the same way. Entries marked "assumed"
# are not published prices — override with COMPUTERUSE_PRICING_OVERRIDES.
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),  # assumed = Sonnet 4.5 list price
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-5": (5.0, 25.0),  # assumed = Opus 4.5 list price (covers claude-opus-5-5)
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-pro": (2.0, 12.0),
    "gemini-3.1-pro": (2.0, 12.0),  # assumed = Gemini 3 Pro list price
    "gemini-3-flash": (0.50, 3.0),
    "gemini-3.": (0.50, 3.0),  # assumed for 3.x flash variants
}


def set_pricing_overrides(overrides: dict[str, tuple[float, float]]) -> None:
    for prefix, (pin, pout) in overrides.items():
        PRICING[prefix] = (float(pin), float(pout))


def estimate_cost_usd(model: str, usage: Usage) -> float | None:
    provider, _, rest = model.partition(":")
    if provider == "antigravity" and rest:
        return None  # draws on the user's Antigravity quota, not billed in USD; never attribute a price
    bare = rest if provider in ("anthropic", "vertex", "gemini") and rest else model
    match = max((p for p in PRICING if bare.startswith(p)), key=len, default=None)
    if match is None:
        return None
    pin, pout = PRICING[match]
    return round(
        (usage.input_tokens + usage.cache_creation_input_tokens * 1.25
         + usage.cache_read_input_tokens * 0.1) / 1e6 * pin
        + usage.output_tokens / 1e6 * pout,
        6,
    )


class SessionRecord(BaseModel):
    id: str
    task: str
    backend: str
    model: str
    status: SessionStatus = SessionStatus.CREATED
    outcome: Outcome | None = None
    outcome_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    steps: int = 0
    turns: int = 0
    duration_ms: float | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    display_width: int | None = None
    display_height: int | None = None
    live_view_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    eval_run_id: str | None = None
    eval_task_id: str | None = None
    eval_passed: bool | None = None
    eval_detail: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def success(self) -> bool | None:
        """Task-level success: checker verdict when evaluated, else model completion."""
        if self.eval_passed is not None:
            return self.eval_passed
        if self.status.terminal:
            return self.outcome == Outcome.COMPLETED
        return None
