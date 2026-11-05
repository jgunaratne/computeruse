#!/usr/bin/env python3
"""Headless-Chrome screenshot helper for `craft-ds shoot` (zero extra npm deps).

Invoked via `craft-ds.json` `screenshots.command`:
  uv run python scripts/craft_shoot.py --url {url} --out {out} --width {width} --height {height} --theme {theme}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import websockets


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str:
    env = os.environ.get("CHROME_BIN")
    for name in ([env] if env else []) + ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("no Chrome/Chromium found on PATH (set CHROME_BIN)")


def record_report(out: Path, entry: dict) -> None:
    report_path = out.parent / "report.json"
    entries: list[dict] = []
    if report_path.is_file():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("shots"), list):
                entries = [e for e in loaded["shots"] if e.get("file") != out.name]
        except Exception:
            entries = []
    entries.append(entry)
    summary = {
        "count": len(entries),
        "errors": sum(len(e.get("errors") or []) for e in entries),
        "overflow": sum(1 for e in entries if e.get("overflow")),
        "theme_mismatch": sum(1 for e in entries if not e.get("theme_applied")),
        "shots": entries,
    }
    report_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


async def shoot(url: str, out: Path, width: int, height: int, theme: str) -> int:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    font = (qs.get("font") or [None])[0]
    cdp_port = free_port()
    profile = tempfile.mkdtemp(prefix="cu-craft-shoot-")
    chrome = subprocess.Popen(
        [
            find_chrome(),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={cdp_port}",
            f"--window-size={width},{height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                tabs = httpx.get(f"http://127.0.0.1:{cdp_port}/json", timeout=0.5).json()
                page_tab = next((t for t in tabs if t.get("type") == "page"), None)
                if page_tab:
                    ws_url = page_tab["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        if not ws_url:
            raise RuntimeError("Chrome DevTools did not start")

        seq = 0
        errors: list[str] = []

        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            async def call(method: str, params: dict | None = None) -> dict:
                nonlocal seq
                seq += 1
                want = seq
                await ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                    if msg.get("id") == want:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error']}")
                        return msg.get("result", {})
                    if msg.get("method") == "Runtime.exceptionThrown":
                        d = msg["params"]["exceptionDetails"]
                        errors.append(d.get("exception", {}).get("description") or d.get("text", "exception"))

            async def js(expr: str):
                res = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in res:
                    raise RuntimeError(f"js `{expr}`: {res['exceptionDetails']}")
                return res.get("result", {}).get("value")

            await call("Page.enable")
            await call("Runtime.enable")
            await call(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": False},
            )
            await call("Page.navigate", {"url": url})
            ready_deadline = time.monotonic() + 12
            while time.monotonic() < ready_deadline:
                ready = await js("document.readyState === 'complete' && !!document.querySelector('#root')?.children.length")
                if ready:
                    break
                await asyncio.sleep(0.1)

            theme_js = json.dumps(theme)
            font_js = json.dumps(font)
            await js(
                f"""(async () => {{
                    if (window.CraftDS?.setTheme) {{
                        window.CraftDS.setTheme({theme_js});
                        if ({font_js} && window.CraftDS.setFontSize) window.CraftDS.setFontSize({font_js});
                    }} else {{
                        document.documentElement.dataset.craftTheme = {theme_js};
                        if ({font_js}) document.documentElement.dataset.craftFontSize = {font_js};
                    }}
                    if (document.fonts && document.fonts.ready) await document.fonts.ready;
                }})()"""
            )
            await asyncio.sleep(0.35)
            state = await js(
                """(() => ({
                    sw: document.documentElement.scrollWidth,
                    cw: document.documentElement.clientWidth,
                    applied: {
                        theme: document.documentElement.dataset.craftTheme || null,
                        pro: document.documentElement.dataset.craftPro || null,
                        bevel: document.documentElement.dataset.craftBevel || null,
                        round_chips: document.documentElement.dataset.craftRoundChips || null,
                        dark: document.documentElement.dataset.craftDark || null,
                        font_size: document.documentElement.dataset.craftFontSize || null,
                    }
                }))()"""
            )
            sw, cw = state["sw"], state["cw"]
            applied = state["applied"]
            data = (await call("Page.captureScreenshot", {"format": "png"}))["data"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(data))
            record_report(
                out,
                {
                    "file": out.name,
                    "url": url,
                    "width": width,
                    "height": height,
                    "requested_theme": theme,
                    "requested_font_size": font or "small",
                    "applied": applied,
                    "theme_applied": applied.get("theme") == theme,
                    "scroll_width": sw,
                    "client_width": cw,
                    "overflow": sw > cw,
                    "errors": errors,
                },
            )
            if sw > cw:
                print(f"  WARN {out.name}: horizontal overflow scrollWidth={sw} > clientWidth={cw}")
                return 1
            if errors:
                print(f"  WARN {out.name}: page errors: {errors}")
                return 1
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=3)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--theme", default="proMedium")
    args = ap.parse_args()
    return asyncio.run(shoot(args.url, Path(args.out), args.width, args.height, args.theme))


if __name__ == "__main__":
    raise SystemExit(main())
