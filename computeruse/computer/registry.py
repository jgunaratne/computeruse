"""Backend registry: the catalogue of computers the harness can drive.

    desktop    This VM's own GNOME desktop (Mutter RemoteDesktop/ScreenCast).
    browser    A real Google Chrome on a private virtual display (Xvfb), isolated
               from the user's desktop. Default for browser-control tasks.
    simulated  Deterministic fake OS for evals, tests and offline demos.
    remote     Any machine running the control daemon (URL + token).
    docker     Sandbox container from docker/Dockerfile (daemon + Chromium).
    x11        An existing X display (advanced / CI).
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass, field
from typing import Any

from computeruse.computer.base import Computer
from computeruse.computer.profile import MODES, BrowserProfile
from computeruse.computer.remote import (
    BrowserComputer,
    DaemonComputer,
    RemoteComputer,
    find_chrome,
)
from computeruse.computer.simulated import SimFaults, SimulatedComputer
from computeruse.computer.sso import security_key_forwarding
from computeruse.config import Settings


@dataclass
class BackendInfo:
    id: str
    label: str
    description: str
    available: bool
    reason: str | None = None
    isolated: bool = True  # False when actions hit the user's real desktop
    options: dict[str, Any] = field(default_factory=dict)


def _gnome_available() -> tuple[bool, str | None]:
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or (
        f"unix:path={os.environ.get('XDG_RUNTIME_DIR', '')}/bus"
    )
    if "unix:path=" not in bus or not os.path.exists(bus.split("unix:path=", 1)[1].split(",")[0]):
        return False, "no D-Bus session bus"
    try:
        import importlib

        for mod in ("gi", "dbus"):
            try:
                importlib.import_module(mod)
            except ImportError:
                from computeruse.computer.daemon import _ensure_system_gi

                _ensure_system_gi()
                importlib.import_module(mod)
    except ImportError as e:
        return False, f"missing system bindings: {e.name}"
    try:
        from computeruse.computer.daemon import _ensure_system_gi

        _ensure_system_gi()
        import dbus

        bus_obj = dbus.SessionBus()
        names = {str(n) for n in bus_obj.list_names()}
        if "org.gnome.Mutter.RemoteDesktop" not in names or "org.gnome.Mutter.ScreenCast" not in names:
            return False, "Mutter RemoteDesktop/ScreenCast not on the session bus (not a GNOME session?)"
    except Exception as e:
        return False, f"D-Bus probe failed: {e}"
    return True, None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackendRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.browser_profile = BrowserProfile(settings.chrome_profile_path(), settings.chrome_profile_mode)

    def list(self) -> list[BackendInfo]:
        s = self.settings
        chrome = find_chrome(s.chrome_binary)
        xvfb = shutil.which("Xvfb")
        gnome_ok, gnome_reason = _gnome_available()
        docker = shutil.which("docker")
        return [
            BackendInfo(
                id="browser",
                label="Browser — real Chrome (sandboxed display)",
                description="Google Chrome on a private virtual X display. Real web, isolated from your desktop; "
                            "cookies and logins persist between sessions.",
                available=bool(chrome and xvfb),
                reason=None if (chrome and xvfb) else ("Chrome not found" if not chrome else "Xvfb not found"),
                options={"size": s.browser_size, "chrome": chrome, "profile": self.browser_profile.status(),
                         "security_key": security_key_forwarding()},
            ),
            BackendInfo(
                id="desktop",
                label="Desktop — this VM's GNOME session",
                description="Drives the live desktop of this machine through Mutter's RemoteDesktop/ScreenCast APIs. You can watch it in your remote-desktop window.",
                available=gnome_ok,
                reason=gnome_reason,
                isolated=False,
            ),
            BackendInfo(
                id="simulated",
                label="Simulated OS (eval fixture)",
                description="Deterministic in-process desktop with Notes, Calculator, Settings and a toy Browser.",
                available=True,
            ),
            BackendInfo(
                id="remote",
                label="Remote VM (control daemon)",
                description="Any VM running `computeruse daemon`; configure COMPUTERUSE_REMOTE_DAEMON_URL.",
                available=bool(s.remote_daemon_url),
                reason=None if s.remote_daemon_url else "COMPUTERUSE_REMOTE_DAEMON_URL not set",
                options={"url": s.remote_daemon_url},
            ),
            BackendInfo(
                id="docker",
                label="Docker sandbox VM",
                description=f"Container from docker/Dockerfile ({s.docker_image}) with Chromium + noVNC live view.",
                available=bool(docker),
                reason=None if docker else "docker CLI not found",
            ),
        ]

    def info(self, backend_id: str) -> BackendInfo:
        for b in self.list():
            if b.id == backend_id:
                return b
        raise KeyError(f"unknown backend {backend_id!r}")

    def create(self, backend_id: str, options: dict[str, Any] | None = None) -> Computer:
        """Instantiate (but do not start) a computer for a session."""
        s = self.settings
        options = options or {}
        if backend_id == "simulated":
            faults = SimFaults(**options.get("faults", {})) if options.get("faults") else None
            return SimulatedComputer(faults=faults)
        if backend_id == "browser":
            chrome = find_chrome(s.chrome_binary)
            if not chrome:
                raise RuntimeError("Google Chrome / Chromium not found; set COMPUTERUSE_CHROME_BINARY")
            return BrowserComputer(
                chrome=chrome, size=s.browser_dims(), profile=self.browser_profile,
                profile_mode=self._profile_mode(options), start_url=options.get("start_url") or s.chrome_start_url,
                devtools_port=_free_port(),
            )
        if backend_id == "desktop":
            return DaemonComputer("gnome", name="desktop", connector=options.get("connector") or s.desktop_connector)
        if backend_id == "x11":
            display = options.get("display") or os.environ.get("DISPLAY")
            return DaemonComputer("x11", name="x11", display=display)
        if backend_id == "remote":
            url = options.get("url") or s.remote_daemon_url
            if not url:
                raise RuntimeError("remote backend requires a daemon URL")
            return RemoteComputer(url, token=options.get("token") or s.remote_daemon_token,
                                  name="remote", live_view=s.remote_live_view_url)
        if backend_id == "docker":
            from computeruse.computer.docker_vm import DockerComputer

            # The container has its own Chromium; mounting the host profile is opt-in because the
            # container user must be able to write it (see docker/entrypoint.sh).
            profile_dir = str(self.browser_profile.root) if options.get("profile") == "persistent" else None
            return DockerComputer(options.get("image") or s.docker_image, size=s.browser_dims(),
                                  start_url=options.get("start_url") or s.chrome_start_url, profile_dir=profile_dir)
        raise KeyError(f"unknown backend {backend_id!r}")

    @staticmethod
    def _profile_mode(options: dict[str, Any]) -> str | None:
        mode = options.get("profile")
        if mode in (None, ""):
            return None
        if mode not in MODES:
            raise ValueError(f"unknown browser profile mode {mode!r}; expected one of {MODES}")
        return str(mode)
