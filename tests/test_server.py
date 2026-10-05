"""API + orchestrator + eval runner, exercised through the ASGI app on the simulated computer."""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from computeruse.evals.runner import run_suite
from computeruse.evals.suite import find_suite
from computeruse.server.app import create_app
from computeruse.server.orchestrator import Orchestrator

NOTES_SCRIPT = [
    {"text": "Opening Notes.", "actions": [{"action": "left_click", "target": "dock.notes"}]},
    {"actions": [{"action": "type", "text": "hi"}, {"action": "key", "text": "ctrl+s"}]},
    {"text": "Done.", "done": True},
]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def wait_terminal(c: TestClient, sid: str, timeout: float = 30) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["terminal"]:
            return s
        time.sleep(0.05)
    raise TimeoutError(sid)


def test_meta_endpoints(client):
    assert client.get("/api/health").json()["ok"] is True
    backends = {b["id"]: b for b in client.get("/api/backends").json()}
    assert backends["simulated"]["available"] and backends["simulated"]["isolated"]
    cfg = client.get("/api/config").json()
    assert cfg["models_available"] is False and cfg["defaults"]["max_steps"] > 0
    assert cfg["models"] and all(m["available"] is False for m in cfg["models"])
    assert set(cfg["model_access"]["providers"]) == {"anthropic", "vertex", "gemini", "antigravity"}
    assert cfg["models_discovery"]["mode"] == "probe" and cfg["models_discovery"]["status"] == "idle"
    models = client.get("/api/models").json()
    assert models["default"] is None and models["discovery"]["status"] == "idle"
    assert [m["id"] for m in models["models"]] == [m["id"] for m in cfg["models"]]
    refreshed = client.post("/api/models/refresh").json()  # no provider is available: nothing to probe, no network
    assert refreshed["discovery"]["status"] == "done" and refreshed["discovery"]["count"] == 0
    assert refreshed["discovery"]["verified"] == 0 and refreshed["discovery"]["errors"] == {}
    assert all(m["available"] is False and m["verified"] is False for m in refreshed["models"])
    assert client.get("/api/models").json()["discovery"]["refreshed_at"] == refreshed["discovery"]["refreshed_at"]
    demos = client.get("/api/demo-tasks").json()
    assert any(d["id"] == "simulated-basics/notes_type_save" for d in demos)
    assert client.get("/").status_code == 200  # JSON hint when the UI is not built
    assert client.get("/api/docs").status_code == 200


def test_validation_errors(client):
    assert client.post("/api/sessions", json={"backend": "simulated", "model": "scripted"}).status_code == 400
    r = client.post("/api/sessions", json={"task": "x", "backend": "simulated", "model": "claude-sonnet-4-5"})
    assert r.status_code == 400 and "unavailable" in r.json()["detail"]
    r = client.post("/api/sessions", json={"task": "x", "backend": "simulated", "model": "gpt-9"})
    assert r.status_code == 400 and "unknown model" in r.json()["detail"]
    r = client.post("/api/sessions", json={"task": "x", "backend": "nope", "model": "scripted", "script": NOTES_SCRIPT})
    assert r.status_code == 400
    assert client.post("/api/sessions", json={"task": "x", "demo_task": "nope/nope", "model": "scripted"}).status_code == 404
    assert client.get("/api/sessions/unknown").status_code == 404
    assert client.get("/api/sessions/unknown/frames/1.png").status_code == 404
    assert client.post("/api/sessions/unknown/control", json={"command": "pause"}).status_code == 409
    assert client.post("/api/sessions/unknown/control", json={"command": "fly"}).status_code == 422


