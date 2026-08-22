"""Tiny, privacy-safe, opt-in telemetry for macOS Harness."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path

from ._version import __version__

POSTHOG_KEY = "phc_nud39qe8UBkoFaMM2RwQ8LPWbWDNQNdPUeGShNTCHVXv"
POSTHOG_HOST = "https://eu.i.posthog.com"
DISABLE_ENVS = (
    "MACOS_HARNESS_TELEMETRY",
    "ANONYMIZED_TELEMETRY",
    "DO_NOT_TRACK",
)
COMMANDS = {"python", "doctor", "apps", "repl", "skill", "see", "state"}

#: In-process test seam for `_endpoint()`. Never read from the
#: environment: production code never assigns this, and no environment
#: variable -- in this process or any other -- can change it. A test sets
#: it directly with `monkeypatch.setattr(telemetry, "_endpoint_override",
#: "http://127.0.0.1:PORT")` to point `_send()` at a local mock server.
_endpoint_override: str | None = None


def _config_dir() -> Path:
    override = os.environ.get("MACOS_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "macos-harness"


def _config_path() -> Path:
    return _config_dir() / "telemetry.json"


def _load() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _save(config: dict) -> None:
    try:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_disabled() -> bool:
    """True if a kill-switch env var forces telemetry off. These can only
    push the effective state toward disabled; none of them can enable
    telemetry -- that requires the explicit `macos-harness telemetry
    enable` command."""
    false = {"0", "false", "no", "off"}
    for name in DISABLE_ENVS:
        value = (os.environ.get(name) or "").lower()
        if name == "DO_NOT_TRACK":
            if value in {"1", "true", "yes", "on"}:
                return True
        elif value in false:
            return True
    return False


def _install_id(config: dict | None = None, *, create: bool = True) -> str | None:
    config = config if config is not None else _load()
    raw = config.get("install_id")
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError):
        if not create:
            return None
    install_id = str(uuid.uuid4())
    _save({**config, "install_id": install_id})
    return install_id


def is_enabled() -> bool:
    """False until `macos-harness telemetry enable` has run, and always
    false while a kill-switch env var is set: a fresh install sends
    nothing."""
    return not _env_disabled() and bool(_load().get("enabled"))


def _endpoint() -> str:
    """Resolve the ingestion host. `_endpoint_override` is an in-process
    test seam only -- never read from the environment -- so no environment
    variable, in this process or any other, can redirect telemetry
    anywhere; a test sets it directly (`monkeypatch.setattr(telemetry,
    "_endpoint_override", ...)`) to point at a loopback mock server. The
    production override `MACOS_HARNESS_POSTHOG_HOST` is honored only when
    it is an HTTPS URL; any other value is ignored in favor of the built-in
    default, so no environment variable can silently redirect real
    telemetry to an unencrypted or unintended endpoint."""
    if _endpoint_override is not None:
        return _endpoint_override.rstrip("/")
    host = os.environ.get("MACOS_HARNESS_POSTHOG_HOST", POSTHOG_HOST).rstrip("/")
    return host if host.startswith("https://") else POSTHOG_HOST


def status() -> dict:
    config = _load()
    env_disabled = _env_disabled()
    enabled_by_config = bool(config.get("enabled"))
    enabled = not env_disabled and enabled_by_config
    return {
        "enabled": enabled,
        "disabled_by_env": env_disabled,
        "enabled_by_config": enabled_by_config,
        "install_id": _install_id(config, create=enabled),
        "config_path": str(_config_path()),
        "endpoint": _endpoint(),
    }


def set_enabled(enabled: bool) -> dict:
    config = _load()
    config["enabled"] = enabled
    _save(config)
    return status()


def _agent() -> str | None:
    markers = (
        ("CODEX_THREAD_ID", "codex"),
        ("CODEX_SANDBOX", "codex"),
        ("CLAUDECODE", "claude-code"),
        ("CURSOR_AGENT", "cursor"),
        ("GEMINI_CLI", "gemini-cli"),
        ("OPENCODE", "opencode"),
    )
    return next((client for name, client in markers if os.environ.get(name)), None)


_SENDER = """
import json, sys, urllib.request
try:
    job = json.load(sys.stdin)
    request = urllib.request.Request(
        job['url'], method='POST',
        data=json.dumps(job['payload']).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'macos-harness'},
    )
    urllib.request.urlopen(request, timeout=job['timeout']).close()
except Exception:
    pass
"""


def _send(payload: dict) -> None:
    job = {
        "url": f"{_endpoint()}/i/v0/e/",
        "timeout": float(os.environ.get("MACOS_HARNESS_TELEMETRY_TIMEOUT", "5")),
        "payload": payload,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _SENDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdin is not None:
        process.stdin.write(json.dumps(job).encode())
        process.stdin.close()


def capture_cli(command: str | None, success: bool, duration_seconds: float) -> None:
    """Capture only a known command and coarse runtime metadata—never user data."""
    if not is_enabled():
        return
    try:
        payload = {
            "api_key": POSTHOG_KEY,
            "distinct_id": _install_id(),
            "event": "macos_harness_cli",
            "properties": {
                "$process_person_profile": False,
                "$geoip_disable": True,
                "command": command if command in COMMANDS else "python",
                "success": success,
                "duration_seconds": round(max(duration_seconds, 0), 2),
                "macos_harness_version": __version__,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                "os": platform.system() or "unknown",
                "machine": platform.machine() or "unknown",
                "agent": _agent(),
            },
        }
        _send(payload)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return


def run_cli(argv: list[str]) -> int:
    if not argv or argv == ["status"]:
        print(json.dumps(status(), indent=2))
        return 0
    if argv == ["enable"]:
        print(json.dumps(set_enabled(True), indent=2))
        return 0
    if argv == ["disable"]:
        print(json.dumps(set_enabled(False), indent=2))
        return 0
    print("usage: macos-harness telemetry [status|enable|disable]", file=sys.stderr)
    return 2
