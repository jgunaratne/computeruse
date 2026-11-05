"""Drive the built console in headless Chrome and check that it behaves (`make ui-check`).

Starts a throwaway server (temporary data dir, a free port on 127.0.0.1) and, over the Chrome DevTools
protocol, renders every page, builds a flow on the Agents tab with real drag-and-drop and mouse input,
runs the offline pattern to completion, and checks a narrow window for horizontal overflow. Uncaught
page errors fail the run. Screenshots land in the output directory (default `.ui-check/`): look at them,
because layout regressions rarely show up as failed assertions.

    uv run python scripts/ui_check.py [--out DIR]

Needs a built UI (`web/dist`) and Chrome or Chromium (set CHROME_BIN to pick one).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
WIDE, NARROW = (1500, 950), (820, 900)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str:
    for name in (os.environ.get("CHROME_BIN"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if name and (path := shutil.which(name) or (name if Path(name).is_file() else None)):
            return path
    raise SystemExit("ui-check: Chrome or Chromium not found (set CHROME_BIN)")


def get_json(url: str, method: str = "GET", body: object = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


@contextlib.contextmanager
def throwaway_server(log_path: Path):
    """`computeruse serve` on a free loopback port with an empty data dir, stopped and removed on exit."""
    port, data = free_port(), Path(tempfile.mkdtemp(prefix="cu-ui-check-"))
    env = dict(os.environ, COMPUTERUSE_DATA_DIR=str(data), COMPUTERUSE_HOST="127.0.0.1",
               COMPUTERUSE_MODEL_DISCOVERY="off")
    env.pop("COMPUTERUSE_ACCESS_TOKEN", None)
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "computeruse.cli", "serve", "--host", "127.0.0.1",
                                 "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(150):
                if proc.poll() is not None:
                    raise SystemExit(f"ui-check: the server exited early; see {log_path}")
                with contextlib.suppress(OSError):
                    get_json(f"{base}/api/health")
                    break
                time.sleep(0.2)
            else:
                raise SystemExit(f"ui-check: the server did not come up; see {log_path}")
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(data, ignore_errors=True)


class Page:
    """A minimal DevTools client: commands are matched to replies; page errors are collected as they come."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.exceptions: list[str] = []
        self.console_errors: list[str] = []

    async def pump(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg:
                fut = self.pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.exceptions.append((d.get("exception") or {}).get("description") or d.get("text") or "?")
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"].get("type") == "error":
                args = msg["params"].get("args", [])
                self.console_errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args))

    async def call(self, method: str, **params) -> dict:
        self.seq += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = fut
        await self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        msg = await asyncio.wait_for(fut, 30)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"evaluate failed: {r['exceptionDetails'].get('text')}: {expr[:80]}")
        return r.get("result", {}).get("value")

    async def mouse(self, kind: str, x: float, y: float, buttons: int = 0, button: str = "left") -> None:
        await self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, buttons=buttons,
                        clickCount=0 if kind == "mouseMoved" else 1)

    async def drag(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> None:
        await self.mouse("mouseMoved", x0, y0)
        await self.mouse("mousePressed", x0, y0, 1)
        for i in range(1, steps + 1):
            await self.mouse("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, 1)
            await asyncio.sleep(0.01)
        await self.mouse("mouseReleased", x1, y1)

    async def center(self, selector: str):
        return await self.js(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return null;"
                             " const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()")


# A palette item dropped on the canvas, with a real HTML5 DragEvent sequence; true if the drop was accepted.
DROP = """(([title, fx, fy]) => {
  const item = [...document.querySelectorAll('.flow-palette-item')].find(b => b.querySelector('.flow-palette-title').textContent === title);
  const canvas = document.querySelector('.flow-canvas');
  const dt = new DataTransfer();
  item.dispatchEvent(new DragEvent('dragstart', {bubbles: true, cancelable: true, dataTransfer: dt}));
  const r = canvas.getBoundingClientRect();
  const o = {bubbles: true, cancelable: true, dataTransfer: dt, clientX: r.left + r.width * fx, clientY: r.top + r.height * fy};
  const over = canvas.dispatchEvent(new DragEvent('dragover', o));
  canvas.dispatchEvent(new DragEvent('drop', o));
  item.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
  return !over;
})"""


async def checks(page: Page, base: str, out: Path, check) -> None:
    api = lambda method, path, body=None: get_json(f"{base}{path}", method, body)  # noqa: E731

    async def shot(name: str) -> None:
        r = await page.call("Page.captureScreenshot", format="png")
        (out / f"{name}.png").write_bytes(base64.b64decode(r["data"]))

    async def size(w: int, h: int) -> None:
        await page.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)

    async def goto(path: str, wait: float = 2.0) -> None:
        await page.call("Page.navigate", url=f"{base}{path}")
        await asyncio.sleep(wait)

    async def wires() -> int:
        return await page.js("document.querySelectorAll('.flow-wire:not(.ghost)').length")

    await size(*WIDE)

    # Every page renders its heading.
    for path, title in (
        ("/", "Sessions"),
        ("/schedules", "Schedules"),
        ("/metrics", "Metrics"),
        ("/evals", "Evals"),
        ("/agents", "Agents"),
    ):
        await goto(path)
        h1 = await page.js("document.querySelector('main h1, h1')?.textContent || ''")
        check(h1.strip() == title, f"{path} renders ({h1!r})")
        await shot(f"page_{title.lower()}")

    # Schedules: add a starter schedule, open the side-pane editor, and prefill Chat from a sample prompt.
    await goto("/schedules")
    check(await page.js("document.querySelectorAll('.schedule-suggested-card').length") >= 4,
          "schedules: suggested starter cards render")
    await page.js("[...document.querySelectorAll('.schedule-suggested-card .btn.primary')][1].click()")
    await asyncio.sleep(0.8)
    check(await page.js("document.querySelectorAll('.schedule-row-card').length") == 1,
          "schedules: adding a suggested routine creates an active schedule row")
    await page.js("document.querySelector('.schedule-row-card .schedule-card-title').click()")
    await asyncio.sleep(0.6)
    check(await page.js("!!document.querySelector('.schedule-side-pane')"),
          "schedules: clicking a schedule opens the side-pane editor")
    await shot("schedules_editor")
    await page.js("document.querySelector('.perm-prompt-chip').click()")
    await asyncio.sleep(0.4)
    check(await page.js("!!document.querySelector('.chat-panel') && document.querySelector('.chat-input')?.value.length > 10"),
          "schedules: sample prompt chip opens Chat and prefills the composer")
    await page.js("document.querySelector('.chat-head button[aria-label=\"Close chat\"]').click()")
    await asyncio.sleep(0.2)
    await goto("/agents")

    # Agents: the start screen offers every pattern plus a blank canvas.
    check(await page.js("document.querySelectorAll('.flow-template').length") >= 2, "agents: pattern cards on the start screen")
    await page.js("[...document.querySelectorAll('.flow-template')].find(b => b.textContent.includes('Iterative loop')).click()")
    await asyncio.sleep(2.0)
    check(await page.js("location.pathname.startsWith('/agents/')"), "agents: a pattern opens as a flow")
    check(await page.js("document.querySelectorAll('.flow-node').length") == 7 and await wires() == 12,
          "agents: the iterative loop has 7 nodes and 12 wires")
    zoom = await page.js("document.querySelector('.flow-zoom-level').textContent")
    check(int(zoom.rstrip("%")) >= 60, f"agents: the pattern fits at a readable zoom ({zoom})")
    await shot("agents_iterative_loop")

    # A blank flow: palette drag-and-drop, then wiring with real mouse input.
    await page.js("document.querySelector('.flow-new > button').click()")
    await asyncio.sleep(0.3)
    await page.js("[...document.querySelectorAll('.flow-new-menu button')].find(b => b.textContent.includes('Blank canvas')).click()")
    await asyncio.sleep(2.0)
    blank_id = await page.js("location.pathname.split('/')[2]")
    accepted = await page.js(f"{DROP}(['Goal', 0.2, 0.22])")
    await page.js(f"{DROP}(['Computer agent', 0.5, 0.55])")
    await page.js(f"{DROP}(['Output', 0.8, 0.22])")
    await asyncio.sleep(0.4)
    check(accepted is True and await page.js("document.querySelectorAll('.flow-node').length") == 3,
          "agents: three palette drops make three nodes")
    a = await page.center(".flow-node.nt-goal .flow-port[data-port='task']")
    b = await page.center(".flow-node.nt-computer .flow-port[data-port='task']")
    await page.drag(*a, *b)
    await asyncio.sleep(0.2)
    c = await page.center(".flow-node.nt-computer .flow-port[data-port='result']")
    d = await page.center(".flow-node.nt-output")  # dropped on the card: its first compatible port
    await page.drag(*c, *d)
    await asyncio.sleep(0.2)
    check(await wires() == 2, "agents: two mouse drags make two wires")
    s = await page.center(".flow-node.nt-computer .flow-port[data-port='screen']")
    g = await page.center(".flow-node.nt-goal")
    await page.drag(*s, *g)
    await asyncio.sleep(0.3)
    check(await wires() == 2, "agents: a screen output cannot be wired into a text input")
    o = await page.center(".flow-node.nt-output .flow-port[data-port='screen']")
    await page.mouse("mouseMoved", *s)
    await page.mouse("mousePressed", *s, 1)
    for i in range(1, 8):
        await page.mouse("mouseMoved", s[0] + (o[0] - s[0]) * i / 10, s[1] + (o[1] - s[1]) * i / 10 + 40, 1)
    await asyncio.sleep(0.2)
    lit = await page.js("[...document.querySelectorAll('.flow-port[data-node]')].filter(p => getComputedStyle(p).opacity === '1').length")
    check(lit == 1, f"agents: while dragging, only the compatible port stays lit ({lit})")
    await shot("agents_connecting")
    await page.mouse("mouseReleased", *o)
    await asyncio.sleep(0.3)
    check(await wires() == 3, "agents: screen output wired to the output's screen input")
    head = await page.center(".flow-node.nt-computer .flow-node-head")
    await page.mouse("mousePressed", head[0] - 30, head[1], 2, "right")
    await page.mouse("mouseReleased", head[0] - 30, head[1], 0, "right")
    await asyncio.sleep(0.3)
    check(await page.js("!!document.querySelector('.flow-menu')"), "agents: right-click opens the node menu")
    await page.js("[...document.querySelectorAll('.flow-menu button')].find(b => b.textContent === 'Mute').click()")
    await asyncio.sleep(0.2)
    check(await page.js("document.querySelector('.flow-node.nt-computer').classList.contains('st-muted')"),
          "agents: Mute mutes the node")
    await page.js("document.querySelector('.flow-node.nt-computer .flow-mute').click()")
    await asyncio.sleep(1.2)  # autosave debounce
    saved = api("GET", f"/api/flows/{blank_id}")
    check(len(saved["nodes"]) == 3 and len(saved["edges"]) == 3 and not any(n["muted"] for n in saved["nodes"]),
          "agents: every edit was autosaved")
    await shot("agents_blank_wired")

    # The offline pattern runs to completion with no model or credentials.
    demo = api("POST", "/api/flows", {"template": "offline-demo"})
    await goto(f"/agents/{demo['id']}")
    await page.js("[...document.querySelectorAll('.flow-toolbar .btn')].find(b => b.textContent.includes('Run flow')).click()")
    status = None
    for _ in range(90):
        await asyncio.sleep(0.5)
        status = await page.js("document.querySelector('.flow-run-status .pill')?.textContent")
        if status and status != "running":
            break
    check(status == "completed", f"agents: the offline run completes ({status})")
    await asyncio.sleep(0.6)
    check(await page.js("document.querySelectorAll('.flow-node.nt-computer .flow-thumb img').length") == 2,
          "agents: both computer agents show their final screen")
    await shot("agents_offline_run")
    await page.js("[...document.querySelectorAll('.flow-tabs button')].find(b => b.textContent.startsWith('Run log')).click()")
    await asyncio.sleep(0.3)
    rows = await page.js("document.querySelectorAll('.flow-log-row').length")
    check(bool(rows) and rows > 8, f"agents: the run log lists the messages ({rows})")

    # Narrow windows: nothing scrolls sideways.
    await size(*NARROW)
    for path, name in (
        ("/", "sessions"),
        ("/schedules", "schedules"),
        (f"/agents/{demo['id']}", "agents"),
        ("/metrics", "metrics"),
        ("/evals", "evals"),
    ):
        await goto(path)
        sw, cw = await page.js("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check(sw <= cw, f"narrow {name}: no horizontal overflow ({sw}px content in {cw}px)")
        await shot(f"narrow_{name}")


async def run(out: Path) -> int:
    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(("PASS " if ok else "FAIL ") + label, flush=True)
        if not ok:
            failures.append(label)

    if not (ROOT / "web" / "dist" / "index.html").exists():
        raise SystemExit("ui-check: no built UI; run `make web` first")
    out.mkdir(parents=True, exist_ok=True)
    port, profile = free_port(), tempfile.mkdtemp(prefix="cu-ui-check-chrome-")
    with throwaway_server(out / "server.log") as base:
        flags = ["--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                 f"--window-size={WIDE[0]},{WIDE[1]}", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            flags.append("--no-sandbox")
        chrome = subprocess.Popen([find_chrome(), *flags, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                with contextlib.suppress(OSError, StopIteration, ValueError):
                    target = next(t for t in get_json(f"http://127.0.0.1:{port}/json") if t["type"] == "page")
                    break
                time.sleep(0.1)
            else:
                raise SystemExit("ui-check: Chrome did not start")
            async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
                page = Page(ws)
                pump = asyncio.create_task(page.pump())
                await page.call("Page.enable")
                await page.call("Runtime.enable")
                try:
                    await checks(page, base, out, check)
                except Exception as e:  # noqa: BLE001 - a broken step is a failure, not a crash
                    check(False, f"the checks stopped early: {type(e).__name__}: {e}")
                    with contextlib.suppress(Exception):
                        (out / "failure.png").write_bytes(base64.b64decode(
                            (await page.call("Page.captureScreenshot", format="png"))["data"]))
                pump.cancel()
                check(not page.exceptions, "no uncaught errors in the page" +
                      (f": {page.exceptions[:3]}" if page.exceptions else ""))
                for msg in page.console_errors[:5]:
                    print(f"note: console error: {msg[:200]}")
        finally:
            chrome.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                chrome.wait(10)
            shutil.rmtree(profile, ignore_errors=True)
    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'all checks passed'} — screenshots in {out}/")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / ".ui-check", help="screenshot directory (default .ui-check/)")
    return asyncio.run(run(parser.parse_args().out))


if __name__ == "__main__":
    sys.exit(main())
