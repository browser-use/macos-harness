"""Tests for opt-in telemetry.

A fresh install must send nothing until the user explicitly runs
`macos-harness telemetry enable`; a kill-switch env var must always win even
over an explicit enable; and the payload, endpoint, and storage location
must match what the docs promise.
"""

from __future__ import annotations

import json
import stat

import pytest

from macos_harness import __version__, telemetry


@pytest.fixture(autouse=True)
def _clean_telemetry_env(monkeypatch):
    """Isolate every test from whatever telemetry-related env vars happen to
    be set in the host shell (a developer's own `DO_NOT_TRACK=1`, for
    example), so behavior here depends only on what each test sets."""
    for name in (*telemetry.DISABLE_ENVS, "MACOS_HARNESS_POSTHOG_HOST"):
        monkeypatch.delenv(name, raising=False)


def test_fresh_install_sends_nothing_until_enabled(tmp_path, monkeypatch) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(home))

    status = telemetry.status()

    assert status["enabled"] is False
    assert status["enabled_by_config"] is False
    assert status["disabled_by_env"] is False
    assert status["install_id"] is None
    assert not (home / "telemetry.json").exists()
    assert telemetry.is_enabled() is False

    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)
    telemetry.capture_cli("doctor", True, 0.1)
    assert sent == []


def test_enable_persists_a_stable_install_id_and_enables_sending(tmp_path, monkeypatch) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(home))

    enabled_status = telemetry.set_enabled(True)
    path = home / "telemetry.json"

    assert enabled_status["enabled"] is True
    assert enabled_status["install_id"]
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert telemetry.is_enabled() is True

    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)
    telemetry.capture_cli("doctor", True, 0.1)
    assert len(sent) == 1
    assert sent[0]["distinct_id"] == enabled_status["install_id"]

    # The install id is stable across a disable/re-enable cycle, not rotated.
    telemetry.set_enabled(False)
    assert telemetry.is_enabled() is False
    reenabled = telemetry.set_enabled(True)
    assert reenabled["install_id"] == enabled_status["install_id"]


def test_env_kill_switches_win_even_over_an_explicit_enable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    telemetry.set_enabled(True)
    assert telemetry.is_enabled() is True

    monkeypatch.setenv("MACOS_HARNESS_TELEMETRY", "0")
    assert telemetry.is_enabled() is False
    monkeypatch.delenv("MACOS_HARNESS_TELEMETRY")
    assert telemetry.is_enabled() is True

    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    assert telemetry.is_enabled() is False
    monkeypatch.delenv("ANONYMIZED_TELEMETRY")
    assert telemetry.is_enabled() is True

    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert telemetry.is_enabled() is False
    monkeypatch.delenv("DO_NOT_TRACK")
    assert telemetry.is_enabled() is True


def test_env_vars_can_only_disable_never_force_enable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    telemetry.set_enabled(False)

    monkeypatch.setenv("MACOS_HARNESS_TELEMETRY", "1")
    assert telemetry.is_enabled() is False

    monkeypatch.setenv("DO_NOT_TRACK", "0")
    assert telemetry.is_enabled() is False


def test_capture_has_no_channel_for_user_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    telemetry.set_enabled(True)
    sent = []
    monkeypatch.setattr(telemetry, "_send", sent.append)

    telemetry.capture_cli("unknown-user-value", True, 1.234)

    payload = sent[0]
    serialized = json.dumps(payload)
    props = payload["properties"]
    assert payload["event"] == "macos_harness_cli"
    assert props["command"] == "python"
    assert props["success"] is True
    assert props["duration_seconds"] == 1.23
    assert props["macos_harness_version"] == __version__
    assert props["python_version"]
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


def test_endpoint_override_requires_https(monkeypatch) -> None:
    monkeypatch.setenv("MACOS_HARNESS_POSTHOG_HOST", "http://insecure.example.com")
    assert telemetry._endpoint() == telemetry.POSTHOG_HOST

    monkeypatch.setenv("MACOS_HARNESS_POSTHOG_HOST", "https://secure.example.com/")
    assert telemetry._endpoint() == "https://secure.example.com"

    monkeypatch.delenv("MACOS_HARNESS_POSTHOG_HOST")
    assert telemetry._endpoint() == telemetry.POSTHOG_HOST


def test_endpoint_override_seam_is_in_process_only_not_env_controlled(monkeypatch) -> None:
    """The test seam that bypasses the HTTPS restriction (so a test can
    point `_send()` at a plain-HTTP loopback mock server) is a Python
    attribute, not an environment variable: setting the old env var name
    does nothing, and only `monkeypatch.setattr` on the module itself can
    reach it."""
    monkeypatch.setenv("MACOS_HARNESS_TELEMETRY_TEST_HOST", "http://127.0.0.1:9999/")
    assert telemetry._endpoint() == telemetry.POSTHOG_HOST

    monkeypatch.setattr(telemetry, "_endpoint_override", "http://127.0.0.1:9999/")
    assert telemetry._endpoint() == "http://127.0.0.1:9999"
