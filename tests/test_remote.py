"""Host-side daemon clients: `DaemonComputer` boot handshake, failure diagnostics,
and the `computeruse daemon` CLI passthrough."""

import os
import shutil

import pytest

from computeruse.cli import _daemon_passthrough
from computeruse.computer.actions import parse_action
from computeruse.computer.base import ComputerError
from computeruse.computer.remote import DaemonComputer

needs_xvfb = pytest.mark.skipif(not shutil.which("Xvfb"), reason="Xvfb not installed")


@needs_xvfb
async def test_daemon_computer_boots_private_display_and_round_trips_actions():
    comp = DaemonComputer("x11", name="t", size=(640, 480))
    await comp.start()
    try:
        assert comp.base_url.startswith("http://127.0.0.1:") and not comp.base_url.endswith(":0")
        assert comp.display.size == (640, 480)
        health = await comp.health()
        assert health.ok and "private Xvfb" in health.detail
        frame = await comp.screenshot()
        assert (frame.width, frame.height) == (640, 480) and frame.sha1
        assert (await comp.execute(parse_action({"action": "left_click", "coordinate": [100, 100]}))).ok
        assert (await comp.execute(parse_action({"action": "cursor_position"}))).output == "(100, 100)"
        # Out-of-bounds coordinates are rejected client-side, before hitting the daemon.
        bad = await comp.execute(parse_action({"action": "left_click", "coordinate": [5000, 10]}))
        assert not bad.ok and bad.error
        # Shell exec runs inside the sandbox environment (DISPLAY points at the private Xvfb).
        code, out = await comp.run_shell("echo $DISPLAY")
        assert code == 0 and out.strip().startswith(":")
    finally:
        await comp.stop()
    assert not (await comp.health()).ok


@needs_xvfb
async def test_daemon_computer_surfaces_boot_failure_with_diagnostics():
    comp = DaemonComputer("x11", name="t", size=(320, 240), app=["/nonexistent/browser-binary"])
    with pytest.raises(ComputerError) as info:
        await comp.start()
    msg = str(info.value)
    assert "exited during startup" in msg
    assert "nonexistent" in msg or "FileNotFoundError" in msg, msg
    await comp.stop()  # idempotent after a failed boot


def test_cli_daemon_passthrough_hands_flags_to_the_daemon_parser():
    assert _daemon_passthrough(["daemon", "--driver", "x11", "--port", "0"]) == ["--driver", "x11", "--port", "0"]
    assert _daemon_passthrough(["-v", "--data-dir", "/tmp/x", "daemon", "--driver", "gnome"]) == ["--driver", "gnome"]
    assert _daemon_passthrough(["--data-dir=/tmp/x", "daemon"]) == []
    assert _daemon_passthrough(["serve"]) is None
    assert _daemon_passthrough(["run", "--task", "daemon"]) is None
    assert _daemon_passthrough([]) is None


@needs_xvfb
async def test_browser_computer_releases_its_profile_lease_when_chrome_fails_to_start(tmp_path):
    from computeruse.computer.profile import BrowserProfile
    from computeruse.computer.remote import BrowserComputer

    profile = BrowserProfile(tmp_path / "profile")
    comp = BrowserComputer(chrome="/nonexistent/chrome", size=(320, 240), profile=profile, devtools_port=0)
    assert comp.session_info() == {"profile": None, "devtools_url": None}  # nothing pinned before boot
    with pytest.raises(ComputerError):
        await comp.start()
    assert comp.lease is None and profile.status()["in_use"] is False
    assert f"--user-data-dir={tmp_path / 'profile'}" in comp.app  # Chrome was pointed at the persistent profile
    await comp.stop()  # idempotent

    # Ephemeral mode never touches the shared profile, and its temp dir is gone after the failed boot.
    comp = BrowserComputer(chrome="/nonexistent/chrome", size=(320, 240), profile=profile, profile_mode="ephemeral")
    with pytest.raises(ComputerError):
        await comp.start()
    tmp_profile = next(a for a in comp.app if a.startswith("--user-data-dir=")).split("=", 1)[1]
    assert tmp_profile != str(tmp_path / "profile") and not os.path.exists(tmp_profile)


def test_registry_browser_profile_options(settings, monkeypatch):
    from computeruse.computer import registry as reg
    from computeruse.computer.remote import BrowserComputer

    monkeypatch.setattr(reg, "find_chrome", lambda _binary: "/usr/bin/true")
    r = reg.BackendRegistry(settings)
    assert r.browser_profile.root == settings.data_dir / "chrome-profile"
    status = r.info("browser").options["profile"]
    assert status["path"] == str(settings.data_dir / "chrome-profile") and status["mode"] == "persistent"
    assert status["exists"] is False and status["in_use"] is False

    comp = r.create("browser")
    assert isinstance(comp, BrowserComputer) and comp.profile_mode is None and comp.profile is r.browser_profile
    assert r.create("browser", {"profile": "ephemeral"}).profile_mode == "ephemeral"
    assert r.create("browser", {"profile": ""}).profile_mode is None
    with pytest.raises(ValueError, match="unknown browser profile mode"):
        r.create("browser", {"profile": "incognito"})
