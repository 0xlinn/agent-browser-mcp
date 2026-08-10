# ABM Background-Safe Input, Dialog, and Permission Design

Date: 2026-08-10
Status: Approved in conversation
Upstream baseline: `https://github.com/335234131/agent-browser-mcp`

## Context

ABM exists to control the operator's already-running daily Chrome, Edge, or
Opera profile. That real browser state is the product advantage: existing
cookies, login state, extensions, profile history, and browser environment stay
intact. The implementation must not silently replace that browser with a
Playwright-launched, headless, or automation-only profile.

The current protocol and DOM tools can already work against a selected tab in
the background. The interference comes from two separate behaviors:

1. `switch_tab` activates the selected tab by default.
2. `mouse_move`, `mouse_click`, `mouse_drag`, `type_text`, and `hotkey` use
   process-global OS input through `pyautogui`.

The project also treats dialogs unevenly. The injected wrapper temporarily
suppresses `alert`, `confirm`, and `prompt`, but native `beforeunload` dialogs,
site permission prompts, and browser or OS UI need distinct policies.

## Goals

- Keep all ordinary ABM work on the operator's real, already-running browser.
- Let tab-scoped page work run without changing the visible tab, window focus,
  mouse position, or keyboard focus.
- Make physical input an exact, one-call capability that requires explicit
  user approval and never waits to steal input later.
- Handle JavaScript dialogs and `beforeunload` through the browser protocol.
- Scope site permission changes to one origin and restore them automatically.
- Allow Turnstile and similar challenge widgets to be clicked in the real
  browser, with bounded retries and physical input only after approval.
- Return structured, actionable states for expected interruptions.
- Preserve the existing bridge, extension, session IDs, login state, and
  backward-compatible tool names wherever their old behavior is not unsafe.

## Non-Goals

- Launching or managing a Playwright browser, headless browser, or separate
  automation profile.
- Automatically granting permanent browser permissions.
- Automating arbitrary browser chrome or operating-system dialogs without
  approval.
- Coordinating with unrelated automation programs that do not participate in
  ABM's lock. ABM can detect recent input and avoid collisions, but it cannot
  reserve the desktop globally against uncooperative software.
- Building a general CAPTCHA solver. ABM may interact with a challenge in the
  real browser and report progress, but it must stop bounded retry loops.

## Design Principles

1. A selected tab is not necessarily a foreground tab.
2. Page input and desktop input are different capabilities and use different
   tools and authorization rules.
3. Every state-changing page operation is bound to an explicit full
   `session_id` for the duration of the call and restores the shared default.
4. Expected user-interaction states are structured results, not opaque
   exceptions.
5. Broad operator authority does not imply broad default authority. Permission
   is granted for the smallest action, origin, and duration that can work.

## Architecture

### 1. Real Browser Session Layer

The existing extension, WebSocket bridge, `TMWebDriver`, and composite session
IDs remain the control plane. ABM never launches another browser as a fallback.

`switch_tab` changes its default to `activate=false`. It retargets the bridge
without changing the visible tab. `activate_tab` remains the explicit operation
for bringing a tab and its window to the foreground.

All new page-input tools require or resolve a full `session_id`, save the
driver's previous default, operate on the requested tab, and restore the
previous default in `finally`. A directed call must not leave a shared default
pointing at another task's tab.

### 2. Background Page Input Layer

Add tab-scoped input tools implemented with CDP `Input` commands. These commands
target the requested real tab and do not move the OS cursor or focus another
application:

- `page_click`: click by CSS selector or viewport coordinates. A selector may
  include an offset so callers can click a location inside a cross-origin iframe
  rectangle without accessing the iframe DOM.
- `page_type`: focus an optional selector, optionally clear it, insert text,
  and optionally submit with a key.
- `page_press`: dispatch a key or supported modifier chord to the selected tab.
- `page_drag`: dispatch a bounded sequence of mouse move, press, move, and
  release events inside the selected tab.

Selector and coordinate modes are mutually exclusive. Coordinates are viewport
coordinates, never desktop coordinates. Responses include the resolved target,
`session_id`, `input_mode="cdp"`, and `foreground_changed=false`.

The existing `execute_js` remains the preferred tool for straightforward DOM
operations. The page-input tools cover cases where a trusted browser input
event, cross-origin frame hit target, or realistic event sequence is required.

### 3. Physical Input Gateway

All five `pyautogui` tools call one shared gateway before importing or invoking
`pyautogui`.

The gateway performs these checks in order:

1. Describe the exact action, coordinates or target tab, and expected duration
   through MCP form elicitation.
