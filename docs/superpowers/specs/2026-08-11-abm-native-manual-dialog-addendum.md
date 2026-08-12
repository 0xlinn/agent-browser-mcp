# ABM Native Manual Dialog Addendum

## Decision

Replace the rejected `Runtime.terminateExecution` design for
`execute_js(dialog_policy="manual")` with the browser's native dialog pause.
Manual means that ABM observes the native dialog, returns structured metadata,
and waits for a later explicit `handle_dialog(accept|dismiss)` call. ABM does
not choose a confirm/prompt value, terminate the evaluation, reload the page,
close the tab, or activate the browser.

## Required behavior

- Manual user JavaScript executes in the normal MAIN/default page context.
- No manual wrapper, saved-native escape hatch, source rewriter, dynamic-code
  denylist, or `Runtime.terminateExecution` is used.
- Attach the debugger and enable `Page`/`Runtime` before evaluation.
- Race evaluation settlement against the matching tab's
  `Page.javascriptDialogOpening` event.
- If evaluation settles first, return the normal execute result and release the
  debugger lease.
- If a native alert/confirm/prompt opens first, return
  `status="blocked_by_dialog"`, `handled=false`, dialog metadata, and
  `pending_execution=true`. Leave the native dialog open and keep the pending
  evaluation/lease owned by that tab.
- `handle_dialog(accept|dismiss)` performs the explicit
  `Page.handleJavaScriptDialog` action. The pending evaluation then settles and
  releases its debugger lease in `finally`.
- A second manual execution on the same tab returns `busy`; it must not attach
  again or disturb the first execution. Other tabs remain independent.
- Wrong-tab, stale, or expired dialog events do not resolve a pending call.
- Tab removal, navigation replacement, debugger detach, or evaluation failure
  clears pending state and releases only the owning lease.
- `beforeunload="manual"` follows the same native-dialog principle.

## Safety and compatibility

- Preserve full composite session IDs and hard-fail directed dead sessions.
- Preserve normal MAIN-world page globals, computed eval, Function
  constructors, division, regex, templates, and async JavaScript semantics.
- Do not call physical input, activate a window, launch a browser, reload a
  page, or use `chrome.runtime.reload()`.
- The explicitly requested manual policy may leave that tab blocked until the
  operator or agent calls `handle_dialog`; unrelated tabs and desktop input
  remain unaffected.

## Verification

- Mocked-CDP tests must execute the dialog-first and evaluation-first races.
- Assert dialog-first returns before evaluation settles, performs no automatic
  handle/terminate action, and retains exactly one lease.
- Assert explicit accept/dismiss settles the pending evaluation and releases
  the lease.
- Assert wrong-tab events are ignored and same-tab concurrent manual calls are
  `busy`.
- Keep the existing token isolation, navigation failure, directed-session, and
  non-live regression suites green.
