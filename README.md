<img src="https://raw.githubusercontent.com/browser-use/macos-harness/main/static/banner-ink.svg" alt="macOS Harness" width="100%" />

# macOS Harness ⌘

Give any coding agent the apps on your Mac. One shell command per step. It works in
the background: your pointer does not move and your front app stays in front.

```text
● agent: needs something only an app's UI can do
│
● macos-harness get_app_state --app "TextEdit"       the window as numbered text
│
● macos-harness click --app "TextEdit" --element_index 12
│
● macos-harness get_app_state --app "TextEdit"       only what changed
│
✓ task complete                                      you kept working the whole time
```

**Your agent now has a Mac.**

## Give it to your agent

Paste this into Claude Code, Codex, Pi or OpenCode:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Then run `macos-harness install --agent NAME`, where NAME is the agent CLI you are (claude, codex, pi or opencode), and `macos-harness doctor`. Explain any missing macOS permission and ask before requesting it. Finally read one app that is already running, without bringing it to the front: `macos-harness get_app_state --app Finder`.
```

That is it. The agent installs the package, gets the skill, checks permissions and
reads its first app. [Manual setup](install.md) is available too. To go back to an
older release, run `macos-harness uninstall --agent all` first.

## Eleven tools. Any app.

```bash
macos-harness get_app_state --app "TextEdit"
macos-harness click --app "TextEdit" --element_index 12
macos-harness type_text --app "TextEdit" --text "hello world"
macos-harness press_key --app "TextEdit" --key "super+s"
macos-harness set_value --app "TextEdit" --element_index 7 --value "report.txt"
macos-harness scroll --app "TextEdit" --element_index 3 --direction down --pages 1
macos-harness get_app_state --app "TextEdit"
```

Read, act, read again. `get_app_state` prints one line per thing on screen, each with
a number, and the path of a screenshot. Every other tool takes that number, or a pixel
of that screenshot. The rest of the eleven: `list_apps`, `drag`, `paste`,
`select_text` and `perform_secondary_action`.

There are no Spotify tools, Slack tools or Final Cut tools. The model gets the same
eleven commands for every app. The [skill](skills/macos-harness/SKILL.md) is one page,
and it is all an agent needs to read.

## How it works

```text
   agent's shell                      helper (starts on the first call)
        │                                         │
  macos-harness click ...  ── local socket ──▶  numbers, last state, sessions
        │                                         │
   text on stdout          ◀──────────────  accessibility tree + window screenshot
                                                  │
                                    accessibility actions first, else events
                                    sent to the app's own process
```

- Reads a window as text, and on the next read prints only what changed
- Acts through accessibility actions first, then through events sent to the app's own
  process, so the real pointer never moves and your front app stays in front. Three
  exceptions, all on purpose: the action `Raise`, which the model asks for by name;
  System Settings, opened when a permission is missing; and a menu-bar menu an app
  fills only while it is open, which is opened for at most a quarter of a second
- Draws a pointer of its own, so you can see where the agent acts
- Waits by itself until the app has settled before it reads again
- Asks nothing by default: the agent drives any app except its own host and the
  system's password sheets. To be asked before an agent's first use of each app, put
  `ask_before_first_use = true` in `policy.toml` in the state folder; `strict = true`
  forbids terminal apps too
- Notices when you touch an app the agent is using
- Stops when its agent is interrupted: Esc or Ctrl+C in the agent's own app ends the
  running action at its next key or click. There is no stop button of its own: an
  agent nobody can interrupt (a headless or scheduled run) is stopped by ending its
  process; `macos-harness helper stop` also ends the action that is running
- Limits worth knowing: pages on the `blocked_urls` list of a policy file are noticed
  when the page is read and before the action after one that may have changed it, not
  while a page is loading; apps built on Electron or a plain web view give no URL to
  check; one call clicks at most 10 times and types at most 20,000 characters

## Permissions and privacy

`macos-harness doctor` reports the macOS permissions: Accessibility and Screen
Recording, which every call needs, and two optional ones, for the app that starts the
harness.

While `paste` runs, what was on your clipboard is kept in a file only you can read
(in the state folder) for about four seconds, so that it can be put back, also after
a crash.

Anonymous telemetry is enabled by default. It records only the CLI command
category, success, duration, package version, OS/architecture, and detected agent
client. It never records prompts, app names, screenshots, UI text, scripts, paths,
or window titles.

```bash
macos-harness telemetry disable
```

Experimental. macOS only. [MIT licensed](LICENSE).
