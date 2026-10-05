"""Action schema parsing/normalisation and key chord handling."""

import pytest

from computeruse.computer import keys
from computeruse.computer.actions import (
    HARNESS_KINDS,
    MUTATING_KINDS,
    ActionError,
    action_coordinates,
    describe_action,
    parse_action,
)


@pytest.mark.parametrize(
    "payload, kind",
    [
        ({"action": "screenshot"}, "screenshot"),
        ({"action": "left_click", "coordinate": [10, 20]}, "left_click"),
        ({"action": "double_click", "coordinate": (5, 5)}, "double_click"),
        ({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]}, "left_click_drag"),
        ({"action": "type", "text": "hello"}, "type"),
        ({"action": "key", "text": "ctrl+l"}, "key"),
        ({"action": "scroll", "coordinate": [100, 100], "scroll_direction": "down", "scroll_amount": 3}, "scroll"),
        ({"action": "wait", "duration": 1.5}, "wait"),
        ({"action": "cursor_position"}, "cursor_position"),
    ],
)
def test_parse_valid_actions(payload, kind):
    action = parse_action(payload)
    assert action.action == kind
    assert describe_action(action)


def test_parse_is_lenient_about_common_model_mistakes():
    # coordinates as strings / floats, alias names
    a = parse_action({"action": "left_click", "coordinate": ["10.6", 20.2]})
    assert a.coordinate == (11, 20) or a.coordinate == [11, 20]
    k = parse_action({"action": "key", "text": "Return"})
    assert k.text == "Return"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "bogus"},
        {"action": "mouse_move"},  # missing coordinate
        {"action": "type", "text": ""},
        {"action": "scroll", "coordinate": [1, 1], "scroll_direction": "sideways", "scroll_amount": 1},
        {"action": "left_click", "coordinate": [-5, 10]},
    ],
)
def test_parse_rejects_invalid(payload):
    with pytest.raises(ActionError):
        parse_action(payload)


def test_click_without_coordinate_means_click_at_cursor():
    # Per the computer_20250124 spec a click may omit `coordinate`.
    a = parse_action({"action": "left_click"})
    assert a.coordinate is None
    assert action_coordinates(a) == []


def test_coordinates_and_mutating_classification():
    drag = parse_action({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]})
    assert [tuple(c) for c in action_coordinates(drag)] == [(1, 2), (3, 4)]
    assert "left_click" in MUTATING_KINDS and "screenshot" not in MUTATING_KINDS


def test_key_chords():
    ctrl_l = keys.parse_chord("ctrl+l")
    assert len(ctrl_l) == 2 and keys.is_modifier(ctrl_l[0]) and not keys.is_modifier(ctrl_l[1])
    assert keys.parse_chord("Return") == keys.parse_chord("enter")
    assert keys.parse_chord("ctrl++")[-1] == keys.keysym_for_char("+")
    assert keys.keysym_for_char("é") > 0
    with pytest.raises((KeyError, ValueError)):
        keys.parse_chord("")


# -- zoom / key repeat (toolset-era vocabulary) ------------------------------------------


def test_zoom_parses_normalises_and_describes():
    z = parse_action({"action": "zoom", "region": ["10.4", 20, 110.6, "60"]})
    assert z.action == "zoom" and tuple(z.region) == (10, 20, 111, 60)
    assert [tuple(c) for c in action_coordinates(z)] == [(10, 20), (111, 60)]
    assert describe_action(z) == "zoom into (10, 20)–(111, 60)"
    assert "zoom" in HARNESS_KINDS and "zoom" not in MUTATING_KINDS


@pytest.mark.parametrize("region", [[10, 10, 10, 50], [10, 10, 50, 5], [1, 2, 3], [-1, 0, 5, 5], None])
def test_zoom_rejects_degenerate_regions(region):
    payload = {"action": "zoom"} if region is None else {"action": "zoom", "region": region}
    with pytest.raises(ActionError):
        parse_action(payload)


def test_key_repeat_defaults_coerces_and_bounds():
    assert parse_action({"action": "key", "text": "Tab"}).repeat == 1
    k = parse_action({"action": "key", "text": "BackSpace", "repeat": "3"})
    assert k.repeat == 3 and describe_action(k) == "press BackSpace ×3"
    assert parse_action({"action": "key", "text": "Down", "repeat": 2.0}).repeat == 2
    for bad in (0, 101):
        with pytest.raises(ActionError):
            parse_action({"action": "key", "text": "Down", "repeat": bad})
