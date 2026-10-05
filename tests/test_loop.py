"""Agent loop behaviour: budgets, stuck detection, guardrails, operator controls, approvals, scaling."""

import asyncio

import pytest
from conftest import default_policy, make_loop

from computeruse.agent.loop import MAX_TRUNCATED_TURNS, CoordinateScaler, SessionController
from computeruse.agent.model import ModelError, ModelTurn, ScriptedModelClient

MOVES = [{"actions": [{"action": "mouse_move", "coordinate": [i * 7 % 1000, 300]}]} for i in range(50)]


async def test_happy_path_completes_and_records_frames():
    sim, rec, loop = make_loop([
        {"text": "I'll open Notes.", "actions": [{"action": "left_click", "target": "dock.notes"}]},
        {"text": "Typing.", "actions": [{"action": "type", "text": "hello world"}, {"action": "key", "text": "ctrl+s"}]},
        {"text": "Saved. Done.", "done": True},
    ])
    res = await loop.run()
    assert res.outcome.value == "completed" and res.steps == 3 and res.turns == 3
    assert res.final_text == "Saved. Done."
    assert sim.state()["apps"]["notes"]["saved_text"] == "hello world"
    assert rec.types[0] == "session.started" and rec.types[-1] == "session.ended"
    assert rec.frames == 4  # initial + one per action
    executed = rec.of("action.executed")
    assert all(e["ok"] for e in executed) and executed[1]["screen_changed"] is True
    ended = rec.of("session.ended")[0]
    assert ended["outcome"] == "completed" and ended["steps"] == 3


async def test_stuck_detection_nudges_then_fails():
    _, rec, loop = make_loop([{"actions": [{"action": "left_click", "coordinate": [10, 300]}]} for _ in range(10)],
                             stuck_threshold=2)
    res = await loop.run()
    assert res.outcome.value == "stuck"
    assert rec.of("stuck.nudged"), "model should have been nudged before giving up"
    assert res.steps < 10


async def test_guardrails_block_and_domain_allowlist():
    sim, rec, loop = make_loop([
        {"actions": [{"action": "key", "text": "ctrl+alt+Delete"}]},
        {"actions": [{"action": "left_click", "target": "dock.browser"}]},
        {"actions": [{"action": "left_click", "target": "browser.url"}, {"action": "type", "text": "https://evil.example.net\n"}]},
        {"actions": [{"action": "type", "text": "https://example.com\n"}]},
        {"done": True},
    ], policy=default_policy(allowed_domains=["example.com"]))
    res = await loop.run()
    assert res.outcome.value == "completed"
    assert sim.state()["apps"]["browser"]["url"] == "example.com"
    blocked = [d for d in rec.of("guardrail.decision") if d["decision"] == "block"]
    assert [b["rule"] for b in blocked] == ["blocked_keys", "domain_policy"]
    assert sum(1 for e in rec.of("action.executed") if e.get("blocked")) == 2


async def test_repeated_blocks_end_the_session():
    _, _, loop = make_loop([{"actions": [{"action": "key", "text": "ctrl+alt+Delete"}]} for _ in range(10)])
    res = await loop.run()
    assert res.outcome.value == "guardrail_blocked"


async def test_max_steps_budget():
    _, _, loop = make_loop(MOVES, max_steps=5)
    res = await loop.run()
    assert res.outcome.value == "max_steps" and res.steps == 5


async def test_max_duration_budget():
    # Distinct actions (no stuck detection) + real model latency so wall-clock advances.
    _, _, loop = make_loop(MOVES, max_duration_s=0.25, latency_ms=40)
    res = await loop.run()
    assert res.outcome.value == "timeout"
    assert 1 <= res.steps < len(MOVES)


async def test_repeated_waits_on_unchanged_screen_count_as_stuck():
    # "Wait forever" is a classic failure mode: it must be nudged and then stopped.
    _, rec, loop = make_loop([{"actions": [{"action": "wait", "duration": 0.01}]} for _ in range(20)],
                             stuck_threshold=2)
    res = await loop.run()
    assert res.outcome.value == "stuck" and rec.of("stuck.nudged")


async def test_model_error_ends_session():
    sim, rec, loop = make_loop([{"actions": [{"action": "left_click", "target": "nonexistent.widget"}]}])
    res = await loop.run()
    assert res.outcome.value == "model_error"


