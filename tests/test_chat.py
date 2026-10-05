"""Chat panel: the API, the session-grounding context and the conversation lifecycle, against `FakeLS`.

The app runs with `COMPUTERUSE_MODEL_PROVIDER=antigravity`, the Antigravity probe is replaced by a canned
detection, and the chat service talks to an in-memory Language Server through an injected transport —
no socket is ever opened.
"""

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fake_ls import ENDPOINT, MODELS, FakeLS, SlowLS, error_step, planner
from fastapi.testclient import TestClient

from computeruse.agent import providers
from computeruse.agent.antigravity import parse_model_configs
from computeruse.config import Settings
from computeruse.server.app import create_app
from computeruse.server.chat import (
    CHAT_SYSTEM,
    CONTEXT_MARKER,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_LINES,
    describe_event,
    latest_frame,
    session_context,
)
from computeruse.telemetry.events import Event, EventType, SessionRecord, SessionStatus

SCRIPT = [
    {"text": "Opening Notes.", "actions": [{"action": "left_click", "target": "dock.notes"}]},
    {"actions": [{"action": "type", "text": "hi"}, {"action": "key", "text": "ctrl+s"}]},
    {"text": "Done.", "done": True},
]


def antigravity_settings(tmp_path: Path, **overrides) -> Settings:
    kw = dict(data_dir=tmp_path / "data", web_dist=tmp_path / "nodist", anthropic_api_key=None, gemini_api_key=None,
              model_provider="antigravity", antigravity_address="localhost:5387", antigravity_csrf_token="tok-123",
              model_discovery="off", _env_file=None)
    kw.update(overrides)
    return Settings(**kw)  # type: ignore[call-arg]


@pytest.fixture
def detected(monkeypatch):
    monkeypatch.setattr(providers, "detect_antigravity",
                        lambda address, token: (ENDPOINT, "Antigravity Language Server at localhost:5387 (4 models)",
                                                parse_model_configs(MODELS)))


@pytest.fixture
def chat_app(tmp_path, detected):
    @contextmanager
    def start(ls: FakeLS, **overrides):
        with TestClient(create_app(antigravity_settings(tmp_path, **overrides))) as c:
            c.app.state.orch.chat.transport = ls.transport()
            yield c

    return start