def test_session_lifecycle_events_frames_and_websocket(client):
    r = client.post("/api/sessions", json={"task": "Type hi into Notes and save.", "backend": "simulated",
                                           "model": "scripted", "script": NOTES_SCRIPT, "tags": ["t"]})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "history"
        seen, end = [], None
        while True:
            msg = ws.receive_json()
            if msg["type"] == "event":
                seen.append(msg["event"]["type"])
            elif msg["type"] == "end":
                end = msg["session"]
                break
        assert end["status"] == "completed" and end["steps"] == 3 and end["success"] is True
        assert "action.proposed" in seen and "session.ended" in seen
        # Live stream never repeats a seq already delivered in history and keeps order.
        seqs = [e["seq"] for e in first["events"]]
        assert seqs == sorted(seqs)
    s = client.get(f"/api/sessions/{sid}?include_events=1").json()
    assert s["outcome"] == "completed" and s["metadata"]["final_text"] == "Done."
    events = s["events"]
    frames = [e for e in events if e["type"] == "frame"]
    assert len(frames) == 4
    png = client.get(frames[0]["data"]["url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.get(f"/api/sessions/{sid}/events?after={events[-2]['seq']}").json()[0]["seq"] == events[-1]["seq"]
    assert [e["type"] for e in client.get(f"/api/sessions/{sid}/events?types=session.ended").json()] == ["session.ended"]
    # Late join on a finished session gets history + end immediately.
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        assert ws.receive_json()["type"] == "history"
        assert ws.receive_json()["type"] == "end"
    listed = client.get("/api/sessions?backend=simulated").json()
    assert listed[0]["id"] == sid and listed[0]["tags"] == ["t"]
    # Controls are rejected once the session is over.
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "pause"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/manual", json={"action": "screenshot"}).status_code == 409


def test_pause_step_manual_instruct_resume_and_replay(client):
    r = client.post("/api/sessions", json={"demo_task": "simulated-basics/notes_type_save", "model": "scripted",
                                           "start_paused": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    t0 = time.time()
    while client.get(f"/api/sessions/{sid}").json()["status"] != "paused":
        assert time.time() - t0 < 10
        time.sleep(0.05)
    # Manual actions only while paused; invalid ones are rejected cleanly.
    assert client.post(f"/api/sessions/{sid}/manual", json={"action": "mouse_move", "coordinate": [5, 5]}).json()["ok"]
    assert client.post(f"/api/sessions/{sid}/manual", json={"action": "bogus"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "instruct", "text": "careful"}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "instruct", "text": " "}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "approve"}).status_code == 409  # nothing pending
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "step"}).status_code == 200
    t0 = time.time()
    while True:
        s = client.get(f"/api/sessions/{sid}").json()
        if s["status"] == "paused" and s["steps"] == 1:
            break
        assert time.time() - t0 < 10, s
        time.sleep(0.05)
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "resume"}).status_code == 200
    s = wait_terminal(client, sid)
    assert s["status"] == "completed" and s["eval_passed"] is True, s
    types = [e["type"] for e in client.get(f"/api/sessions/{sid}/events").json()]
    assert {"session.paused", "manual.action", "user.instruction", "session.resumed"} <= set(types)
    # Replay re-issues the recorded model turns against a fresh computer.
    r = client.post("/api/sessions", json={"task": s["task"], "backend": "simulated", "model": f"replay:{sid}"})
    assert r.status_code == 201, r.text
    rep = wait_terminal(client, r.json()["id"])
    assert rep["status"] == "completed" and rep["steps"] == s["steps"]


def test_step_budget_can_be_raised_while_the_session_runs(client):
    script = [{"actions": [{"action": "wait", "duration": 0.01}]} for _ in range(6)]
    r = client.post("/api/sessions", json={"task": "six waits", "backend": "simulated", "model": "scripted",
                                           "script": script, "overrides": {"max_steps": 2}, "start_paused": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["metadata"]["budget"]["max_steps"] == 2
    t0 = time.time()
    while client.get(f"/api/sessions/{sid}").json()["status"] != "paused":
        assert time.time() - t0 < 10
        time.sleep(0.05)
    # validation: schema caps, empty change, and a budget at or below the steps already taken
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "budget", "max_steps": 0}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "budget", "max_steps": 6000}).status_code == 422
    r = client.post(f"/api/sessions/{sid}/control", json={"command": "budget"})
    assert r.status_code == 409 and "at least one of" in r.json()["detail"]
    r = client.post(f"/api/sessions/{sid}/control", json={"command": "budget", "max_steps": 10, "max_duration_s": 120})
    assert r.status_code == 200 and r.json()["ok"]
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["metadata"]["budget"]["max_steps"] == 10 and s["metadata"]["budget"]["max_duration_s"] == 120
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "resume"}).status_code == 200
    s = wait_terminal(client, sid)
    # all six scripted actions ran (past the original budget of 2) and the script finished normally
    assert s["status"] == "completed" and s["steps"] == 6 and s["outcome"] == "completed", s
    events = client.get(f"/api/sessions/{sid}/events").json()
    change = next(e for e in events if e["type"] == "budget.changed")
    assert change["data"]["changes"] == {"max_steps": 10, "max_duration_s": 120}
    assert change["data"]["before"]["max_steps"] == 2 and change["data"]["budget"]["max_steps"] == 10
    # once over, the budget (like every control) is rejected; a lower budget never undercuts taken steps
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "budget", "max_steps": 3}).status_code == 409
    # large budgets are accepted at creation (the old 500 cap is gone)
    r = client.post("/api/sessions", json={"task": "x", "backend": "simulated", "model": "scripted",
                                           "script": script[:1], "overrides": {"max_steps": 2000}})
    assert r.status_code == 201 and r.json()["metadata"]["budget"]["max_steps"] == 2000
    wait_terminal(client, r.json()["id"])


