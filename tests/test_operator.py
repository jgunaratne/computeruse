"""Operator takeover bookkeeping (summary / snapshot) and the websocket input pump."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from computeruse.computer.actions import parse_action
from computeruse.server.operator import OperatorControl
from computeruse.server.orchestrator import describe_operator_action
from computeruse.server.routes import InputPump


def act(**payload: Any):
    return parse_action(payload)


def test_operator_counts_and_summary_never_include_typed_text():
    op = OperatorControl()
    assert op.active is False and op.inputs == 0
    op.start(now=1000.0)
    assert op.active is True and op.since == 1000.0
    op.note(act(action="mouse_move", coordinate=[1, 1]))
    op.note(act(action="left_click", coordinate=[10, 10]))
    op.note(act(action="left_click", coordinate=[10, 10]))
    op.note(act(action="left_mouse_down", coordinate=[10, 10]))
    assert "left" in op.buttons_down
    op.note(act(action="left_mouse_up", coordinate=[40, 40]))  # press/release pair counts as one click
    assert not op.buttons_down
    op.note(act(action="left_click_drag", start_coordinate=[1, 1], coordinate=[5, 5]))
    op.note(act(action="type", text="hunter2 secret"))
    op.note(act(action="key", text="Return"))
    op.note(act(action="key", text="Return"))
    op.note(act(action="key", text="ctrl+l"))
    op.note(act(action="key", text="a"))  # unnamed keys are only counted
    op.note(act(action="scroll", coordinate=[5, 5], scroll_direction="down", scroll_amount=3))
    op.note(act(action="key", text="x"), ok=False)

    assert (op.clicks, op.drags, op.typed_chars, op.scrolls, op.moves, op.errors) == (3, 1, 14, 1, 1, 1)
    assert op.keys == {"Return": 2, "ctrl+l": 1, "a": 1}
    assert op.inputs == 3 + 14 + 4 + 1 + 1
    summary = op.summary()
    assert "3 clicks" in summary and "1 drag" in summary and "typed 14 characters" in summary
    assert "pressed Return ×2 and ctrl+l" in summary and "1 other key press" in summary and "scrolled 1×" in summary
    assert "hunter2" not in summary and "secret" not in summary
    snap = op.snapshot()
    assert snap["active"] is True and snap["clicks"] == 3 and snap["typed_chars"] == 14 and snap["keys"] == 4
    assert "text" not in str(snap)

    final = op.stop()
    assert final["summary"] == summary or final["summary"].startswith("manual control for")
    assert final["inputs"] == snap["inputs"]
    assert op.active is False and op.inputs == 0 and op.since is None  # reset for the next takeover


def test_operator_summary_without_inputs_and_duration_format():
    op = OperatorControl()
    op.start(now=0.0)
    s = op.summary()
    assert s.startswith("manual control for") and s.endswith("with no inputs (just looked at the screen)")
    assert "h " in s  # since epoch 0: hours
    op.start()
    assert op.summary().startswith("manual control for 0s")


def test_describe_operator_action_redacts_text():
    assert describe_operator_action(act(action="type", text="p@ssw0rd")) == "type 8 characters"
    assert describe_operator_action(act(action="type", text="x")) == "type 1 character"
    assert "ctrl+l" in describe_operator_action(act(action="key", text="ctrl+l"))
    assert "10, 20" in describe_operator_action(act(action="left_click", coordinate=[10, 20])).replace("(", "").replace(")", "")


class FakeOrch:
    """Stands in for the orchestrator: records inputs, fails on request, and is slow enough to queue."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()

    async def operator_input(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.started.set()
        await asyncio.sleep(0.02)
        self.calls.append(payload)
        if payload.get("action") == "bogus":
            raise ValueError("unknown action")
        if payload.get("action") == "boom":
            raise RuntimeError("daemon went away")
        return {"ok": True, "error": None}


@pytest.mark.asyncio
async def test_input_pump_coalesces_moves_merges_typing_and_reports_results():
    orch, sent = FakeOrch(), []

    async def send(msg):
        sent.append(msg)

    pump = InputPump(orch, "s1", send)  # type: ignore[arg-type]
    task = asyncio.create_task(pump.run())
    # First message starts executing immediately; everything pushed meanwhile queues behind it.
    assert pump.push({"type": "input", "action": {"action": "left_click", "coordinate": [1, 1]}, "id": 1})
    await orch.started.wait()
    for x in (10, 20, 30):
        pump.push({"type": "input", "action": {"action": "mouse_move", "coordinate": [x, x]}})
    pump.push({"type": "input", "action": {"action": "type", "text": "he"}, "id": 2})
    pump.push({"type": "input", "action": {"action": "type", "text": "llo"}, "id": 3})
    pump.push({"type": "input", "action": {"action": "key", "text": "Return"}})
    pump.push({"type": "input", "action": {"action": "bogus"}, "id": 4})
    pump.push({"type": "input", "action": {"action": "boom"}})
    with pytest.raises(ValueError, match="needs an 'action' object"):
        pump.push({"type": "input"})

    for _ in range(100):
        if len(orch.calls) >= 6:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    kinds = [c["action"] for c in orch.calls]
    # Discrete actions in order, typing merged into one, the latest move executed last (once).
    assert kinds == ["left_click", "type", "key", "bogus", "boom", "mouse_move"]
    assert orch.calls[1]["text"] == "hello" and orch.calls[-1]["coordinate"] == [30, 30]
    # Results only for ids and failures; the merged type reports the last id.
    assert [(m["id"], m["ok"]) for m in sent] == [(1, True), (3, True), (4, False), (None, False)]
    assert "unknown action" in sent[2]["error"] and "RuntimeError" in sent[3]["error"]


@pytest.mark.asyncio
async def test_input_pump_bounds_its_queue():
    orch, sent = FakeOrch(), []

    async def send(msg):
        sent.append(msg)

    pump = InputPump(orch, "s1", send)  # type: ignore[arg-type]
    for i in range(InputPump.MAX_QUEUE):
        assert pump.push({"type": "input", "action": {"action": "key", "text": "a"}, "id": i})
    assert pump.push({"type": "input", "action": {"action": "key", "text": "a"}}) is False
    assert pump.dropped == 1
    assert pump.push({"type": "input", "action": {"action": "mouse_move", "coordinate": [1, 1]}})  # moves never queue
