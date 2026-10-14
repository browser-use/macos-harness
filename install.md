# Install macOS Harness

## Let your agent do it

Paste this into Claude Code, Codex, Pi or OpenCode:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Then run `macos-harness install --agent NAME`, where NAME is the agent CLI you are (claude, codex, pi or opencode), and `macos-harness doctor`. Explain any missing macOS permission and ask before requesting it. Finally read one app that is already running, without bringing it to the front: `macos-harness get_app_state --app Finder`.
```

## Or install it yourself

```bash
uv tool install --python 3.12 --upgrade --force macos-harness
macos-harness install --agent claude
macos-harness doctor
```

`install` writes the skill, and hooks that tell the harness when a turn of the agent
starts and ends. Leave `--agent` out to install for all four. `--scope user` (the
default) is for every project, `--scope project` is for the folder you are in.

| Flag | Skill, project scope | Skill, user scope | Turn hooks |
|---|---|---|---|
| `--agent claude` | `.claude/skills` | `~/.claude/skills` | `UserPromptSubmit`, `Stop` and `SessionEnd` in `~/.claude/settings.json` (project scope: `.claude/settings.local.json`) |
| `--agent codex` | `.agents/skills` | `${CODEX_HOME:-~/.codex}/skills` | `UserPromptSubmit`, `Stop`, `Interrupt` and `SessionEnd` in `hooks.json` (project scope: `.codex/hooks.json`). Codex runs a new hook only after you trust it: open `/hooks` once |
| `--agent pi` | `.agents/skills` | `~/.agents/skills` | the extension `~/.pi/agent/extensions/macos-harness.ts` (project scope: `.pi/extensions`). A headless run loads project skills only with `--approve` |
| `--agent opencode` | `.opencode/skills` | `~/.config/opencode/skills` | the plugin `~/.config/opencode/plugins/macos-harness.js` (project scope: `.opencode/plugins`), on `session.idle` |

Your own settings in those files stay as they are. To take everything out again, run
`uninstall` with the same flags:

```bash
macos-harness uninstall --agent claude
```

The Pi and OpenCode plugin files name the install that wrote them, so run `uninstall`
with the same install you installed from.

Going back to an older release: run `macos-harness uninstall --agent all` first, while
this release is installed. Releases before 0.2.0 have no `uninstall`, and their command
does not know the skill or the hooks this one wrote.

Any other agent: put the text that `macos-harness skill` prints into a `SKILL.md` where
that agent reads skills. Without hooks everything still works. The harness then forgets
an agent's app sessions when the agent exits or after 15 idle minutes, not at the end of
each turn.

## Permissions

```bash
macos-harness doctor
```

Every call needs Accessibility and Screen Recording; `doctor` also reports two
optional grants (sending events, and Input Monitoring, which lets the agent step back
when you use an app). Grant only what it reports as missing. macOS gives them to the
app that starts the harness (your terminal, or the agent's desktop app), and `doctor`
names that app.

## You stay in charge

- By default nothing asks you first: the agent drives any app except its own host
  and the system's password sheets. With `ask_before_first_use = true` in `policy.toml`
  in the state folder, the first time an agent uses an app a small panel asks you:
  deny, allow for this session, or always allow. Then, for a run with nobody at the
  Mac, approve ahead of time:

  ```bash
  macos-harness approve --app TextEdit --always
  ```

- The harness never moves your pointer and never brings an app to the front. When you
  touch an app the agent is using, the agent is told to read it again first.
- To stop the agent, interrupt it in its own app (Esc in Claude Code or Codex,
  Ctrl+C in a terminal): the action it is running stops at its next key or click.

- Notes of your own for one app go into
  `~/Library/Application Support/macos-harness/instructions/<bundle id>.md`
  (for every browser: `browser.md`). The agent gets the note with its first read of
  that app. A note of yours replaces the built-in one for that app.

## Check it

Read an app in the background. Your front app and your pointer stay where they are:

```bash
macos-harness get_app_state --app Finder
```

## Telemetry

Anonymous telemetry contains only the CLI command category, success, duration,
package version, OS/architecture, and detected agent client. It never includes
prompts, app names, screenshots, text, scripts, paths, or window titles.

```bash
macos-harness telemetry disable
```
