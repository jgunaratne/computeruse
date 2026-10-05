"""Host-side clients for computers reached through the control daemon.

* `RemoteComputer`  – talks to an already-running daemon (any VM, any host).
* `DaemonComputer`  – spawns the daemon locally as a subprocess (desktop/browser
                      backends on this machine) and then behaves like RemoteComputer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import sys
import time
from typing import Any

import httpx

from computeruse.computer.actions import Action, ActionResult
from computeruse.computer.base import Computer, ComputerError, ComputerHealth, DisplayInfo, Frame
from computeruse.computer.profile import BrowserProfile, ProfileError, ProfileLease

log = logging.getLogger(__name__)

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def find_chrome(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.isabs(explicit) and os.path.exists(explicit) else shutil.which(explicit)
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def chrome_command(
    binary: str,
    size: tuple[int, int],
    profile_dir: str,
    start_url: str = "about:blank",
    extra_flags: list[str] | None = None,
) -> list[str]:
    """A real Chrome, pinned to the virtual display and stripped of first-run noise.

    Deliberately *not* passed: `--disable-background-networking`. Besides metrics and variations
    it turns off the extension updater, and that is what installs the extensions an enterprise
    policy force-lists — on a managed machine those are the security-key (WebAuthn) forwarding and
    device-trust pieces that corporate single sign-on depends on. Enterprise policy itself (system
    files and cloud management) applies to every profile, this one included.
    """
    w, h = size
    cmd = [
        binary,
        "--ozone-platform=x11",  # the host session may be Wayland; the sandbox display is X11
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-sync",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-features=TranslateUI,MediaRouter",
        "--password-store=basic",
        "--window-position=0,0",
        f"--window-size={w},{h}",
        "--start-maximized",
        f"--user-data-dir={profile_dir}",
    ]
    if os.geteuid() == 0:  # inside containers Chrome refuses to run its sandbox as root
        cmd.append("--no-sandbox")
    cmd.extend(extra_flags or [])
    cmd.append(start_url)
    return cmd


class RemoteComputer(Computer):
    """HTTP client for the control daemon API."""

    name = "remote"

    def __init__(self, base_url: str, token: str | None = None, name: str | None = None,
                 live_view: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        if name:
            self.name = name
        self._live_view = live_view
        self._display = DisplayInfo(width=0, height=0)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        self._detail: str = ""

    @property
    def display(self) -> DisplayInfo:
        return self._display

    async def start(self) -> None:
        health = await self.health()
        if not health.ok:
            raise ComputerError(f"daemon unhealthy: {health.detail}")

    async def stop(self) -> None:
        await self._client.aclose()

    async def health(self) -> ComputerHealth:
        try:
            r = await self._client.get("/health", timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return ComputerHealth(ok=False, backend=self.name, detail=f"{type(e).__name__}: {e}")
        d = data.get("display") or {}
        if d.get("width") and d.get("height"):
            self._display = DisplayInfo(width=int(d["width"]), height=int(d["height"]))
        self._detail = data.get("detail") or ""
        if data.get("live_view_url") and not self._live_view:
            self._live_view = data["live_view_url"]
        return ComputerHealth(ok=bool(data.get("ok")), backend=self.name, detail=self._detail,
                              live_view_url=self.live_view_url())

    def live_view_url(self) -> str | None:
        return self._live_view

    async def screenshot(self) -> Frame:
        try:
            r = await self._client.get("/screenshot")
        except httpx.HTTPError as e:
            raise ComputerError(f"screenshot failed: {e}") from e
        if r.status_code != 200:
            raise ComputerError(f"screenshot failed: {self._error_text(r)}")
        return Frame.from_png(r.content)

    async def preview_jpeg(self, quality: int = 55) -> bytes:
        """Cheap frame for the live view (not what the model sees)."""
        r = await self._client.get("/frame.jpg", params={"q": quality})
        if r.status_code != 200:
            raise ComputerError(f"preview failed: {self._error_text(r)}")
        return r.content

    async def execute(self, action: Action) -> ActionResult:
        err = self.validate_coordinates(action)
        if err:
            return ActionResult.failure(err)
        payload: dict[str, Any] = action.model_dump(exclude_none=True)
        t0 = time.perf_counter()
        try:
            r = await self._client.post("/action", json=payload,
                                        timeout=max(60.0, float(getattr(action, "duration", 0) or 0) + 30))
        except httpx.HTTPError as e:
            raise ComputerError(f"action transport error: {e}") from e
        if r.status_code != 200:
            raise ComputerError(f"action failed: {self._error_text(r)}")
        data = r.json()
        return ActionResult(
            ok=bool(data.get("ok")), output=data.get("output"), error=data.get("error"),
            duration_ms=float(data.get("duration_ms") or (time.perf_counter() - t0) * 1000),
        )

    async def run_shell(self, command: str, timeout: float = 30.0) -> tuple[int, str]:
        r = await self._client.post("/exec", json={"command": command, "timeout": timeout},
                                    timeout=timeout + 10)
        if r.status_code != 200:
            raise ComputerError(f"exec failed: {self._error_text(r)}")
        data = r.json()
        return int(data.get("code", -1)), str(data.get("output", ""))

    async def devtools_tabs(self) -> list[dict[str, Any]] | None:
        """Chrome tab list proxied by the daemon; None when the sandbox has no DevTools."""
        try:
            r = await self._client.get("/devtools/tabs", timeout=10)
        except httpx.HTTPError as e:
            raise ComputerError(f"devtools query failed: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ComputerError(f"devtools query failed: {self._error_text(r)}")
        data = r.json()
        return data if isinstance(data, list) else None

    async def shutdown_daemon(self) -> None:
        try:
            await self._client.post("/shutdown", json={}, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _error_text(r: httpx.Response) -> str:
        try:
            return r.json().get("error") or r.text
        except Exception:
            return r.text or f"HTTP {r.status_code}"


class DaemonComputer(RemoteComputer):
    """Spawns `computeruse.computer.daemon` locally and proxies to it."""

    def __init__(
        self,
        driver: str,
        *,
        name: str,
        size: tuple[int, int] = (1280, 800),
        display: str | None = None,
        app: list[str] | None = None,
        connector: str | None = None,
        live_view: str | None = None,
        python: str | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        self._token = secrets.token_urlsafe(24)
        super().__init__("http://127.0.0.1:0", token=self._token, name=name, live_view=live_view)
        self.driver = driver
        self.size = size
        self.display_name = display
        self.app = app or []
        self.connector = connector
        self.python = python or sys.executable
        self.startup_timeout = startup_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: list[str] = []

    async def start(self) -> None:
        if self._proc is not None:
            return
        cmd = [self.python, "-m", "computeruse.computer.daemon", "--driver", self.driver,
               "--host", "127.0.0.1", "--port", "0"]
        if self.driver == "x11":
            cmd += ["--size", f"{self.size[0]}x{self.size[1]}"]
            if self.display_name:
                cmd += ["--display", self.display_name]
            if self.app:
                cmd += ["--app", *self.app]
        elif self.driver == "gnome" and self.connector:
            cmd += ["--connector", self.connector]
        env = dict(os.environ, COMPUTERUSE_DAEMON_TOKEN=self._token, PYTHONUNBUFFERED="1")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._drain_stderr())
        try:
            port = await asyncio.wait_for(self._await_listening(), timeout=self.startup_timeout)
        except TimeoutError:
            await self.stop()
            raise ComputerError(f"{self.name} daemon did not start within {self.startup_timeout:.0f}s") from None
        except ComputerError:
            await self.stop()
            raise
        self.base_url = f"http://127.0.0.1:{port}"
        await self._client.aclose()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0,
                                         headers={"Authorization": f"Bearer {self._token}"})
        await super().start()

    async def _await_listening(self) -> int:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                tail = "\n".join(self._stderr_tail[-15:])
                raise ComputerError(f"{self.name} daemon exited during startup:\n{tail}")
            text = line.decode(errors="replace").strip()
            if text.startswith("LISTENING"):
                return int(text.split()[2])

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode(errors="replace").rstrip())
            del self._stderr_tail[:-200]

    async def health(self) -> ComputerHealth:
        if self._proc is not None and self._proc.returncode is not None:
            return ComputerHealth(ok=False, backend=self.name,
                                  detail=f"daemon exited with code {self._proc.returncode}")
        return await super().health()

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.returncode is None:
                await self.shutdown_daemon()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=8)
                except TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except TimeoutError:
                        proc.kill()
        await super().stop()

    def diagnostics(self) -> str:
        return "\n".join(self._stderr_tail[-30:])


class BrowserComputer(DaemonComputer):
    """A real Chrome on a private Xvfb display, with a managed profile (see `profile.py`).

    The profile lease is taken when the session boots — not when the computer object is
    created — so a session that fails validation never pins the shared profile, and it is
    released once Chrome has exited so the next session sees the cookies it wrote.
    """

    def __init__(
        self,
        *,
        chrome: str,
        size: tuple[int, int],
        profile: BrowserProfile,
        start_url: str = "about:blank",
        profile_mode: str | None = None,
        profile_wait_s: float = 10.0,
        devtools_port: int | None = None,
        python: str | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        super().__init__("x11", name="browser", size=size, python=python, startup_timeout=startup_timeout)
        self.chrome = chrome
        self.profile = profile
        self.profile_mode = profile_mode
        self.profile_wait_s = profile_wait_s
        self.start_url = start_url
        self.devtools_port = devtools_port
        self.devtools_url = f"http://127.0.0.1:{devtools_port}" if devtools_port else None
        self.lease: ProfileLease | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self.lease = await asyncio.to_thread(self.profile.acquire, self.profile_mode, self.profile_wait_s)
        except ProfileError as e:
            raise ComputerError(str(e)) from e
        flags = [f"--remote-debugging-port={self.devtools_port}"] if self.devtools_port else []
        self.app = chrome_command(self.chrome, self.size, self.lease.path, start_url=self.start_url, extra_flags=flags)
        try:
            await super().start()
        except BaseException:
            self._release_lease()
            raise

    async def stop(self) -> None:
        try:
            if self._proc is not None and self.lease is not None and self.lease.persistent:
                await self._close_chrome_gracefully()
            await super().stop()
        finally:
            self._release_lease()

    async def _close_chrome_gracefully(self, timeout: float = 10.0) -> bool:
        """Ask Chrome to quit through DevTools (`Browser.close`) and wait for it to exit.

        Chrome's cookie store lives in its network-service process and is flushed on an orderly
        shutdown; this is the same path Puppeteer uses for `browser.close()`. Logins made in the
        final seconds of a session survive this way. Best effort: on any problem the daemon's
        signal-based shutdown still runs.
        """
        if not self.devtools_url:
            return False
        try:
            import websockets

            async with httpx.AsyncClient(timeout=3) as c:
                ws_url = (await c.get(f"{self.devtools_url}/json/version")).json()["webSocketDebuggerUrl"]
            async with websockets.connect(ws_url, max_size=None, open_timeout=3) as ws:
                await ws.send('{"id": 1, "method": "Browser.close"}')
                try:
                    await asyncio.wait_for(ws.recv(), 3)  # {"id":1,"result":{}}; the socket then drops
                except Exception:  # noqa: BLE001
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                try:
                    async with httpx.AsyncClient(timeout=1) as c:
                        await c.get(f"{self.devtools_url}/json/version")
                except httpx.HTTPError:
                    return True  # DevTools is gone: Chrome has exited
            log.warning("Chrome did not exit within %.0fs of Browser.close; falling back to signals", timeout)
        except Exception as e:  # noqa: BLE001
            log.debug("graceful Chrome close skipped: %s: %s", type(e).__name__, e)
        return False

    def _release_lease(self) -> None:
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.release()

    def session_info(self) -> dict[str, Any]:
        """Facts about this computer worth showing on the session page."""
        return {"profile": self.lease.describe() if self.lease else None, "devtools_url": self.devtools_url}
