# Install macOS Harness

## Let your agent do it

Paste this into Codex or Claude Code:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Register the skill printed by `macos-harness skill`, then run `macos-harness doctor`. Explain any missing macOS permissions and ask before requesting them. Finally, verify the harness by capturing one already-running app without bringing it to the foreground.
```

## Or install it yourself

```bash
uv tool install --python 3.12 --upgrade --force macos-harness
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/macos-harness"
macos-harness skill > "${CODEX_HOME:-$HOME/.codex}/skills/macos-harness/SKILL.md"
macos-harness doctor
```

Grant only the permissions reported by `doctor`. macOS may request Accessibility,
Screen Recording, and Automation access. Input Monitoring is not required.

Verify without focusing the target app:

```bash
macos-harness <<'PY'
print(mac.see("Finder"))
PY
```

Anonymous telemetry contains only the CLI command category, success, duration,
package version, OS/architecture, and detected agent client. It never includes
prompts, app names, screenshots, text, scripts, paths, or window titles.

```bash
macos-harness telemetry disable
```