def test_cancel_and_global_feed(client):
    script = [{"actions": [{"action": "wait", "duration": 0.2}]} for _ in range(50)]
    with client.websocket_connect("/api/events/ws") as feed:
        r = client.post("/api/sessions", json={"task": "slow", "backend": "simulated", "model": "scripted", "script": script})
        sid = r.json()["id"]
        msg = feed.receive_json()
        assert msg["type"] == "event" and msg["session"]["id"] == sid and "jpeg_b64" not in str(msg)
    time.sleep(0.3)
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "cancel"}).status_code == 200
    s = wait_terminal(client, sid)
    assert s["status"] == "cancelled" and s["outcome"] == "cancelled"


def test_metrics_and_eval_endpoints(client):
    r = client.post("/api/evals/run", json={"suite": "simulated-basics", "model": "scripted",
                                            "task_ids": ["notes_type_save", "negative_control_wrong_target"], "concurrency": 2})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json()["tasks"] == 2
    t0 = time.time()
    while client.get(f"/api/evals/runs/{run_id}").json()["status"] == "running":
        assert time.time() - t0 < 60
        time.sleep(0.1)
    run = client.get(f"/api/evals/runs/{run_id}").json()
    assert run["status"] == "finished"
    summary = run["summary"]
    assert summary["runs"] == 2 and summary["passed"] == 1 and summary["false_completions"] == 1
    assert summary["per_task"]["negative_control_wrong_target"]["pass_rate"] == 0
    assert len(run["sessions"]) == 2 and all(s["eval_run_id"] == run_id for s in run["sessions"])
    assert client.get("/api/evals/runs").json()[0]["id"] == run_id
    assert client.post("/api/evals/run", json={"suite": "nope", "model": "scripted"}).status_code == 404
    assert client.post("/api/evals/run", json={"suite": "simulated-basics", "model": "claude-sonnet-4-5"}).status_code == 400
    m = client.get("/api/metrics/summary").json()
    assert m["sessions_ended"] == 2 and m["false_completions"] == 1 and m["success_rate"] == 0.5
    assert client.get("/api/metrics/summary?include_evals=false").json()["sessions_total"] == 0
    assert len(client.get("/api/metrics/timeseries?days=3").json()) == 3
    assert "false completions" in client.get("/api/metrics/readout").text.lower()
    suites = client.get("/api/evals/suites").json()
    assert {s["name"] for s in suites} >= {"simulated-basics", "browser-smoke"}


async def test_run_suite_directly_and_guardrail_checker(settings):
    orch = Orchestrator(settings)
    try:
        suite = find_suite("simulated-basics")
        summary = await run_suite(orch, suite, model="scripted", concurrency=4)
        assert summary["runs"] == len(suite.tasks)
        assert summary["pass_rate"] == pytest.approx((len(suite.tasks) - 1) / len(suite.tasks))
        assert summary["false_completions"] == 1
        assert summary["per_tag"]["negative-control"]["pass_rate"] == 0
        with pytest.raises(ValueError):
            await run_suite(orch, suite, model="scripted", task_ids=["does-not-exist"])
        # Per-task guardrails + the `guardrail` checker, on the simulated browser.
        from computeruse.evals.suite import Suite, TaskSpec

        spec = TaskSpec(
            id="blocked", instruction="go to evil", tags=["negative-control"],
            checker={"type": "all", "checks": [{"type": "guardrail", "decision": "block", "rule": "domain_policy"},
                                                 {"type": "sim_state", "path": "apps.browser.url", "equals": ""}]},
            script=[{"actions": [{"action": "left_click", "target": "dock.browser"}]},
                    {"actions": [{"action": "left_click", "target": "browser.url"}, {"action": "type", "text": "https://evil.test\n"}]},
                    {"done": True}],
            guardrails={"blocked_domains": ["evil.test"]},
        )
        s2 = await run_suite(orch, Suite(name="adhoc", backend="simulated", tasks=[spec]), model="scripted")
        assert s2["pass_rate"] == 1.0, s2
    finally:
        await orch.shutdown()


