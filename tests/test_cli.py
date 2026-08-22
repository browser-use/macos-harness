"""CI-safe tests for the CLI's argument parser.

Focused narrowly on the ``agent`` subcommand's removal -- the shared
daemon it used to start/check/stop no longer exists, and no replacement
(not even a ``build`` prewarm command) was kept. Every other subcommand's
own behavior is exercised elsewhere; this file only guards the cutover.
"""

from __future__ import annotations

import pytest

from macos_harness import cli


def test_agent_is_not_a_recognized_subcommand() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["agent", "start"])
    assert excinfo.value.code == 2


def test_agent_build_is_not_a_recognized_subcommand_either() -> None:
    """No shim, no partial cutover: not even ``agent build`` survives."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["agent", "build"])
    assert excinfo.value.code == 2


def test_run_agent_command_helper_no_longer_exists() -> None:
    assert not hasattr(cli, "_run_agent_command")


def test_every_other_subcommand_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MACOS_HARNESS_BACKEND", raising=False)
    parser = cli._build_parser()

    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["apps"]).command == "apps"
    assert parser.parse_args(["repl"]).command == "repl"
    assert parser.parse_args(["skill"]).command == "skill"
    assert parser.parse_args(["telemetry", "status"]).command == "telemetry"
    assert parser.parse_args(["see", "Finder"]).command == "see"
    assert parser.parse_args(["state", "Finder"]).command == "state"


def test_main_exits_with_argparse_error_for_a_stale_agent_invocation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real end-to-end entry point, not just the parser in isolation:
    a user typing the removed ``macos-harness agent ...`` invocation gets
    argparse's own usage error, never a branch that quietly does nothing
    or falls through to executing stdin as Python.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["agent", "status"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'agent'" in capsys.readouterr().err


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"macos-harness {cli.__version__}"
