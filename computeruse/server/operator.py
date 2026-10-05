"""Operator takeover bookkeeping.

While a human drives the computer through the console, every pointer move, click
and keystroke flows through the session as a stream of ordinary `Action`s. We do
not persist that stream (it can contain passwords and is far too chatty for the
timeline); instead this class keeps counts and a short summary that is recorded
when control is handed back and folded into the model's next turn, e.g.

    manual control for 1m 12s (4 clicks, typed 23 characters, pressed Return ×2
    and ctrl+l, scrolled 3×)
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from computeruse.computer.actions import (
    Action,
    Click,
    Drag,
    KeyPress,
    MouseDown,
    MouseUp,
    Scroll,
    TypeText,
)

# Keys whose presses are worth naming in the summary (everything else is just counted).
NOTABLE_KEYS = {"Return", "Tab", "Escape", "BackSpace", "Delete", "Page_Up", "Page_Down", "Home", "End"}


class OperatorControl:
    def __init__(self) -> None:
        self.active = False
        self.since: float | None = None
        self.last_input_at: float | None = None
        self.clicks = 0
        self.typed_chars = 0
        self.scrolls = 0
        self.drags = 0
        self.moves = 0
        self.errors = 0
        self.keys: Counter[str] = Counter()
        self.buttons_down: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def start(self, now: float | None = None) -> None:
        self.__init__()
        self.active = True
        self.since = now if now is not None else time.time()

    def stop(self) -> dict[str, Any]:
        """End the takeover; returns the snapshot that should be recorded."""
        snap = self.snapshot()
        snap["summary"] = self.summary()
        self.__init__()
        return snap

    # -- accounting ----------------------------------------------------------

    def note(self, action: Action, ok: bool = True) -> None:
        self.last_input_at = time.time()
        if not ok:
            self.errors += 1
            return
        if isinstance(action, Click):
            self.clicks += 1
        elif isinstance(action, MouseDown):
            self.buttons_down.add("left")
        elif isinstance(action, MouseUp):
            if "left" in self.buttons_down:
                self.buttons_down.discard("left")
                self.clicks += 1  # a complete press/release pair; drags are clicks that moved
        elif isinstance(action, Drag):
            self.drags += 1
        elif isinstance(action, TypeText):
            self.typed_chars += len(action.text)
        elif isinstance(action, KeyPress):
            self.keys[action.text] += action.repeat
        elif isinstance(action, Scroll):
            self.scrolls += 1
        elif action.action == "mouse_move":
            self.moves += 1

    @property
    def inputs(self) -> int:
        return self.clicks + self.typed_chars + sum(self.keys.values()) + self.scrolls + self.drags

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "active": self.active, "since": self.since,
            "duration_s": round(now - self.since, 1) if self.since else 0.0,
            "clicks": self.clicks, "typed_chars": self.typed_chars, "keys": sum(self.keys.values()),
            "scrolls": self.scrolls, "drags": self.drags, "errors": self.errors, "inputs": self.inputs,
        }

    def summary(self) -> str:
        dur = _fmt_duration((time.time() - self.since) if self.since else 0.0)
        parts: list[str] = []
        if self.clicks:
            parts.append(f"{self.clicks} click{'s' if self.clicks != 1 else ''}")
        if self.drags:
            parts.append(f"{self.drags} drag{'s' if self.drags != 1 else ''}")
        if self.typed_chars:
            parts.append(f"typed {self.typed_chars} character{'s' if self.typed_chars != 1 else ''}")
        if self.keys:
            named = [f"{k} ×{n}" if n > 1 else k for k, n in self.keys.most_common()
                     if k in NOTABLE_KEYS or "+" in k][:6]
            other = sum(n for k, n in self.keys.items() if not (k in NOTABLE_KEYS or "+" in k))
            if named:
                parts.append("pressed " + _join(named))
            if other:
                parts.append(f"{other} other key press{'es' if other != 1 else ''}")
        if self.scrolls:
            parts.append(f"scrolled {self.scrolls}×")
        if not parts:
            return f"manual control for {dur} with no inputs (just looked at the screen)"
        return f"manual control for {dur} ({', '.join(parts)})"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"
