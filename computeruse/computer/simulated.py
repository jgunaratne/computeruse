"""A deterministic, in-process simulated desktop.

Why this exists:
* Evals and unit tests need a computer whose state can be inspected exactly
  (`state()`), that renders identically for identical state (so screenshot
  hashes are stable), and that boots in microseconds.
* It lets the whole product (agent loop → telemetry → web UI) run end to end
  without Docker or an API key.
* Fault injection (`SimFaults`) reproduces the flaky-UI conditions that cause
  real-world reliability bugs in the harness: dropped clicks, slow repaints,
  screenshot failures.

The desktop has a top bar, a dock with four apps and draggable windows:
  Notes     – free text editing, ctrl+s to save
  Calc      – button grid + keyboard entry, "=" evaluates
  Settings  – checkboxes + Apply (dark mode visibly re-themes the desktop)
  Browser   – URL bar, Return navigates to canned pages with a clickable link
"""

from __future__ import annotations

import ast
import asyncio
import operator
import random
import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from computeruse.computer.actions import (
    Action,
    ActionResult,
    Click,
    CursorPosition,
    Drag,
    HoldKey,
    KeyPress,
    MouseDown,
    MouseMove,
    MouseUp,
    Screenshot,
    Scroll,
    TypeText,
    Wait,
)
from computeruse.computer.base import Computer, ComputerError, ComputerHealth, DisplayInfo, Frame

WIDTH, HEIGHT = 1024, 768
TOPBAR_H = 28
DOCK_H = 72
TITLE_H = 32

APP_ORDER = ["notes", "calc", "settings", "browser"]
APP_TITLES = {"notes": "Notes", "calc": "Calculator", "settings": "Settings", "browser": "Browser"}
APP_COLORS = {
    "notes": (250, 204, 21),
    "calc": (52, 211, 153),
    "settings": (148, 163, 184),
    "browser": (96, 165, 250),
}
DEFAULT_WINDOW = {
    "notes": (212, 110, 600, 440),
    "calc": (352, 120, 320, 460),
    "settings": (262, 150, 500, 360),
    "browser": (162, 90, 700, 520),
}

BROWSER_PAGES: dict[str, dict[str, Any]] = {
    "example.com": {
        "title": "Example Domain",
        "body": [
            "This domain is for use in illustrative examples in documents.",
            "You may use this domain in literature without prior coordination.",
        ],
        "link": ("More information...", "iana.org"),
    },
    "iana.org": {
        "title": "IANA — Internet Assigned Numbers Authority",
        "body": ["Domain names, number resources and protocol assignments."],
        "link": None,
    },
    "docs.internal": {
        "title": "Internal Docs — Onboarding",
        "body": ["Welcome! Step 1: request access. Step 2: read the runbook."],
        "link": ("Open runbook", "docs.internal/runbook"),
    },
    "docs.internal/runbook": {
        "title": "Runbook",
        "body": ["1. Page on-call.", "2. Roll back the last deploy.", "3. File a postmortem."],
        "link": None,
    },
}


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.w, self.y + self.h

    def inset(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w - 2 * dx, self.h - 2 * dy)


@dataclass
class SimFaults:
    """Fault injection knobs. All default to a perfectly reliable machine."""

    click_drop_rate: float = 0.0  # probability a click is silently ignored
    screenshot_fail_rate: float = 0.0  # probability screenshot() raises
    action_latency_ms: float = 0.0  # artificial latency per action
    seed: int = 0


@dataclass
class Window:
    app: str
    rect: Rect
    scroll: int = 0


@dataclass
class NotesState:
    text: str = ""
    saved_text: str | None = None
    select_all: bool = False

    @property
    def saved(self) -> bool:
        return self.saved_text is not None and self.saved_text == self.text


@dataclass
class CalcState:
    expression: str = ""
    result: str = ""


@dataclass
class SettingsState:
    dark_mode: bool = False
    notifications: bool = True
    applied: dict[str, bool] = field(default_factory=lambda: {"dark_mode": False, "notifications": True})


@dataclass
class BrowserState:
    url: str = ""
    url_input: str = ""
    history: list[str] = field(default_factory=list)
    status: str = "New tab"


