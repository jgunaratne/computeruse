"""Agent flows: graph validation, the API, and runs end to end.

Offline runs use scripted computer agents on the simulated desktop (the offline-demo template's
reference scripts). Model agents and critics talk to `FakeLS`, the in-memory Antigravity Language
Server, whose replies are consumed in message order — so those flows are built to call it sequentially.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path

import pytest
from fake_ls import ENDPOINT, MODELS, FakeLS, SlowLS, planner
from fastapi.testclient import TestClient

from computeruse.agent import providers
from computeruse.agent.antigravity import parse_model_configs
from computeruse.config import Settings
from computeruse.flows.engine import parse_verdict
from computeruse.flows.spec import NODE_TYPES, Flow, validate_flow
from computeruse.flows.templates import COL, TEMPLATES
from computeruse.server.app import create_app

TERMINAL = {"completed", "incomplete", "failed", "cancelled", "interrupted"}
CALC = "simulated-basics/calc_multiply"
NOTES = "simulated-basics/notes_type_save"


def node(node_id: str, type_: str, label: str = "", **config) -> dict:
    return {"id": node_id, "type": type_, "label": label, "pos": [0, 0], "config": config}


def wire(source: str, source_port: str, target: str, target_port: str) -> dict:
    return {"source": source, "source_port": source_port, "target": target, "target_port": target_port}


def create(c: TestClient, nodes: list[dict], edges: list[dict], **extra) -> dict:
    r = c.post("/api/flows", json={"name": "test", "nodes": nodes, "edges": edges, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def run_flow(c: TestClient, flow_id: str, timeout: float = 60, **body) -> dict:
    r = c.post(f"/api/flows/{flow_id}/runs", json=body)
    assert r.status_code == 202, r.text
    run = r.json()
    t0 = time.time()
    while run["status"] not in TERMINAL:
        assert time.time() - t0 < timeout, f"run did not finish: {run['status']}"
        run = c.get(f"/api/flow-runs/{run['id']}", params={"after": run["version"], "wait_s": 2}).json()
    return run


def messages(run: dict) -> list[tuple[str, str, str, str]]:
    return [(e["source"], e["source_port"], e["target"], e["target_port"]) for e in run["log"] if e["type"] == "message"]


# -- Antigravity-backed app ---------------------------------------------------------------------------


def antigravity_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", web_dist=tmp_path / "nodist", anthropic_api_key=None,  # type: ignore[call-arg]
                    gemini_api_key=None, model_provider="antigravity", antigravity_address="localhost:5387",
                    antigravity_csrf_token="tok-123", model_discovery="off", _env_file=None)


@pytest.fixture
def ag_app(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "detect_antigravity",
                        lambda address, token: (ENDPOINT, "Antigravity Language Server at localhost:5387",
                                                parse_model_configs(MODELS)))

    @contextmanager
    def start(ls: FakeLS):
        with TestClient(create_app(antigravity_settings(tmp_path))) as c:
            c.app.state.orch.chat.transport = ls.transport()
            yield c

    return start


# -- spec -------------------------------------------------------------------------------------------------


def test_validation_normalises_configs_and_names_bad_wires():
    flow = Flow.model_validate({"id": "f", "name": "x", "nodes": [
        node("g", "goal", text="hi"), node("w", "computer", max_steps=999999, bogus=1, backend=None),
        node("o", "output")], "edges": [wire("g", "task", "w", "task"), wire("w", "result", "o", "in")]})
    flow = validate_flow(flow)
    w = flow.node("w")
    assert w is not None and w.config["max_steps"] == 5000 and "bogus" not in w.config and w.config["backend"] == ""
    assert [e.id for e in flow.edges] == ["g.task>w.task", "w.result>o.in"]

    def bad(nodes, edges, match):
        with pytest.raises(ValueError, match=match):
            validate_flow(Flow.model_validate({"id": "f", "name": "x", "nodes": nodes, "edges": edges}))

    bad([node("a", "goal"), node("a", "output")], [], "duplicate node id")
    bad([node("a", "wizard")], [], "unknown type")
    bad([node("g", "goal")], [wire("g", "task", "nope", "in")], "does not exist")
    bad([node("j", "join")], [wire("j", "out", "j", "in")], "to itself")
    bad([node("w", "computer"), node("o", "output")], [wire("w", "screen", "o", "in")], "port types differ")
    bad([node("g", "goal"), node("o", "output")], [wire("g", "result", "o", "in")], "no output 'result'")
    bad([node("g", "goal"), node("o", "output")], [wire("g", "task", "o", "context")], "no input 'context'")
    bad([node("g", "goal"), node("o", "output")], [wire("g", "task", "o", "in"), wire("g", "task", "o", "in")],
        "duplicate wire")


def test_ports_declare_which_inputs_trigger_work():
    computer = NODE_TYPES["computer"]
    assert computer.input("task").trigger and not computer.input("context").trigger
    assert [p.name for p in computer.outputs] == ["result", "screen"]
    assert not NODE_TYPES["critic"].input("screen").trigger and NODE_TYPES["critic"].input("in").trigger
    assert NODE_TYPES["critic"].needs_antigravity and not NODE_TYPES["computer"].needs_antigravity


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_templates_are_valid_and_laid_out_on_the_grid(template_id):
    for backend in ("browser", "simulated"):
        nodes, edges = TEMPLATES[template_id].build(backend)
        flow = validate_flow(Flow.model_validate({"id": "t", "name": "t", "nodes": nodes, "edges": edges}))
        # Only goals (and notes) start cold; everything else is fed by at least one wire.
        assert {n.id for n in flow.nodes if n.type not in ("goal", "note")} <= {e.target for e in flow.edges}
        # Cards sit in the grid's columns, far enough apart that they don't stack on one another.
        assert {n.pos[0] for n in flow.nodes} <= set(COL)
        for x in COL:
            ys = sorted(n.pos[1] for n in flow.nodes if n.pos[0] == x)
            assert all(b - a >= 200 for a, b in pairwise(ys)), (template_id, x, ys)


def test_parse_verdict_is_conservative():
    assert parse_verdict("**VERDICT: PASS**\nLooks right.") == ("pass", "Looks right.")
    assert parse_verdict("### Verdict: `PASS`\nGood") == ("pass", "Good")
    assert parse_verdict("VERDICT: REVISE\n\nThe price is missing.") == ("revise", "The price is missing.")
    assert parse_verdict("I think it passes") == ("revise", "I think it passes")  # no verdict line: not a pass


# -- API ----------------------------------------------------------------------------------------------------


def test_catalog_describes_nodes_ports_and_templates(settings):
    with TestClient(create_app(settings)) as c:
        cat = c.get("/api/flows/catalog").json()
        types = {t["type"]: t for t in cat["node_types"]}
        assert set(types) == {"goal", "computer", "model", "critic", "join", "journal", "output", "note"}
        assert [(p["name"], p["trigger"]) for p in types["computer"]["inputs"]] == [("task", True), ("context", False)]
        backend = next(f for f in types["computer"]["fields"] if f["key"] == "backend")
        assert backend["default"] in {b["id"] for b in c.get("/api/backends").json()}
        assert {t["id"] for t in cat["templates"]} == {"iterative-loop", "verifiable-goal", "fan-out", "offline-demo"}
        assert set(cat["port_types"]) == {"text", "screen"}
        assert cat["antigravity"]["available"] is False and cat["antigravity"]["models"] == []
        assert {r["value"] for r in cat["roles"]} >= {"director", "researcher", "summarizer", "custom"}


def test_flow_crud(settings):
    with TestClient(create_app(settings)) as c:
        blank = c.post("/api/flows", json={}).json()
        assert blank["name"] == "Untitled flow" and blank["nodes"] == [] and blank["settings"]["max_agent_calls"] == 24
        demo = c.post("/api/flows", json={"template": "offline-demo"}).json()
        assert demo["name"] == "Parallel workers (offline)" and demo["template"] == "offline-demo"
        assert len(demo["nodes"]) == 7 and len(demo["edges"]) == 8
        assert c.post("/api/flows", json={"template": "nope"}).status_code == 404
        assert [f["id"] for f in c.get("/api/flows").json()] == [demo["id"], blank["id"]]

        body = {**demo, "name": "Renamed", "viewport": {"x": 10, "y": 20, "zoom": 0.5}}
        body["nodes"][1]["pos"] = [123, 456]
        saved = c.put(f"/api/flows/{demo['id']}", json=body).json()
        assert saved["name"] == "Renamed" and saved["nodes"][1]["pos"] == [123, 456]
        assert saved["created_at"] == demo["created_at"] and saved["updated_at"] >= demo["updated_at"]
        assert c.get(f"/api/flows/{demo['id']}").json()["viewport"]["zoom"] == 0.5

        bad = {**body, "edges": [*body["edges"], wire("calc", "screen", "join", "in")]}
        r = c.put(f"/api/flows/{demo['id']}", json=bad)
        assert r.status_code == 400 and "port types differ" in r.json()["detail"]
        r = c.put(f"/api/flows/{demo['id']}", json={**body, "name": ""})
        assert r.status_code == 400 and "name" in r.json()["detail"]
        assert c.put("/api/flows/missing", json=body).status_code == 404
        assert c.get("/api/flows/bad.id").status_code == 404

        assert c.delete(f"/api/flows/{blank['id']}").status_code == 204
        assert c.get(f"/api/flows/{blank['id']}").status_code == 404
        assert [f["id"] for f in c.get("/api/flows").json()] == [demo["id"]]


def test_preflight_names_the_node_that_cannot_run(settings):
    with TestClient(create_app(settings)) as c:
        def start(nodes, edges=(), **body):
            flow = create(c, nodes, list(edges))
            return c.post(f"/api/flows/{flow['id']}/runs", json=body)

        r = start([node("o", "output")])
        assert r.status_code == 400 and "add a Goal" in r.json()["detail"]
        r = start([node("g", "goal", "Ask")])
        assert r.status_code == 400 and "'Ask' is empty" in r.json()["detail"]
        r = start([node("g", "goal", text="hi"), node("m", "model", "Planner")], [wire("g", "task", "m", "in")])
        assert r.status_code == 400 and "'Planner' needs a running Antigravity" in r.json()["detail"]
        r = start([node("g", "goal", text="hi"), node("w", "computer", "W", demo_task="simulated-basics/nope")])
        assert r.status_code == 400 and "unknown demo task" in r.json()["detail"]
        r = start([node("g", "goal", text="hi"), node("w", "computer", "W", backend="mainframe")])
        assert r.status_code == 400 and "unknown computer 'mainframe'" in r.json()["detail"]
        r = start([node("g", "goal", text="hi"), node("w", "computer", "W", backend="simulated")])
        assert r.status_code == 400 and r.json()["detail"].startswith("'W': ")  # no model provider configured
        # a muted agent is not checked, and a run-level goal fills an empty goal
        r = start([node("g", "goal"), {**node("m", "model"), "muted": True}], goal="override")
        assert r.status_code == 202


def test_offline_parallel_workers_run_to_completion(settings):
    with TestClient(create_app(settings)) as c:
        flow = c.post("/api/flows", json={"template": "offline-demo"}).json()
        run = run_flow(c, flow["id"])
        assert run["status"] == "completed", run["reason"]
        assert run["reason"] == "every output received a result" and run["invocations"] == 2
        nodes = run["nodes"]
        for wid in ("calc", "notes"):
            assert nodes[wid]["status"] == "done" and nodes[wid]["runs"] == 1 and nodes[wid]["steps"] > 0
            assert nodes[wid]["session_id"] and nodes[wid]["frame"]["url"].endswith(".png")
        assert nodes["join"]["expected"] == 2 and nodes["join"]["status"] == "done"
        assert nodes["journal"]["entries"] == 2
        out = run["results"][0]
        assert out["node"] == "output" and out["frame"] is not None
        assert "## Calculator agent\nThe display shows 84." in out["text"]
        assert "## Notes agent\nThe status line shows Saved. Done." in out["text"]
        hops = messages(run)
        assert ("goal", "task", "calc", "task") in hops and ("join", "out", "output", "in") in hops
        assert ("calc", "screen", "output", "screen") in hops

        session = c.get(f"/api/sessions/{nodes['calc']['session_id']}").json()
        assert "flow" in session["tags"] and session["metadata"]["flow"]["run_id"] == run["id"]
        assert session["task"].startswith("Run both SimOS demo tasks") and session["status"] == "completed"

        runs = c.get(f"/api/flows/{flow['id']}/runs").json()
        assert runs[0]["id"] == run["id"] and runs[0]["status"] == "completed"
        assert c.get("/api/flows").json()[0]["last_run"]["status"] == "completed"
        run_dir = settings.data_dir / "flow_runs" / run["id"]
        assert json.loads((run_dir / "run.json").read_text())["status"] == "completed"
        journal = (run_dir / "journal-journal.md").read_text()
        assert "Calculator agent (result)" in journal and "Notes agent (result)" in journal


def test_latched_context_reaches_the_agent_without_starting_it(settings):
    with TestClient(create_app(settings)) as c:
        flow = create(c, [
            node("g", "goal", text="Compute twelve times seven."),
            node("j", "journal", seed="Remember: report only the number."),
            node("w", "computer", "Worker", demo_task=CALC),
            node("o", "output"),
        ], [wire("g", "task", "w", "task"), wire("g", "task", "j", "append"), wire("j", "context", "w", "context"),
            wire("w", "result", "o", "in")])
        run = run_flow(c, flow["id"])
        assert run["status"] == "completed" and run["nodes"]["w"]["runs"] == 1  # journal updates never started it
        task = c.get(f"/api/sessions/{run['nodes']['w']['session_id']}").json()["task"]
        assert task.startswith("Compute twelve times seven.\n\nContext from the rest of the team:\nFrom Journal:")
        assert "Remember: report only the number." in task


def test_muted_nodes_drop_messages_and_joins_stop_waiting_for_them(settings):
    with TestClient(create_app(settings)) as c:
        demo = c.post("/api/flows", json={"template": "offline-demo"}).json()
        for n in demo["nodes"]:
            n["muted"] = n["id"] == "notes"
        c.put(f"/api/flows/{demo['id']}", json=demo)
        run = run_flow(c, demo["id"])
        assert run["status"] == "completed" and run["invocations"] == 1
        assert run["nodes"]["notes"]["status"] == "muted" and run["nodes"]["notes"]["sessions"] == []
        assert run["nodes"]["join"]["expected"] == 1
        dropped = [e for e in run["log"] if e["type"] == "message" and e["target"] == "notes"]
        assert dropped and all(e["dropped"] for e in dropped)
        assert run["results"][0]["text"].startswith("## Calculator agent")


def test_a_flow_that_goes_quiet_is_incomplete(settings):
    with TestClient(create_app(settings)) as c:
        flow = create(c, [
            node("g", "goal", text="go"), node("w", "computer", "Worker", demo_task=CALC),
            node("j", "journal"), node("join", "join", "Join"), node("o", "output", "Report"),
        ], [wire("g", "task", "w", "task"), wire("w", "result", "join", "in"), wire("j", "context", "join", "in"),
            wire("join", "out", "o", "in")])
        run = run_flow(c, flow["id"])
        assert run["status"] == "incomplete" and "Report" in run["reason"] and run["results"] == []
        assert run["nodes"]["join"]["status"] == "stopped" and run["nodes"]["join"]["detail"] == "1 of 2 in"


def test_runs_left_running_by_a_previous_server_are_marked_interrupted(settings):
    run_dir = settings.data_dir / "flow_runs" / "abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"id": "abc123", "flow_id": "f1", "status": "running",
                                                  "started_at": 1.0}))
    with TestClient(create_app(settings)) as c:
        run = c.get("/api/flow-runs/abc123").json()
        assert run["status"] == "interrupted" and run["ended_at"]
        assert c.get("/api/flow-runs/nope").status_code == 404


# -- with model agents -------------------------------------------------------------------------------------


def test_director_worker_critic_loop(ag_app):
    ls = FakeLS(replies=[
        [planner("Open the calculator and compute 12 * 7.")],
        [planner("VERDICT: REVISE\nThe report does not show the number.")],
        [planner("Compute 12 * 7 again and read the display.")],
        [planner("VERDICT: PASS\nThe display shows 84.")],
    ])
    with ag_app(ls) as c:
        flow = create(c, [
            node("goal", "goal", text="What is 12 * 7? Use the calculator."),
            node("director", "model", "Director", role="director"),
            node("worker", "computer", "Worker", demo_task=CALC),
            node("critic", "critic", "Critic", rubric="The number is reported.", max_rounds=3),
            node("out", "output", "Done"),
        ], [wire("goal", "task", "director", "in"), wire("goal", "task", "critic", "context"),
            wire("director", "out", "worker", "task"), wire("worker", "result", "critic", "in"),
            wire("worker", "screen", "critic", "screen"), wire("critic", "pass", "out", "in"),
            wire("critic", "revise", "director", "in")])
        run = run_flow(c, flow["id"])
        assert run["status"] == "completed", run["reason"]
        n = run["nodes"]
        assert n["critic"]["rounds"] == 2 and n["critic"]["verdict"] == "pass"
        assert n["director"]["runs"] == 2 and len(n["worker"]["sessions"]) == 2
        assert run["invocations"] == 6
        assert run["results"][0]["text"] == "The display shows 84." and run["results"][0]["verdict"] == "pass"

        starts = ls.bodies("StartCascade")
        assert len(starts) == 2  # the director keeps one conversation; so does the critic
        sends = ls.bodies("SendUserCascadeMessage")
        assert len(sends) == 4
        first_critic = sends[1]["items"][0]["text"]
        assert first_critic.startswith("[Goal and context]\nFrom Goal:\nWhat is 12 * 7?")
        assert "[Work to judge: round 1 of at most 3, from Worker]\nThe display shows 84." in first_critic
        assert len(sends[1]["media"]) == 1  # the worker's final screen, for visual verification
        second_director = sends[2]["items"][0]["text"]
        assert second_director.startswith("[Message from Critic]\nRound 1 was not accepted.")
        task = c.get(f"/api/sessions/{n['worker']['sessions'][1]}").json()["task"]
        assert task == "Compute 12 * 7 again and read the display."
        assert all(ls.annotations[cid]["archived"] for cid in ("casc-1", "casc-2"))


def test_critic_stops_after_max_rounds(ag_app):
    ls = FakeLS(replies=[[planner("VERDICT: REVISE\nNo.")], [planner("draft two")], [planner("VERDICT: REVISE\nStill no.")]])
    with ag_app(ls) as c:
        flow = create(c, [
            node("g", "goal", text="draft one"), node("critic", "critic", "Critic", max_rounds=2),
            node("fixer", "model", "Fixer", role="custom", instructions="Improve the draft."), node("o", "output"),
        ], [wire("g", "task", "critic", "in"), wire("critic", "revise", "fixer", "in"), wire("fixer", "out", "critic", "in"),
            wire("critic", "pass", "o", "in")])
        run = run_flow(c, flow["id"])
        assert run["status"] == "incomplete" and "max rounds" in run["reason"], run["reason"]
        assert run["nodes"]["critic"]["status"] == "stopped" and run["nodes"]["critic"]["verdict"] == "max_rounds"
        assert run["results"][0] == {**run["results"][0], "text": "draft two", "verdict": "max_rounds"}
        assert run["nodes"]["o"]["status"] == "stopped"


def test_agent_call_budget_stops_a_runaway_loop(ag_app):
    ls = FakeLS(replies=[[planner("ping")], [planner("pong")], [planner("ping")]])
    with ag_app(ls) as c:
        flow = create(c, [node("g", "goal", text="start"), node("a", "model", "A", role="custom"),
                          node("b", "model", "B", role="custom")],
                      [wire("g", "task", "a", "in"), wire("a", "out", "b", "in"), wire("b", "out", "a", "in")],
                      settings={"max_agent_calls": 3})
        run = run_flow(c, flow["id"])
        assert run["status"] == "failed" and "all 3 agent calls" in run["reason"]
        assert run["invocations"] == 3 and len(ls.bodies("SendUserCascadeMessage")) == 3


def test_cancel_stops_the_run_and_the_server_side_turn(ag_app):
    ls = SlowLS(replies=[[planner("late")]])
    with ag_app(ls) as c:
        flow = create(c, [node("g", "goal", text="slow"), node("m", "model", "Slow"), node("o", "output")],
                      [wire("g", "task", "m", "in"), wire("m", "out", "o", "in")])
        run = c.post(f"/api/flows/{flow['id']}/runs", json={}).json()
        t0 = time.time()
        while not ls.waiting.is_set():
            assert time.time() - t0 < 10
            time.sleep(0.02)
        r = c.post(f"/api/flows/{flow['id']}/runs", json={})
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        run = c.post(f"/api/flow-runs/{run['id']}/cancel").json()
        assert run["status"] == "cancelled" and run["reason"] == "stopped by the user"
        assert run["nodes"]["m"]["status"] == "stopped" and run["results"] == []
        assert ls.cancelled == ["casc-1"]
