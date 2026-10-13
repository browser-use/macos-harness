"""Put the skill, and the turn hooks, where each agent CLI reads them."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import errors

NAME = "macos-harness"
# A hook prints nothing and never fails: some CLIs read a hook's output and exit code as
# an order to block the prompt or to keep the turn going.
STARTED = f"{NAME} turn-started >/dev/null 2>&1 || true"
ENDED = f"{NAME} turn-ended >/dev/null 2>&1 || true"

PI_EXTENSION = f"""\
// Tells {NAME} when a turn starts and ends.
export default function (pi) {{
  const tell = (word) => pi.exec("{NAME}", [word]).catch(() => {{}});
  pi.on("agent_start", async () => {{
    await tell("turn-started");
  }});
  pi.on("agent_end", async () => {{
    await tell("turn-ended");
  }});
}}
"""

OPENCODE_PLUGIN = f"""\
// Tells {NAME} when a turn ends.
export const MacosHarness = async ({{ $ }}) => ({{
  event: async ({{ event }}) => {{
    if (event.type === "session.idle") await $`{NAME} turn-ended`.quiet().nothrow();
  }},
}});
"""

# skill: the folder that gets macos-harness/SKILL.md, per scope.
# hooks: a JSON settings file per scope, and the command each of its events runs.
# plugin: a file of ours that the CLI loads by itself, per scope, and its text.
# No subagent event anywhere: a subagent shares its parent's session, so a subagent
# that ends would drop the app sessions its parent is still using.
AGENTS = {
    "claude": {
        "skill": {"project": ".claude/skills", "user": "~/.claude/skills"},
        "hooks": {
            "project": ".claude/settings.local.json",
            "user": "~/.claude/settings.json",
        },
        "events": {"UserPromptSubmit": STARTED, "Stop": ENDED, "SessionEnd": ENDED},
    },
    "codex": {
        "skill": {"project": ".agents/skills", "user": "$CODEX_HOME/skills"},
        "hooks": {"project": ".codex/hooks.json", "user": "$CODEX_HOME/hooks.json"},
        "events": {
            "UserPromptSubmit": STARTED,
            "Stop": ENDED,
            "Interrupt": ENDED,
            "SessionEnd": ENDED,
        },
    },
    "pi": {
        "skill": {"project": ".agents/skills", "user": "~/.agents/skills"},
        "plugin": {
            "project": f".pi/extensions/{NAME}.ts",
            "user": f"~/.pi/agent/extensions/{NAME}.ts",
        },
        "source": PI_EXTENSION,
    },
    "opencode": {
        "skill": {"project": ".opencode/skills", "user": "~/.config/opencode/skills"},
        "plugin": {
            "project": f".opencode/plugins/{NAME}.js",
            "user": f"~/.config/opencode/plugins/{NAME}.js",
        },
        "source": OPENCODE_PLUGIN,
    },
}
SCOPES = ("project", "user")


def skill_text() -> str:
    """The skill file: the packaged copy, or the one of a source checkout."""
    here = Path(__file__).resolve()
    for path in (
        here.with_name("SKILL.md"),
        here.parents[2] / "skills" / NAME / "SKILL.md",
    ):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise errors.skill_missing()


def install(
    agent: str | None = None, scope: str = "user", cwd: str = ".", env=None
) -> list[str]:
    """Write the skill and the turn hooks. Returns every file that now holds them.

    No agent named means all of them. Running it again changes nothing. `env` is the
    caller's environment: its home folders are the ones written to.
    """
    return _each(agent, scope, cwd, env, add=True)


def uninstall(
    agent: str | None = None, scope: str = "user", cwd: str = ".", env=None
) -> list[str]:
    """Take away what install wrote, and nothing else. Returns the files it changed."""
    return _each(agent, scope, cwd, env, add=False)


def _each(agent: str | None, scope: str, cwd: str, env, add: bool) -> list[str]:
    names = list(AGENTS) if agent in (None, "", "all") else [agent]
    if scope not in SCOPES or any(name not in AGENTS for name in names):
        agents, scopes = ", ".join(AGENTS), " or ".join(SCOPES)
        raise errors.usage("install", agents=agents, scopes=scopes, example="claude")
    done = {}  # path -> True. Two CLIs can share one skill folder.
    where = lambda template: _path(template, cwd, os.environ if env is None else env)
    for name in names:
        spec = AGENTS[name]
        skill = where(spec["skill"][scope]) / NAME / "SKILL.md"
        changes = [(skill, _file(skill, skill_text() if add else None))]
        if "hooks" in spec:
            hooks = where(spec["hooks"][scope])
            changes.append((hooks, _hooks(hooks, spec["events"], add)))
        if "plugin" in spec:
            plugin = where(spec["plugin"][scope])
            # an edited plugin file is the person's: uninstall leaves it
            if add or _read(plugin) == spec["source"]:
                text = spec["source"] if add else None
                changes.append((plugin, _file(plugin, text)))
        done.update({str(path): True for path, changed in changes if add or changed})
    return list(done)


def _path(template: str, cwd: str, env) -> Path:
    """A path of the table for one caller: its home folders, not the helper's."""
    home = env.get("HOME") or str(Path.home())
    codex = env.get("CODEX_HOME") or f"{home}/.codex"
    return Path(cwd, template.replace("$CODEX_HOME", codex).replace("~", home, 1))


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _file(path: Path, text: str | None) -> bool:
    """Write `text`, or remove the file when it is None. True when something changed."""
    # The file is ours, and so is its folder when the folder carries our name.
    ours = [path] + [path.parent] * (path.parent.name == NAME)
    if any(item.is_symlink() for item in ours):
        # A link leads into another folder, often a source checkout. Never write there.
        raise errors.usage("install_link", path=path)
    if _read(path) == text:
        return False
    if text is None:
        path.unlink()
        for folder in ours[1:]:
            if not any(folder.iterdir()):
                folder.rmdir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        draft = path.with_name(path.name + ".tmp")
        draft.write_text(text, encoding="utf-8")
        if path.exists():  # a settings file kept private (it may hold keys) stays so
            shutil.copymode(path, draft)
        os.replace(draft, path)  # never leave half a settings file
    return True


def _hooks(path: Path, events: dict[str, str], add: bool) -> bool:
    """Add our command to each event of a hooks file, or take exactly it out."""
    try:
        data = json.loads(_read(path) or "{}")
        hooks = {**data.get("hooks", {})}
        for event, command in events.items():
            groups = []
            for group in hooks.get(event, []):
                kept = [h for h in group["hooks"] if h.get("command") != command]
                groups += [{**group, "hooks": kept}] if kept else []
            if add:
                groups.append({"hooks": [{"type": "command", "command": command}]})
            hooks[event] = groups
            if not groups:
                del hooks[event]
    except (ValueError, AttributeError, KeyError, TypeError):
        raise errors.usage("install_hooks", path=path) from None
    after = {**data, "hooks": hooks}
    if not hooks:
        del after["hooks"]
    if after == data:
        return False  # nothing of ours to add or to take out: leave their layout alone
    return _file(path, json.dumps(after, indent=2) + "\n" if after else None)
