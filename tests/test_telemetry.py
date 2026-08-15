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
    assert payload["properties"]["command"] == "python"
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
