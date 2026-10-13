from __future__ import annotations

import json
import stat

from macos_harness import telemetry


def test_status_creates_private_anonymous_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / "config"))

    status = telemetry.status()
    path = tmp_path / "config" / "telemetry.json"

    assert status["enabled"] is True
    assert status["install_id"]
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_environment_and_config_can_disable_telemetry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    monkeypatch.setenv("MACOS_HARNESS_TELEMETRY", "0")
    assert telemetry.is_enabled() is False

    monkeypatch.delenv("MACOS_HARNESS_TELEMETRY")
    telemetry.set_enabled(False)
    assert telemetry.is_enabled() is False

    telemetry.set_enabled(True)
    assert telemetry.is_enabled() is True


def test_capture_has_no_channel_for_user_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)

    telemetry.capture_cli("unknown-user-value", True, 1.234)

    payload = sent[0]
    serialized = json.dumps(payload)
    assert payload["event"] == "macos_harness_cli"
    assert payload["properties"]["command"] == "other"
    assert payload["properties"]["success"] is True
    assert payload["properties"]["duration_seconds"] == 1.23
    for forbidden in (
        "app",
        "path",
        "prompt",
        "script",
        "screenshot",
        "text",
        "title",
        "window",
    ):
        assert forbidden not in serialized.lower()


def captured(monkeypatch, *args) -> dict:
    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)
    telemetry.capture_cli(*args)
    return sent[0]["properties"] if sent else {}


def test_one_event_with_fixed_fields_and_nothing_about_app_session_or_turn(
    monkeypatch,
) -> None:
    properties = captured(monkeypatch, "get_app_state", True, 0.5)
    assert set(properties) == {
        "$process_person_profile",
        "$geoip_disable",
        "command",
        "success",
        "status",
        "duration_seconds",
        "macos_harness_version",
        "python_version",
        "os",
        "machine",
        "agent",
    }
    assert properties["command"] == "get_app_state"  # one of a fixed list of names


def test_every_tool_and_command_name_may_be_sent() -> None:
    from macos_harness import tools

    assert set(tools.TOOLS) | set(tools.COMMANDS) == telemetry.COMMANDS


def test_status_is_cancelled_only_when_the_person_did_it(monkeypatch) -> None:
    status = lambda *args: captured(monkeypatch, "click", *args)["status"]
    assert status(True, 1) == "ok"
    assert status(False, 1, None, "stopped_by_user") == "cancelled"
    assert status(False, 1, None, "user_took_over") == "cancelled"
    assert status(False, 1, None, "no_such_app") == "failed"
    assert status(False, 1) == "failed"


def test_duration_is_clamped(monkeypatch) -> None:
    seconds = lambda value: captured(monkeypatch, "click", True, value)[
        "duration_seconds"
    ]
    assert seconds(-3) == 0
    assert seconds(1e12) == 2147483.65  # 2147483647 ms


def test_the_callers_environment_can_switch_it_off(monkeypatch) -> None:
    """The helper outlives the shell it was started from: each call brings its own."""
    for env in ({"MACOS_HARNESS_TELEMETRY": "0"}, {"DO_NOT_TRACK": "1"}):
        assert captured(monkeypatch, "click", True, 1, env) == {}
    assert captured(monkeypatch, "click", True, 1, {"MACOS_HARNESS_DIFF": "0"})


def test_the_telemetry_command_returns_its_text() -> None:
    code, out, err = telemetry.run_cli(["disable"])
    assert (code, err) == (0, "") and json.loads(out)["enabled"] is False
    assert json.loads(telemetry.run_cli([])[1])["disabled_by_config"] is True
    assert telemetry.run_cli(["enable"])[0] == 0
    assert telemetry.run_cli(["never"]) == (
        2,
        "",
        "usage: macos-harness telemetry [status|enable|disable]\n",
    )