async def test_pause_step_instruct_cancel():
    ctl = SessionController()
    _, rec, loop = make_loop(MOVES, controller=ctl, latency_ms=20)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.15)
    ctl.pause()
    await asyncio.sleep(0.15)
    at_pause = loop.steps
    await asyncio.sleep(0.2)
    assert loop.steps == at_pause, "must not progress while paused"
    ctl.step()
    await asyncio.sleep(0.25)
    assert loop.steps == at_pause + 1, "step runs exactly one action"
    ctl.instruct("try the dock instead")
    ctl.note_manual_action("clicked Notes")
    ctl.resume()
    await asyncio.sleep(0.2)
    ctl.cancel()
    res = await task
    assert res.outcome.value == "cancelled"
    assert {"session.paused", "session.resumed", "user.instruction"} <= set(rec.types)
    assert rec.of("user.instruction")[0]["text"] == "try the dock instead"


async def test_start_paused_then_step_executes_exactly_one_action():
    ctl = SessionController(start_paused=True)
    _, rec, loop = make_loop(MOVES, controller=ctl, latency_ms=5, max_steps=3)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    assert loop.steps == 0 and rec.of("session.paused")
    ctl.step()
    await asyncio.sleep(0.2)
    assert loop.steps == 1
    ctl.resume()
    res = await task
    assert res.outcome.value == "max_steps" and res.steps == 3