def settle(c: TestClient, chat_id: str, timeout: float = 10) -> dict:
    """Long-poll until the pending reply has landed."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chat = c.get(f"/api/chat/{chat_id}", params={"wait_s": 2}).json()
        if not chat["pending"]:
            return chat
    raise TimeoutError(chat_id)


def run_session(c: TestClient, task: str = "Type hi into Notes and save.") -> dict:
    r = c.post("/api/sessions", json={"task": task, "backend": "simulated", "model": "scripted", "script": SCRIPT})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    t0 = time.time()
    while time.time() - t0 < 30:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["terminal"]:
            return s
        time.sleep(0.05)
    raise TimeoutError(sid)


# -- API -----------------------------------------------------------------------------------------------


def test_chat_is_unavailable_without_antigravity(settings):
    with TestClient(create_app(settings)) as c:
        idx = c.get("/api/chat").json()
        assert idx["available"] is False and idx["models"] == [] and idx["chats"] == [] and idx["default_model"] is None
        assert "disabled by COMPUTERUSE_MODEL_PROVIDER=none" in idx["reason"]
        r = c.post("/api/chat", json={})
        assert r.status_code == 503 and "needs a running Antigravity" in r.json()["detail"]
        assert c.post("/api/chat/nope/messages", json={"text": "hi"}).status_code == 404
        assert c.get("/api/chat/nope").status_code == 404
        assert c.post("/api/chat/nope/cancel").status_code == 404
        assert c.delete("/api/chat/nope").status_code == 404


def test_chat_round_trip_uses_a_tool_less_conversation(chat_app):
    ls = FakeLS(replies=[[planner("**Hi!** I can help.", thinking="greet")]])
    with chat_app(ls) as c:
        idx = c.get("/api/chat").json()
        assert idx["available"] is True and idx["default_model"] == "gemini-flash-lite"  # first usable "Flash"
        models = {m["id"]: m for m in idx["models"]}
        assert list(models) == ["gemini-pro-high", "sonnet-high", "gemini-flash-lite", "text-only"]  # disabled hidden
        assert models["sonnet-high"]["available"] is False
        assert models["sonnet-high"]["reason"] == "quota exhausted until 2026-10-05T20:00:00Z"
        assert models["text-only"]["supports_images"] is False and models["text-only"]["available"] is True
        assert models["gemini-flash-lite"]["reason"].startswith("listed by Antigravity · quota 93% left")

        r = c.post("/api/chat", json={})
        assert r.status_code == 201, r.text
        chat = r.json()
        cid = chat["id"]
        assert chat["model"] == "gemini-flash-lite" and chat["model_label"] == "Gemini Flash Lite"
        assert chat["title"] is None and chat["pending"] is False and chat["messages"] == []
        assert ls.calls == []  # nothing talks to Antigravity until the first message

        r = c.post(f"/api/chat/{cid}/messages", json={"text": "  Hello there\nsecond line  "})
        assert r.status_code == 202, r.text
        accepted = r.json()
        assert accepted["user_message"]["role"] == "user" and accepted["user_message"]["text"] == "Hello there\nsecond line"
        assert accepted["user_message"]["context"] is None
        assert accepted["message"]["role"] == "assistant" and accepted["message"]["model"] == "gemini-flash-lite"
        assert "messages" not in accepted["chat"] and accepted["chat"]["title"] == "Hello there"

        chat = settle(c, cid)
        user, assistant = chat["messages"]
        assert user["id"] == accepted["user_message"]["id"] and assistant["id"] == accepted["message"]["id"]
        assert assistant["pending"] is False and assistant["error"] is None
        assert assistant["text"] == "**Hi!** I can help." and assistant["thinking"] == "greet"
        assert assistant["usage"]["input_tokens"] == 2380 and assistant["usage"]["output_tokens"] == 495
        assert assistant["latency_ms"] >= 0 and assistant["stop_reason"] == "STOP_REASON_STOP_PATTERN"
        assert assistant["model"] == "gemini-flash-lite" and assistant["model_label"] == "Gemini Flash Lite"
        assert chat["conversation_id"] == "casc-1" and chat["message_count"] == 2 and chat["sessions"] == []

        start = ls.bodies("StartCascade")[0]
        spec = start["customAgentSpec"]
        assert spec["customAgent"] == {"systemPromptSections": [{"title": "CHAT", "content": CHAT_SYSTEM}],
                                       "toolNames": [], "excludeDefaultComponents": True}
        assert spec["commandExecutionPolicy"] == "off"
        assert spec["cascadeConfig"]["plannerConfig"]["planModel"] == "MODEL_PLACEHOLDER_M1"
        assert start["tags"] == ["computeruse", "chat"]
        assert ls.annotations["casc-1"] == {"title": "computeruse chat · Hello there"}
        send = ls.bodies("SendUserCascadeMessage")[0]
        assert send["items"] == [{"text": "Hello there\nsecond line"}] and "media" not in send
        assert send["blocking"] is True and send["cascadeConfig"]["plannerConfig"]["planModel"] == "MODEL_PLACEHOLDER_M1"
        assert "tok-123" not in c.get(f"/api/chat/{cid}").text

        idx = c.get("/api/chat").json()
        assert [x["id"] for x in idx["chats"]] == [cid] and idx["chats"][0]["message_count"] == 2
        assert "messages" not in idx["chats"][0]

        assert c.delete(f"/api/chat/{cid}").status_code == 204
        assert ls.annotations["casc-1"]["archived"] is True and ls.cancelled == []
        assert c.get(f"/api/chat/{cid}").status_code == 404 and c.delete(f"/api/chat/{cid}").status_code == 404
        assert c.get("/api/chat").json()["chats"] == []


def test_chat_switches_model_per_message_and_validates_references(chat_app):
    ls = FakeLS(replies=[[planner("one", model="MODEL_PLACEHOLDER_M1")], [planner("two", model="MODEL_PLACEHOLDER_M2")],
                         [planner("three", model="MODEL_PLACEHOLDER_M2")]])
    with chat_app(ls) as c:
        cid = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{cid}/messages", json={"text": "a"})
        assert settle(c, cid)["messages"][-1]["model"] == "gemini-flash-lite"
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "b", "model": "antigravity:Gemini Pro (High)"})
        assert r.status_code == 202, r.text
        chat = settle(c, cid)
        assert chat["messages"][-1]["model"] == "gemini-pro-high" and chat["model"] == "gemini-pro-high"
        c.post(f"/api/chat/{cid}/messages", json={"text": "c"})  # sticks to the switched model
        assert settle(c, cid)["messages"][-1]["model"] == "gemini-pro-high"
        plan = [s["cascadeConfig"]["plannerConfig"]["planModel"] for s in ls.bodies("SendUserCascadeMessage")]
        assert plan == ["MODEL_PLACEHOLDER_M1", "MODEL_PLACEHOLDER_M2", "MODEL_PLACEHOLDER_M2"]
        assert len(ls.bodies("StartCascade")) == 1  # same conversation throughout

        for ref, msg in [("gemini-4", "does not offer model 'gemini-4'"), ("retired", "does not offer"),
                         ("sonnet-high", "quota for 'Claude Sonnet (High)' is exhausted until 2026-10-05T20:00:00Z")]:
            r = c.post(f"/api/chat/{cid}/messages", json={"text": "x", "model": ref})
            assert r.status_code == 400 and msg in r.json()["detail"], (ref, r.text)
        assert c.post("/api/chat", json={"model": "gemini-4"}).status_code == 400
        assert c.post("/api/chat", json={"model": "MODEL_PLACEHOLDER_M999"}).json()["model"] == "text-only"
        assert c.post(f"/api/chat/{cid}/messages", json={"text": "   "}).status_code == 400
        assert c.post(f"/api/chat/{cid}/messages", json={"text": ""}).status_code == 422
        assert len(ls.bodies("SendUserCascadeMessage")) == 3  # none of the rejected requests reached Antigravity
    assert ls.annotations["casc-1"]["archived"] is True  # server shutdown archives what is left


def test_default_chat_model_can_be_configured(chat_app):
    with chat_app(FakeLS(), chat_model="antigravity:gemini-pro-high") as c:
        assert c.get("/api/chat").json()["default_model"] == "gemini-pro-high"
        assert c.post("/api/chat", json={}).json()["model"] == "gemini-pro-high"
    with chat_app(FakeLS(), chat_model="no-such-model") as c:  # falls back instead of failing
        assert c.get("/api/chat").json()["default_model"] == "gemini-flash-lite"


def test_chat_attaches_unseen_session_context_and_the_latest_screenshot(chat_app):
    ls = FakeLS(replies=[[planner("It typed hi.")], [planner("Nothing new.")], [planner("Text only.")],
                         [planner("Fresh chat.")]])
    with chat_app(ls) as c:
        session = run_session(c)
        sid = session["id"]
        assert session["status"] == "completed" and session["steps"] == 3
        events = c.get(f"/api/sessions/{sid}/events").json()
        frames = [e for e in events if e["type"] == "frame"]
        last_frame = frames[-1]["data"]["seq"]
        png = c.get(f"/api/sessions/{sid}/frames/{last_frame}.png").content

        cid = c.post("/api/chat", json={}).json()["id"]
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "What happened?", "session_id": sid})
        assert r.status_code == 202, r.text
        ctx = r.json()["user_message"]["context"]
        assert ctx["session_id"] == sid and ctx["first"] is True and ctx["screenshot"] is True
        assert ctx["after_seq"] == -1 and ctx["until_seq"] == events[-1]["seq"] and ctx["events"] == len(events)
        chat = settle(c, cid)
        assert chat["messages"][-1]["text"] == "It typed hi." and chat["sessions"] == [sid]
        assert chat["messages"][0]["text"] == "What happened?"  # the transcript keeps the user's own words only

        send = ls.bodies("SendUserCascadeMessage")[0]
        text = send["items"][0]["text"]
        assert text.startswith(f"What happened?\n\n{CONTEXT_MARKER}: session {sid} · completed · 3 steps, 3 turns]\n")
        assert "Task: Type hi into Notes and save.\nBackend: simulated · model: scripted\nTimeline:\n" in text
        assert "- Session started on the simulated backend with model scripted, display " in text
        assert "- Agent: Opening Notes.\n" in text
        assert "- Step 0: " in text and "- Step 2: " in text and " — ok" in text
        assert "- Session ended: completed" in text and "Agent's final message: Done." in text
        assert text.rstrip().endswith(f"[screenshot attached: the latest screen of session {sid} (frame {last_frame})]")
        assert send["media"][0]["mimeType"] == "image/png" and send["media"][0]["inlineData"] == base64.b64encode(png).decode()
        assert "model.called" not in text and "turn.started" not in text

        # nothing happened since: no screenshot, an explicit "no changes" note, the seen marker holds
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "Anything new?", "session_id": sid})
        ctx = r.json()["user_message"]["context"]
        assert ctx == {"session_id": sid, "events": 0, "first": False, "screenshot": False,
                       "after_seq": events[-1]["seq"], "until_seq": events[-1]["seq"]}
        settle(c, cid)
        send = ls.bodies("SendUserCascadeMessage")[1]
        assert "media" not in send
        assert send["items"][0]["text"] == (f"Anything new?\n\n{CONTEXT_MARKER}: session {sid} · completed · 3 steps, 3 turns]\n"
                                            "No new timeline entries since your previous reply.")

        # text-only models get the timeline but no image
        tid = c.post("/api/chat", json={"model": "text-only"}).json()["id"]
        r = c.post(f"/api/chat/{tid}/messages", json={"text": "Describe the screen", "session_id": sid})
        ctx = r.json()["user_message"]["context"]
        assert ctx["screenshot"] is False and ctx["note"] == "Text Only does not accept images; screenshot not attached"
        settle(c, tid)
        send = ls.bodies("SendUserCascadeMessage")[2]
        assert "media" not in send and "[screenshot attached" not in send["items"][0]["text"]

        # screenshot=false: timeline only, even on a fresh chat
        fid = c.post("/api/chat", json={}).json()["id"]
        r = c.post(f"/api/chat/{fid}/messages", json={"text": "Summarise", "session_id": sid, "screenshot": False})
        assert r.json()["user_message"]["context"]["screenshot"] is False
        settle(c, fid)
        send = ls.bodies("SendUserCascadeMessage")[3]
        assert "media" not in send and "Task: Type hi into Notes and save." in send["items"][0]["text"]

        r = c.post(f"/api/chat/{cid}/messages", json={"text": "x", "session_id": "nope"})
        assert r.status_code == 404 and "unknown session" in r.json()["detail"]


def test_chat_keeps_context_unseen_when_the_reply_fails(chat_app):
    ls = FakeLS(replies=[[error_step("Model quota exceeded for Gemini Flash Lite")], [planner("Recovered.")]])
    with chat_app(ls) as c:
        sid = run_session(c)["id"]
        cid = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{cid}/messages", json={"text": "Why?", "session_id": sid})
        chat = settle(c, cid)
        assert chat["messages"][-1]["error"] == "Antigravity (Gemini Flash Lite): Model quota exceeded for Gemini Flash Lite"
        assert chat["messages"][-1]["text"] == "" and chat["pending"] is False and chat["sessions"] == []
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "Again?", "session_id": sid})
        assert r.json()["user_message"]["context"]["first"] is True  # re-attached in full
        chat = settle(c, cid)
        assert chat["messages"][-1]["text"] == "Recovered." and chat["sessions"] == [sid]
        assert "Task: Type hi into Notes and save." in ls.bodies("SendUserCascadeMessage")[1]["items"][0]["text"]


def test_chat_nudges_once_on_an_empty_reply_then_reports_it(chat_app):
    ls = FakeLS(replies=[[planner("", thinking="…")], [planner("After nudge.")], [planner("")], []])
    with chat_app(ls) as c:
        cid = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{cid}/messages", json={"text": "hello"})
        chat = settle(c, cid)
        assert chat["messages"][-1]["text"] == "After nudge." and chat["messages"][-1]["error"] is None
        sends = ls.bodies("SendUserCascadeMessage")
        assert len(sends) == 2 and sends[1]["items"][0]["text"].startswith("Your reply was empty")
        c.post(f"/api/chat/{cid}/messages", json={"text": "again"})
        chat = settle(c, cid)
        assert chat["messages"][-1]["error"] == "Antigravity (Gemini Flash Lite) returned no reply (stop reason STOP_REASON_STOP_PATTERN)"
        assert len(ls.bodies("SendUserCascadeMessage")) == 4


def test_chat_cancel_stops_the_server_side_turn_and_rejects_concurrent_sends(chat_app):
    ls = SlowLS(replies=[[planner("late")]])
    with chat_app(ls) as c:
        cid = c.post("/api/chat", json={}).json()["id"]
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "slow one"})
        assert r.status_code == 202
        t0 = time.time()
        chat = c.get(f"/api/chat/{cid}", params={"wait_s": 0.3}).json()
        assert chat["pending"] is True and 0.25 <= time.time() - t0 < 5  # long-poll timed out, reply still pending
        r = c.post(f"/api/chat/{cid}/messages", json={"text": "impatient"})
        assert r.status_code == 409 and "still being generated" in r.json()["detail"]

        chat = c.post(f"/api/chat/{cid}/cancel").json()
        assert chat["pending"] is False
        assert chat["messages"][-1]["error"] == "cancelled" and chat["messages"][-1]["text"] == ""
        assert chat["messages"][-1]["latency_ms"] is not None
        assert ls.cancelled == ["casc-1"]
        assert c.post(f"/api/chat/{cid}/cancel").json()["pending"] is False  # idle: a no-op
        assert ls.cancelled == ["casc-1"]

        # the conversation stays usable; the gate is open now so the next turn completes
        c.post(f"/api/chat/{cid}/messages", json={"text": "once more"})
        chat = settle(c, cid)
        assert chat["messages"][-1]["text"] == "late" and chat["messages"][-1]["error"] is None
        assert len(chat["messages"]) == 4


def test_deleting_a_busy_chat_cancels_it_first(chat_app):
    ls = SlowLS(replies=[[planner("late")]])
    with chat_app(ls) as c:
        cid = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{cid}/messages", json={"text": "slow one"})
        assert c.get(f"/api/chat/{cid}", params={"wait_s": 0.2}).json()["pending"] is True
        assert c.delete(f"/api/chat/{cid}").status_code == 204
        assert ls.cancelled == ["casc-1"] and ls.annotations["casc-1"]["archived"] is True
        assert c.get("/api/chat").json()["chats"] == []


def test_chat_eviction_drops_the_oldest_idle_chat(chat_app):
    ls = FakeLS(replies=[[planner("first")]])
    with chat_app(ls) as c:
        c.app.state.orch.chat.max_chats = 2
        first = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{first}/messages", json={"text": "hi"})
        settle(c, first)
        second = c.post("/api/chat", json={}).json()["id"]
        third = c.post("/api/chat", json={}).json()["id"]
        assert [x["id"] for x in c.get("/api/chat").json()["chats"]] == [third, second]
        assert ls.annotations["casc-1"]["archived"] is True  # the evicted chat's conversation was archived
        assert c.get(f"/api/chat/{first}").status_code == 404


def test_chat_without_archiving_leaves_conversations_visible(chat_app):
    ls = FakeLS(replies=[[planner("kept")]])
    with chat_app(ls, antigravity_archive=False) as c:
        cid = c.post("/api/chat", json={}).json()["id"]
        c.post(f"/api/chat/{cid}/messages", json={"text": "hi"})
        settle(c, cid)
        assert c.delete(f"/api/chat/{cid}").status_code == 204
    assert ls.annotations["casc-1"] == {"title": "computeruse chat · hi"}


# -- context formatting --------------------------------------------------------------------------------


def ev(n: int, type: EventType, **data) -> Event:
    return Event(session_id="s1", seq=n, type=type, data=data)


def test_describe_event_covers_the_timeline_and_skips_noise():
    lines = [describe_event(e) for e in [
        ev(1, EventType.SESSION_STARTED, task="t", backend="browser", model="m", display={"width": 1280, "height": 800}),
        ev(2, EventType.TURN_STARTED, turn=0, step=0),
        ev(3, EventType.MODEL_CALLED, turn=0, latency_ms=1),
        ev(4, EventType.ASSISTANT_TEXT, turn=0, text="I will click Login."),
        ev(5, EventType.ACTION_PROPOSED, step=0, kind="left_click"),
        ev(6, EventType.GUARDRAIL_DECISION, step=0, decision="allow", rule=None, reason=None),
        ev(7, EventType.ACTION_EXECUTED, step=0, kind="left_click", ok=True, description="left_click at (10, 20)",
           screen_changed=True),
        ev(8, EventType.FRAME, seq=1, kind="after_action", step=0),
        ev(9, EventType.GUARDRAIL_DECISION, step=1, decision="block", rule="domain", reason="example.net is blocked"),
        ev(10, EventType.ACTION_EXECUTED, step=1, kind="navigate", ok=False, blocked=True, error="example.net is blocked",
           description="navigate to example.net"),
        ev(11, EventType.APPROVAL_REQUESTED, step=2, kind="key", description="press Enter", rule="submit"),
        ev(12, EventType.APPROVAL_RESOLVED, step=2, approved=False),
        ev(13, EventType.ACTION_EXECUTED, step=2, kind="key", ok=False, rejected=True, error="rejected by user",
           description="press Enter"),
        ev(14, EventType.STUCK_NUDGED, step=3, repeats=3, description="scroll down"),
        ev(15, EventType.USER_INSTRUCTION, text="Use the search box instead."),
        ev(16, EventType.OPERATOR_CONTROL, state="taken", status="paused"),
        ev(17, EventType.MANUAL_ACTION, description="click at (5, 5)", kind="left_click", ok=True, error=None),
        ev(18, EventType.OPERATOR_CONTROL, state="released", resumed=True),
        ev(19, EventType.BUDGET_CHANGED, changes={"max_steps": 80}, before={"max_steps": 40}),
        ev(20, EventType.SESSION_PAUSED, step=4, turn=3),
        ev(21, EventType.SESSION_RESUMED, step=4, turn=3),
        ev(22, EventType.ERROR, where="model", message="rate limited"),
        ev(23, EventType.ACTION_EXECUTED, step=4, kind="type", ok=False, error="element gone", description="type 'x'",
           output=None),
        ev(24, EventType.ACTION_EXECUTED, step=5, kind="zoom", ok=True, description="zoom", output="zoomed",
           screen_changed=False),
        ev(25, EventType.TURN_STARTED, phase="eval_check", passed=True, detail="note saved"),
        ev(26, EventType.SESSION_ENDED, outcome="completed", reason="model declared done", final_text="All set."),
    ]]
    assert lines == [
        "Session started on the browser backend with model m, display 1280×800",
        None, None,
        "Agent: I will click Login.",
        None, None,
        "Step 0: left_click at (10, 20) — ok, screen changed",
        None,
        "Guardrail block (rule domain) at step 1: example.net is blocked",
        "Step 1: navigate to example.net — blocked: example.net is blocked",
        "Approval requested for step 2: press Enter",
        "Approval rejected for step 2",
        "Step 2: press Enter — rejected by the user",
        "Stuck: 'scroll down' repeated 3×; the agent was nudged to try something else",
        "Operator instruction to the agent: Use the search box instead.",
        "Operator took control of the mouse and keyboard",
        "Operator performed: click at (5, 5) — ok",
        "Operator released control and resumed the agent",
        "Budget changed: max_steps=80",
        "Session paused (after step 4)",
        "Session resumed (step 4)",
        "Error (model): rate limited",
        "Step 4: type 'x' — failed: element gone",
        "Step 5: zoom — ok, screen unchanged; output: zoomed",
        "Eval check passed: note saved",
        "Session ended: completed — model declared done. Agent's final message: All set.",
    ]


def test_latest_frame_prefers_full_screens_over_zoom_crops():
    assert latest_frame([]) is None
    frames = [ev(1, EventType.FRAME, seq=1, kind="initial"), ev(2, EventType.FRAME, seq=2, kind="after_action"),
              ev(3, EventType.FRAME, seq=3, kind="zoom"), ev(4, EventType.ACTION_EXECUTED, step=1)]
    assert latest_frame(frames) == 2
    assert latest_frame([ev(3, EventType.FRAME, seq=3, kind="zoom")]) == 3


def test_session_context_formats_headers_and_truncates_long_timelines():
    rec = SessionRecord(id="abc", task="Book a table " * 100, backend="browser", model="antigravity:gemini-flash-lite",
                        status=SessionStatus.RUNNING, steps=120, turns=90)
    events = [ev(i, EventType.ACTION_EXECUTED, step=i, kind="left_click", ok=True, description=f"click {i} " + "x" * 400)
              for i in range(100)]
    block = session_context(rec, events, first=True, screenshot="the latest screen (frame 9)")
    head, task, backend, omitted, timeline, *rest = block.splitlines()
    assert head == f"{CONTEXT_MARKER}: session abc · running · 120 steps, 90 turns]"
    assert task.startswith("Task: Book a table Book a table") and task.endswith("…") and len(task) <= 1006
    assert backend == "Backend: browser · model: antigravity:gemini-flash-lite"
    assert omitted.startswith("(… ") and omitted.endswith(" earlier entries omitted)")
    assert timeline == "Timeline:"
    assert rest[-1] == "[screenshot attached: the latest screen (frame 9)]"
    entries = rest[:-1]
    assert 0 < len(entries) < MAX_CONTEXT_LINES and entries[-1].startswith("- Step 99: click 99 ")
    assert all(len(x) <= 303 and x.endswith("…") for x in entries)  # per-line clip
    assert sum(len(x) + 1 for x in entries) <= MAX_CONTEXT_CHARS
    assert int(omitted.split()[1]) == 100 - len(entries)

    block = session_context(rec, events[:3], first=False, screenshot=None)
    assert block.splitlines()[1] == "Timeline since your previous reply:" and block.count("\n- Step ") == 3
    assert "Task:" not in block and "[screenshot" not in block
    assert session_context(rec, [], first=False, screenshot=None).endswith("No new timeline entries since your previous reply.")
    assert session_context(rec, [], first=True, screenshot=None).endswith("No timeline entries yet.")
