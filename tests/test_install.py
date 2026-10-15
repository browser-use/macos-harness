"""Install puts the skill and the turn hooks where each agent CLI reads them."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from macos_harness import errors, install, tools
from macos_harness.model import AgentSession

ROOT = Path(__file__).resolve().parents[1]
STARTED, ENDED, CANCELLED = install.STARTED, install.ENDED, install.CANCELLED
# Where each CLI reads a skill, a hooks file and a plugin. `~` is the temp home, a path
# without it is inside the project folder. Measured per CLI, then written down here a
# second time on purpose: a typo in the table of install.py must fail a test.
WHERE = {
    ("claude", "project"): [
        ".claude/skills/macos-harness/SKILL.md",
        ".claude/settings.local.json",
    ],
    ("claude", "user"): [
        "~/.claude/skills/macos-harness/SKILL.md",
        "~/.claude/settings.json",
    ],
    ("codex", "project"): [
        ".agents/skills/macos-harness/SKILL.md",
        ".codex/hooks.json",
    ],
    ("codex", "user"): [
        "~/.codex/skills/macos-harness/SKILL.md",
        "~/.codex/hooks.json",
    ],
    ("pi", "project"): [
        ".agents/skills/macos-harness/SKILL.md",
        ".pi/extensions/macos-harness.ts",
    ],
    ("pi", "user"): [
        "~/.agents/skills/macos-harness/SKILL.md",
        "~/.pi/agent/extensions/macos-harness.ts",
    ],
    ("opencode", "project"): [
        ".opencode/skills/macos-harness/SKILL.md",
        ".opencode/plugins/macos-harness.js",
    ],
    ("opencode", "user"): [
        "~/.config/opencode/skills/macos-harness/SKILL.md",
        "~/.config/opencode/plugins/macos-harness.js",
    ],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # No command next to this python: the hooks call it by name (a test sets a path)
    monkeypatch.setattr(install.sys, "executable", str(tmp_path / "bin/python"))
    return folder


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def tree(*folders: Path) -> dict[str, str]:
    """Every file under the folders, with its text."""
    return {
        str(path): path.read_text()
        for folder in folders
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def place(spot: str, home: Path, project: Path) -> Path:
    return home / spot[2:] if spot.startswith("~/") else project / spot


def group(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


# --- the right folder per CLI ---------------------------------------------------------


def test_the_table_lists_the_four_agent_clis():
    assert sorted(install.AGENTS) == sorted({agent for agent, _ in WHERE})


@pytest.mark.parametrize("agent,scope", WHERE)
def test_writes_where_that_cli_reads(agent, scope, home, project):
    written = install.install(agent, scope, str(project))
    expected = [str(place(spot, home, project)) for spot in WHERE[agent, scope]]
    assert written == expected
    assert sorted(tree(home, project)) == sorted(expected)  # and nothing else
    assert Path(expected[0]).read_text() == install.skill_text()


def test_the_skill_it_installs_is_the_skill_of_this_repo():
    text = (ROOT / "skills/macos-harness/SKILL.md").read_text()
    assert install.skill_text() == text
    assert text.startswith("---\nname: macos-harness\n")


def test_a_home_of_its_own_for_the_cli_that_has_one(
    home, project, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    written = install.install("codex", "user", str(project))
    assert written == [
        str(tmp_path / "elsewhere/skills/macos-harness/SKILL.md"),
        str(tmp_path / "elsewhere/hooks.json"),
    ]


def test_the_callers_home_folders_are_written_not_the_helpers(home, project, tmp_path):
    """The helper outlives the shell that started it: the caller's env decides."""
    theirs = {"HOME": str(tmp_path / "caller"), "CODEX_HOME": str(tmp_path / "cx")}
    assert install.install("claude", "user", str(project), theirs) == [
        str(tmp_path / "caller/.claude/skills/macos-harness/SKILL.md"),
        str(tmp_path / "caller/.claude/settings.json"),
    ]
    assert install.install("codex", "user", str(project), theirs)[0] == str(
        tmp_path / "cx/skills/macos-harness/SKILL.md"
    )
    assert not (home / ".claude").exists()
    install.uninstall(None, "user", str(project), theirs)
    assert not (tmp_path / "caller/.claude/skills/macos-harness").exists()


