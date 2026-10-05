"""Keyboard chord parsing shared by every real-computer driver.

The model emits xdotool-style chords ("ctrl+s", "alt+Tab", "Return", "shift+a").
We translate these to X11 keysyms, which both drivers accept:
  * X11/XTest driver resolves keysyms to keycodes (remapping spare keycodes
    for symbols missing from the layout, like xdotool does).
  * GNOME/Mutter driver passes keysyms straight to NotifyKeyboardKeysym.
"""

from __future__ import annotations

try:  # python-xlib is pure Python and present in the project venv
    from Xlib import XK as _XK

    _XK.load_keysym_group("xkb")
except Exception:  # pragma: no cover - fallback table below still works
    _XK = None

# Common aliases the model (and humans) use that differ from X11 names.
ALIASES: dict[str, str] = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "option": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Super_L",
    "cmd": "Super_L",
    "command": "Super_L",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "pgdn": "Page_Down",
    "insert": "Insert",
    "capslock": "Caps_Lock",
    "printscreen": "Print",
    "print": "Print",
    "menu": "Menu",
    "minus": "minus",
    "plus": "plus",
    "equal": "equal",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "quote": "apostrophe",
    "grave": "grave",
    "bracketleft": "bracketleft",
    "bracketright": "bracketright",
}

# Minimal keysym table so chord parsing works even without python-xlib.
_FALLBACK_KEYSYMS: dict[str, int] = {
    "Control_L": 0xFFE3, "Alt_L": 0xFFE9, "Shift_L": 0xFFE1, "Super_L": 0xFFEB,
    "Return": 0xFF0D, "Escape": 0xFF1B, "BackSpace": 0xFF08, "Delete": 0xFFFF,
    "Tab": 0xFF09, "space": 0x0020, "Up": 0xFF52, "Down": 0xFF54, "Left": 0xFF51,
    "Right": 0xFF53, "Home": 0xFF50, "End": 0xFF57, "Page_Up": 0xFF55, "Page_Down": 0xFF56,
    "Insert": 0xFF63, "Caps_Lock": 0xFFE5, "Print": 0xFF61, "Menu": 0xFF67,
    "minus": 0x2D, "plus": 0x2B, "equal": 0x3D, "comma": 0x2C, "period": 0x2E,
    "slash": 0x2F, "backslash": 0x5C, "semicolon": 0x3B, "apostrophe": 0x27, "grave": 0x60,
    "bracketleft": 0x5B, "bracketright": 0x5D,
    **{f"F{i}": 0xFFBD + i for i in range(1, 13)},
}

MODIFIER_KEYSYMS = {0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFE1, 0xFFE2, 0xFFEB, 0xFFEC, 0xFF7E}


class KeyError_(ValueError):
    """Unknown key name in a chord."""


def keysym_for_char(ch: str) -> int:
    """X11 keysym for a single printable character."""
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp  # X11 Unicode keysym convention


def keysym_for_name(name: str) -> int:
    """Resolve a single key token (not a chord) to a keysym."""
    if not name:
        raise KeyError_("empty key name")
    if len(name) == 1:
        return keysym_for_char(name)
    canonical = ALIASES.get(name.lower(), name)
    if _XK is not None:
        ks = _XK.string_to_keysym(canonical)
        if ks == 0 and canonical != name:
            ks = _XK.string_to_keysym(name)
        if ks == 0:  # try case-insensitive match against the fallback table
            for k, v in _FALLBACK_KEYSYMS.items():
                if k.lower() == name.lower():
                    return v
        if ks:
            return ks
    else:
        if canonical in _FALLBACK_KEYSYMS:
            return _FALLBACK_KEYSYMS[canonical]
        for k, v in _FALLBACK_KEYSYMS.items():
            if k.lower() == name.lower():
                return v
    raise KeyError_(f"unknown key {name!r}")


def parse_chord(chord: str) -> list[int]:
    """"ctrl+shift+t" -> [Control_L, Shift_L, t] as keysyms (press order)."""
    chord = chord.strip()
    if not chord:
        raise KeyError_("empty chord")
    if chord == "+":
        return [keysym_for_char("+")]
    parts = [p for p in chord.split("+")]
    # "ctrl++" style: a trailing empty token means the literal '+' key
    tokens: list[str] = []
    for i, p in enumerate(parts):
        if p == "":
            if i == len(parts) - 1 and tokens:
                tokens.append("plus")
            continue
        tokens.append(p.strip())
    if not tokens:
        raise KeyError_(f"cannot parse chord {chord!r}")
    return [keysym_for_name(t) for t in tokens]


def is_modifier(keysym: int) -> bool:
    return keysym in MODIFIER_KEYSYMS
