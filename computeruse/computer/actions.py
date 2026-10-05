"""Action model shared by every computer backend.

Actions mirror the Anthropic `computer_20250124` tool surface so that a tool_use
block from the model can be parsed directly into a typed, validated `Action`.
Every backend (simulated desktop, local Xvfb, Docker sandbox, remote VM daemon)
consumes the same `Action` objects, which keeps the agent loop backend-agnostic.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator


class ActionError(ValueError):
    """Raised when a tool_use payload cannot be parsed into a valid action."""


_Px = Annotated[int, Field(ge=0)]
# Screen coordinates in model space. Negative values are always a model error;
# upper bounds depend on the display and are checked by the agent loop.
Coordinate = tuple[_Px, _Px]


class ActionKind(str, Enum):
    SCREENSHOT = "screenshot"
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    MIDDLE_CLICK = "middle_click"
    DOUBLE_CLICK = "double_click"
    TRIPLE_CLICK = "triple_click"
    MOUSE_MOVE = "mouse_move"
    LEFT_CLICK_DRAG = "left_click_drag"
    LEFT_MOUSE_DOWN = "left_mouse_down"
    LEFT_MOUSE_UP = "left_mouse_up"
    TYPE = "type"
    KEY = "key"
    HOLD_KEY = "hold_key"
    SCROLL = "scroll"
    WAIT = "wait"
    CURSOR_POSITION = "cursor_position"
    ZOOM = "zoom"


CLICK_KINDS = {
    ActionKind.LEFT_CLICK,
    ActionKind.RIGHT_CLICK,
    ActionKind.MIDDLE_CLICK,
    ActionKind.DOUBLE_CLICK,
    ActionKind.TRIPLE_CLICK,
}

# Actions that change VM state (as opposed to pure observations).
MUTATING_KINDS = set(ActionKind) - {
    ActionKind.SCREENSHOT,
    ActionKind.CURSOR_POSITION,
    ActionKind.WAIT,
    ActionKind.MOUSE_MOVE,
    ActionKind.ZOOM,
}

# Observations the agent loop answers itself without touching the backend.
HARNESS_KINDS = {ActionKind.ZOOM}


class _Base(BaseModel):
    model_config = {"extra": "forbid"}


class Screenshot(_Base):
    action: Literal["screenshot"] = "screenshot"


class CursorPosition(_Base):
    action: Literal["cursor_position"] = "cursor_position"


class MouseMove(_Base):
    action: Literal["mouse_move"] = "mouse_move"
    coordinate: Coordinate


class Click(_Base):
    action: Literal["left_click", "right_click", "middle_click", "double_click", "triple_click"]
    coordinate: Coordinate | None = None
    # xdotool-style modifier string held during the click, e.g. "shift" or "ctrl".
    text: str | None = None


class Drag(_Base):
    action: Literal["left_click_drag"] = "left_click_drag"
    start_coordinate: Coordinate
    coordinate: Coordinate


class MouseDown(_Base):
    action: Literal["left_mouse_down"] = "left_mouse_down"
    coordinate: Coordinate | None = None


class MouseUp(_Base):
    action: Literal["left_mouse_up"] = "left_mouse_up"
    coordinate: Coordinate | None = None


class TypeText(_Base):
    action: Literal["type"] = "type"
    text: str

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if v == "":
            raise ValueError("type action requires non-empty text")
        return v


class KeyPress(_Base):
    """Press a key chord using xdotool syntax, e.g. "Return", "ctrl+s", "alt+Tab"."""

    action: Literal["key"] = "key"
    text: str
    repeat: int = Field(default=1, ge=1, le=100)

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("key action requires a key name")
        return v.strip()


class HoldKey(_Base):
    action: Literal["hold_key"] = "hold_key"
    text: str
    duration: float = Field(gt=0, le=30)


class Scroll(_Base):
    action: Literal["scroll"] = "scroll"
    coordinate: Coordinate
    scroll_direction: Literal["up", "down", "left", "right"]
    scroll_amount: int = Field(default=3, ge=0, le=50)
    text: str | None = None  # optional modifier


class Wait(_Base):
    action: Literal["wait"] = "wait"
    duration: float = Field(default=1.0, ge=0, le=60)


class Zoom(_Base):
    """Magnified view of a screen region: [x0, y0, x1, y1] in model pixels.

    A pure observation answered by the agent loop (crop + upscale of a fresh
    screenshot); it never reaches the backend.
    """

    action: Literal["zoom"] = "zoom"
    region: tuple[_Px, _Px, _Px, _Px]

    @field_validator("region")
    @classmethod
    def _non_empty_region(cls, v: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = v
        if x1 <= x0 or y1 <= y0:
            raise ValueError("region must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0")
        return v


Action = Annotated[
    Screenshot | CursorPosition | MouseMove | Click | Drag | MouseDown | MouseUp | TypeText | KeyPress | HoldKey | Scroll | Wait | Zoom,
    Field(discriminator="action"),
]

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(payload: dict[str, Any]) -> Action:
    """Parse a raw tool_use input dict (from the model) into a typed Action.

    Raises ActionError with a human-readable message that is safe to feed back
    to the model as an error tool_result so it can self-correct.
    """
    if not isinstance(payload, dict):
        raise ActionError("tool input must be an object")
    if "action" not in payload:
        raise ActionError("missing required field 'action'")
    try:
        return _ACTION_ADAPTER.validate_python(_normalise(payload))
    except ValidationError as e:  # pragma: no cover - message formatting only
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors()
        )
        raise ActionError(f"invalid action {payload.get('action')!r}: {problems}") from e


def _normalise(payload: dict[str, Any]) -> dict[str, Any]:
    """Tolerate common model slips: list coordinates, string ints, float px."""
    out = dict(payload)
    for key in ("coordinate", "start_coordinate"):
        if key in out and out[key] is not None:
            c = out[key]
            if isinstance(c, (list, tuple)) and len(c) == 2:
                out[key] = (int(round(float(c[0]))), int(round(float(c[1]))))
    region = out.get("region")
    if isinstance(region, (list, tuple)) and len(region) == 4:
        try:
            out["region"] = tuple(int(round(float(v))) for v in region)
        except (TypeError, ValueError):
            pass
    for key in ("scroll_amount", "repeat"):
        if key in out and isinstance(out[key], (str, float)):
            try:
                out[key] = int(float(out[key]))
            except ValueError:
                pass
    if "duration" in out and isinstance(out["duration"], str):
        try:
            out["duration"] = float(out["duration"])
        except ValueError:
            pass
    return out


def action_kind(action: Action) -> ActionKind:
    return ActionKind(action.action)


def action_coordinates(action: Action) -> list[Coordinate]:
    """All coordinates referenced by an action (used for validation + UI overlays)."""
    coords: list[Coordinate] = []
    start = getattr(action, "start_coordinate", None)
    if start is not None:
        coords.append(tuple(start))  # type: ignore[arg-type]
    c = getattr(action, "coordinate", None)
    if c is not None:
        coords.append(tuple(c))  # type: ignore[arg-type]
    if isinstance(action, Zoom):
        x0, y0, x1, y1 = action.region
        coords.extend([(x0, y0), (x1, y1)])
    return coords


def describe_action(action: Action) -> str:
    """Short human-readable label used in the timeline UI and logs."""
    kind = action.action
    if isinstance(action, Click):
        mods = f" ({action.text})" if action.text else ""
        where = f" at {action.coordinate}" if action.coordinate else ""
        return f"{kind.replace('_', ' ')}{where}{mods}"
    if isinstance(action, MouseMove):
        return f"move mouse to {action.coordinate}"
    if isinstance(action, Drag):
        return f"drag {action.start_coordinate} → {action.coordinate}"
    if isinstance(action, TypeText):
        preview = action.text if len(action.text) <= 40 else action.text[:37] + "…"
        return f"type {preview!r}"
    if isinstance(action, KeyPress):
        times = f" ×{action.repeat}" if action.repeat > 1 else ""
        return f"press {action.text}{times}"
    if isinstance(action, HoldKey):
        return f"hold {action.text} for {action.duration:g}s"
    if isinstance(action, Scroll):
        return f"scroll {action.scroll_direction} ×{action.scroll_amount} at {action.coordinate}"
    if isinstance(action, Wait):
        return f"wait {action.duration:g}s"
    if isinstance(action, Zoom):
        x0, y0, x1, y1 = action.region
        return f"zoom into ({x0}, {y0})–({x1}, {y1})"
    return kind.replace("_", " ")


class ActionResult(BaseModel):
    """Outcome of executing an action on a computer backend."""

    ok: bool = True
    output: str | None = None  # textual payload (e.g. cursor position)
    error: str | None = None
    duration_ms: float = 0.0
    # Populated by backends that return a fresh frame with the result.
    screenshot_png: bytes | None = Field(default=None, repr=False, exclude=True)

    @classmethod
    def failure(cls, error: str, duration_ms: float = 0.0) -> ActionResult:
        return cls(ok=False, error=error, duration_ms=duration_ms)