2. Continue only when the client returns `accept`. `decline` and `cancel`
   return `requires_user_action` without side effects.
3. If the client did not advertise elicitation or rejects the request method,
   return `requires_user_action`; do not silently downgrade to automatic input.
4. Acquire `~/.agent-browser-mcp/physical-input.lock` atomically. The lock is
   per action, records PID and expiry metadata, rejects contention immediately
   with `busy`, and removes only its own lock in `finally`. It does not queue,
   because delayed acquisition could steal input after the user has moved on.
5. After approval, observe a short quiet window. On Windows use
   `GetLastInputInfo`; also sample pointer position. New activity returns
   `input_activity_detected` and releases the lock without acting.
6. Execute exactly one tool call, then release the lock. Approval cannot be
   reused by another action or MCP process.

`pointer_info` and desktop capture do not move input and remain read-only.
Desktop capture continues to carry an explicit privacy warning in its tool
description and skill routing.

### 4. Dialog Controller

The extension adds per-tab, bounded dialog state. It listens for
`Page.javascriptDialogOpening` while a guarded action is attached and records
the dialog type, message, URL, and default prompt text. It never keeps the
debugger attached between unrelated operations.

Add `handle_dialog(action, prompt_text, session_id)` using
`Page.handleJavaScriptDialog`. It supports `alert`, `confirm`, `prompt`, and
`beforeunload`.

Policies are:

- Ordinary `alert`: acknowledge and report it.
- `confirm` and `prompt`: dismiss by default and report the captured dialog;
  acceptance must be explicit.
- `beforeunload`: dismiss by default, leaving the page in place and returning
  `blocked_by_beforeunload`. `open_url(..., beforeunload="accept")` may leave
  only when the caller explicitly selected that policy.
- A caller may select manual handling. The dialog remains open and the result is
  `blocked_by_dialog` until `handle_dialog` or the user resolves it.

The injected `disable_dialogs.js` no longer silently returns `true` from every
automation-time `confirm`. It receives the per-call policy and reports every
suppressed dialog back to the extension so the MCP result remains truthful.

### 5. Site Permission Controller

Add origin-scoped permission tools backed by `chrome.contentSettings` where the
Chrome extension API supports the requested permission. The first supported set
is notifications, geolocation, camera, and microphone. Clipboard permission is
feature-probed through CDP; if the running Chrome and extension debugger path do
not support it, the result is `unsupported` rather than a physical click.

Any `allow` request uses MCP elicitation and states the permission, exact origin,
and duration. The extension stores the previous setting in
`chrome.storage.local`, applies the temporary setting, and restores it with a
persisted alarm. Because MV3 alarms have a one-minute floor, supported durations
are 60 to 600 seconds. Explicit reset and startup recovery restore expired
records after service-worker suspension or browser restart.

Enterprise-controlled settings and operating-system permission dialogs return
`requires_user_action` or `unsupported`; they do not trigger physical input
automatically.

### 6. Challenge Handling

Turnstile and similar browser challenges stay in the same real daily-browser
tab. Their presence does not cause ABM to launch a different browser.

ABM may click a challenge with `page_click`, including selector-relative iframe
coordinates. This is the first attempt because it does not occupy the desktop.
If the challenge requires foreground or OS-level interaction, the caller may
invoke the physical tool, which follows the one-action elicitation and lock
flow above.

Challenge state is checked between attempts. Repeated challenge reloads or an
unchanged challenge after a bounded number of attempts return
`challenge_stalled`. ABM must not enter an infinite click, reload, or polling
loop. The user can then take over the same real tab and the agent can resume
after verification completes.

## Tool Contract Changes

### Changed tools

- `switch_tab(..., activate: bool = False)`
- `open_url(..., beforeunload: "dismiss" | "accept" | "manual" = "dismiss")`
- `execute_js(..., dialog_policy: "dismiss" | "accept" | "manual" = "dismiss")`
- `mouse_move`, `mouse_click`, `mouse_drag`, `type_text`, and `hotkey` request
  one-action elicitation before physical input.

### New tools

- `page_click`
- `page_type`
- `page_press`
- `page_drag`
- `handle_dialog`
- `set_site_permission`
- `reset_site_permissions`

Tool descriptions must state whether coordinates are viewport or desktop
coordinates, whether a call can focus the browser, whether approval is
required, and which `session_id` is affected.

## Structured Results

Expected control states return a dictionary with `status`, `session_id`,
`reason`, and a concrete next action where applicable:

