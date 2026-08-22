<img src="https://raw.githubusercontent.com/browser-use/macos-harness/main/static/banner-ink.svg" alt="macOS Harness" width="100%" />

# macOS Harness ⌘

The simplest, thinnest harness that gives an LLM complete freedom to complete
virtually any task on a Mac.

The agent writes what is missing, mid-task. No framework, no recipes, no rails.
One Python process connected directly to macOS, your real browser, and your files.

```text
● agent: wants to do something no helper exists for
│
● sees the app and uses raw macOS primitives
│
● writes the missing logic in ordinary Python
│
✓ task complete                                  no app-specific tool added
```

**Your agent now has a Mac.**

## Give it to your agent

Paste this into Codex or Claude Code:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Register the skill printed by `macos-harness skill`, then run `macos-harness doctor`. Explain any missing macOS permissions and ask before requesting them. Finally, verify the harness by capturing one already-running app without bringing it to the foreground.
```

That is it. The agent installs the package, teaches itself the workflow, checks
permissions, and verifies the connection. [Manual setup](install.md) is available too.

## Machine-first: `mac.do` for mutations, raw primitives underneath

Prefer `mac.do` for anything that changes state. It runs the same primitives
underneath, but returns a `Receipt` instead of a bare value, so you can tell
what actually happened instead of trusting it blindly:

```bash
macos-harness <<'PY'
from macos_harness.receipts import OperationError, gone

not_now = dict(text="Not Now", role="button", all_apps=True)
postcondition = gone(**not_now)

receipt = mac.do.press(**not_now, postcondition=postcondition, once="dismiss-not-now")
print(receipt.outcome, receipt.acted, receipt.verified)  # done yes True

# A repeat call with the same `once` token *and* the same request (reusing
# `not_now`/`postcondition` guarantees that here) replays the recorded
# receipt instead of risking a second press.
replay = mac.do.press(**not_now, postcondition=postcondition, once="dismiss-not-now")
assert replay.replayed

# A dry run validates and resolves without ever dispatching anything or
# touching the once-token ledger.
plan = mac.do.press(**not_now, dry_run=True)
assert plan.outcome == "planned" and plan.acted == "no"

# A failed operation raises OperationError, never a bare exception --
# exc.receipt is the exact same Receipt a success would have returned.
try:
    mac.do.press(text="Does Not Exist", role="button", all_apps=True, timeout=1)
except OperationError as exc:
    print(exc.receipt.outcome, exc.receipt.error["code"])  # failed timeout
PY
```

- `press`, `set`, `toggle`, `run`, and `key` mutate; `recall(once)` looks up
  a past receipt by its token, without dispatching anything. `set`/`toggle`
  are convergent: they read the current state first and report
  `outcome="already"` instead of touching anything already correct.
- Every call returns an immutable, JSON-safe `Receipt` on success, or
  raises `OperationError` on failure — `exc.receipt` is the exact same
  `Receipt` a success would have had, so you never have to choose between
  catching the exception and reading the structured detail: `outcome`
  (`planned`, `done`, `already`, `failed`), `acted` (`no`, `yes`,
  `unknown`), the backend and executor that actually ran it, the
  normalized request and target, whether anything changed, whether a
  postcondition verified the effect, duration_s, and — on failure — a
  structured error. `receipt.to_json()` is ready for `json.dumps`.
- `changed` is only ever an observed fact, never a guess. `set`/`toggle`
  read the target back before and after and know for certain; `press`/
  `run`/`key` have no readback of their own, so `changed` is `None` unless
  you pass a `postcondition` that confirms the effect actually took hold.
- A bad argument — an unknown role, a malformed postcondition, reusing a
  `once` token for a genuinely different request — raises `MacOSError`
  directly, before anything is dispatched: there is no receipt, because
  nothing was ever attempted. Once a mutating call actually begins, every
  failure comes back as a `Receipt`, raised inside `OperationError`.
- `present(...)`/`gone(...)` (the same call shape as `mac.ax.wait`/
  `mac.ax.wait_gone`) verify an operation's real effect; left unscoped, a
  postcondition inherits the operation's own `app`/`apps`/`all_apps` scope
  — except for `run`, which has no scope of its own, so its postcondition
  must set `app=`, `all_apps=True`, or `apps=` explicitly. `press`/`set`/
  `toggle` accept an `interval` for their own AX resolution polling;
  `run` and `key` poll for nothing of their own, so neither takes one.
- `timeout` is one cooperative budget across resolution, dispatch, and
  verification. The harness does not start a mutation after the budget
  expires and it terminates a timed-out script process group. A synchronous
  macOS Accessibility or input call that is already in progress cannot be
  preempted safely and can return after the budget.
- `press`, `run`, and `key` accept a nonempty `once` keyword for
  at-most-once dispatch. The ledger lives only in the memory of the one
  live `MacOS` instance that dispatched the call — a crash, a fresh
  `MacOS()`, or a new process all start with an empty ledger — and it
  keeps every finished receipt only for as long as that instance does,
  never on disk and never shared with another process. A repeat call with
  the same token *and* the same request replays the recorded receipt
  instead of dispatching again; an interrupted or still in-flight attempt
  fails closed with a failed, `acted="unknown"` receipt rather than
  risking a second dispatch. `set`/`toggle` never need one -- reading
  before mutating already makes them safe to repeat.
- A `Receipt` stays safe to keep or log by default. Script source and
  arguments, `set`/`toggle` values, and script output use length and SHA-256
  metadata instead of raw values. Pass `capture_output=True` only when you
  need bounded stdout/stderr text and accept that it can contain sensitive
  data.

When no `mac.do` verb fits, drop to the six raw primitives below — an escape
hatch, not a deprecated path: unchanged, fully supported, just without a
receipt or an idempotency token.

## Six primitives. The whole Mac.

```bash
macos-harness <<'PY'
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.script('tell application "Spotify" to play')

