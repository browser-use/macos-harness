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

## Permissions and privacy

`macos-harness doctor` reports the macOS permissions actually needed. The harness
never activates or raises a target app and never moves the physical pointer.

Anonymous telemetry is enabled by default. It records only the CLI command
category, success, duration, package version, OS/architecture, and detected agent
client. It never records prompts, app names, screenshots, UI text, scripts, paths,
or window titles.

```bash
macos-harness telemetry disable
```

Experimental. macOS only. [MIT licensed](LICENSE).
