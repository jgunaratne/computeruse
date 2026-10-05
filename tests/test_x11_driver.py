"""Real X11 driver on a private Xvfb (skipped when Xvfb is not installed)."""

import shutil
import threading

import pytest

from computeruse.computer.actions import parse_action
from computeruse.computer.daemon import X11Driver

pytestmark = pytest.mark.skipif(not shutil.which("Xvfb"), reason="Xvfb not installed")


def wire(payload: dict) -> dict:
    """What the daemon hands the driver: a validated action serialised back to a dict."""
    return parse_action(payload).model_dump(exclude_none=True)


def test_private_display_screenshot_and_input():
    drv = X11Driver(display=None, size=(640, 480), startup_cmd=None)
    drv.start()
    try:
        assert drv.display_name.startswith(":")
        assert (drv.width, drv.height) == (640, 480)
        img = drv.screenshot()
        assert img.size == (640, 480)
        assert drv.execute(wire({"action": "mouse_move", "coordinate": [123, 45]}))["ok"]
        res = drv.execute(wire({"action": "cursor_position"}))
        assert res["ok"] and res["output"] == "(123, 45)"
        assert drv.execute(wire({"action": "key", "text": "ctrl+shift+t"}))["ok"]
        assert drv.execute(wire({"action": "type", "text": "héllo ✓"}))["ok"]
        assert drv.execute(wire({"action": "left_click", "coordinate": [10, 10]}))["ok"]
        assert drv.execute(wire({"action": "scroll", "coordinate": [50, 50], "scroll_direction": "down",
                                 "scroll_amount": 2}))["ok"]
    finally:
        drv.stop()


def test_concurrent_boots_get_distinct_displays():
    """Regression: two sandboxes booting at once must never share an X display."""
    drivers = [X11Driver(display=None, size=(320, 240), startup_cmd=None) for _ in range(3)]
    errors: list[Exception] = []

    def boot(d):
        try:
            d.start()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=boot, args=(d,)) for d in drivers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    try:
        assert not errors, errors
        names = {d.display_name for d in drivers}
        assert len(names) == 3, names
    finally:
        for d in drivers:
            d.stop()
