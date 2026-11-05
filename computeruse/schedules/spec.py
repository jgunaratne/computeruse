"""Data models, trigger formatting, starter templates, and permission catalog for schedules.

Schedules automate three kinds of work on a recurring or one-off cadence:
  - ``antigravity_chat``: send a scheduled prompt to an Antigravity model conversation
  - ``session``: launch a computer-use session (simulated, browser, or desktop)
  - ``flow``: run a multi-agent flow from the Agents tab

Each schedule also carries smart human-readable trigger descriptions (weekday/weekend detection,
ordinal day-of-month, quarter-hour marks, and optional natural-language conditions) and tracks
unread completion state plus recent execution history.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
WEEKDAYS = [1, 2, 3, 4, 5]
WEEKENDS = [0, 6]
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


class ScheduleFrequency(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONCE = "once"


class ScheduleStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class PauseReason(str, Enum):
    USER_ACTION = "user_action"
    EXECUTION_FAILED = "execution_failed"


class ScheduleTargetKind(str, Enum):
    ANTIGRAVITY_CHAT = "antigravity_chat"
    SESSION = "session"
    FLOW = "flow"


class PermissionLevel(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


def ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class ScheduleTrigger(BaseModel):
    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    minute_of_hour: int = Field(default=0, ge=0, le=59)
    time_of_day: str = Field(default="09:00")
    days_of_week: list[int] = Field(default_factory=lambda: list(WEEKDAYS))
    day_of_month: int = Field(default=1, ge=1, le=31)
    run_date: str | None = None
    condition: str = Field(default="", max_length=500)
    end_date: str | None = None

    @field_validator("time_of_day")
    @classmethod
    def _check_time(cls, v: str) -> str:
        v = (v or "09:00").strip()
        if not TIME_RE.match(v):
            raise ValueError("time_of_day must be HH:MM in 24-hour format")
        return v

    @field_validator("days_of_week")
    @classmethod
    def _check_days(cls, v: list[int]) -> list[int]:
        cleaned = sorted({int(d) for d in v if 0 <= int(d) <= 6})
        return cleaned or list(WEEKDAYS)

    @field_validator("run_date", "end_date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        s = str(v).strip()
        if not DATE_RE.match(s):
            raise ValueError("date must be YYYY-MM-DD")
        return s


class ScheduleRun(BaseModel):
    id: str
    schedule_id: str
    schedule_title: str
    target_kind: ScheduleTargetKind
    status: Literal["running", "completed", "failed"] = "running"
    started_at: float
    ended_at: float | None = None
    duration_ms: int | None = None
    summary: str = ""
    error: str | None = None
    chat_id: str | None = None
    session_id: str | None = None
    flow_id: str | None = None
    flow_run_id: str | None = None


class Schedule(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=140)
    plan: str = Field(min_length=1, max_length=10_000)
    target_kind: ScheduleTargetKind = ScheduleTargetKind.ANTIGRAVITY_CHAT
    backend: str = "simulated"
    model: str | None = None
    flow_id: str | None = None
    demo_task: str | None = None
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    pause_reason: PauseReason | None = None
    trigger: ScheduleTrigger = Field(default_factory=ScheduleTrigger)
    unread: bool = False
    created_at: float
    updated_at: float
    last_run_at: float | None = None
    next_run_at: float | None = None
    run_count: int = 0
    last_run: ScheduleRun | None = None

    def to_summary(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["badge"] = schedule_badge(self)
        data["trigger_summary"] = describe_trigger(self.trigger)
        return data


def schedule_badge(schedule: Schedule) -> str:
    """Lifecycle status takes precedence over trigger cadence; conditional active tasks show Tracking."""
    if schedule.status == ScheduleStatus.PAUSED:
        return "Paused"
    if schedule.status == ScheduleStatus.COMPLETED:
        return "Completed"
    if schedule.trigger.condition.strip():
        return "Tracking"
    return {
        ScheduleFrequency.HOURLY: "Hourly",
        ScheduleFrequency.DAILY: "Daily",
        ScheduleFrequency.WEEKLY: "Weekly",
        ScheduleFrequency.MONTHLY: "Monthly",
        ScheduleFrequency.ONCE: "Once",
    }[schedule.trigger.frequency]


def describe_trigger(trigger: ScheduleTrigger) -> str:
    """Human-readable cadence summary with weekday/weekend collapsing and ordinal day formatting."""
    f = trigger.frequency
    if f == ScheduleFrequency.HOURLY:
        if trigger.minute_of_hour == 0:
            base = "Hourly around the top of the hour"
        else:
            base = f"Hourly around the :{trigger.minute_of_hour:02d}-min mark"
    elif f == ScheduleFrequency.DAILY:
        base = f"Daily around {trigger.time_of_day}"
    elif f == ScheduleFrequency.WEEKLY:
        days = sorted({d for d in trigger.days_of_week if 0 <= d <= 6})
        if days == ALL_DAYS:
            base = f"Daily around {trigger.time_of_day}"
        elif days == WEEKDAYS:
            base = f"Weekly on weekdays around {trigger.time_of_day}"
        elif days == WEEKENDS:
            base = f"Weekly on weekends around {trigger.time_of_day}"
        else:
            names = ", ".join(DAY_NAMES[d] for d in days)
            base = f"Weekly on {names} around {trigger.time_of_day}"
    elif f == ScheduleFrequency.MONTHLY:
        base = f"Monthly on {ordinal(trigger.day_of_month)} around {trigger.time_of_day}"
    else:
        when = trigger.run_date or "next slot"
        base = f"Will run once on {when} around {trigger.time_of_day}"

    cond = trigger.condition.strip()
    if cond:
        short = cond if len(cond) <= 56 else cond[:55] + "…"
        return f"{base} · when {short}"
    return base


def _parse_hhmm(time_of_day: str) -> tuple[int, int]:
    m = TIME_RE.match(time_of_day.strip())
    if not m:
        return 9, 0
    return int(m.group(1)), int(m.group(2))


def compute_next_run(trigger: ScheduleTrigger, now_ts: float | None = None) -> float | None:
    """Compute the next execution timestamp (epoch seconds) in local server time."""
    now = dt.datetime.fromtimestamp(now_ts if now_ts is not None else dt.datetime.now().timestamp())
    hour, minute = _parse_hhmm(trigger.time_of_day)

    if trigger.end_date:
        try:
            end_dt = dt.datetime.strptime(trigger.end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            if now >= end_dt:
                return None
        except ValueError:
            end_dt = None
    else:
        end_dt = None

    f = trigger.frequency
    if f == ScheduleFrequency.HOURLY:
        candidate = now.replace(minute=trigger.minute_of_hour, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(hours=1)
    elif f == ScheduleFrequency.DAILY:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
    elif f == ScheduleFrequency.WEEKLY:
        # Convert Sunday=0..Saturday=6 into Python weekday (Monday=0..Sunday=6)
        allowed_sun0 = set(trigger.days_of_week or WEEKDAYS)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for offset in range(8):
            probe = candidate + dt.timedelta(days=offset)
            sun0 = (probe.weekday() + 1) % 7
            if sun0 in allowed_sun0 and probe > now:
                candidate = probe
                break
        else:
            candidate = now + dt.timedelta(days=1)
    elif f == ScheduleFrequency.MONTHLY:
        year, month = now.year, now.month
        last_day = calendar.monthrange(year, month)[1]
        day = min(trigger.day_of_month, last_day)
        candidate = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            last_day = calendar.monthrange(year, month)[1]
            day = min(trigger.day_of_month, last_day)
            candidate = candidate.replace(year=year, month=month, day=day)
    else:  # ONCE
        if trigger.run_date:
            try:
                d = dt.datetime.strptime(trigger.run_date, "%Y-%m-%d")
                candidate = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += dt.timedelta(days=1)

    if end_dt is not None and candidate > end_dt:
        return None
    return candidate.timestamp()


# -- Suggested Starter Schedules -------------------------------------------------------------------

SUGGESTED_SCHEDULES: list[dict[str, Any]] = [
    {
        "id": "morning-antigravity-brief",
        "title": "Morning Antigravity briefing",
        "description": "Ask Antigravity every weekday morning to review recent sessions, failure modes, and next steps.",
        "target_kind": "antigravity_chat",
        "backend": "simulated",
        "plan": (
            "Review our recent computer-use sessions and summarize:\n"
            "1. Which tasks succeeded or failed and why.\n"
            "2. Any guardrail blocks or repeated stuck nudges.\n"
            "3. Three concrete follow-ups for today."
        ),
        "trigger": {
            "frequency": "weekly",
            "time_of_day": "09:00",
            "days_of_week": [1, 2, 3, 4, 5],
            "minute_of_hour": 0,
            "day_of_month": 1,
            "condition": "",
        },
        "badge": "Weekly",
    },
    {
        "id": "hourly-smoke-check",
        "title": "Hourly desktop smoke check",
        "description": "Run the simulated calculator task at the top of every hour to verify agent loop health.",
        "target_kind": "session",
        "backend": "simulated",
        "demo_task": "simulated-basics/calc_multiply",
        "plan": "Open Calculator on the simulated desktop and compute 12 * 7 to verify action execution.",
        "trigger": {
            "frequency": "hourly",
            "minute_of_hour": 0,
            "time_of_day": "09:00",
            "days_of_week": [1, 2, 3, 4, 5],
            "day_of_month": 1,
            "condition": "",
        },
        "badge": "Hourly",
    },
    {
        "id": "conditional-page-watch",
        "title": "Conditional status page tracker",
        "description": "Check a status dashboard daily and flag when any service indicator changes.",
        "target_kind": "session",
        "backend": "simulated",
        "demo_task": "simulated-basics/notes_type_save",
        "plan": "Inspect the workspace notes and verify all checklist items are recorded.",
        "trigger": {
            "frequency": "daily",
            "time_of_day": "10:30",
            "minute_of_hour": 30,
            "days_of_week": [1, 2, 3, 4, 5],
            "day_of_month": 1,
            "condition": "An open checklist item or status alert is present",
        },
        "badge": "Tracking",
    },
    {
        "id": "mwf-agent-flow-audit",
        "title": "Multi-agent verification pipeline",
        "description": "Run a multi-agent flow on Mon, Wed, Fri to cross-check two desktop workers.",
        "target_kind": "flow",
        "backend": "simulated",
        "flow_id": "offline-demo",
        "plan": "Run both simulated desktop workers in parallel and join their verified screens.",
        "trigger": {
            "frequency": "weekly",
            "time_of_day": "14:00",
            "days_of_week": [1, 3, 5],
            "minute_of_hour": 0,
            "day_of_month": 1,
            "condition": "",
        },
        "badge": "Weekly",
    },
]


# -- Granular Per-Operation Tool Permissions & Sample Prompts --------------------------------------

PERMISSION_CATALOG: list[dict[str, Any]] = [
    {
        "id": "browser",
        "title": "Browser & Navigation",
        "description": "Controls how the agent opens URLs, interacts with forms, and downloads content.",
        "operations": [
            {
                "key": "browser::navigate_url",
                "label": "Navigate to URLs & follow links",
                "description": "Type URLs into the address bar and follow in-page links.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "browser::fill_forms",
                "label": "Fill search boxes & form inputs",
                "description": "Enter non-sensitive queries and text into page fields.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "browser::submit_external",
                "label": "Submit forms or post external data",
                "description": "Press Enter or click submit buttons on external web forms.",
                "default": PermissionLevel.ASK.value,
            },
            {
                "key": "browser::download_files",
                "label": "Download files to workspace",
                "description": "Save files triggered by browser navigation.",
                "default": PermissionLevel.ASK.value,
            },
        ],
        "sample_prompts": [
            {
                "label": "Inspect page & summarize",
                "prompt": "@Computer use start a browser session, take a screenshot of the landing page, and summarize the main sections.",
            },
            {
                "label": "Verify navigation flow",
                "prompt": "@Computer use open the browser and check that navigation and form inputs respond cleanly.",
            },
        ],
    },
    {
        "id": "input",
        "title": "Pointer & Keyboard Actions",
        "description": "Per-operation permissions for clicking, dragging, typing, and keyboard shortcuts.",
        "operations": [
            {
                "key": "input::pointer_click",
                "label": "Click, double-click & move pointer",
                "description": "Left, right, middle, and double clicks inside the display.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "input::pointer_drag",
                "label": "Click and drag elements",
                "description": "Drag sliders, windows, or canvas items between coordinates.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "input::keyboard_type",
                "label": "Type standard text",
                "description": "Type normal text strings that do not match secret patterns.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "input::keyboard_shortcuts",
                "label": "Send key chords & shortcuts",
                "description": "Press modifier combinations such as Ctrl+L, Alt+Tab, or Escape.",
                "default": PermissionLevel.ALLOW.value,
            },
        ],
        "sample_prompts": [
            {
                "label": "Run simulated calculator",
                "prompt": "@Computer use start a simulated session to compute 7 + 5 on the Calculator app.",
            },
            {
                "label": "Step through one action",
                "prompt": "@Computer use pause the current session, take one step forward, and explain what changed on screen.",
            },
        ],
    },
    {
        "id": "system",
        "title": "System & Sensitive Guardrails",
        "description": "Approval rules for credentials, privileged shell commands, and destructive operations.",
        "operations": [
            {
                "key": "system::terminal_commands",
                "label": "Execute terminal or shell commands",
                "description": "Interact with terminal windows or command prompts.",
                "default": PermissionLevel.ASK.value,
            },
            {
                "key": "system::sensitive_text",
                "label": "Type credentials, tokens, or card numbers",
                "description": "Type text matching API keys, passwords, or payment patterns.",
                "default": PermissionLevel.ASK.value,
            },
            {
                "key": "system::destructive_actions",
                "label": "Run destructive commands or force pushes",
                "description": "Recursive deletes, system shutdown, or destructive SQL.",
                "default": PermissionLevel.DENY.value,
            },
        ],
        "sample_prompts": [
            {
                "label": "Audit guardrail interventions",
                "prompt": "Check recent sessions for any guardrail blocks or approval requests and summarize what triggered them.",
            },
            {
                "label": "Tighten session step budget",
                "prompt": "@Computer use update the active session budget to max_steps 8.",
            },
        ],
    },
    {
        "id": "automation",
        "title": "Scheduled Routines & Operator Control",
        "description": "Permissions for background scheduled runs and live human takeover.",
        "operations": [
            {
                "key": "automation::scheduled_chat",
                "label": "Run scheduled Antigravity prompts",
                "description": "Call Antigravity automatically when a scheduled chat task fires.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "automation::scheduled_sessions",
                "label": "Launch scheduled computer sessions",
                "description": "Start unattended computer-use sessions from active schedules.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "automation::scheduled_flows",
                "label": "Run scheduled multi-agent flows",
                "description": "Execute multi-agent graphs on a recurring schedule.",
                "default": PermissionLevel.ALLOW.value,
            },
            {
                "key": "automation::operator_takeover",
                "label": "Manual mouse & keyboard takeover",
                "description": "Allow the operator to pause the agent and drive the screen directly.",
                "default": PermissionLevel.ALLOW.value,
            },
        ],
        "sample_prompts": [
            {
                "label": "Draft a daily morning check",
                "prompt": "Design a daily 09:00 schedule that checks our key web pages and summarizes any layout regressions.",
            },
            {
                "label": "Take over & inspect screen",
                "prompt": "@Computer use pause the active session and take screenshot so I can inspect the current state.",
            },
        ],
    },
]


def default_permissions() -> dict[str, str]:
    out: dict[str, str] = {}
    for cat in PERMISSION_CATALOG:
        for op in cat["operations"]:
            out[op["key"]] = op["default"]
    return out
