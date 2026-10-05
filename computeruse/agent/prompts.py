"""System prompt and tool definitions for the computer-use agent."""

from __future__ import annotations

import platform
from datetime import UTC, datetime

BACKEND_NOTES = {
    "browser": (
        "You are operating a real Google Chrome window that fills the whole screen. "
        "There is no desktop, dock or window manager behind it. Use the omnibox (ctrl+l) to navigate. "
        "Pages load from the live internet; wait for them to finish loading before acting."
    ),
    "desktop": (
        "You are operating a real GNOME Linux desktop. Google Chrome and a terminal are in the dock at the "
        "bottom; the Activities overview opens with the Super key. Other people's windows may be open: never "
        "close, move or type into windows that are unrelated to your task."
    ),
    "docker": (
        "You are operating a sandboxed Linux desktop with Chromium filling the screen. "
        "Use the omnibox (ctrl+l) to navigate."
    ),
    "remote": "You are operating a remote Linux machine through its screen, mouse and keyboard.",
    "simulated": (
        "You are operating SimOS, a small desktop with four apps in the bottom dock: Notes, Calculator, "
        "Settings and Browser. Click a dock icon to open an app; windows have a red close button at the "
        "top-right. In Notes, ctrl+s saves. In the Browser, click the address bar, type a URL and press Return."
    ),
}


def system_prompt(backend: str, display: tuple[int, int], extra: str | None = None) -> str:
    w, h = display
    now = datetime.now(UTC).strftime("%A, %B %d, %Y")
    note = BACKEND_NOTES.get(backend, BACKEND_NOTES["remote"])
    parts = [
        f"<SYSTEM_CAPABILITY>\n"
        f"* You control a computer through screenshots and a `computer` tool (mouse, keyboard, scrolling).\n"
        f"* The display is {w}x{h} pixels. Coordinates are absolute screen pixels with (0, 0) at the top-left.\n"
        f"* {note}\n"
        f"* Architecture: {platform.machine()}. Today's date is {now}.\n"
        f"* After every action you automatically receive a fresh screenshot; do not request extra screenshots unless the screen may still be changing, in which case use `wait`.\n"
        f"* Prefer keyboard shortcuts for navigation and text selection (ctrl+l, ctrl+a, ctrl+f, Tab). Type text with the `type` action; press chords with `key`.\n"
        f"* Before clicking, locate the exact target in the latest screenshot and aim at its centre. If a click had no visible effect, do not repeat it blindly: zoom in on what changed, scroll, or try a different element.\n"
        f"* If text is too small to read reliably, use the `zoom` action (when available) on that region before acting.\n"
        f"* Dialogs, cookie banners and login walls are normal: dismiss them when they block the task. Never enter credentials or payment details unless the task explicitly provides them.\n"
        f"</SYSTEM_CAPABILITY>",
        "<IMPORTANT>\n"
        "* Work step by step and narrate briefly what you see and what you will do next before each action.\n"
        "* When the task is fully complete, stop calling tools and reply with a short summary of the result "
        "(include any requested information verbatim). If the task is impossible or unsafe, stop and explain why.\n"
        "* Do not claim success you cannot see on screen.\n"
        "</IMPORTANT>",
    ]
    if extra:
        parts.append(extra.strip())
    return "\n\n".join(parts)


def computer_tool(tool_version: str, display: tuple[int, int], display_number: int | None = None) -> dict:
    """Anthropic's built-in computer tool (served by the API, no schema needed)."""
    w, h = display
    tool = {"type": tool_version, "name": "computer", "display_width_px": w, "display_height_px": h}
    if display_number is not None:
        tool["display_number"] = display_number
    return tool


ACTION_NAMES = [
    "screenshot", "left_click", "right_click", "middle_click", "double_click", "triple_click",
    "mouse_move", "left_click_drag", "left_mouse_down", "left_mouse_up", "type", "key", "hold_key",
    "scroll", "wait", "cursor_position", "zoom",
]


def computer_tool_schema(display: tuple[int, int]) -> dict:
    """The same action vocabulary as a plain JSON-schema tool.

    Used when the model API has no built-in computer tool (Gemini) or rejects
    it (some Claude builds on Vertex). Kept deliberately flat — no `oneOf`,
    `const` or `$ref` — so every provider's schema validator accepts it.
    """
    w, h = display
    coord = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
             "description": "[x, y] in screen pixels"}
    return {
        "name": "computer",
        "description": (
            f"Control the computer through its {w}x{h} screen, mouse and keyboard. Every call returns a fresh "
            "screenshot. Actions: screenshot; left_click/right_click/middle_click/double_click/triple_click "
            "(coordinate, optional modifier in text e.g. 'shift'); mouse_move (coordinate); left_click_drag "
            "(start_coordinate -> coordinate); left_mouse_down/left_mouse_up; type (text, typed literally); "
            "key (xdotool chord in text e.g. 'Return', 'ctrl+l', 'alt+Tab'; optional repeat); hold_key (text, "
            "duration seconds); scroll (coordinate, scroll_direction, scroll_amount in clicks); wait (duration "
            "seconds); cursor_position; zoom (region [x0, y0, x1, y1] returns a magnified view of that area)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ACTION_NAMES, "description": "The action to perform."},
                "coordinate": coord,
                "start_coordinate": {**coord, "description": "[x, y] where a drag starts"},
                "text": {"type": "string", "description": "Text to type, key chord to press, or click modifier."},
                "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "scroll_amount": {"type": "integer", "description": "Scroll clicks (1-50)."},
                "duration": {"type": "number", "description": "Seconds for wait / hold_key."},
                "repeat": {"type": "integer", "description": "Times to press the key (1-100)."},
                "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                           "description": "[x0, y0, x1, y1] region to zoom into"},
            },
            "required": ["action"],
        },
    }