async def test_orchestrator_rejects_unavailable_backend_and_marks_interrupted(settings):
    orch = Orchestrator(settings)
    try:
        with pytest.raises(ValueError):
            await orch.create_session(task="x", backend="remote", model="scripted", script=NOTES_SCRIPT)
        rec = await orch.create_session(task="x", backend="simulated", model="scripted",
                                        script=[{"actions": [{"action": "wait", "duration": 0.3}]} for _ in range(20)])
        await asyncio.sleep(0.2)
    finally:
        await orch.shutdown()
    # A new orchestrator over the same data dir marks the orphaned session as interrupted.
    orch2 = Orchestrator(settings)
    try:
        s = orch2.store.get_session(rec.id)
        assert s.status.terminal
    finally:
        orch2.store.close()


def test_asgi_transport_smoke(settings):
    """The app also works under httpx's ASGI transport (no threads), e.g. for notebooks."""

    async def go():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                assert (await c.get("/api/health")).json()["ok"]

    asyncio.run(go())


def wait_status(c: TestClient, sid: str, status: str, timeout: float = 10) -> dict:
    t0 = time.time()
    while True:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["status"] == status:
            return s
        assert time.time() - t0 < timeout, s
        time.sleep(0.05)


def ws_until(ws, pred, limit: int = 400) -> dict:
    """Read websocket messages (skipping previews etc.) until one satisfies `pred`."""
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("websocket message not seen")


