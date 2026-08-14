"""Deterministic, browser-free CDP page-input payload tests."""

import math

import pytest

from agent_browser_mcp.page_input import (
    ChallengeAttemptTracker,
    InputValidationError,
    click_commands,
    drag_commands,
    press_commands,
    resolve_selector_script,
    type_commands,
)


def test_click_is_move_press_release():
    commands = click_commands(20, 30, button="left", clicks=1)
    assert [c["method"] for c in commands] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert commands[1]["params"]["type"] == "mousePressed"
    assert commands[2]["params"]["type"] == "mouseReleased"


@pytest.mark.parametrize("button", ["primary", "", 1, [], {}])
def test_click_rejects_unknown_buttons(button):
    with pytest.raises(InputValidationError):
        click_commands(1, 2, button=button)
    with pytest.raises(InputValidationError):
        drag_commands(1, 2, 3, 4, button=button)


@pytest.mark.parametrize("clicks", [0, -1, 1.5, True])
def test_click_rejects_non_positive_counts(clicks):
    with pytest.raises(InputValidationError):
        click_commands(1, 2, clicks=clicks)


@pytest.mark.parametrize("coordinate", [math.inf, -math.inf, math.nan])
def test_pointer_operations_reject_non_finite_coordinates(coordinate):
    with pytest.raises(InputValidationError):
        click_commands(coordinate, 1)
    with pytest.raises(InputValidationError):
        drag_commands(0, 0, coordinate, 1)


def test_drag_has_bounded_intermediate_moves():
    commands = drag_commands(0, 0, 100, 50, duration=0.3)
    assert commands[0]["params"]["type"] == "mouseMoved"
    assert commands[1]["params"]["type"] == "mousePressed"
    assert commands[-1]["params"]["type"] == "mouseReleased"
    assert 3 <= len(commands) <= 24


@pytest.mark.parametrize(("button", "buttons"), [("left", 1), ("right", 2), ("middle", 4)])
def test_drag_reports_the_pressed_button_bit(button, buttons):
    commands = drag_commands(0, 0, 10, 10, button=button)
    assert commands[2]["params"]["button"] == button
    assert commands[2]["params"]["buttons"] == buttons


@pytest.mark.parametrize("duration", [-0.01, 10.01, math.nan, math.inf])
def test_drag_rejects_duration_outside_bounds(duration):
    with pytest.raises(InputValidationError):
        drag_commands(0, 0, 1, 1, duration=duration)


def test_press_encodes_ctrl_shift_p():
    commands = press_commands("ctrl,shift,p")
    assert [(c["params"]["type"], c["params"]["key"]) for c in commands] == [
        ("rawKeyDown", "Control"),
        ("rawKeyDown", "Shift"),
        ("keyDown", "p"),
        ("keyUp", "p"),
        ("keyUp", "Shift"),
        ("keyUp", "Control"),
    ]
    assert commands[2]["params"]["modifiers"] == 10
    assert commands[3]["params"]["modifiers"] == 10


@pytest.mark.parametrize("chord", ["", "ctrl", "ctrl,,p", "ctrl,wat,p", "ctrl,p,q"])
def test_press_rejects_malformed_chords(chord):
    with pytest.raises(InputValidationError):
        press_commands(chord)


@pytest.mark.parametrize("chord", ["ctrl,control,p", "meta,cmd,p"])
def test_press_rejects_duplicate_modifier_aliases(chord):
    with pytest.raises(InputValidationError):
        press_commands(chord)


def test_type_focuses_selects_and_optionally_submits():
    commands = type_commands("#name", "hello", select_all=True, submit_key="Enter")
    assert [command["method"] for command in commands] == [
        "Runtime.evaluate",
        "Input.insertText",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    assert "focus" in commands[0]["params"]["expression"]
    assert "select" in commands[0]["params"]["expression"]
    assert commands[1]["params"]["text"] == "hello"


def test_type_can_await_xterm_delivery_before_submitting():
    commands = type_commands(
        ".xterm",
        "printf 'abm'",
        submit_key="enter",
        submit_delay_ms=75,
    )

    assert [command["method"] for command in commands] == [
        "Runtime.evaluate",
        "Input.insertText",
        "Runtime.evaluate",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    delay = commands[2]["params"]
    assert delay["expression"] == "new Promise(resolve => setTimeout(resolve, 75))"
    assert delay["awaitPromise"] is True
    assert delay["returnByValue"] is True


def test_type_retargets_xterm_container_to_helper_textarea():
    commands = type_commands(".xterm", "printf 'abm'", submit_key="enter")
    expression = commands[0]["params"]["expression"]

    assert ".xterm-helper-textarea" in expression
    assert "closest('.xterm')" in expression
    assert "targetKind" in expression
    assert commands[1]["params"]["text"] == "printf 'abm'"


def test_type_without_selector_autofocuses_the_single_xterm_helper():
    commands = type_commands("", "pwd")
    expression = commands[0]["params"]["expression"]

    assert "document.activeElement" in expression
    assert ".xterm-helper-textarea" in expression
    assert commands[1]["method"] == "Input.insertText"


def test_selector_is_json_escaped_and_has_no_match_path():
    script = resolve_selector_script('a[data-x="quoted"]', 2, -3)
    assert 'a[data-x=\\"quoted\\"]' in script
    assert "found:false" in script
    assert "challengeMarker" in script


def test_selector_challenge_marker_uses_the_challenge_not_target_element():
    script = resolve_selector_script("#unrelated-target")
    assert "const markerElement = elementIsChallenge ? element : pageChallengeElement;" in script
    assert "markerElement.tagName" in script
    assert "[location.origin, document.title, element.tagName" not in script


def test_same_stable_challenge_marker_does_not_reset_tracker_for_new_target():
    tracker = ChallengeAttemptTracker(max_attempts=3, window_seconds=120)
    marker = "https://example.test|/protected|iframe|challenge-frame|challenges.cloudflare.com"
    assert tracker.record("c:1", marker, now=1) is False
    assert tracker.record("c:1", marker, now=2) is False
    assert tracker.record("c:1", marker, now=3) is True


def test_identical_challenge_stalls_on_third_attempt():
    tracker = ChallengeAttemptTracker(max_attempts=3, window_seconds=120)
    assert tracker.record("c:1", "turnstile:a", now=1) is False
    assert tracker.record("c:1", "turnstile:a", now=2) is False
    assert tracker.record("c:1", "turnstile:a", now=3) is True


def test_changed_or_expired_challenge_marker_resets_count():
    tracker = ChallengeAttemptTracker(max_attempts=3, window_seconds=120)
    tracker.record("c:1", "turnstile:a", now=1)
    tracker.record("c:1", "turnstile:a", now=2)
    assert tracker.record("c:1", "turnstile:b", now=3) is False
    tracker.record("c:1", "turnstile:b", now=4)
    assert tracker.record("c:1", "turnstile:b", now=125) is False