def test_no_agent_named_means_all_of_them(home, project):
    written = install.install(None, "project", str(project))
    spots = [
        spot
        for (_, scope), spots in WHERE.items()
        if scope == "project"
        for spot in spots
    ]
    assert written == [str(project / spot) for spot in dict.fromkeys(spots)]
    assert install.install("all", "project", str(project)) == written
    assert tree(home) == {}


@pytest.mark.parametrize("agent,scope", [("cursor", "user"), ("claude", "global")])
def test_unknown_agent_or_scope_is_a_usage_error_with_an_example(
    agent, scope, home, project
):
    with pytest.raises(errors.UsageError) as raised:
        install.install(agent, scope, str(project))
    assert "claude, codex, pi, opencode" in raised.value.message
    example = raised.value.message.split("Example: ")[1]
    assert tools.parse(shlex.split(example)[1:])[0] == "install"
    assert tree(home, project) == {}


# --- the turn hooks -------------------------------------------------------------------


def test_hooks_tell_the_helper_when_a_turn_starts_and_ends(home, project):
    install.install("claude", "project", str(project))
    hooks = json.loads((project / ".claude/settings.local.json").read_text())["hooks"]
    assert hooks == {
        "UserPromptSubmit": [group(STARTED)],
        "Stop": [group(ENDED)],  # background agents go on after it: no cancel
        "SessionEnd": [group(CANCELLED)],
    }
    install.install("codex", "project", str(project))
    hooks = json.loads((project / ".codex/hooks.json").read_text())["hooks"]
    assert hooks == {
        "UserPromptSubmit": [group(STARTED)],
        "Stop": [group(ENDED)],
        "Interrupt": [group(CANCELLED)],
        "SessionEnd": [group(CANCELLED)],
    }


def test_an_install_of_0_2_0_gets_the_cancelling_hook_in_place_of_its_own(
    home, project
):
    hooks = project / ".codex/hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(json.dumps({"hooks": {"Interrupt": [group(ENDED)]}}))
    install.install("codex", "project", str(project))
    [only] = json.loads(hooks.read_text())["hooks"]["Interrupt"]
    [hook] = only["hooks"]
    assert hook["command"].endswith(CANCELLED[len("macos-harness") :])


def test_hook_commands_are_commands_the_tool_has():
    for command in (STARTED, ENDED, CANCELLED):
        argv = shlex.split(command.split(" >")[0])
        assert argv[0] == "macos-harness"
        assert tools.parse(argv[1:])[0] in tools.COMMANDS


def test_no_hook_on_a_subagent_event():
    # A subagent shares its parent's session: ending there would drop the parent's apps.
    text = json.dumps(install.AGENTS, default=lambda source: source()).lower()
    assert "subagent" not in text


@pytest.mark.parametrize("command", [STARTED, ENDED, CANCELLED])
def test_a_hook_prints_nothing_and_never_fails(command):
    # Some CLIs read a hook's output or exit code 2 as an order. Here the command is not
    # even installed, which is the worst case.
    done = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/nonexistent"},
        capture_output=True,
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")


def test_hooks_call_this_install_by_its_path_whatever_path_finds(
    home, project, tmp_path, monkeypatch
):
    """The agent's PATH may be another one than the shell's: a hook by name may run an
    older install, or nothing. The hooks name this one, by the path it was started
    with. The bare name of an earlier build gives way; uninstall finds both forms."""
    own = tmp_path / "my venv/bin"
    own.mkdir(parents=True)
    (own / "macos-harness").touch(mode=0o755)
    monkeypatch.setattr(install.sys, "executable", str(own / "python"))
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [group(ENDED)]}}))
    env = {"HOME": str(home), "PATH": str(own)}  # found by name, too
    install.install(None, "project", str(project), env)
    path = shlex.quote(str(own / "macos-harness"))
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["Stop"] == [group(path + ENDED[len("macos-harness") :])]  # one hook
    pi = (project / ".pi/extensions/macos-harness.ts").read_text()
    assert f"pi.exec({json.dumps(str(own / 'macos-harness'))}, words)" in pi
    assert "${" in (project / ".opencode/plugins/macos-harness.js").read_text()
    install.uninstall(None, "project", str(project), env)
    assert tree(home, project) == {}