print(browser.page_info())
print(list(Path.home().iterdir()))
PY
```

Think in `see`, `key`, `type`, `click`, `ax`, and `script`. `browser`, `Path`, and
`subprocess` are ready in the same Python process.

There are no Spotify tools, Slack tools, or Final Cut tools. The model gets raw
primitives and writes the rest.

## Background AutoFill and system popovers

`mac.ax` can search and act on running processes without requesting activation.
Use one call when the owning process is unknown:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

`query`, `query_all`, `wait`, `wait_gone`, and `press` accept `text` as the
first positional argument. Use `role=` instead of a raw `search_key` for common
targets: `any`, `button`, `checkbox`, `combo box`, `image`, `link`, `list`,
`menu`, `menu item`, `radio button`, `static text`, `table`, `text area`, and
`text field`. An unknown role raises `MacOSError`. Do not pass both `role` and
`search_key`.

Use `apps=` to limit a cross-process search. Pass one app selector or an
iterable of selectors. Each selector can be an app name, bundle ID, path, or
PID. Duplicate PIDs are removed.

```python
mac.ax.wait("Not Now", role="button", apps="Spotify")
mac.ax.wait_gone("Not Now", role="button", apps=["Spotify"])
```

`query_all` searches every running app, or only the apps named in `apps`. It
uses one positive global `limit`, applies a timeout to each process, and adds
owner metadata (`name`, `bundle_id`, `pid`, `path`) to every match. A broad
search skips inaccessible processes. A scoped `apps` search reports a target
failure. Element handles remain valid until the next AX snapshot or search.

Cross-process calls require non-empty search text. Default result attributes
exclude `AXValue`. Reading a value remains a separate, explicit `ax.get` call
or custom `attributes` choice.

`wait` polls one `app`, every app with `all_apps=True`, or the target set in
`apps`. Pass exactly one scope. Zero matches keep polling until `timeout`.
Multiple matches fail closed with owner, role, and title details.

`wait_gone` requires two consecutive empty polls. A named app that exits counts
as gone. `press` waits for one match, requires `AXPress`, performs it, and
returns the match. The harness never calls an activate or raise API. If the
target makes itself frontmost, `press` detects that change and raises
`FocusChangedError`. It cannot undo a focus change initiated by the target.

These calls act only on accessible UI that macOS already rendered. They cannot
make secure UI appear in an inactive app. They cannot bypass Touch ID, passkeys,
CAPTCHA, account recovery, or another check that requires the user.

## Native backend

`mac.*` runs entirely in Python by default. A separate, optional Swift agent
process can take over a fixed set of Accessibility calls when you want it;
nothing about the primitives above changes when it does.

Native is a process-isolation option, not a speed mode — default `python`
remains the recommended choice for latency. On an M4 Pro, a 50-query Finder
benchmark measured a Python median of 1.75 ms against a native steady-state
median of 2.07 ms, plus about 240 ms of native cold-launch cost on first use.
Results are workload-specific; measure your own with `bench/ax_smoke.py`.

```bash
export MACOS_HARNESS_BACKEND=native   # python (default) | native | auto
```

`MACOS_HARNESS_BACKEND` selects the backend for every `MacOS()` instance the
CLI creates; construct `MacOS(backend="python" | "native" | "auto")` directly
for the same choice in your own code. The default stays `python`: nothing
launches an agent process unless you opt in.

- **`python`** (default) — every call runs in-process, as it always has. The
  harness never launches or talks to an agent.
- **`native`** — every routed call goes to the agent. If the agent is
  unreachable, the call raises immediately; there is no silent fallback.
- **`auto`** — routed calls prefer the agent and fall back to the Python path
  only when the agent is unreachable *before* a real response arrives: a
  failed spawn or build, or an EOF or timeout ahead of the handshake. Once a
  genuine `ping` response has come back, `auto` never falls back: a protocol
  error, a semantic error (bad request, unknown element, timeout), a
  permission failure, or a mutating call that may have already taken effect
  all surface as errors instead of silently retrying in Python.

Each `MacOS(backend="native" | "auto")` instance that actually dispatches a
routed call launches its own private `macos-harness-agent` child process,
connected over an inherited, validated UNIX-domain socket pair that only
that Python process holds either end of. There is no shared daemon, no
well-known socket path, and no pidfile — nothing for another process on
your machine to discover or connect to. The harness confirms the child's
actual PID in its first `ping` response before routing any call to it.

```python
mac = MacOS(backend="native")
try:
    ...