- `ok`: action completed and was verified as far as the protocol allows.
- `blocked_by_dialog`: a JavaScript dialog remains open.
- `blocked_by_beforeunload`: navigation was cancelled to preserve the page.
- `requires_user_action`: approval is unavailable, declined, or a native UI
  cannot be controlled safely.
- `busy`: another ABM process owns the physical-input lock.
- `input_activity_detected`: input changed during the post-approval quiet
  window, so no physical action was sent.
- `unsupported`: the browser or extension API cannot provide the requested
  capability.
- `challenge_stalled`: a browser challenge made no bounded progress.

Bridge disconnects, invalid protocol replies, invalid parameters, and explicit
dead session IDs remain exceptions with actionable messages. A directed dead
session must never fall through to another live tab.

## Concurrency and Recovery

- Background page operations are isolated by explicit session IDs and may run
  concurrently on different tabs.
- The shared driver default is restored after directed calls.
- Physical input is serialized across ABM MCP processes and is never queued.
- Dialog records are bounded and discarded on tab close, debugger detach,
  extension reload, or expiry.
- Permission restoration data survives service-worker suspension.
- Stale physical-input locks are recoverable only after their PID is dead or
  their hard expiry has elapsed.

## Files and Boundaries

- `server.py`: MCP contracts and orchestration only.
- New `page_input.py`: validation and CDP input payload construction.
- New `physical_input.py`: elicitation, lock lifecycle, quiet-window detection,
  and `pyautogui` gateway.
- `chrome_extension/background.js`: guarded CDP action, dialog state, and site
  permission commands.
- `chrome_extension/disable_dialogs.js`: per-call dialog policy and reporting.
- `chrome_extension/manifest.json`: add only the permission required for
  origin-scoped content settings.
- `tests/`: offline contract, concurrency, policy, payload, and live-browser
  regression coverage.
- `README.md` and `README.zh-CN.md`: defaults, new tools, status semantics, and
  real-browser invariant.
- `browser-mcp-default` and `abm-bridge-recovery` skills: background-first
  routing, physical-input approval, dialog flow, challenge bounds, and stale
  extension recovery.

The existing large `server.py` should not absorb lock or CDP payload internals;
the two focused modules keep the behavioral boundaries testable without an
unrelated refactor.

## Test Plan

### Offline tests

- Tool schemas and the new `switch_tab` default.
- Directed calls restore the previous default session.
- Page input produces correct CDP event sequences without calling `_activate`
  or `pyautogui`.
- Selector and coordinate validation rejects ambiguous calls.
- Elicitation accept, decline, cancel, and unsupported-client paths.
- Lock contention, stale-lock recovery, release on exception, and no queuing.
- Quiet-window activity aborts before importing or invoking `pyautogui`.
- Dialog policy mapping and structured result classification.
- Permission origin validation, TTL bounds, and restoration records.
- Challenge attempt limits and `challenge_stalled` classification.

### Extension checks

- `node --check` for extension JavaScript.
- Static manifest and command-routing assertions.
- Mocked `chrome.debugger`, `chrome.contentSettings`, storage, and alarm behavior
  where practical without loading the real extension.

### Live browser tests

- Operate only on the existing scratch tab and restore the previously active
  tab after the suite.
- Select and operate on a background tab while asserting the visible active tab
  did not change.
- CDP click, type, key, and drag produce page-observable events.
- `alert`, `confirm`, `prompt`, and `beforeunload` follow their selected policy.
- A directed dead session does not switch to another browser or tab.
- Physical-input execution is not exercised without real user elicitation;
  only its rejection path is tested live.
- Permission tests use a local test origin and restore the original setting.

### Documentation and Skill checks

- Compare registered MCP tools with both README tool lists.
- Validate each changed skill with the local skill validator.
- Verify the Codex/agents junction and the independent Claude copy have matching
  content hashes.
- Start a fresh MCP session or reload the server so changed tool schemas and
  descriptions are actually visible to clients.

## Acceptance Criteria

1. An ordinary ABM workflow against a background real-browser tab does not
   change the foreground tab, focused window, mouse position, or keyboard focus.
2. No physical-input tool acts without an accepted elicitation for that exact
   call, an acquired lock, and a quiet input window.
3. Lock contention and new input return promptly without delayed execution.
4. `beforeunload` preserves the page by default and can be explicitly accepted.
5. Dialogs and supported permissions have explicit, origin- or action-scoped
   policies and truthful structured results.
6. Turnstile remains in the same real browser and can be clicked by page input
   or approved physical input without an unbounded retry loop.
7. Offline tests, extension syntax checks, relevant live tests, README parity,
   and skill validation pass.