async def test_approval_rejected_keeps_secret_out():
    ctl = SessionController()
    sim, rec, loop = make_loop([
        {"actions": [{"action": "left_click", "target": "dock.notes"}]},
        {"actions": [{"action": "type", "text": "password=hunter2secret"}]},
        {"done": True},
    ], controller=ctl, approval_timeout_s=5)
    task = asyncio.create_task(loop.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if ctl.pending_approval_id:
            break
    assert ctl.pending_approval_id
    assert ctl.resolve_approval(ctl.pending_approval_id, False)
    res = await task
    assert res.outcome.value == "completed"
    assert sim.state()["apps"]["notes"]["text"] == ""
    assert rec.of("approval.resolved")[0]["approved"] is False
    assert any(e.get("rejected") for e in rec.of("action.executed"))


async def test_auto_approve_executes_sensitive_action():
    sim, rec, loop = make_loop([
        {"actions": [{"action": "left_click", "target": "dock.notes"}]},
        {"actions": [{"action": "type", "text": "password=hunter2secret"}]},
        {"done": True},
    ], auto_approve=True)
    res = await loop.run()
    assert res.outcome.value == "completed"
    assert sim.state()["apps"]["notes"]["text"] == "password=hunter2secret"
    assert not rec.of("approval.requested")


async def test_approval_timeout_is_a_rejection():
    ctl = SessionController()
    sim, _, loop = make_loop([
        {"actions": [{"action": "left_click", "target": "dock.notes"}]},
        {"actions": [{"action": "type", "text": "password=hunter2secret"}]},
        {"done": True},
    ], controller=ctl, approval_timeout_s=0.2)
    res = await loop.run()
    assert res.outcome.value == "completed" and sim.state()["apps"]["notes"]["text"] == ""


def test_coordinate_scaler_round_trips():
    s = CoordinateScaler.for_display(1280, 960)
    assert s.active and s.model == (1024, 768)
    assert s.to_physical(512, 384) == (640, 480)
    assert s.to_model(640, 480) == (512, 384)
    assert s.scale_output("(640, 480)") == "(512, 384)"
    assert s.scale_action_input({"action": "left_click", "coordinate": [1024, 768]})["coordinate"] == [1280, 960]
    small = CoordinateScaler.for_display(1024, 768)
    assert not small.active and small.scale_output("(1, 2)") == "(1, 2)"
    wide = CoordinateScaler.for_display(3840, 2160)
    assert wide.model[0] <= 1568 and wide.model[0] * wide.model[1] <= 1_200_000


def test_scripted_client_rejects_unknown_locator():
    client = ScriptedModelClient([{"actions": [{"action": "left_click", "target": "x"}]}], locator=lambda k: (_ for _ in ()).throw(KeyError(k)))
    with pytest.raises(ModelError):
        asyncio.run(client.create(system="", messages=[], tools=[], max_tokens=10))


# -- zoom / key repeat / truncation recovery ------------------------------------------------


class CapturingScripted(ScriptedModelClient):
    """Scripted client that keeps the messages it was shown, so tests can inspect tool_results."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen: list[list[dict]] = []

    async def create(self, *, system, messages, tools, max_tokens):
        self.seen.append([dict(m) for m in messages])
        return await super().create(system=system, messages=messages, tools=tools, max_tokens=max_tokens)


class Truncating:
    """Wraps a client: the first `n` turns come back cut off by max_tokens with no tool call."""

    name = "truncating"

    def __init__(self, inner, n):
        self.inner, self.left, self.seen = inner, n, []

    async def create(self, **kw):
        self.seen.append(list(kw["messages"]))
        if self.left:
            self.left -= 1
            return ModelTurn(content=[{"type": "text", "text": "I will now carefully"}], stop_reason="max_tokens")
        return await self.inner.create(**kw)


async def test_zoom_is_answered_by_the_harness_not_the_backend():
    sim, rec, loop = make_loop([
        {"actions": [{"action": "zoom", "region": [0, 0, 200, 100]}]},
        {"done": True},
    ], model_cls=CapturingScripted)
    calls: list[str] = []
    orig = sim.execute

    async def counting(action):
        calls.append(action.action)
        return await orig(action)

    sim.execute = counting  # type: ignore[method-assign]
    res = await loop.run()
    assert res.outcome.value == "completed" and res.steps == 1
    assert calls == [], "zoom must never reach the backend"
    (executed,) = rec.of("action.executed")
    assert executed["kind"] == "zoom" and executed["ok"] and executed["coordinates"] == [[0, 0], [200, 100]]
    assert executed["screen_changed"] is False
    zoom_frame = rec.of("frame")[-1]
    assert zoom_frame["kind"] == "zoom" and zoom_frame["region"] == [0, 0, 200, 100]
    assert zoom_frame["width"] > 200 and zoom_frame["width"] <= 1024, "small regions are upscaled, capped at model dims"
    # the model saw a text note with the mapping formula plus the magnified image
    tool_result = loop.model.seen[1][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    note, image = tool_result["content"]
    assert "Zoomed view of screen region (0, 0)–(200, 100)" in note["text"] and "screen_x = 0 +" in note["text"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"


async def test_zoom_region_is_clamped_to_the_screen_or_rejected():
    # Partly off-screen regions are clamped; regions below the 4px floor become a tool error, not a crash.
    sim, rec, loop = make_loop([
        {"actions": [{"action": "zoom", "region": [900, 700, 1200, 900]}]},  # overhangs the 1024x768 screen
        {"actions": [{"action": "zoom", "region": [1022, 766, 1024, 768]}]},  # 2x2 px corner
        {"done": True},
    ], model_cls=CapturingScripted)
    res = await loop.run()
    assert res.outcome.value == "completed"
    clamped, tiny = rec.of("action.executed")
    assert clamped["ok"] is True and rec.of("frame")[-1]["region"] == [900, 700, 1024, 768]
    assert tiny["kind"] == "zoom" and tiny["ok"] is False and "empty" in tiny["error"]
    tool_result = loop.model.seen[2][-1]["content"][0]
    assert tool_result.get("is_error") is True


async def test_key_repeat_is_unrolled_into_single_presses():
    sim, rec, loop = make_loop([
        {"actions": [{"action": "left_click", "target": "dock.notes"}]},
        {"actions": [{"action": "type", "text": "abcd"}]},
        {"actions": [{"action": "key", "text": "BackSpace", "repeat": 3}]},
        {"done": True},
    ])
    presses: list[int] = []
    orig = sim.execute

    async def spy(action):
        if action.action == "key":
            presses.append(action.repeat)
        return await orig(action)

    sim.execute = spy  # type: ignore[method-assign]
    res = await loop.run()
    assert res.outcome.value == "completed" and res.steps == 3
    assert presses == [1, 1, 1], "backend receives three plain presses"
    executed = rec.of("action.executed")[-1]
    assert executed["kind"] == "key" and executed["ok"] and executed["description"] == "press BackSpace ×3"
    assert sim.state()["apps"]["notes"]["text"] == "a"


async def test_truncated_turn_is_asked_to_continue():
    def factory(script, **kw):
        return Truncating(ScriptedModelClient(script, **kw), n=1)

    sim, rec, loop = make_loop([{"actions": [{"action": "left_click", "target": "dock.notes"}]}, {"done": True}],
                               model_cls=factory)
    res = await loop.run()
    assert res.outcome.value == "completed" and res.steps == 1 and res.turns == 3
    assert any(e.get("recoverable") and "max_tokens" in e["message"] for e in rec.of("error"))
    second_call = loop.model.seen[1]
    assert second_call[-2]["role"] == "assistant" and second_call[-2]["content"][0]["text"] == "I will now carefully"
    assert second_call[-1]["role"] == "user" and "cut off" in second_call[-1]["content"][0]["text"]
    assert sim.state()["focused"] == "notes", "the scripted click ran after the continuation prompt"


async def test_truncation_recovery_gives_up_after_the_limit():
    def factory(script, **kw):
        return Truncating(ScriptedModelClient(script, **kw), n=10)

    _, rec, loop = make_loop([{"done": True}], model_cls=factory)
    res = await loop.run()
    assert res.outcome.value == "completed" and res.steps == 0
    assert res.turns == MAX_TRUNCATED_TURNS + 1
    assert res.final_text == "I will now carefully"
    assert len([e for e in rec.of("error") if e.get("recoverable")]) == MAX_TRUNCATED_TURNS
