---
name: macos-harness
description: Control a whole Mac from one persistent Python session with screenshots, PID-targeted input, an animated virtual pointer, targeted Apple Accessibility, Apple Events, Browser Harness CDP, and filesystem access. Use for native, Electron, browser, dialog, file, or cross-app tasks without moving the physical cursor or forcing apps into the foreground.
---

# macOS Harness

Use one CLI call per decision point, not per primitive:

```bash
macos-harness <<'PY'
app = "Spotify"
mac.see(app)
mac.key("cmd+k", app=app)
mac.type("Alessia Cara", app=app)
print(mac.see(app))
PY
```

The CLI preloads `mac`, `browser`, `Path`, and `subprocess`. Prefer bounded stdin
programs; reserve `macos-harness repl` for manual exploration and always exit it.

## Minimize round trips

- Bundle deterministic, reversible steps into one program, then verify once. Opening
  search, typing a query, and capturing the results is one burst—not three calls.
- Stop at a genuine decision boundary: ambiguous identity, new coordinates, an
  irreversible action, or unexpected state. Inspect once, then run the next burst.
- Do not screenshot merely to confirm that a known shortcut opened a text field
  before typing. Let the final screenshot verify the whole sequence.
- Poll exact AX or Apple Events state inside the same Python program when possible;
  do not make the LLM repeatedly ask whether a transition finished.
- Use the cheapest strong end-state check. Prefer one screenshot for visible state
  or one exact API/AX query for semantic state; use both only when they prove
  different things.

## Use the small surface

Think in six verbs: `see`, `key`, `type`, `click`, `ax`, `script`.

```python
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.ax.perform(item["element_index"], "AXPress")

mac.script('tell application "Spotify" to play')
```

Use ordinary Python for local context and one-off logic. Do not add app-specific
helpers when a short program can resolve the task.

`mac.ax` also covers background AutoFill sheets and system popovers outside
the app you already target:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

`query`, `query_all`, `wait`, `wait_gone`, and `press` accept search text as
the first positional argument. Use `role=` for common targets: `any`, `button`,
`checkbox`, `combo box`, `image`, `link`, `list`, `menu`, `menu item`,
`radio button`, `static text`, `table`, `text area`, and `text field`. An
unknown role raises `MacOSError`. Do not pass both `role` and `search_key`.

Use `apps=` to limit a cross-process search. Pass one app name, bundle ID,
path, or PID, or pass an iterable of selectors. Duplicate PIDs are removed.
`apps="Safari"` is one selector, not an iterable of characters. An empty
iterable raises instead of widening the search.

`query_all` searches every running app or the set named in `apps`. It applies
one positive global `limit`, times out each process, and returns owner metadata.
A broad search skips inaccessible processes. A scoped search reports target
failures. Element handles remain valid until the next AX snapshot or search.

Cross-process calls require non-empty search text. Default attributes exclude
`AXValue`; reading a value requires a separate `ax.get` or explicit attributes.

`wait` accepts exactly one scope: `app`, `all_apps=True`, or `apps`. Zero
matches keep polling. Multiple matches fail closed and report owner, role, and
title details. `wait_gone` requires two consecutive empty polls; a named app
that exits counts as gone. `press` waits for one match, requires `AXPress`, and
returns the match. It never requests activation. If the target makes itself
frontmost, `press` raises `FocusChangedError`; it cannot undo that focus change.

These operations act only on accessible UI that macOS already rendered. They
cannot make secure UI appear in an inactive app or bypass Touch ID, passkeys,
CAPTCHA, account recovery, or another check that requires the user.

## Choose the lowest useful mode

1. When identity depends on local context (`my`, `friend`, or prior activity),
   inspect that context and correlate stable fields; a loose text hit is not enough.
2. Use `mac.script()` for a known exact, focus-safe app command.
3. Otherwise use `mac.see(app)` and vision.
4. Prefer a known keyboard route; use a verified coordinate for a visible,
   low-risk target.
5. Use targeted `mac.ax` only when semantic identity or state matters. Do not dump
   a full AX tree before trying the direct route.

