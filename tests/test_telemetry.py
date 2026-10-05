"""SQLite store, event bus and product metrics."""

import asyncio
import time

from computeruse.telemetry import metrics
from computeruse.telemetry.bus import EventBus
from computeruse.telemetry.events import (
    Event,
    EventType,
    Outcome,
    SessionRecord,
    SessionStatus,
    Usage,
    estimate_cost_usd,
)
from computeruse.telemetry.store import Store


def _rec(store: Store, i: int, *, status=SessionStatus.COMPLETED, outcome=Outcome.COMPLETED, eval_passed=None,
         backend="simulated", tags=(), steps=3, cost=0.01, duration_ms=5000.0) -> SessionRecord:
    r = SessionRecord(id=f"s{i:03d}", task=f"task {i}", backend=backend, model="m", tags=list(tags))
    store.create_session(r)
    r.status, r.outcome, r.eval_passed = status, outcome, eval_passed
    r.steps, r.cost_usd, r.duration_ms = steps, cost, duration_ms
    r.started_at, r.ended_at = time.time() - 10, time.time()
    store.update_session(r)
    return r


def test_store_roundtrip_and_filters(tmp_path):
    store = Store(tmp_path / "d")
    try:
        _rec(store, 1)
        _rec(store, 2, backend="browser", status=SessionStatus.FAILED, outcome=Outcome.STUCK)
        r3 = _rec(store, 3, status=SessionStatus.RUNNING, outcome=None)
        assert store.get_session("s001").task == "task 1"
        assert store.get_session("nope") is None
        assert [s.id for s in store.list_sessions()] == ["s003", "s002", "s001"]
        assert [s.id for s in store.list_sessions(backend="browser")] == ["s002"]
        assert [s.id for s in store.list_sessions(status="running")] == ["s003"]
        for seq in range(3):
            store.append_event(Event(session_id=r3.id, seq=seq, type=EventType.ACTION_EXECUTED,
                                     data={"ok": True, "duration_ms": 10 * seq, "kind": "left_click"}))
        assert len(store.get_events(r3.id)) == 3
        assert [e.seq for e in store.get_events(r3.id, after_seq=0)] == [1, 2]
        assert len(store.events_by_type(EventType.ACTION_EXECUTED)) == 3
        p = store.save_frame(r3.id, 1, b"\x89PNG")
        assert p.exists() and store.frame_path(r3.id, 1) == p and store.frame_path(r3.id, 2) is None
        # A restart marks still-running sessions as interrupted.
        assert store.mark_interrupted() == 1
        s3 = store.get_session("s003")
        assert s3.status == SessionStatus.FAILED and s3.outcome == Outcome.INTERNAL_ERROR
    finally:
        store.close()


def test_eval_runs(tmp_path):
    store = Store(tmp_path / "d")
    try:
        store.create_eval_run("r1", "suite", "scripted", "simulated")
        assert store.get_eval_run("r1")["status"] == "running"
        store.finish_eval_run("r1", {"pass_rate": 0.5})
        run = store.get_eval_run("r1")
        assert run["status"] == "finished" and run["summary"]["pass_rate"] == 0.5
        assert [r["id"] for r in store.list_eval_runs()] == ["r1"]
    finally:
        store.close()


def test_metrics_summary_and_timeseries(tmp_path):
    store = Store(tmp_path / "d")
    try:
        _rec(store, 1, tags=["browser"], eval_passed=True)
        _rec(store, 2, tags=["browser"], eval_passed=False)  # false completion
        _rec(store, 3, status=SessionStatus.FAILED, outcome=Outcome.STUCK, tags=["desktop"])
        _rec(store, 4, status=SessionStatus.FAILED, outcome=Outcome.MAX_STEPS)
        _rec(store, 5, status=SessionStatus.RUNNING, outcome=None)
        store.append_event(Event(session_id="s001", seq=1, type=EventType.MODEL_CALLED, data={"latency_ms": 1200, "retries": 1}))
        store.append_event(Event(session_id="s001", seq=2, type=EventType.ACTION_EXECUTED, data={"ok": True, "duration_ms": 50, "kind": "left_click"}))
        store.append_event(Event(session_id="s001", seq=3, type=EventType.ACTION_EXECUTED, data={"ok": False, "duration_ms": 10, "kind": "type"}))
        store.append_event(Event(session_id="s001", seq=4, type=EventType.GUARDRAIL_DECISION, data={"decision": "block", "rule": "domain_policy"}))
        store.append_event(Event(session_id="s001", seq=5, type=EventType.APPROVAL_RESOLVED, data={"approved": False}))
        store.append_event(Event(session_id="s003", seq=1, type=EventType.STUCK_NUDGED, data={}))
        m = metrics.summarize(store)
        assert m["sessions_total"] == 5 and m["sessions_ended"] == 4 and m["sessions_active"] == 1
        assert m["success_rate"] == 0.25  # only s001 counts: s002 is a verified false completion
        assert m["false_completions"] == 1
        assert m["failure_taxonomy"] == {"stuck": 1, "max_steps": 1}
        assert m["p50_model_latency_ms"] == 1200 and m["model_retries"] == 1
        assert m["actions"] == 2 and m["action_error_rate"] == 0.5
        assert m["guardrail_interventions"] == {"block:domain_policy": 1}
        assert m["approvals"] == {"requested": 1, "approved": 0, "rejected": 1}
        assert m["stuck_nudges"] == 1
        assert m["by_tag"]["browser"]["sessions"] == 2 and m["by_tag"]["browser"]["success_rate"] == 0.5
        assert "Success rate" in metrics.readout_markdown(m) or "success" in metrics.readout_markdown(m).lower()
        ts = metrics.timeseries(store, days=3)
        assert len(ts) == 3 and ts[-1]["sessions"] == 4 and ts[-1]["success_rate"] == 0.25
        assert metrics.summarize(store, since_s=1)["sessions_total"] == 5
    finally:
        store.close()


def test_cost_estimate_and_usage():
    u = Usage(input_tokens=1000, output_tokens=500)
    cost = estimate_cost_usd("claude-sonnet-4-5", u)
    assert cost is not None and cost > 0
    assert estimate_cost_usd("scripted", u) in (None, 0.0)


async def test_event_bus_fanout_and_slow_consumer():
    bus = EventBus()
    q = bus.subscribe("s1", maxsize=2)
    g = bus.subscribe(None)
    for i in range(5):
        bus.publish(Event(session_id="s1", seq=i, type=EventType.FRAME, data={}))
    bus.publish(Event(session_id="other", seq=0, type=EventType.FRAME, data={}))
    # Oldest items were dropped for the slow per-session subscriber; the global one saw everything.
    got = [q.get_nowait().seq for _ in range(q.qsize())]
    assert got == [3, 4]
    assert g.qsize() == 6
    assert bus.subscriber_count("s1") == 1
    bus.close_session("s1")
    assert await asyncio.wait_for(q.get(), 1) is None
    bus.unsubscribe(q, "s1")
    assert bus.subscriber_count("s1") == 0
