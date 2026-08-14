"""Pure, deterministic CDP payload builders for page-level input."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any


class InputValidationError(ValueError):
    """Raised when a page-input request cannot be represented safely."""


_MODIFIER_ALIASES = {"alt": "alt", "ctrl": "control", "control": "control", "meta": "meta", "cmd": "meta", "shift": "shift"}
_MODIFIERS = {"alt": 1, "control": 2, "meta": 4, "shift": 8}
_MODIFIER_KEYS = {
    "alt": ("Alt", "AltLeft", 18),
    "control": ("Control", "ControlLeft", 17),
    "meta": ("Meta", "MetaLeft", 91),
    "shift": ("Shift", "ShiftLeft", 16),
}
_NAMED_KEYS = {
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
    "delete": ("Delete", "Delete", 46),
}
_BUTTONS = {"left", "middle", "right"}
_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}


def _command(method: str, **params: Any) -> dict[str, Any]:
    return {"cmd": "cdp", "method": method, "params": params}


def _number(value: Any, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputValidationError(f"{name} must be a finite number")
    return value


def _mouse_event(event_type: str, x: float | int, y: float | int, **extra: Any) -> dict[str, Any]:
    return _command("Input.dispatchMouseEvent", type=event_type, x=x, y=y, **extra)


def _button(value: Any) -> str:
    if not isinstance(value, str) or value not in _BUTTONS:
        raise InputValidationError("button must be left, middle, or right")
    return value


def click_commands(x: float, y: float, *, button: str = "left", clicks: int = 1) -> list[dict[str, Any]]:
    """Build a move, press, release CDP click sequence."""
    x = _number(x, "x")
    y = _number(y, "y")
    button = _button(button)
    if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
        raise InputValidationError("clicks must be a positive integer")
    shared = {"button": button, "clickCount": clicks}
    return [
        _mouse_event("mouseMoved", x, y),
        _mouse_event("mousePressed", x, y, **shared),
        _mouse_event("mouseReleased", x, y, **shared),
    ]


def drag_commands(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    duration: float = 0.3,
    button: str = "left",
) -> list[dict[str, Any]]:
    """Build a bounded, deterministic press-and-drag mouse sequence."""
    start_x = _number(start_x, "start_x")
    start_y = _number(start_y, "start_y")
    end_x = _number(end_x, "end_x")
    end_y = _number(end_y, "end_y")
    duration = _number(duration, "duration")
    if not 0 <= duration <= 10:
        raise InputValidationError("duration must be between 0 and 10 seconds")
    button = _button(button)

    # At most twenty in-flight points keeps even a ten-second drag compact.
    steps = min(20, max(1, math.ceil(duration * 20)))
    commands = [
        _mouse_event("mouseMoved", start_x, start_y),
        _mouse_event("mousePressed", start_x, start_y, button=button, clickCount=1),
    ]
    for step in range(1, steps + 1):
        fraction = step / steps
        commands.append(
            _mouse_event(
                "mouseMoved",
                start_x + (end_x - start_x) * fraction,
                start_y + (end_y - start_y) * fraction,
                button=button,
                buttons=_BUTTON_BITS[button],
            )
        )
    commands.append(_mouse_event("mouseReleased", end_x, end_y, button=button, clickCount=1))
    return commands


def _key_details(key: str) -> tuple[str, str, int]:
    if not isinstance(key, str) or not key:
        raise InputValidationError("key must be a supported key name or one printable character")
    named = _NAMED_KEYS.get(key.lower())
    if named:
        return named
    if len(key) == 1 and key.isprintable():
        if key.isalpha():
            return key, f"Key{key.upper()}", ord(key.upper())
        if key.isdigit():
            return key, f"Digit{key}", ord(key)
        return key, "", ord(key)
    raise InputValidationError(f"unsupported key: {key}")


def press_commands(chord: str) -> list[dict[str, Any]]:
    """Build CDP keyboard events for a comma-delimited key chord."""
    if not isinstance(chord, str):
        raise InputValidationError("chord must be a comma-delimited string")
    parts = [part.strip() for part in chord.split(",")]
    if not parts or any(not part for part in parts):
        raise InputValidationError("chord contains an empty key")
    if len(parts) > 1:
        raw_modifier_names = [part.lower() for part in parts[:-1]]
        if any(name not in _MODIFIER_ALIASES for name in raw_modifier_names):
            raise InputValidationError("only modifiers may precede the final key")
        modifier_names = [_MODIFIER_ALIASES[name] for name in raw_modifier_names]
        if len(set(modifier_names)) != len(modifier_names):
            raise InputValidationError("a modifier may appear only once")
    else:
        modifier_names = []
    key, code, key_code = _key_details(parts[-1])
    if parts[-1].lower() in _MODIFIER_ALIASES:
        raise InputValidationError("chord must end with a non-modifier key")

    modifiers = 0
    commands: list[dict[str, Any]] = []
    for modifier_name in modifier_names:
        modifier_key, modifier_code, modifier_code_value = _MODIFIER_KEYS[modifier_name]
        modifiers |= _MODIFIERS[modifier_name]
        commands.append(
            _command(
                "Input.dispatchKeyEvent",
                type="rawKeyDown",
                key=modifier_key,
                code=modifier_code,
                windowsVirtualKeyCode=modifier_code_value,
                nativeVirtualKeyCode=modifier_code_value,
                modifiers=modifiers,
            )
        )

    key_params = {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": key_code,
        "nativeVirtualKeyCode": key_code,
        "modifiers": modifiers,
    }
    commands.extend(
        [
            _command("Input.dispatchKeyEvent", type="keyDown", **key_params),
            _command("Input.dispatchKeyEvent", type="keyUp", **key_params),
        ]
    )
    for modifier_name in reversed(modifier_names):
        modifier_key, modifier_code, modifier_code_value = _MODIFIER_KEYS[modifier_name]
        commands.append(
            _command(
                "Input.dispatchKeyEvent",
                type="keyUp",
                key=modifier_key,
                code=modifier_code,
                windowsVirtualKeyCode=modifier_code_value,
                nativeVirtualKeyCode=modifier_code_value,
                modifiers=modifiers,
            )
        )
        modifiers &= ~_MODIFIERS[modifier_name]
    return commands


def type_commands(
    selector: str,
    text: str,
    *,
    select_all: bool = False,
    submit_key: str | None = None,
    submit_delay_ms: int = 0,
) -> list[dict[str, Any]]:
    """Focus a matching element, insert text, and optionally submit a key.

    An empty selector means "use the focused editor". Xterm.js keeps its real
    input sink in a hidden ``.xterm-helper-textarea``; a background tab often
    leaves ``document.body`` focused, so plain ``Input.insertText`` otherwise
    disappears. Resolve xterm containers/descendants to that helper before the
    trusted CDP input is dispatched.
    """
    if not isinstance(text, str):
        raise InputValidationError("text must be a string")
    if (
        isinstance(submit_delay_ms, bool)
        or not isinstance(submit_delay_ms, int)
        or submit_delay_ms < 0
    ):
        raise InputValidationError("submit_delay_ms must be a non-negative integer")
    expression = type_target_script(selector, select_all=select_all)
    commands = [
        _command("Runtime.evaluate", expression=expression, returnByValue=True),
        _command("Input.insertText", text=text),
    ]
    if submit_key is not None:
        if submit_delay_ms:
            commands.append(
                _command(
                    "Runtime.evaluate",
                    expression=(
                        "new Promise(resolve => setTimeout(resolve, "
                        f"{submit_delay_ms}))"
                    ),
                    awaitPromise=True,
                    returnByValue=True,
                )
            )
        commands.extend(press_commands(submit_key))
    return commands


def type_target_script(selector: str, *, select_all: bool = False) -> str:
    """Resolve and focus the exact sink used by :func:`type_commands`.

    The server runs this resolver first and only sends trusted CDP text/key
    input after ``found:true``. That split is deliberate: a missing selector
    must never let ``Input.insertText`` fall through to some previously focused
    field in the same tab.
    """
    if not isinstance(selector, str):
        raise InputValidationError("selector must be a string")
    if not isinstance(select_all, bool):
        raise InputValidationError("select_all must be a boolean")
    selector_json = json.dumps(selector)
    select_all_json = json.dumps(select_all)
    return f"""(() => {{
  const selector = {selector_json};
  const selectAll = {select_all_json};
  let el = selector ? document.querySelector(selector) : document.activeElement;
  if (selector && !el) return {{found:false, targetKind:'missing'}};
  const helpers = [...document.querySelectorAll('.xterm-helper-textarea')];
  let xtermRoot = null;
  if (el && el.matches && el.matches('.xterm')) xtermRoot = el;
  else if (el && el.closest) xtermRoot = el.closest('.xterm');
  let helper = el && el.matches && el.matches('.xterm-helper-textarea') ? el : null;
  if (!helper && xtermRoot) helper = xtermRoot.querySelector('.xterm-helper-textarea');
  const textCapable = el && (
    /^(INPUT|TEXTAREA)$/.test(el.tagName || '') || el.isContentEditable
  );
  if (!selector && !textCapable && helpers.length === 1) helper = helpers[0];
  if (helper) el = helper;
  if (!el || el === document.body || el === document.documentElement) {{
    return {{found:false, targetKind:'missing'}};
  }}
  try {{ el.focus({{preventScroll:true}}); }} catch (_) {{ el.focus(); }}
  if (selectAll) {{
    if (typeof el.select === 'function') el.select();
    else if (el.isContentEditable) {{
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }}
  }}
  return {{
    found:true,
    targetKind: helper ? 'xterm' : 'element',
    tagName: el.tagName || '',
  }};
}})()"""


def resolve_selector_script(selector: str, offset_x: float = 0, offset_y: float = 0) -> str:
    """Return a browser-side selector resolver with deterministic JSON quoting."""
    if not isinstance(selector, str) or not selector:
        raise InputValidationError("selector must be a non-empty string")
    offset_x = _number(offset_x, "offset_x")
    offset_y = _number(offset_y, "offset_y")
    return """(() => {
  const selector = %s;
  const offsetX = %s;
  const offsetY = %s;
  const element = document.querySelector(selector);
  if (!element) return {found:false};
  const rect = element.getBoundingClientRect();
  const challengeSelector = '.cf-turnstile, cf-turnstile, iframe[src*="challenges.cloudflare.com"], [src*="challenges.cloudflare.com"]';
  const elementSignal = [element.tagName, element.id, element.className, element.getAttribute('src'), element.getAttribute('name')]
    .filter(Boolean).join(' ').toLowerCase();
  const pageSignal = [document.title, location.hostname, location.href].join(' ').toLowerCase();
  const elementIsChallenge = element.matches(challengeSelector) ||
    elementSignal.includes('cf-turnstile') || elementSignal.includes('challenges.cloudflare.com');
  const pageChallengeElement = document.querySelector(challengeSelector);
  const markerElement = elementIsChallenge ? element : pageChallengeElement;
  const challengeTitle = /cloudflare.*challenge|challenge.*cloudflare|just a moment/.test(document.title.toLowerCase());
  const challenge = elementIsChallenge || pageSignal.includes('challenges.cloudflare.com') ||
    Boolean(pageChallengeElement) || challengeTitle;
  const challengeMarker = challenge
    ? [location.origin, location.pathname, markerElement ? markerElement.tagName : 'page',
      markerElement ? markerElement.id : '', markerElement ? markerElement.className : '',
      markerElement ? markerElement.getAttribute('src') || '' : '',
      markerElement ? markerElement.getAttribute('data-sitekey') || '' : ''].join('|')
    : null;
  return {
    found:true,
    x:rect.left + offsetX,
    y:rect.top + offsetY,
    width:rect.width,
    height:rect.height,
    challengeMarker:challengeMarker
  };
})()""" % (json.dumps(selector), json.dumps(offset_x), json.dumps(offset_y))


@dataclass
class _ChallengeState:
    marker: str
    started_at: float
    count: int


class ChallengeAttemptTracker:
    """Track repeated challenge attempts per CDP session without browser state."""

    def __init__(self, max_attempts: int = 3, window_seconds: float = 120) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise InputValidationError("max_attempts must be a positive integer")
        window_seconds = _number(window_seconds, "window_seconds")
        if window_seconds <= 0:
            raise InputValidationError("window_seconds must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._states: dict[str, _ChallengeState] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, marker: str, *, now: float | None = None) -> bool:
        """Record an attempt and return true once the identical marker stalls."""
        if not isinstance(session_id, str) or not session_id:
            raise InputValidationError("session_id must be a non-empty string")
        if not isinstance(marker, str) or not marker:
            raise InputValidationError("marker must be a non-empty string")
        current = time.monotonic() if now is None else _number(now, "now")
        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.marker != marker or current - state.started_at >= self.window_seconds:
                state = _ChallengeState(marker=marker, started_at=current, count=0)
                self._states[session_id] = state
            state.count += 1
            return state.count >= self.max_attempts

    def clear(self, session_id: str) -> None:
        """Forget all challenge attempts for one session."""
        if not isinstance(session_id, str) or not session_id:
            raise InputValidationError("session_id must be a non-empty string")
        with self._lock:
            self._states.pop(session_id, None)