def test_a_run_from_uvs_cache_calls_us_by_name_and_says_so(
    home, project, tmp_path, monkeypatch
):
    """uvx: the folder goes with the cache, so a path in a hook would go dead."""
    run = tmp_path / "cache/archive-v0/abc/bin"
    run.mkdir(parents=True)
    (run / "macos-harness").touch(mode=0o755)
    monkeypatch.setattr(install.sys, "executable", str(run / "python"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    install.install("claude", "project", str(project))
    hooks = json.loads((project / ".claude/settings.local.json").read_text())["hooks"]
    assert hooks["Stop"] == [group(ENDED)]
    assert "install it with `uv tool install macos-harness`" in install.hook_note()
    monkeypatch.delenv("UV_CACHE_DIR")
    assert install.hook_note() is None


def test_plugins_call_the_same_two_commands():
    pi, opencode = install.pi_extension(), install.opencode_plugin()
    assert 'pi.on("agent_start"' in pi and 'tell("turn-started")' in pi
    assert 'pi.on("agent_end"' in pi and 'tell("turn-ended", "--cancel")' in pi
    assert 'pi.exec("macos-harness", words)' in pi
    assert 'event.type === "session.idle"' in opencode
    assert "$`macos-harness turn-ended`" in opencode
    for text in (pi, opencode):
        assert text.count("{") == text.count("}") and text.count("(") == text.count(")")


# --- the person's own settings survive ------------------------------------------------

THEIRS = {
    "model": "opus",
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
        ],
    },
    "permissions": {"allow": ["Bash(ls:*)"]},
}


def test_hooks_are_added_next_to_the_persons_own_and_taken_out_again(home, project):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(THEIRS, indent=4))
    install.install("claude", "user", str(project))
    now = json.loads(settings.read_text())
    assert list(now) == list(THEIRS)  # their keys, in their order
    assert now["hooks"]["Stop"] == [*THEIRS["hooks"]["Stop"], group(ENDED)]
    assert now["hooks"]["PreToolUse"] == THEIRS["hooks"]["PreToolUse"]
    assert now["hooks"]["UserPromptSubmit"] == [group(STARTED)]
    install.uninstall("claude", "user", str(project))
    assert json.loads(settings.read_text()) == THEIRS


def test_a_command_of_theirs_that_ends_like_ours_stays(home, project):
    theirs = [
        "osascript -e 'beep'; /opt/bin/" + ENDED,
        "cd ~/proj && ./" + ENDED,
        '"/My Apps/macos-harness" turn-ended >/dev/null 2>&1 || true',  # ours, quoted
    ]
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [group(c) for c in theirs]}}))
    install.install("claude", "project", str(project))
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["Stop"] == [*(group(c) for c in theirs[:2]), group(ENDED)]
    install.uninstall("claude", "project", str(project))
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks == {"Stop": [group(c) for c in theirs[:2]]}


def test_our_command_inside_a_group_of_theirs_is_taken_out_alone(home, project):
    mixed = {
        "hooks": {"Stop": [{"hooks": [group(ENDED)["hooks"][0], {"command": "say"}]}]}
    }
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(mixed))
    install.uninstall("claude", "project", str(project))
    assert json.loads(settings.read_text()) == {
        "hooks": {"Stop": [{"hooks": [{"command": "say"}]}]}
    }


@pytest.mark.parametrize(
    "text", ["{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": [3]}}']
)
def test_a_hooks_file_it_cannot_read_is_left_alone(text, home, project):
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(text)
    with pytest.raises(errors.UsageError, match="Nothing was changed"):
        install.install("claude", "project", str(project))
    assert settings.read_text() == text


def test_a_settings_file_with_nothing_of_ours_keeps_its_layout(home, project):
    settings = project / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{"model":   "opus"}')
    assert install.uninstall("claude", "project", str(project)) == []
    assert settings.read_text() == '{"model":   "opus"}'


# --- again, and away ------------------------------------------------------------------


@pytest.mark.parametrize("scope", install.SCOPES)
def test_running_it_again_changes_nothing(scope, home, project):
    first = install.install(None, scope, str(project))
    before = tree(home, project)
    assert install.install(None, scope, str(project)) == first
    assert tree(home, project) == before
    assert not [path for path in before if path.endswith(".tmp")]


def test_an_older_skill_is_replaced(home, project):
    skill = project / ".claude/skills/macos-harness/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("the skill of an older version")
    install.install("claude", "project", str(project))
    assert skill.read_text() == install.skill_text()