_FONT_CACHE: dict[int, ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}


def _font(size: int = 14):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.load_default(size=size)
        except TypeError:  # very old Pillow
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def safe_eval(expr: str) -> str:
    """Evaluate a calculator expression containing digits, '.', + - * /."""
    expr = expr.replace("×", "*").replace("÷", "/")
    if not expr.strip():
        return ""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return "Error"

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValueError("unsupported")

    try:
        value = ev(tree)
    except ZeroDivisionError:
        return "Error"
    except ValueError:
        return "Error"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


class SimulatedComputer(Computer):
    name = "simulated"

    def __init__(self, faults: SimFaults | None = None) -> None:
        self.faults = faults or SimFaults()
        self._rng = random.Random(self.faults.seed)
        self._display = DisplayInfo(width=WIDTH, height=HEIGHT)
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self.cursor = (WIDTH // 2, HEIGHT // 2)
        self.windows: list[Window] = []  # back-to-front
        self.focused_field: str | None = None  # 'notes.text' | 'browser.url' | 'calc'
        self.notes = NotesState()
        self.calc = CalcState()
        self.settings = SettingsState()
        self.browser = BrowserState()
        self.mouse_down_at: tuple[int, int] | None = None
        self.action_count = 0

    @property
    def display(self) -> DisplayInfo:
        return self._display

    async def health(self) -> ComputerHealth:
        return ComputerHealth(ok=True, backend=self.name, detail="in-process simulated desktop")

    # -- observation -------------------------------------------------------

    async def screenshot(self) -> Frame:
        if self.faults.screenshot_fail_rate and self._rng.random() < self.faults.screenshot_fail_rate:
            raise ComputerError("simulated screenshot failure")
        return Frame.from_image(self.render())

    def state(self) -> dict[str, Any]:
        """Exact machine state; used by eval checkers and tests."""
        return {
            "open_windows": [w.app for w in self.windows],
            "focused": self.windows[-1].app if self.windows else None,
            "focused_field": self.focused_field,
            "cursor": list(self.cursor),
            "dark_mode": self.settings.applied["dark_mode"],
            "apps": {
                "notes": {
                    "text": self.notes.text,
                    "saved": self.notes.saved,
                    "saved_text": self.notes.saved_text,
                },
                "calc": {"expression": self.calc.expression, "result": self.calc.result},
                "settings": {
                    "dark_mode": self.settings.dark_mode,
                    "notifications": self.settings.notifications,
                    "applied": dict(self.settings.applied),
                },
                "browser": {
                    "url": self.browser.url,
                    "url_input": self.browser.url_input,
                    "history": list(self.browser.history),
                },
            },
            "action_count": self.action_count,
        }

    # -- actions -----------------------------------------------------------

    async def execute(self, action: Action) -> ActionResult:
        t0 = time.perf_counter()
        if self.faults.action_latency_ms:
            await asyncio.sleep(self.faults.action_latency_ms / 1000)
        err = self.validate_coordinates(action)
        if err:
            return ActionResult.failure(err, (time.perf_counter() - t0) * 1000)
        self.action_count += 1
        output: str | None = None
        try:
            if isinstance(action, (Screenshot,)):
                pass
            elif isinstance(action, CursorPosition):
                output = f"({self.cursor[0]}, {self.cursor[1]})"
            elif isinstance(action, MouseMove):
                self.cursor = tuple(action.coordinate)
            elif isinstance(action, Click):
                if action.coordinate:
                    self.cursor = tuple(action.coordinate)
                dropped = self.faults.click_drop_rate and self._rng.random() < self.faults.click_drop_rate
                if not dropped:
                    self._click(action.action, *self.cursor)
            elif isinstance(action, Drag):
                self._drag(tuple(action.start_coordinate), tuple(action.coordinate))
                self.cursor = tuple(action.coordinate)
            elif isinstance(action, MouseDown):
                if action.coordinate:
                    self.cursor = tuple(action.coordinate)
                self.mouse_down_at = self.cursor
            elif isinstance(action, MouseUp):
                if action.coordinate:
                    self.cursor = tuple(action.coordinate)
                if self.mouse_down_at:
                    if self.mouse_down_at == self.cursor:
                        self._click("left_click", *self.cursor)
                    else:
                        self._drag(self.mouse_down_at, self.cursor)
                self.mouse_down_at = None
            elif isinstance(action, TypeText):
                for ch in action.text:
                    self._type_char(ch)
            elif isinstance(action, KeyPress):
                self._key(action.text)
            elif isinstance(action, HoldKey):
                await asyncio.sleep(min(action.duration, 0.05))
            elif isinstance(action, Scroll):
                self.cursor = tuple(action.coordinate)
                self._scroll(action.scroll_direction, action.scroll_amount)
            elif isinstance(action, Wait):
                await asyncio.sleep(min(action.duration, 0.05))
        except Exception as e:  # defensive: never let the fake OS crash the harness
            return ActionResult.failure(f"simulated OS error: {e}", (time.perf_counter() - t0) * 1000)
        return ActionResult(ok=True, output=output, duration_ms=(time.perf_counter() - t0) * 1000)

    # -- window management -------------------------------------------------

    def _window_for(self, app: str) -> Window | None:
        return next((w for w in self.windows if w.app == app), None)

    def _open(self, app: str) -> None:
        win = self._window_for(app)
        if win is None:
            x, y, w, h = DEFAULT_WINDOW[app]
            offset = 24 * len(self.windows)
            win = Window(app, Rect(x + offset, y + offset, w, h))
            self.windows.append(win)
        else:
            self.windows.remove(win)
            self.windows.append(win)
        self.focused_field = {"notes": "notes.text", "calc": "calc", "browser": None}.get(app)

    def _close(self, win: Window) -> None:
        self.windows.remove(win)
        self.focused_field = None
        if self.windows:
            self._open(self.windows[-1].app)

    def _dock_rects(self) -> dict[str, Rect]:
        total = len(APP_ORDER) * 48 + (len(APP_ORDER) - 1) * 48
        x0 = (WIDTH - total) // 2
        return {
            app: Rect(x0 + i * 96, HEIGHT - DOCK_H + 4, 48, 48) for i, app in enumerate(APP_ORDER)
        }

    def _close_rect(self, win: Window) -> Rect:
        return Rect(win.rect.x + win.rect.w - 28, win.rect.y + 6, 20, 20)

    def _title_rect(self, win: Window) -> Rect:
        return Rect(win.rect.x, win.rect.y, win.rect.w, TITLE_H)

    def _content_rect(self, win: Window) -> Rect:
        return Rect(win.rect.x, win.rect.y + TITLE_H, win.rect.w, win.rect.h - TITLE_H)

    # -- input handling ----------------------------------------------------

    def _click(self, kind: str, x: int, y: int) -> None:
        # dock
        for app, rect in self._dock_rects().items():
            if rect.contains(x, y):
                self._open(app)
                return
        # windows, top-most first
        for win in reversed(self.windows):
            if win.rect.contains(x, y):
                if win is not self.windows[-1]:
                    self._open(win.app)
                if self._close_rect(win).contains(x, y):
                    self._close(win)
                    return
                if self._title_rect(win).contains(x, y):
                    return
                getattr(self, f"_click_{win.app}")(win, kind, x, y)
                return
        # desktop click: drop focus
        self.focused_field = None

    def _drag(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        for win in reversed(self.windows):
            if self._title_rect(win).contains(*start):
                dx, dy = end[0] - start[0], end[1] - start[1]
                win.rect = Rect(
                    max(0, min(WIDTH - win.rect.w, win.rect.x + dx)),
                    max(TOPBAR_H, min(HEIGHT - DOCK_H - TITLE_H, win.rect.y + dy)),
                    win.rect.w,
                    win.rect.h,
                )
                self._open(win.app)
                return

    def _scroll(self, direction: str, amount: int) -> None:
        for win in reversed(self.windows):
            if win.rect.contains(*self.cursor) and win.app in ("notes", "browser"):
                delta = amount if direction == "down" else -amount if direction == "up" else 0
                win.scroll = max(0, win.scroll + delta)
                return

    def _type_char(self, ch: str) -> None:
        if ch == "\n":
            self._key("Return")
            return
        if self.focused_field == "notes.text":
            if self.notes.select_all:
                self.notes.text = ""
                self.notes.select_all = False
            self.notes.text += ch
        elif self.focused_field == "browser.url":
            self.browser.url_input += ch
        elif self.focused_field == "calc" and ch in "0123456789.+-*/":
            self._calc_input(ch)
        elif self.focused_field == "calc" and ch == "=":
            self._calc_input("=")

    def _key(self, chord: str) -> None:
        parts = [p.strip() for p in chord.split("+") if p.strip()]
        mods = {p.lower() for p in parts[:-1]}
        key = parts[-1] if parts else ""
        lower = key.lower()
        if "ctrl" in mods or "control" in mods:
            if lower == "s" and self._window_for("notes") and self.focused_field == "notes.text":
                self.notes.saved_text = self.notes.text
            elif lower == "a" and self.focused_field == "notes.text":
                self.notes.select_all = True
            elif lower == "l" and self._window_for("browser"):
                self._open("browser")
                self.focused_field = "browser.url"
                self.browser.url_input = ""
            elif lower == "w" and self.windows:
                self._close(self.windows[-1])
            return
        if lower in ("return", "enter", "kp_enter"):
            if self.focused_field == "notes.text":
                self.notes.text += "\n"
            elif self.focused_field == "browser.url":
                self._navigate(self.browser.url_input)
            elif self.focused_field == "calc":
                self._calc_input("=")
        elif lower == "backspace":
            if self.focused_field == "notes.text":
                self.notes.text = "" if self.notes.select_all else self.notes.text[:-1]
                self.notes.select_all = False
            elif self.focused_field == "browser.url":
                self.browser.url_input = self.browser.url_input[:-1]
            elif self.focused_field == "calc":
                self.calc.expression = self.calc.expression[:-1]
        elif lower == "escape":
            self.focused_field = None
        elif lower == "space":
            self._type_char(" ")
        elif lower == "tab":
            self._type_char("\t")
        elif len(key) == 1:
            self._type_char(key)

    # -- app: notes --------------------------------------------------------

    def _click_notes(self, win: Window, kind: str, x: int, y: int) -> None:
        self.focused_field = "notes.text"
        if kind == "triple_click":
            self.notes.select_all = True

    # -- app: calc ---------------------------------------------------------

    CALC_KEYS = [["7", "8", "9", "/"], ["4", "5", "6", "*"], ["1", "2", "3", "-"], ["C", "0", "=", "+"]]

    def _calc_buttons(self, win: Window) -> list[tuple[str, Rect]]:
        c = self._content_rect(win).inset(12, 12)
        display_h = 56
        grid_top = c.y + display_h + 12
        gap = 8
        bw = (c.w - 3 * gap) // 4
        bh = (c.h - display_h - 12 - 3 * gap) // 4
        out = []
        for r, row in enumerate(self.CALC_KEYS):
            for col, label in enumerate(row):
                out.append((label, Rect(c.x + col * (bw + gap), grid_top + r * (bh + gap), bw, bh)))
        return out

    def _click_calc(self, win: Window, kind: str, x: int, y: int) -> None:
        self.focused_field = "calc"
        for label, rect in self._calc_buttons(win):
            if rect.contains(x, y):
                self._calc_input(label)
                return

    def _calc_input(self, label: str) -> None:
        if label == "C":
            self.calc.expression, self.calc.result = "", ""
        elif label == "=":
            self.calc.result = safe_eval(self.calc.expression)
        else:
            if self.calc.result and label in "0123456789.":
                self.calc.expression = ""
            elif self.calc.result and label in "+-*/":
                self.calc.expression = self.calc.result
            self.calc.result = ""
            self.calc.expression += label

    # -- app: settings -----------------------------------------------------

    def _settings_widgets(self, win: Window) -> dict[str, Rect]:
        c = self._content_rect(win)
        return {
            "dark_mode": Rect(c.x + 24, c.y + 28, 22, 22),
            "notifications": Rect(c.x + 24, c.y + 76, 22, 22),
            "apply": Rect(c.x + 24, c.y + 136, 110, 36),
        }

    def _click_settings(self, win: Window, kind: str, x: int, y: int) -> None:
        self.focused_field = None
        for name, rect in self._settings_widgets(win).items():
            if rect.contains(x, y):
                if name == "apply":
                    self.settings.applied = {
                        "dark_mode": self.settings.dark_mode,
                        "notifications": self.settings.notifications,
                    }
                else:
                    setattr(self.settings, name, not getattr(self.settings, name))
                return

    # -- app: browser ------------------------------------------------------

    def _browser_widgets(self, win: Window) -> dict[str, Rect]:
        c = self._content_rect(win)
        return {
            "url": Rect(c.x + 12, c.y + 10, c.w - 24 - 70, 32),
            "go": Rect(c.x + c.w - 12 - 60, c.y + 10, 60, 32),
            "link": Rect(c.x + 24, c.y + 150, 220, 22),
        }

    def _click_browser(self, win: Window, kind: str, x: int, y: int) -> None:
        widgets = self._browser_widgets(win)
        if widgets["url"].contains(x, y):
            self.focused_field = "browser.url"
            if kind == "triple_click":
                self.browser.url_input = ""
            return
        if widgets["go"].contains(x, y):
            self._navigate(self.browser.url_input)
            return
        page = BROWSER_PAGES.get(self.browser.url)
        if page and page.get("link") and widgets["link"].contains(x, y):
            self._navigate(page["link"][1])
            return
        self.focused_field = None

    def _navigate(self, raw: str) -> None:
        url = raw.strip().lower()
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                url = url[len(prefix) :]
        url = url.rstrip("/")
        self.browser.url = url
        self.browser.url_input = url
        self.browser.history.append(url)
        self.browser.status = "Loaded" if url in BROWSER_PAGES else "Could not resolve host"
        self.focused_field = None
        win = self._window_for("browser")
        if win:
            win.scroll = 0

    # -- rendering ---------------------------------------------------------

    def render(self) -> Image.Image:
        dark = self.settings.applied["dark_mode"]
        bg = (17, 24, 39) if dark else (30, 58, 138)
        img = Image.new("RGB", (WIDTH, HEIGHT), bg)
        d = ImageDraw.Draw(img)
        # subtle diagonal band so the wallpaper is not a flat colour
        band = (31, 41, 55) if dark else (37, 99, 235)
        d.polygon([(0, HEIGHT), (WIDTH, 120), (WIDTH, HEIGHT)], fill=band)
        # top bar
        d.rectangle((0, 0, WIDTH, TOPBAR_H), fill=(15, 23, 42))
        d.text((12, 6), "SimOS", fill=(226, 232, 240), font=_font(14))
        d.text((WIDTH - 60, 6), "10:42", fill=(226, 232, 240), font=_font(14))
        # windows (back to front)
        for win in self.windows:
            self._render_window(d, win, focused=win is self.windows[-1])
        # dock
        d.rectangle((0, HEIGHT - DOCK_H, WIDTH, HEIGHT), fill=(15, 23, 42))
        for app, rect in self._dock_rects().items():
            d.rounded_rectangle(rect.box, radius=10, fill=APP_COLORS[app])
            d.text((rect.x + 16, rect.y + 10), APP_TITLES[app][0], fill=(15, 23, 42), font=_font(22))
            label = APP_TITLES[app]
            d.text((rect.x + 24 - 3 * len(label), rect.y + 50), label, fill=(203, 213, 225), font=_font(11))
            if self._window_for(app):
                d.ellipse((rect.x + 21, HEIGHT - 7, rect.x + 27, HEIGHT - 1), fill=(255, 255, 255))
        self._render_cursor(d)
        return img

    def _render_window(self, d: ImageDraw.ImageDraw, win: Window, focused: bool) -> None:
        r = win.rect
        d.rectangle((r.x + 4, r.y + 4, r.x + r.w + 4, r.y + r.h + 4), fill=(0, 0, 0))
        d.rectangle(r.box, fill=(248, 250, 252), outline=(100, 116, 139))
        title_fill = (51, 65, 85) if focused else (148, 163, 184)
        d.rectangle(self._title_rect(win).box, fill=title_fill)
        d.text((r.x + 12, r.y + 8), APP_TITLES[win.app], fill=(255, 255, 255), font=_font(14))
        cr = self._close_rect(win)
        d.rounded_rectangle(cr.box, radius=4, fill=(239, 68, 68))
        d.line((cr.x + 6, cr.y + 6, cr.x + 14, cr.y + 14), fill=(255, 255, 255), width=2)
        d.line((cr.x + 14, cr.y + 6, cr.x + 6, cr.y + 14), fill=(255, 255, 255), width=2)
        getattr(self, f"_render_{win.app}")(d, win)

    def _render_notes(self, d: ImageDraw.ImageDraw, win: Window) -> None:
        c = self._content_rect(win)
        area = Rect(c.x + 12, c.y + 12, c.w - 24, c.h - 48)
        active = self.focused_field == "notes.text"
        d.rectangle(area.box, fill=(255, 255, 255), outline=(59, 130, 246) if active else (203, 213, 225))
        lines: list[str] = []
        for para in self.notes.text.split("\n"):
            while len(para) > 68:
                lines.append(para[:68])
                para = para[68:]
            lines.append(para)
        visible = lines[win.scroll : win.scroll + (area.h - 16) // 18]
        if self.notes.select_all and self.notes.text:
            d.rectangle((area.x + 6, area.y + 6, area.x + area.w - 6, area.y + 6 + 18 * len(visible)), fill=(191, 219, 254))
        for i, line in enumerate(visible):
            d.text((area.x + 8, area.y + 6 + i * 18), line, fill=(15, 23, 42), font=_font(14))
        if active and not visible:
            d.text((area.x + 8, area.y + 6), "Start typing…", fill=(148, 163, 184), font=_font(14))
        status = "Saved" if self.notes.saved else ("Unsaved changes" if self.notes.text else "Empty note")
        d.text((c.x + 12, c.y + c.h - 28), f"{status}  ·  ctrl+s to save  ·  {len(self.notes.text)} chars", fill=(71, 85, 105), font=_font(12))

    def _render_calc(self, d: ImageDraw.ImageDraw, win: Window) -> None:
        c = self._content_rect(win).inset(12, 12)
        d.rectangle((c.x, c.y, c.x + c.w, c.y + 56), fill=(15, 23, 42))
        text = self.calc.result or self.calc.expression or "0"
        d.text((c.x + 10, c.y + 6), self.calc.expression if self.calc.result else "", fill=(148, 163, 184), font=_font(12))
        d.text((c.x + 10, c.y + 24), text[-24:], fill=(255, 255, 255), font=_font(22))
        for label, rect in self._calc_buttons(win):
            fill = (37, 99, 235) if label == "=" else (254, 226, 226) if label == "C" else (226, 232, 240)
            d.rounded_rectangle(rect.box, radius=6, fill=fill, outline=(148, 163, 184))
            color = (255, 255, 255) if label == "=" else (15, 23, 42)
            d.text((rect.x + rect.w // 2 - 6, rect.y + rect.h // 2 - 10), label, fill=color, font=_font(18))

    def _render_settings(self, d: ImageDraw.ImageDraw, win: Window) -> None:
        widgets = self._settings_widgets(win)
        labels = {"dark_mode": "Dark mode", "notifications": "Notifications"}
        for name, label in labels.items():
            rect = widgets[name]
            checked = getattr(self.settings, name)
            d.rectangle(rect.box, fill=(37, 99, 235) if checked else (255, 255, 255), outline=(71, 85, 105), width=2)
            if checked:
                d.line((rect.x + 5, rect.y + 11, rect.x + 9, rect.y + 16, rect.x + 17, rect.y + 6), fill=(255, 255, 255), width=3)
            d.text((rect.x + 34, rect.y + 2), label, fill=(15, 23, 42), font=_font(15))
        apply = widgets["apply"]
        d.rounded_rectangle(apply.box, radius=6, fill=(37, 99, 235))
        d.text((apply.x + 32, apply.y + 9), "Apply", fill=(255, 255, 255), font=_font(15))
        pending = (
            self.settings.applied["dark_mode"] != self.settings.dark_mode
            or self.settings.applied["notifications"] != self.settings.notifications
        )
        d.text((apply.x + 130, apply.y + 10), "Unapplied changes" if pending else "All changes applied", fill=(71, 85, 105), font=_font(13))

    def _render_browser(self, d: ImageDraw.ImageDraw, win: Window) -> None:
        c = self._content_rect(win)
        widgets = self._browser_widgets(win)
        url = widgets["url"]
        active = self.focused_field == "browser.url"
        d.rounded_rectangle(url.box, radius=6, fill=(255, 255, 255), outline=(59, 130, 246) if active else (148, 163, 184), width=2)
        shown = self.browser.url_input if (active or self.browser.url_input) else "Search or enter address"
        d.text((url.x + 10, url.y + 8), shown[-70:], fill=(15, 23, 42) if (active or self.browser.url_input) else (148, 163, 184), font=_font(14))
        go = widgets["go"]
        d.rounded_rectangle(go.box, radius=6, fill=(37, 99, 235))
        d.text((go.x + 18, go.y + 8), "Go", fill=(255, 255, 255), font=_font(14))
        page_top = c.y + 56
        d.line((c.x, page_top - 4, c.x + c.w, page_top - 4), fill=(203, 213, 225))
        page = BROWSER_PAGES.get(self.browser.url)
        if not self.browser.url:
            d.text((c.x + 24, page_top + 40), "New tab", fill=(100, 116, 139), font=_font(20))
        elif page is None:
            d.text((c.x + 24, page_top + 24), "This site can't be reached", fill=(15, 23, 42), font=_font(20))
            d.text((c.x + 24, page_top + 60), f"{self.browser.url}'s server IP address could not be found.", fill=(71, 85, 105), font=_font(13))
        else:
            d.text((c.x + 24, page_top + 20 - win.scroll * 18), page["title"], fill=(15, 23, 42), font=_font(22))
            for i, line in enumerate(page["body"]):
                d.text((c.x + 24, page_top + 62 + i * 22 - win.scroll * 18), line, fill=(51, 65, 85), font=_font(14))
            if page.get("link"):
                link = widgets["link"]
                d.text((link.x, link.y), page["link"][0], fill=(37, 99, 235), font=_font(14))
                d.line((link.x, link.y + 18, link.x + 8 * len(page["link"][0]), link.y + 18), fill=(37, 99, 235))
        d.text((c.x + 12, c.y + c.h - 22), self.browser.status, fill=(100, 116, 139), font=_font(12))

    def _render_cursor(self, d: ImageDraw.ImageDraw) -> None:
        x, y = self.cursor
        d.polygon([(x, y), (x, y + 16), (x + 4, y + 12), (x + 11, y + 12)], fill=(255, 255, 255), outline=(0, 0, 0))

    # -- helpers for scripted policies / tests -------------------------------

    def locate(self, target: str) -> tuple[int, int]:
        """Centre pixel of a named UI element ("dock.notes", "calc.7", "settings.apply", ...)."""
        scope, _, item = target.partition(".")
        if scope == "dock":
            r = self._dock_rects()[item]
        elif scope == "close":
            win = self._window_for(item)
            if not win:
                raise KeyError(target)
            r = self._close_rect(win)
        elif scope == "title":
            win = self._window_for(item)
            if not win:
                raise KeyError(target)
            r = self._title_rect(win)
        else:
            win = self._window_for(scope)
            if not win:
                raise KeyError(f"{scope} window not open")
            if scope == "notes":
                c = self._content_rect(win)
                r = Rect(c.x + 12, c.y + 12, c.w - 24, c.h - 48)
            elif scope == "calc":
                r = dict(self._calc_buttons(win))[item]
            elif scope == "settings":
                r = self._settings_widgets(win)[item]
            elif scope == "browser":
                r = self._browser_widgets(win)[item]
            else:
                raise KeyError(target)
        return r.x + r.w // 2, r.y + r.h // 2