After a failed verified burst, switch mode or stop. Never repair uncertainty with
repeated keys, clicks, deletion loops, or bulk input.

## Keep the invariants

- Input targets an already-running app PID and never requests activation or raise.
- A background target becoming frontmost raises `FocusChangedError`; never
  manipulate focus to restore it.
- `mac.click()` is raw PID-targeted input. It never guesses an AX action.
- The animated pointer is click-through and never moves the physical cursor.
- `mac.move()` moves only that pointer; it cannot produce native hover.
- Inactive apps may reject raw clicks. After one verified failure, switch mode.
- Never launch a closed app or use a custom URL scheme when focus is forbidden.
- `mac.ax.query_all/wait/press/wait_gone` never bypass Touch ID, passkeys,
  CAPTCHA, account recovery, or other checks that need the real user present.
- Screenshot coordinates come from the latest `mac.see()` and preserve window
  bounds and Retina scaling.

Secondary primitives are `mac.move`, `drag`, `scroll`, `show_pointer`, and
`hide_pointer`. `mac.ax.query()` returns compact matches and bounds fallback
traversal; lower `max_nodes` for especially large apps. `mac.ax.query_all()`,
`.wait()`, `.press()`, and `.wait_gone()` extend that traversal across every
running process for background AutoFill and system popovers.

## Browser and permissions

Use `browser` for DOM, tabs, network, downloads, and uploads. Do not substitute AX
for CDP inside a web page. While Browser Harness connects, macOS Harness accepts
Chrome's exact `Allow remote debugging?` sheet through system-wide AX. It never
activates Chrome or emits a mouse event.

Run `macos-harness doctor` to inspect permissions without prompting. Run
`macos-harness doctor --request` only with user approval. Accessibility, screen
recording, and event posting are global; Apple Events Automation is per target.

## Native backend (optional)

`mac.*` stays fully local unless you opt in. `MACOS_HARNESS_BACKEND` (`python`
default, `native`, `auto`) or `MacOS(backend=...)` picks the backend;
`python` never launches an agent, `native` raises immediately if the agent
is unreachable instead of falling back, and `auto` falls back to Python only
when the agent is unreachable before a real `ping` response comes back —
never after a protocol, semantic, permission, timeout, or mutating error.

Native is process isolation, not a speed mode — default `python` remains
the latency-recommended choice. Measured on an M4 Pro: a 50-query Finder
benchmark found a Python median of 1.75 ms against a native steady-state
median of 2.07 ms, plus ~240 ms of native cold-launch cost on first use.
Results are workload-specific; measure locally with `bench/ax_smoke.py`.

Each `MacOS(backend="native"/"auto")` instance that dispatches a routed call
launches its own private `macos-harness-agent` child process over an
inherited, validated UNIX-domain socket pair only that instance holds either
end of — no shared daemon, no well-known socket path, nothing for another
process to discover or connect to. The harness verifies the child's actual
PID from its first `ping` response. Executable resolution order: an
explicit `MACOS_HARNESS_AGENT_BIN` path (must exist and be executable, or
the launch fails immediately with no fallback), then the binary bundled
with the installed package, then a fresh local SwiftPM release build
(requires the Xcode Command Line Tools; rebuilt only when missing or stale,
and only at this tier). Call `mac.close()` when done, or use
`with MacOS(backend=...) as mac:` — both stop the child process and close
the socket; `close()` is idempotent and safe even on a `python`-backend
instance. A closed instance raises `MacOSError` on further native-routed
calls instead of relaunching; construct a new `MacOS()` to use `native`/
`auto` again.

Only `list_apps`, `ax.query`/`ax.query_all`, `ax.press`, and the element
primitives `ax.get`/`ax.get_attributes`/`ax.set`/`ax.perform` can route to
the agent. Everything else — screenshots, keyboard/pointer input, the pointer
overlay, AppleScript, full app snapshots — always stays local. A native
`element_index` is interned into the same handle registry a local query
would use, so it behaves exactly like one: stale or reset indices still
raise instead of aliasing a different element.
