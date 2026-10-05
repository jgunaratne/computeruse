"""Simulated computer: deterministic rendering, oracle state, locators, fault injection."""

import pytest

from computeruse.computer.actions import parse_action
from computeruse.computer.base import ComputerError
from computeruse.computer.simulated import SimFaults, SimulatedComputer


async def test_render_is_deterministic():
    a, b = SimulatedComputer(), SimulatedComputer()
    await a.start()
    await b.start()
    fa, fb = await a.screenshot(), await b.screenshot()
    assert fa.sha1 == fb.sha1
    assert (fa.width, fa.height) == (a.display.width, a.display.height) == (1024, 768)


async def test_click_type_save_flow_updates_oracle_state():
    sim = SimulatedComputer()
    await sim.start()
    x, y = sim.locate("dock.notes")
    r = await sim.execute(parse_action({"action": "left_click", "coordinate": [x, y]}))
    assert r.ok
    assert "notes" in sim.state()["open_windows"]
    before = (await sim.screenshot()).sha1
    await sim.execute(parse_action({"action": "type", "text": "hello"}))
    await sim.execute(parse_action({"action": "key", "text": "ctrl+s"}))
    st = sim.state()["apps"]["notes"]
    assert st["text"] == "hello" and st["saved_text"] == "hello"
    assert (await sim.screenshot()).sha1 != before, "screen must change after typing"


async def test_calculator_and_browser_locators():
    sim = SimulatedComputer()
    await sim.start()
    await sim.execute(parse_action({"action": "left_click", "coordinate": list(sim.locate("dock.calc"))}))
    for key in ("calc.7", "calc.*", "calc.3", "calc.="):
        await sim.execute(parse_action({"action": "left_click", "coordinate": list(sim.locate(key))}))
    assert sim.state()["apps"]["calc"]["result"] == "21"
    await sim.execute(parse_action({"action": "left_click", "coordinate": list(sim.locate("dock.browser"))}))
    await sim.execute(parse_action({"action": "left_click", "coordinate": list(sim.locate("browser.url"))}))
    await sim.execute(parse_action({"action": "type", "text": "https://example.com\n"}))
    assert sim.state()["apps"]["browser"]["url"] == "example.com"
    with pytest.raises(KeyError):
        sim.locate("nonexistent.thing")


async def test_fault_injection_drops_clicks_and_fails_screenshots():
    sim = SimulatedComputer(faults=SimFaults(click_drop_rate=1.0, seed=1))
    await sim.start()
    await sim.execute(parse_action({"action": "left_click", "coordinate": list(sim.locate("dock.notes"))}))
    assert "notes" not in sim.state()["open_windows"], "dropped click must not open the window"
    flaky = SimulatedComputer(faults=SimFaults(screenshot_fail_rate=1.0, seed=1))
    await flaky.start()
    with pytest.raises(ComputerError):
        await flaky.screenshot()
