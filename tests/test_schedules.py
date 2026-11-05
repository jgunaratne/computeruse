"""Tests for scheduled automations, smart trigger formatting, and granular operation permissions."""

from __future__ import annotations

from fake_ls import ENDPOINT, MODELS, FakeLS, planner
from fastapi.testclient import TestClient

from computeruse.agent import providers
from computeruse.agent.antigravity import parse_model_configs
from computeruse.agent.guardrails import Decision, GuardrailContext, OperationPermissionRule
from computeruse.computer.actions import Click, Drag, KeyPress, TypeText
from computeruse.config import Settings
from computeruse.schedules.spec import (
    Schedule,
    ScheduleFrequency,
    ScheduleStatus,
    ScheduleTrigger,
    compute_next_run,
    describe_trigger,
    schedule_badge,
)
from computeruse.server.app import create_app


def _client(tmp_path, monkeypatch=None) -> TestClient:
    if monkeypatch is not None:
        monkeypatch.setattr(providers, "detect_antigravity", lambda address, token: (None, "disabled in test", []))
    settings = Settings(
        data_dir=tmp_path,
        model_discovery="off",
        anthropic_api_key=None,
        gemini_api_key=None,
        _env_file=None,  # type: ignore[call-arg]
    )
    return TestClient(create_app(settings))


def test_trigger_formatting_and_badge_precedence() -> None:
    # Hourly top of hour vs quarter mark
    assert describe_trigger(ScheduleTrigger(frequency=ScheduleFrequency.HOURLY, minute_of_hour=0)) == (
        "Hourly around the top of the hour"
    )
    assert describe_trigger(ScheduleTrigger(frequency=ScheduleFrequency.HOURLY, minute_of_hour=15)) == (
        "Hourly around the :15-min mark"
    )

    # Weekly weekday / weekend / custom days
    assert describe_trigger(
        ScheduleTrigger(frequency=ScheduleFrequency.WEEKLY, days_of_week=[1, 2, 3, 4, 5], time_of_day="09:00")
    ) == "Weekly on weekdays around 09:00"
    assert describe_trigger(
        ScheduleTrigger(frequency=ScheduleFrequency.WEEKLY, days_of_week=[0, 6], time_of_day="11:30")
    ) == "Weekly on weekends around 11:30"
    assert describe_trigger(
        ScheduleTrigger(frequency=ScheduleFrequency.WEEKLY, days_of_week=[1, 3, 5], time_of_day="14:00")
    ) == "Weekly on Mon, Wed, Fri around 14:00"

    # Monthly ordinal
    assert describe_trigger(
        ScheduleTrigger(frequency=ScheduleFrequency.MONTHLY, day_of_month=1, time_of_day="08:00")
    ) == "Monthly on 1st around 08:00"
    assert describe_trigger(
        ScheduleTrigger(frequency=ScheduleFrequency.MONTHLY, day_of_month=22, time_of_day="08:00")
    ) == "Monthly on 22nd around 08:00"

    # Conditional trigger -> Tracking badge when active, Paused/Completed take precedence
    sched = Schedule(
        id="s1",
        title="Monitor status",
        plan="Check status page",
        status=ScheduleStatus.ACTIVE,
        trigger=ScheduleTrigger(frequency=ScheduleFrequency.DAILY, condition="Alert banner is visible"),
        created_at=100.0,
        updated_at=100.0,
    )
    assert schedule_badge(sched) == "Tracking"
    assert "when Alert banner is visible" in describe_trigger(sched.trigger)

    sched.status = ScheduleStatus.PAUSED
    assert schedule_badge(sched) == "Paused"
    sched.status = ScheduleStatus.COMPLETED
    assert schedule_badge(sched) == "Completed"


def test_compute_next_run_advances_into_future() -> None:
    now = 1_700_000_000.0
    for freq in (
        ScheduleFrequency.HOURLY,
        ScheduleFrequency.DAILY,
        ScheduleFrequency.WEEKLY,
        ScheduleFrequency.MONTHLY,
    ):
        trig = ScheduleTrigger(frequency=freq, time_of_day="09:00", minute_of_hour=30)
        nxt = compute_next_run(trig, now)
        assert nxt is not None and nxt > now

    expired = ScheduleTrigger(frequency=ScheduleFrequency.DAILY, end_date="2020-01-01")
    assert compute_next_run(expired, now) is None


