"""Computer control daemon.

Runs *on* the machine being controlled and exposes a tiny authenticated HTTP API
that the agent harness drives (`computeruse.computer.remote.RemoteComputer`):

    GET  /health            -> {ok, driver, display:{width,height}, detail}
    GET  /screenshot        -> image/png  (what the model sees)
    GET  /frame.jpg?q=60    -> image/jpeg (cheap frames for the live view)
    GET  /devtools/tabs     -> Chrome DevTools /json tab list (x11 driver with --remote-debugging-port)
    POST /action            -> execute one Action (JSON body) -> ActionResult JSON
    POST /exec              -> run a shell command (eval checkers) -> {code, output}
    POST /shutdown

Drivers
-------
gnome  Controls the real GNOME Shell session of this VM through Mutter's
       RemoteDesktop (input) and ScreenCast (PipeWire frames) D-Bus APIs — the
       same mechanism Chrome Remote Desktop / gnome-remote-desktop use. Works on
       Wayland, including headless CRD sessions, with no XTest/XWayland caveats.
x11    Controls any X display with XTest + XGetImage. Can boot its own private
       Xvfb display and launch an app on it (e.g. a real Google Chrome), which
       gives an isolated browser sandbox that never touches the user's desktop.

The daemon deliberately depends only on the standard library + Pillow
(+ python-xlib for x11, + system gi/dbus for gnome) so the same file can be
dropped into a Docker image or any Linux VM.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import Image

from computeruse.computer import keys as keymod

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
X_BUTTONS = {"left": 1, "middle": 2, "right": 3}
EVDEV_BUTTONS = {"left": BTN_LEFT, "middle": BTN_MIDDLE, "right": BTN_RIGHT}
CLICK_KINDS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
TYPE_DELAY_S = 0.012
CLICK_GAP_S = 0.06


def _ensure_system_gi() -> None:
    """Make system GObject/D-Bus bindings importable from a virtualenv."""
    try:
        import dbus  # noqa: F401
        import gi  # noqa: F401
        return
    except ImportError:
        pass
    for p in (
        "/usr/lib/python3/dist-packages",
        f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/dist-packages",
        f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages",
        f"/usr/lib64/python{sys.version_info.major}.{sys.version_info.minor}/site-packages",
    ):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)


class DriverError(RuntimeError):
    pass


class Driver:
    """Interface implemented by gnome/x11 drivers."""

    name = "driver"
    width = 0
    height = 0

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def screenshot(self) -> Image.Image: ...
    def execute(self, action: dict[str, Any]) -> dict[str, Any]: ...

    def detail(self) -> str:
        return ""

    def live_view_url(self) -> str | None:
        return os.environ.get("COMPUTERUSE_LIVE_VIEW_URL") or None

    def devtools_tabs(self) -> list[dict[str, Any]] | None:
        """Chrome's DevTools tab list (`/json`) when the controlled app exposes one.

        Lets eval verifiers read real browser state inside a sandbox whose
        DevTools port is not reachable from the host. None = not available.
        """
        return None

    def exec_shell(self, command: str, timeout: float = 30.0) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout,
                env=self.shell_env(),
            )
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        out = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode, out[-20000:]

    def shell_env(self) -> dict[str, str]:
        return dict(os.environ)

    # helpers shared by drivers --------------------------------------------

    def _coord(self, action: dict[str, Any], key: str = "coordinate") -> tuple[int, int] | None:
        c = action.get(key)
        if c is None:
            return None
        x, y = int(round(float(c[0]))), int(round(float(c[1])))
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise DriverError(
                f"coordinate ({x}, {y}) is outside the {self.width}x{self.height} display"
            )
        return x, y


# ---------------------------------------------------------------------------
# X11 driver
# ---------------------------------------------------------------------------


def _stop_app(proc: subprocess.Popen, grace: float = 8.0) -> None:
    """Stop the sandboxed app so that it can save its state.

    Browsers keep state in helper processes — Chrome's network service owns the cookie store and
    only writes it out every ~30 s or on an orderly shutdown. Signalling the whole process group
    at once kills those helpers before the main process can ask them to flush, which silently
    drops logins made in the last half minute of a session. So: SIGTERM the main process alone,
    give it time to wind down its children, then sweep whatever is left in the group.
    """
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    for sig, patience in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return  # group is empty: clean exit
        deadline = time.time() + patience
        while time.time() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)


class X11Driver(Driver):
    name = "x11"

    def __init__(
        self,
        display: str | None,
        size: tuple[int, int] = (1280, 800),
        startup_cmd: list[str] | None = None,
        xvfb_args: list[str] | None = None,
    ) -> None:
        self.display_name = display
        self.requested_size = size
        self.startup_cmd = startup_cmd or []
        self.xvfb_args = xvfb_args or []
        self._xvfb: subprocess.Popen | None = None
        self._app: subprocess.Popen | None = None
        self._disp = None
        self._root = None
        self._remapped: dict[int, int] = {}  # keysym -> keycode we injected
        self._spare_keycodes: list[int] = []
        self._pointer = (size[0] // 2, size[1] // 2)
        self.devtools_port: int | None = None
        for arg in self.startup_cmd:
            if arg.startswith("--remote-debugging-port="):
                try:
                    self.devtools_port = int(arg.split("=", 1)[1]) or None
                except ValueError:
                    pass

    # lifecycle ------------------------------------------------------------

    def start(self) -> None:
        from Xlib import X
        from Xlib import display as xdisplay

        if self.display_name is None:
            self._boot_xvfb()
        env_disp = self.display_name
        os.environ["DISPLAY"] = env_disp
        os.environ.pop("WAYLAND_DISPLAY", None)
        deadline = time.time() + 15
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                self._disp = xdisplay.Display(env_disp)
                break
            except Exception as e:  # server still booting
                last_err = e
                time.sleep(0.2)
        if self._disp is None:
            raise DriverError(f"cannot open X display {env_disp}: {last_err}")
        if not self._disp.has_extension("XTEST"):
            raise DriverError("X server lacks the XTEST extension")
        self._root = self._disp.screen().root
        geom = self._root.get_geometry()
        self.width, self.height = geom.width, geom.height
        self._X = X
        self._find_spare_keycodes()
        if self.startup_cmd:
            self._launch_app()
        self._pointer = (self.width // 2, self.height // 2)
        self._move(*self._pointer)

    def _boot_xvfb(self) -> None:
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise DriverError("Xvfb not installed and no --display given")
        w, h = self.requested_size
        # -displayfd makes Xvfb pick a free display number itself and report it
        # once the server is accepting connections: no scan/spawn race between
        # concurrently booting sandboxes (which otherwise end up sharing a display).
        rfd, wfd = os.pipe()
        cmd = [xvfb, "-displayfd", str(wfd), "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-ac",
               *self.xvfb_args]
        self._xvfb = subprocess.Popen(cmd, pass_fds=(wfd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(wfd)
        buf = b""
        deadline = time.time() + 15
        try:
            while b"\n" not in buf:
                if self._xvfb.poll() is not None:
                    raise DriverError(f"Xvfb exited with code {self._xvfb.returncode}")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise DriverError("Xvfb did not report a display within 15s")
                ready, _, _ = select.select([rfd], [], [], min(remaining, 0.5))
                if ready:
                    chunk = os.read(rfd, 64)
                    if not chunk:
                        raise DriverError("Xvfb closed the display pipe without reporting a display")
                    buf += chunk
        finally:
            os.close(rfd)
        display_num = int(buf.splitlines()[0].strip())
        self.display_name = f":{display_num}"

    def _launch_app(self) -> None:
        env = self.shell_env()
        self._app = subprocess.Popen(
            self.startup_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give the app a moment to map its window so the first screenshot is useful.
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                if self._root.query_tree().children:
                    time.sleep(1.5)
                    break
            except Exception:
                pass

    def shell_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display_name or env.get("DISPLAY", "")
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        return env

    def stop(self) -> None:
        app, xvfb, self._app, self._xvfb = self._app, self._xvfb, None, None
        if app and app.poll() is None:
            _stop_app(app)
        if xvfb and xvfb.poll() is None:
            try:
                xvfb.terminate()
                xvfb.wait(timeout=5)
            except Exception:
                try:
                    xvfb.kill()
                except Exception:
                    pass
        if self._disp is not None:
            try:
                self._disp.close()
            except Exception:
                pass
            self._disp = None

    def detail(self) -> str:
        app = f", app pid {self._app.pid}" if self._app and self._app.poll() is None else ""
        owned = "private Xvfb" if self._xvfb else "existing display"
        return f"{owned} {self.display_name} ({self.width}x{self.height}){app}"

    def devtools_tabs(self) -> list[dict[str, Any]] | None:
        if not self.devtools_port:
            return None
        from urllib.request import urlopen

        try:
            with urlopen(f"http://127.0.0.1:{self.devtools_port}/json", timeout=3) as resp:
                tabs = json.loads(resp.read().decode())
        except Exception as e:  # DevTools not up (yet) or Chrome gone
            raise DriverError(f"DevTools unreachable on port {self.devtools_port}: {e}") from e
        return tabs if isinstance(tabs, list) else None

    # observation ----------------------------------------------------------

    def screenshot(self) -> Image.Image:
        X = self._X
        raw = self._root.get_image(0, 0, self.width, self.height, X.ZPixmap, 0xFFFFFFFF)
        data = raw.data
        if isinstance(data, str):  # python-xlib may hand back latin-1 text
            data = data.encode("latin-1")
        depth = getattr(raw, "depth", 24)
        if depth in (24, 32):
            return Image.frombytes("RGB", (self.width, self.height), data, "raw", "BGRX")
        if depth == 16:
            return Image.frombytes("RGB", (self.width, self.height), data, "raw", "BGR;16")
        raise DriverError(f"unsupported X visual depth {depth}")

    # input ----------------------------------------------------------------

    def _move(self, x: int, y: int) -> None:
        from Xlib.ext import xtest

        xtest.fake_input(self._disp, self._X.MotionNotify, x=x, y=y)
        self._disp.sync()
        self._pointer = (x, y)

    def _button(self, button: int, press: bool) -> None:
        from Xlib.ext import xtest

        xtest.fake_input(self._disp, self._X.ButtonPress if press else self._X.ButtonRelease, button)
        self._disp.sync()

    def _key(self, keycode: int, press: bool) -> None:
        from Xlib.ext import xtest

        xtest.fake_input(self._disp, self._X.KeyPress if press else self._X.KeyRelease, keycode)
        self._disp.sync()

    def _find_spare_keycodes(self) -> None:
        info = self._disp.display.info
        lo, hi = info.min_keycode, info.max_keycode
        mapping = self._disp.get_keyboard_mapping(lo, hi - lo + 1)
        self._spare_keycodes = [lo + i for i, syms in enumerate(mapping) if all(s == 0 for s in syms)]
        self._spare_keycodes.reverse()

    def _keycode_for(self, keysym: int) -> tuple[int, bool]:
        """Return (keycode, needs_shift) for a keysym, remapping a spare keycode if needed."""
        if keysym in self._remapped:
            return self._remapped[keysym], False
        keycode = self._disp.keysym_to_keycode(keysym)
        if keycode:
            if self._disp.keycode_to_keysym(keycode, 0) == keysym:
                return keycode, False
            if self._disp.keycode_to_keysym(keycode, 1) == keysym:
                return keycode, True
        if not self._spare_keycodes:
            if keycode:
                return keycode, False
            raise DriverError(f"no keycode for keysym 0x{keysym:x} and no spare keycodes")
        keycode = self._spare_keycodes.pop()
        self._disp.change_keyboard_mapping(keycode, [[keysym, keysym]])
        self._disp.sync()
        time.sleep(0.02)  # let clients process MappingNotify
        self._remapped[keysym] = keycode
        return keycode, False

    def _tap_keysyms(self, keysyms: list[int]) -> None:
        shift_kc = self._disp.keysym_to_keycode(0xFFE1)
        pressed: list[int] = []
        try:
            for ks in keysyms:
                kc, shift = self._keycode_for(ks)
                if shift and shift_kc not in pressed:
                    self._key(shift_kc, True)
                    pressed.append(shift_kc)
                self._key(kc, True)
                pressed.append(kc)
        finally:
            for kc in reversed(pressed):
                self._key(kc, False)

    def _modifiers_down(self, mods: str | None) -> list[int]:
        if not mods:
            return []
        kcs = []
        for ks in keymod.parse_chord(mods):
            kc, _ = self._keycode_for(ks)
            self._key(kc, True)
            kcs.append(kc)
        return kcs

    def _modifiers_up(self, kcs: list[int]) -> None:
        for kc in reversed(kcs):
            self._key(kc, False)

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("action")
        if kind in ("screenshot",):
            return {"ok": True}
        if kind == "cursor_position":
            p = self._root.query_pointer()
            self._pointer = (p.root_x, p.root_y)
            return {"ok": True, "output": f"({p.root_x}, {p.root_y})"}
        if kind == "mouse_move":
            x, y = self._coord(action)
            self._move(x, y)
            return {"ok": True}
        if kind in CLICK_KINDS:
            button_name, count = CLICK_KINDS[kind]
            c = self._coord(action)
            if c:
                self._move(*c)
                time.sleep(0.03)
            mods = self._modifiers_down(action.get("text"))
            try:
                for i in range(count):
                    self._button(X_BUTTONS[button_name], True)
                    time.sleep(0.02)
                    self._button(X_BUTTONS[button_name], False)
                    if i < count - 1:
                        time.sleep(CLICK_GAP_S)
            finally:
                self._modifiers_up(mods)
            return {"ok": True}
        if kind == "left_click_drag":
            sx, sy = self._coord(action, "start_coordinate")
            ex, ey = self._coord(action)
            self._move(sx, sy)
            time.sleep(0.05)
            self._button(1, True)
            steps = 12
            for i in range(1, steps + 1):
                self._move(sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps)
                time.sleep(0.015)
            self._button(1, False)
            return {"ok": True}
        if kind == "left_mouse_down":
            c = self._coord(action)
            if c:
                self._move(*c)
            self._button(1, True)
            return {"ok": True}
        if kind == "left_mouse_up":
            c = self._coord(action)
            if c:
                self._move(*c)
            self._button(1, False)
            return {"ok": True}
        if kind == "type":
            for ch in action["text"]:
                self._tap_keysyms([keymod.keysym_for_char(ch)])
                time.sleep(TYPE_DELAY_S)
            return {"ok": True}
        if kind == "key":
            self._tap_keysyms(keymod.parse_chord(action["text"]))
            return {"ok": True}
        if kind == "hold_key":
            keysyms = keymod.parse_chord(action["text"])
            kcs = []
            try:
                for ks in keysyms:
                    kc, _ = self._keycode_for(ks)
                    self._key(kc, True)
                    kcs.append(kc)
                time.sleep(float(action.get("duration", 1.0)))
            finally:
                for kc in reversed(kcs):
                    self._key(kc, False)
            return {"ok": True}
        if kind == "scroll":
            x, y = self._coord(action)
            self._move(x, y)
            direction = action.get("scroll_direction", "down")
            button = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
            mods = self._modifiers_down(action.get("text"))
            try:
                for _ in range(int(action.get("scroll_amount", 3))):
                    self._button(button, True)
                    self._button(button, False)
                    time.sleep(0.01)
            finally:
                self._modifiers_up(mods)
            return {"ok": True}
        if kind == "wait":
            time.sleep(float(action.get("duration", 1.0)))
            return {"ok": True}
        raise DriverError(f"unsupported action {kind!r}")


# ---------------------------------------------------------------------------
# GNOME / Mutter driver
# ---------------------------------------------------------------------------


class GnomeDriver(Driver):
    """Drive the live GNOME Shell session through Mutter's private D-Bus APIs."""

    name = "gnome"

    RD_NAME = "org.gnome.Mutter.RemoteDesktop"
    SC_NAME = "org.gnome.Mutter.ScreenCast"

    def __init__(self, connector: str | None = None, cursor_mode: int = 1) -> None:
        self.connector = connector
        self.cursor_mode = cursor_mode  # 0 hidden, 1 embedded in frames, 2 metadata
        self._pointer: tuple[int, int] | None = None
        self._last_frame: Image.Image | None = None
        self._last_frame_at = 0.0
        self._lock = threading.RLock()
        self._loop = None
        self._loop_thread: threading.Thread | None = None
        self._pipeline = None
        self._sink = None
        self._stream_path: str | None = None
        self._rd_sess = None
        self._sc_sess = None

    # lifecycle ------------------------------------------------------------

    def start(self) -> None:
        _ensure_system_gi()
        try:
            import dbus
            import gi
            from dbus.mainloop.glib import DBusGMainLoop

            gi.require_version("Gst", "1.0")
            from gi.repository import GLib, Gst
        except ImportError as e:
            raise DriverError(
                "gnome driver needs python3-dbus, python3-gi and GStreamer (gstreamer1.0-pipewire)"
            ) from e

        self._dbus = dbus
        DBusGMainLoop(set_as_default=True)
        Gst.init(None)
        self._Gst = Gst
        bus = dbus.SessionBus()
        self._bus = bus

        rd = dbus.Interface(bus.get_object(self.RD_NAME, "/org/gnome/Mutter/RemoteDesktop"), self.RD_NAME)
        sc = dbus.Interface(bus.get_object(self.SC_NAME, "/org/gnome/Mutter/ScreenCast"), self.SC_NAME)

        # Mutter >= 47 takes `is_trusted`; older versions take no arguments.
        try:
            rd_path = rd.CreateSession(dbus.Boolean(True))
        except dbus.exceptions.DBusException:
            rd_path = rd.CreateSession()
        rd_obj = bus.get_object(self.RD_NAME, rd_path)
        self._rd_sess = dbus.Interface(rd_obj, f"{self.RD_NAME}.Session")
        props = dbus.Interface(rd_obj, "org.freedesktop.DBus.Properties")
        session_id = str(props.Get(f"{self.RD_NAME}.Session", "SessionId"))

        sc_path = sc.CreateSession({"remote-desktop-session-id": dbus.String(session_id)})
        sc_obj = bus.get_object(self.SC_NAME, sc_path)
        self._sc_sess = dbus.Interface(sc_obj, f"{self.SC_NAME}.Session")
        connector = self.connector or self._primary_connector()
        self.connector = connector
        self._stream_path = str(
            self._sc_sess.RecordMonitor(connector, {"cursor-mode": dbus.UInt32(self.cursor_mode)})
        )
        stream_obj = bus.get_object(self.SC_NAME, self._stream_path)
        params = dbus.Interface(stream_obj, "org.freedesktop.DBus.Properties").Get(
            f"{self.SC_NAME}.Stream", "Parameters"
        )
        size = params.get("size")
        if size:
            self.width, self.height = int(size[0]), int(size[1])

        node_ready = threading.Event()
        node_id: dict[str, int] = {}

        def on_added(nid):
            node_id["id"] = int(nid)
            node_ready.set()

        stream_obj.connect_to_signal("PipeWireStreamAdded", on_added, dbus_interface=f"{self.SC_NAME}.Stream")

        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(target=self._loop.run, name="glib-loop", daemon=True)
        self._loop_thread.start()
        self._rd_sess.Start()
        if not node_ready.wait(15):
            raise DriverError("Mutter did not announce a PipeWire stream (is PipeWire running?)")

        self._pipeline = Gst.parse_launch(
            f"pipewiresrc path={node_id['id']} do-timestamp=true keepalive-time=1000 ! "
            "videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink max-buffers=1 drop=true sync=false"
        )
        self._sink = self._pipeline.get_by_name("sink")
        self._pipeline.set_state(Gst.State.PLAYING)
        img = self._pull_frame(timeout_s=10)
        if img is None:
            raise DriverError("no video frames received from PipeWire")
        self.width, self.height = img.size
        # Park the pointer in the centre so cursor_position is meaningful.
        self._pointer_move(self.width // 2, self.height // 2)

    def _primary_connector(self) -> str:
        dbus = self._dbus
        try:
            dc = dbus.Interface(
                self._bus.get_object("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig"),
                "org.gnome.Mutter.DisplayConfig",
            )
            _serial, monitors, logical, _props = dc.GetCurrentState()
            primary = None
            for lm in logical:
                if bool(lm[4]):  # primary flag
                    primary = str(lm[5][0][0]) if lm[5] else None
            if primary:
                return primary
            if monitors:
                return str(monitors[0][0][0])
        except Exception:
            pass
        return "Meta-0"

    def stop(self) -> None:
        with self._lock:
            try:
                if self._pipeline is not None:
                    self._pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                pass
            try:
                if self._rd_sess is not None:
                    self._rd_sess.Stop()
            except Exception:
                pass
            try:
                if self._loop is not None:
                    self._loop.quit()
            except Exception:
                pass
            self._pipeline = self._sink = self._rd_sess = self._sc_sess = None

    def detail(self) -> str:
        return f"GNOME Shell session, monitor {self.connector} ({self.width}x{self.height}) via Mutter RemoteDesktop/ScreenCast"

    # observation ----------------------------------------------------------

    def _pull_frame(self, timeout_s: float) -> Image.Image | None:
        Gst = self._Gst
        sample = self._sink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if sample is None:
            return None
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(data) // h if h else w * 3  # handles row padding without GstVideo meta
        img = Image.frombytes("RGB", (w, h), data, "raw", "RGB", stride)
        self._last_frame, self._last_frame_at = img, time.time()
        return img

    def screenshot(self) -> Image.Image:
        with self._lock:
            # Mutter only emits frames on damage, so a quiet screen yields no new
            # sample; the last frame is then still the truth.
            img = self._pull_frame(timeout_s=0.25)
            if img is None:
                img = self._last_frame
            if img is None:
                raise DriverError("no frame available yet")
            return img

    # input ----------------------------------------------------------------

    def _pointer_move(self, x: int, y: int) -> None:
        self._rd_sess.NotifyPointerMotionAbsolute(self._stream_path, float(x), float(y))
        self._pointer = (x, y)

    def _button(self, button: int, press: bool) -> None:
        self._rd_sess.NotifyPointerButton(self._dbus.Int32(button), self._dbus.Boolean(press))

    def _keysym(self, keysym: int, press: bool) -> None:
        self._rd_sess.NotifyKeyboardKeysym(self._dbus.UInt32(keysym), self._dbus.Boolean(press))

    def _tap(self, keysyms: list[int]) -> None:
        pressed: list[int] = []
        try:
            for ks in keysyms:
                self._keysym(ks, True)
                pressed.append(ks)
        finally:
            for ks in reversed(pressed):
                self._keysym(ks, False)

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._execute(action)

    def _execute(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("action")
        if kind == "screenshot":
            return {"ok": True}
        if kind == "cursor_position":
            p = self._pointer or (self.width // 2, self.height // 2)
            return {"ok": True, "output": f"({p[0]}, {p[1]})"}
        if kind == "mouse_move":
            self._pointer_move(*self._coord(action))
            return {"ok": True}
        if kind in CLICK_KINDS:
            button_name, count = CLICK_KINDS[kind]
            c = self._coord(action)
            if c:
                self._pointer_move(*c)
                time.sleep(0.04)
            mods = keymod.parse_chord(action["text"]) if action.get("text") else []
            for ks in mods:
                self._keysym(ks, True)
            try:
                for i in range(count):
                    self._button(EVDEV_BUTTONS[button_name], True)
                    time.sleep(0.025)
                    self._button(EVDEV_BUTTONS[button_name], False)
                    if i < count - 1:
                        time.sleep(CLICK_GAP_S)
            finally:
                for ks in reversed(mods):
                    self._keysym(ks, False)
            return {"ok": True}
        if kind == "left_click_drag":
            sx, sy = self._coord(action, "start_coordinate")
            ex, ey = self._coord(action)
            self._pointer_move(sx, sy)
            time.sleep(0.05)
            self._button(BTN_LEFT, True)
            steps = 12
            for i in range(1, steps + 1):
                self._pointer_move(sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps)
                time.sleep(0.015)
            self._button(BTN_LEFT, False)
            return {"ok": True}
        if kind == "left_mouse_down":
            c = self._coord(action)
            if c:
                self._pointer_move(*c)
            self._button(BTN_LEFT, True)
            return {"ok": True}
        if kind == "left_mouse_up":
            c = self._coord(action)
            if c:
                self._pointer_move(*c)
            self._button(BTN_LEFT, False)
            return {"ok": True}
        if kind == "type":
            for ch in action["text"]:
                self._tap([keymod.keysym_for_char(ch)])
                time.sleep(TYPE_DELAY_S)
            return {"ok": True}
        if kind == "key":
            self._tap(keymod.parse_chord(action["text"]))
            return {"ok": True}
        if kind == "hold_key":
            keysyms = keymod.parse_chord(action["text"])
            for ks in keysyms:
                self._keysym(ks, True)
            try:
                time.sleep(float(action.get("duration", 1.0)))
            finally:
                for ks in reversed(keysyms):
                    self._keysym(ks, False)
            return {"ok": True}
        if kind == "scroll":
            x, y = self._coord(action)
            self._pointer_move(x, y)
            direction = action.get("scroll_direction", "down")
            axis = 0 if direction in ("up", "down") else 1
            step = 1 if direction in ("down", "right") else -1
            mods = keymod.parse_chord(action["text"]) if action.get("text") else []
            for ks in mods:
                self._keysym(ks, True)
            try:
                for _ in range(int(action.get("scroll_amount", 3))):
                    self._rd_sess.NotifyPointerAxisDiscrete(self._dbus.UInt32(axis), self._dbus.Int32(step))
                    time.sleep(0.015)
            finally:
                for ks in reversed(mods):
                    self._keysym(ks, False)
            return {"ok": True}
        if kind == "wait":
            time.sleep(float(action.get("duration", 1.0)))
            return {"ok": True}
        raise DriverError(f"unsupported action {kind!r}")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class DaemonState:
    def __init__(self, driver: Driver, token: str | None) -> None:
        self.driver = driver
        self.token = token
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.actions = 0


def make_handler(state: DaemonState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "computeruse-daemon/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            if os.environ.get("COMPUTERUSE_DAEMON_VERBOSE"):
                sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

        # helpers ----------------------------------------------------------

        def _authorized(self) -> bool:
            if not state.token:
                return True
            header = self.headers.get("Authorization", "")
            return secrets.compare_digest(header, f"Bearer {state.token}")

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload).encode())

        def _read_json(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid JSON body: {e}") from e
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        # routes -----------------------------------------------------------

        def do_GET(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            try:
                if url.path == "/health":
                    d = state.driver
                    return self._json(200, {
                        "ok": True, "driver": d.name,
                        "display": {"width": d.width, "height": d.height},
                        "detail": d.detail(), "live_view_url": d.live_view_url(),
                        "uptime_s": round(time.time() - state.started_at, 1),
                        "actions": state.actions,
                    })
                if url.path == "/screenshot":
                    with state.lock:
                        img = state.driver.screenshot()
                    buf = io.BytesIO()
                    img.save(buf, format="PNG", compress_level=1)
                    return self._send(200, buf.getvalue(), "image/png")
                if url.path == "/frame.jpg":
                    q = int((parse_qs(url.query).get("q") or ["60"])[0])
                    with state.lock:
                        img = state.driver.screenshot()
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=max(20, min(95, q)))
                    return self._send(200, buf.getvalue(), "image/jpeg")
                if url.path == "/devtools/tabs":
                    tabs = state.driver.devtools_tabs()
                    if tabs is None:
                        return self._json(404, {"error": "no DevTools endpoint for this driver/app"})
                    return self._send(200, json.dumps(tabs).encode())
                return self._json(404, {"error": "not found"})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            try:
                body = self._read_json()
                if url.path == "/action":
                    t0 = time.perf_counter()
                    kind = body.get("action")
                    try:
                        if kind == "wait":  # don't hold the driver lock while sleeping
                            time.sleep(max(0.0, min(60.0, float(body.get("duration", 1.0)))))
                            result = {"ok": True}
                        else:
                            with state.lock:
                                result = state.driver.execute(body)
                        state.actions += 1
                    except (DriverError, keymod.KeyError_, KeyError, ValueError) as e:
                        result = {"ok": False, "error": str(e)}
                    result["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    return self._json(200, result)
                if url.path == "/exec":
                    code, out = state.driver.exec_shell(
                        str(body.get("command", "")), float(body.get("timeout", 30))
                    )
                    return self._json(200, {"code": code, "output": out})
                if url.path == "/shutdown":
                    self._json(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return None
                return self._json(404, {"error": "not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def build_driver(args: argparse.Namespace) -> Driver:
    if args.driver == "gnome":
        return GnomeDriver(connector=args.connector, cursor_mode=args.cursor_mode)
    if args.driver == "x11":
        w, h = (int(v) for v in args.size.lower().split("x"))
        startup = args.app or []
        return X11Driver(display=args.display, size=(w, h), startup_cmd=startup)
    raise SystemExit(f"unknown driver {args.driver}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="computeruse control daemon")
    parser.add_argument("--driver", choices=["gnome", "x11"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--display", help="x11: existing DISPLAY to control (default: boot a private Xvfb)")
    parser.add_argument("--size", default="1280x800", help="x11: Xvfb screen size")
    parser.add_argument("--connector", help="gnome: monitor connector (default: primary)")
    parser.add_argument("--cursor-mode", type=int, default=1, help="gnome: 0 hidden, 1 embedded, 2 metadata")
    parser.add_argument("--app", nargs=argparse.REMAINDER, help="x11: command to launch on the display")
    args = parser.parse_args(argv)

    token = os.environ.get("COMPUTERUSE_DAEMON_TOKEN") or None
    driver = build_driver(args)
    driver.start()
    state = DaemonState(driver, token)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    server.daemon_threads = True
    port = server.server_address[1]
    # The parent process parses this line to discover the port.
    print(f"LISTENING {args.host} {port} {driver.width}x{driver.height}", flush=True)

    def _stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        driver.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