finally:
    mac.close()

# or, equivalently:
with MacOS(backend="native") as mac:
    ...
```

`close()` stops the child process and closes the socket. It is idempotent
and safe to call even on a `python`-backend instance that never launched an
agent. An instance left open at interpreter exit, or dropped without
`close()`, still cleans up its child process through a finalizer, but do not
rely on that for anything time-sensitive — call `close()` yourself. Once
closed, further native-routed calls on that instance raise `MacOSError`
instead of relaunching or falling back; construct a new `MacOS()` to use
`native`/`auto` again.

The harness resolves which executable to launch, in order: an explicit
`MACOS_HARNESS_AGENT_BIN` path (must already exist and be executable, or the
launch fails immediately with no fallback to the tiers below), then the
binary bundled inside the installed package, then a fresh local SwiftPM
release build from `native/macos-harness-agent/` — rebuilt only when
missing or stale against that package's own sources, and only at this third
tier; the first two never trigger a rebuild. PyPI wheels ship a universal2
(arm64 + x86_64) build of the agent already, so most installs never reach
the third tier; building it yourself requires the Xcode Command Line Tools.

Only a fixed, narrow set of calls ever cross the socket: `list_apps`, the
bounded `ax.query`/`ax.query_all` search, `ax.press`, and the element
primitives `ax.get`/`ax.get_attributes`, `ax.set`, and `ax.perform` once you
already hold an `element_index`. Screenshots, keyboard and pointer input, the
animated pointer overlay, AppleScript, full app-state snapshots, and any
unrouted or parameterized AX call stay local to the Python process on every
backend. An `element_index` returned by a native query is not a raw
agent-side number — the client interns it into the same monotonic element
registry local queries use, so `ax.get`/`ax.set`/`ax.perform` accept it
exactly like a Python-minted index, and a stale index still raises instead of
silently aliasing a different element.

## How it works

```text
                              one persistent Python process
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
                 mac.*                  browser.*          Path / subprocess
                    │                      │                      │
        ┌───────────┼───────────┐     Browser Harness        files + shell
        │           │           │            │
    CGWindow     CGEvent      AX + Apple      CDP
   screenshots    to PID       Events          │
        │           │           │          real Chrome
        └───────────┴───────────┘
                    │
              native + Electron apps
```

- Captures background app windows without bringing them forward
- Sends keyboard and coordinate input directly to an app PID
- Draws an animated, click-through pointer without moving your real cursor
- Exposes raw Apple Accessibility and Apple Events when vision is not enough
- Uses Browser Harness for the real, logged-in browser
- Keeps ordinary Python and the local filesystem within reach
- Wraps mutations in `mac.do` for a receipt, optional verification, and
  idempotency — see [Machine-first](#machine-first-macdo-for-mutations-raw-primitives-underneath)
- Can hand a fixed set of Accessibility calls to a supervised native agent
  over a local socket; off by default, see [Native backend](#native-backend)

## Permissions and privacy

`macos-harness doctor` reports the macOS permissions actually needed. The harness
never activates or raises a target app and never moves the physical pointer.

Telemetry is off by default. Nothing is sent until you run `macos-harness
telemetry enable`, and a kill switch always wins even after that:
`DO_NOT_TRACK=1`, `MACOS_HARNESS_TELEMETRY=0`, or `ANONYMIZED_TELEMETRY=0`
disables it regardless of the stored setting, and no environment variable can
force it back on. Once enabled, each CLI invocation sends one event carrying
a persistent random install ID (generated locally, not derived from any
hardware or user identifier) plus the command category, success, duration,
package version, Python `major.minor`, OS, CPU architecture, and detected
coding-agent client (Codex, Claude Code, Cursor, Gemini CLI, or opencode). It
never carries prompts, app names, screenshots, UI text, scripts, paths, or
window titles. Config lives at `$MACOS_HARNESS_HOME/telemetry.json` (default
`~/Library/Application Support/macos-harness/telemetry.json`, mode `0600`);
events go to PostHog EU (`https://eu.i.posthog.com`), and an endpoint
override is honored only when it is HTTPS.

```bash
macos-harness telemetry enable    # opt in
macos-harness telemetry status    # see exactly what would be sent, and where
macos-harness telemetry disable   # opt back out
```

Experimental. Requires macOS 13+ and Python 3.11 or newer (tested
3.11-3.14). [MIT licensed](LICENSE).