def test_schedule_crud_and_execution_flow(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as c:
        cat = c.get("/api/schedules/catalog").json()
        assert len(cat["suggested"]) >= 4
        assert len(cat["permissions"]["categories"]) == 4

        # Create a simulated session schedule
        res = c.post(
            "/api/schedules",
            json={
                "title": "Hourly calculator check",
                "plan": "Compute 12 * 7 on Calculator",
                "target_kind": "session",
                "backend": "simulated",
                "demo_task": "simulated-basics/calc_multiply",
                "trigger": {"frequency": "hourly", "minute_of_hour": 15},
            },
        )
        assert res.status_code == 201
        sched = res.json()
        sid = sched["id"]
        assert sched["badge"] == "Hourly"
        assert sched["trigger_summary"] == "Hourly around the :15-min mark"
        assert sched["unread"] is False

        # Run now (synchronously wait=true)
        run_res = c.post(f"/api/schedules/{sid}/run?wait=true")
        assert run_res.status_code == 202
        payload = run_res.json()
        assert payload["run"]["status"] == "completed"
        assert payload["run"]["session_id"]
        assert payload["schedule"]["unread"] is True
        assert payload["schedule"]["run_count"] == 1

        # Mark read clears unread dot
        read_res = c.post(f"/api/schedules/{sid}/read")
        assert read_res.status_code == 200
        assert read_res.json()["unread"] is False

        # Pause and resume
        paused = c.post(f"/api/schedules/{sid}/status", json={"status": "paused", "pause_reason": "user_action"}).json()
        assert paused["status"] == "paused"
        assert paused["badge"] == "Paused"
        assert paused["next_run_at"] is None

        resumed = c.post(f"/api/schedules/{sid}/status", json={"status": "active"}).json()
        assert resumed["status"] == "active"
        assert resumed["badge"] == "Hourly"
        assert resumed["next_run_at"] is not None

        # Delete
        assert c.delete(f"/api/schedules/{sid}").status_code == 204
        assert c.get(f"/api/schedules/{sid}").status_code == 404


def test_once_schedule_auto_completes_after_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        providers,
        "detect_antigravity",
        lambda address, token: (ENDPOINT, "FakeLS", parse_model_configs(MODELS)),
    )
    ls = FakeLS(replies=[[planner("Here is your scheduled morning briefing: 3 sessions succeeded.")]])
    settings = Settings(
        data_dir=tmp_path,
        model_discovery="off",
        model_provider="antigravity",
        antigravity_address="localhost:5387",
        antigravity_csrf_token="tok-123",
        anthropic_api_key=None,
        gemini_api_key=None,
        _env_file=None,  # type: ignore[call-arg]
    )
    with TestClient(create_app(settings)) as c:
        c.app.state.orch.chat.transport = ls.transport()  # type: ignore[union-attr]
        created = c.post(
            "/api/schedules",
            json={
                "title": "One-off briefing",
                "plan": "Summarize recent sessions",
                "target_kind": "antigravity_chat",
                "trigger": {"frequency": "once", "time_of_day": "09:00", "run_date": "2030-01-01"},
            },
        ).json()
        assert created["badge"] == "Once"

        ran = c.post(f"/api/schedules/{created['id']}/run?wait=true").json()
        assert ran["run"]["status"] == "completed"
        assert "morning briefing" in ran["run"]["summary"]
        assert ran["run"]["chat_id"]
        assert ran["schedule"]["status"] == "completed"
        assert ran["schedule"]["badge"] == "Completed"
        assert ran["schedule"]["next_run_at"] is None


def test_granular_permissions_api_and_guardrail_rule(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as c:
        initial = c.get("/api/permissions").json()
        input_cat = next(cat for cat in initial["categories"] if cat["id"] == "input")
        assert input_cat["level"] == "allow"

        # Update one operation -> category becomes "custom"
        updated = c.put("/api/permissions", json={"updates": {"input::pointer_drag": "ask"}}).json()
        input_cat = next(cat for cat in updated["categories"] if cat["id"] == "input")
        assert input_cat["level"] == "custom"

        # Bulk update entire category -> category level becomes "deny"
        bulk = c.put("/api/permissions", json={"category_id": "input", "level": "deny"}).json()
        input_cat = next(cat for cat in bulk["categories"] if cat["id"] == "input")
        assert input_cat["level"] == "deny"
        assert all(op["level"] == "deny" for op in input_cat["operations"])

        # Reset defaults
        reset = c.put("/api/permissions", json={"reset": True}).json()
        input_cat = next(cat for cat in reset["categories"] if cat["id"] == "input")
        assert input_cat["level"] == "allow"

    # Verify OperationPermissionRule directly on actions
    ctx = GuardrailContext(backend="browser", task="test", step=1)
    rule = OperationPermissionRule({
        "input::pointer_click": "allow",
        "input::pointer_drag": "ask",
        "browser::navigate_url": "deny",
        "input::keyboard_shortcuts": "ask",
    })
    assert rule.check(Click(action="left_click", coordinate=(10, 10)), ctx).decision == Decision.ALLOW
    assert rule.check(Drag(start_coordinate=(0, 0), coordinate=(50, 50)), ctx).decision == Decision.REQUIRE_APPROVAL
    assert rule.check(TypeText(text="https://example.com\n"), ctx).decision == Decision.BLOCK
    assert rule.check(KeyPress(text="ctrl+l"), ctx).decision == Decision.REQUIRE_APPROVAL