@pytest.mark.parametrize("scope", install.SCOPES)
def test_uninstall_removes_exactly_what_install_wrote(scope, home, project):
    theirs = [
        home / ".claude/skills/other-skill/SKILL.md",
        project / ".agents/skills/other-skill/SKILL.md",
        home / ".claude/skills/macos-harness/agents/extra.yaml",
        project / ".claude/skills/macos-harness/agents/extra.yaml",
    ]
    for path in theirs:
        path.parent.mkdir(parents=True)
        path.write_text("theirs")
    before = tree(home, project)
    written = install.install(None, scope, str(project))
    assert install.uninstall(None, scope, str(project)) == written
    assert tree(home, project) == before
    assert install.uninstall(None, scope, str(project)) == []


def test_uninstall_takes_its_own_empty_skill_folder_along(home, project):
    install.install(None, "project", str(project))
    install.uninstall(None, "project", str(project))
    assert list(project.rglob("macos-harness*")) == []
    assert (project / ".claude/skills").is_dir()  # the CLI's own folder stays


def test_uninstall_leaves_a_plugin_the_person_edited(home, project):
    install.install("opencode", "project", str(project))
    plugin = project / ".opencode/plugins/macos-harness.js"
    plugin.write_text(plugin.read_text() + "// mine\n")
    install.uninstall("opencode", "project", str(project))
    assert plugin.read_text().endswith("// mine\n")
    assert not (project / ".opencode/skills").exists() or not list(
        (project / ".opencode/skills").iterdir()
    )


def test_never_writes_through_a_link_into_another_folder(home, project, tmp_path):
    # A skill folder that is a link to a source checkout: writing would change the
    # checkout.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text("the checkout's own file")
    (home / ".claude/skills").mkdir(parents=True)
    (home / ".claude/skills/macos-harness").symlink_to(checkout)
    for change in (install.install, install.uninstall):
        with pytest.raises(errors.UsageError, match="link"):
            change("claude", "user", str(project))
    assert (checkout / "SKILL.md").read_text() == "the checkout's own file"
    assert not (home / ".claude/settings.json").exists()


# --- through the command line ---------------------------------------------------------


def test_the_three_commands_reach_these_functions(home, project):
    def run(*argv):
        return tools.run(list(argv), None, AgentSession(key="a"), str(project), 0.0)

    assert run("skill") == (0, install.skill_text(), "")
    code, out, err = run("install", "--agent", "opencode", "--scope", "project")
    assert (code, err) == (0, "")
    spots = WHERE["opencode", "project"]
    assert out.split() == [str(project / spot) for spot in spots]
    assert run("uninstall", "opencode", "--scope", "project")[0] == 0
    assert tree(home, project) == {}
    code, out, err = run("install", "--agent", "cursor")
    assert code == 2 and "claude, codex, pi, opencode" in err
    assert tree(home, project) == {}


# --- the two texts for the person -----------------------------------------------------


@pytest.mark.parametrize("name", ["install.md", "README.md"])
def test_commands_in_the_texts_for_the_person_parse(name):
    blocks = re.findall(r"```bash\n(.*?)```", (ROOT / name).read_text(), re.DOTALL)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    shown = [line for line in lines if line.startswith("macos-harness ")]
    assert len(shown) >= 4
    for line in shown:
        for part in line.split(" && "):
            argv = shlex.split(part.split(" > ")[0])[1:]
            assert argv[0] in ("helper", "--version") or tools.parse(argv)[0], line


@pytest.mark.parametrize("name", ["install.md", "README.md"])
def test_the_way_back_to_an_older_release_takes_our_files_out_first(name):
    # An older release cannot: it has no uninstall, and would leave this skill behind.
    text = " ".join((ROOT / name).read_text().split())
    assert "run `macos-harness uninstall --agent all` first" in text


def test_uninstall_is_said_to_run_from_the_install_it_came_from():
    # A plugin names the full path of the install that wrote it.
    text = " ".join((ROOT / "install.md").read_text().split())
    assert "with the same install you installed from" in text


def test_install_text_names_every_cli_of_the_table_and_its_folders():
    text = (ROOT / "install.md").read_text()
    for agent, spec in install.AGENTS.items():
        assert f"--agent {agent}" in text
        assert spec["skill"]["project"] in text


def test_a_private_settings_file_stays_private(tmp_path):
    """~/.claude/settings.json was 0600 and held keys; after an install it was a new
    file at 0644."""
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    settings.chmod(0o600)
    assert install._file(settings, '{"hooks": {}}') is True
    assert settings.stat().st_mode & 0o777 == 0o600
    assert settings.read_text(encoding="utf-8") == '{"hooks": {}}'