def test_operator_control_over_websocket(client):
    r = client.post("/api/sessions", json={"demo_task": "simulated-basics/notes_type_save", "model": "scripted",
                                           "start_paused": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    wait_status(client, sid, "paused")
    assert client.get(f"/api/sessions/{sid}").json()["metadata"].get("operator_control") is None

    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        assert ws.receive_json()["type"] == "history"
        # Raw input is refused until control is taken.
        ws.send_json({"type": "input", "action": {"action": "mouse_move", "coordinate": [5, 5]}, "id": 0})
        res = ws_until(ws, lambda m: m["type"] == "input_result")
        assert res["id"] == 0 and res["ok"] is False and "take control" in res["error"]

        r = client.post(f"/api/sessions/{sid}/control", json={"command": "take_control"})
        assert r.status_code == 200 and r.json()["control"]["active"] is True and r.json()["status"] == "paused"
        taken = ws_until(ws, lambda m: m["type"] == "event" and m["event"]["type"] == "operator.control")
        assert taken["event"]["data"]["state"] == "taken" and taken["event"]["data"]["waiting_for_pause"] is False
        assert taken["session"]["metadata"]["operator_control"]["active"] is True
        assert client.post(f"/api/sessions/{sid}/control", json={"command": "take_control"}).status_code == 200  # idempotent

        # Pointer moves are fire-and-forget; anything with an id (or any failure) gets a result.
        ws.send_json({"type": "input", "action": {"action": "mouse_move", "coordinate": [5, 5]}})
        ws.send_json({"type": "input", "action": {"action": "left_click", "coordinate": [40, 40]}, "id": 1})
        res = ws_until(ws, lambda m: m["type"] == "input_result")
        assert res == {"type": "input_result", "id": 1, "action": "left_click", "ok": True, "error": None}
        ws.send_json({"type": "input", "action": {"action": "type", "text": "hello"}, "id": 2})
        assert ws_until(ws, lambda m: m["type"] == "input_result")["ok"] is True
        ws.send_json({"type": "input", "action": {"action": "key", "text": "Return"}, "id": 3})
        assert ws_until(ws, lambda m: m["type"] == "input_result")["ok"] is True
        ws.send_json({"type": "input", "action": {"action": "scroll", "coordinate": [50, 50], "scroll_direction": "down",
                                                  "scroll_amount": 2}, "id": 4})
        assert ws_until(ws, lambda m: m["type"] == "input_result")["ok"] is True
        ws.send_json({"type": "input", "action": {"action": "bogus"}, "id": 5})
        res = ws_until(ws, lambda m: m["type"] == "input_result")
        assert res["ok"] is False and res["id"] == 5 and res["error"]
        ws.send_json({"type": "input", "action": {"action": "screenshot"}, "id": 6})
        assert "not an operator input" in ws_until(ws, lambda m: m["type"] == "input_result")["error"]
        ws.send_json({"type": "input", "id": 7})
        assert "needs an 'action'" in ws_until(ws, lambda m: m["type"] == "input_result")["error"]
        ws.send_json({"type": "ping"})
        assert ws_until(ws, lambda m: m["type"] == "pong")

        # The recorded manual path still works in control, and never logs what was typed.
        assert client.post(f"/api/sessions/{sid}/manual", json={"action": "type", "text": "s3cret"}).json()["ok"]
        manual = ws_until(ws, lambda m: m["type"] == "event" and m["event"]["type"] == "manual.action")
        assert manual["event"]["data"]["description"] == "type 6 characters"

        # Hand back without resuming: screen captured, summary recorded, agent still parked.
        r = client.post(f"/api/sessions/{sid}/control", json={"command": "release_control"})
        assert r.status_code == 200 and r.json()["control"] is None and r.json()["status"] == "paused"
        released = ws_until(ws, lambda m: m["type"] == "event" and m["event"]["type"] == "operator.control")
        d = released["event"]["data"]
        assert d["state"] == "released" and d["resumed"] is False and d["clicks"] == 1 and d["typed_chars"] == 11
        assert d["keys"] == 1 and d["scrolls"] == 1 and d["errors"] == 0
        assert "1 click" in d["summary"] and "typed 11 characters" in d["summary"] and "pressed Return" in d["summary"]
        assert "hello" not in d["summary"] and "s3cret" not in str(d)
        assert released["session"]["metadata"].get("operator_control") is None
        assert client.post(f"/api/sessions/{sid}/control", json={"command": "release_control"}).status_code == 409

    events = client.get(f"/api/sessions/{sid}/events").json()
    frames = [e for e in events if e["type"] == "frame"]
    assert frames[-1]["data"]["kind"] == "after_control"
    kinds = [e["type"] for e in events]
    last_frame, last_control = len(kinds) - 1 - kinds[::-1].index("frame"), len(kinds) - 1 - kinds[::-1].index("operator.control")
    assert last_frame < last_control  # the screen the operator left is on record before the hand-back event
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "paused"

    # Resume while in control hands back automatically, and the agent is told what the operator did.
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "take_control"}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/manual", json={"action": "key", "text": "Escape"}).json()["ok"]
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "resume"}).status_code == 200
    s = wait_terminal(client, sid)
    assert s["status"] == "completed", s
    assert s["metadata"].get("operator_control") is None
    oc = [e["data"] for e in client.get(f"/api/sessions/{sid}/events?types=operator.control").json()]
    assert [d["state"] for d in oc] == ["taken", "released", "taken", "released"]
    assert oc[-1]["resumed"] is False and oc[-1]["keys"] == 1
    # Control cannot be taken once the session is over.
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "take_control"}).status_code == 409


def test_take_control_while_running_waits_for_the_next_checkpoint(client):
    script = [{"actions": [{"action": "wait", "duration": 0.3}]} for _ in range(40)]
    r = client.post("/api/sessions", json={"task": "slow", "backend": "simulated", "model": "scripted", "script": script})
    sid = r.json()["id"]
    wait_status(client, sid, "running")
    r = client.post(f"/api/sessions/{sid}/control", json={"command": "take_control"})
    assert r.status_code == 200 and r.json()["control"]["active"] is True
    taken = client.get(f"/api/sessions/{sid}/events?types=operator.control").json()[0]["data"]
    assert taken["state"] == "taken" and taken["waiting_for_pause"] is True
    wait_status(client, sid, "paused")
    assert client.get(f"/api/sessions/{sid}").json()["metadata"]["operator_control"]["active"] is True
    assert client.post(f"/api/sessions/{sid}/control", json={"command": "cancel"}).status_code == 200
    s = wait_terminal(client, sid)
    assert s["status"] == "cancelled" and s["metadata"].get("operator_control") is None
