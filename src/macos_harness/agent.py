"""Lifecycle management for the native macOS accessibility agent process.

The native agent is an optional, separately-built SwiftPM executable (see
``native/macos-harness-agent`` at the repository root) that owns every
Accessibility call on one serial dispatch queue. This module is the *only*
place that starts, supervises, and stops that process. It never imports
``macos.py`` — callers (``MacOS``, the CLI) import this module, not the
other way around.

State lives under ``state_dir()`` (``~/Library/Application Support/
macos-harness/`` unless ``MACOS_HARNESS_HOME`` overrides it — the same
directory and override telemetry.py already uses): a pidfile, a lockfile
used to serialize concurrent ``start()``/``stop()`` calls, a log file for the
agent's stdout/stderr, and the NDJSON socket itself. The directory is kept
at mode 0700 and the socket at 0600 on every lifecycle call, defensively,
regardless of which process created them first.

The three OS-process-touching primitives (build, spawn, signal/liveness)
are separate, independently-monkeypatchable module functions so tests can
exercise every start/stop/status code path — locking, pidfile handling,
stale-socket recovery, readiness polling, idempotency — against a fake
in-process agent without spawning a real subprocess or requiring the Swift
toolchain.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native import NativeClient, NativeProtocolError
from .telemetry import _config_dir as _shared_state_dir

_EXECUTABLE_NAME = "macos-harness-agent"
_SOCKET_NAME = "agent.sock"
_PID_NAME = "agent.pid"
_LOCK_NAME = "agent.lock"
_LOG_NAME = "agent.log"

_DEFAULT_START_TIMEOUT = 10.0
_DEFAULT_STOP_TIMEOUT = 5.0
_DEFAULT_STATUS_TIMEOUT = 2.0


class AgentUnavailableError(RuntimeError):
    """The native agent could not be reached, started, or built.

    Raised by ``start()``/``ensure_running()`` for every reason the agent
    isn't usable: no Swift toolchain, a failed build, a spawn failure, or a
    readiness timeout. Routing code uses this single type to decide
    ``auto`` fallback (catch it, use the Python engine) versus ``native``
    hard failure (let it propagate).
    """


# --- paths ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentPaths:
    state_dir: Path
    socket: Path
    pid_file: Path
    lock_file: Path
    log_file: Path


def state_dir() -> Path:
    """Shared macOS Harness state directory (``MACOS_HARNESS_HOME`` or default)."""
    return _shared_state_dir()


def socket_path() -> Path:
    return state_dir() / _SOCKET_NAME


def pid_path() -> Path:
    return state_dir() / _PID_NAME


def lock_path() -> Path:
    return state_dir() / _LOCK_NAME


def log_path() -> Path:
    return state_dir() / _LOG_NAME


def agent_paths() -> AgentPaths:
    base = state_dir()
    return AgentPaths(
        state_dir=base,
        socket=base / _SOCKET_NAME,
        pid_file=base / _PID_NAME,
        lock_file=base / _LOCK_NAME,
        log_file=base / _LOG_NAME,
    )


def _repo_native_package_dir() -> Path:
    # src/macos_harness/agent.py -> src/macos_harness -> src -> repo root
    return Path(__file__).resolve().parents[2] / "native" / _EXECUTABLE_NAME


def _ensure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _empty_status(paths: AgentPaths) -> dict[str, Any]:
    return {
        "running": False,
        "pid": None,
        "agent_version": None,
        "trusted": None,
        "socket_path": str(paths.socket),
        "protocol": None,
        "uptime_s": None,
    }


def _read_pid_file(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    # pid 0 addresses this process's own group and pid 1 is the
    # kernel/launchd to every signal call below (`kill`, `waitpid`) —
    # neither is ever a real agent's own pid. A pidfile holding one is
    # corrupt or forged; treat it exactly like a missing pidfile rather
    # than let it reach `os.kill`.
    if pid <= 1:
        return None
    return pid


def _write_pid_file(path: Path, pid: int) -> None:
    path.write_text(f"{pid}\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _clear_stale_files(paths: AgentPaths) -> None:
    for path in (paths.pid_file, paths.socket):
        with contextlib.suppress(OSError):
            path.unlink()


def _read_log_tail(path: Path, *, max_bytes: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-max_bytes:].decode("utf-8", errors="replace").strip()


# --- process-touching seams (monkeypatchable for tests) ----------------------


def _build_executable(*, configuration: str = "release") -> Path:
    """Build the native agent on demand and return its executable path.

    Raises ``AgentUnavailableError`` with a clear, actionable message when
    the Swift toolchain is missing, the package source isn't checked out
    (e.g. an installed wheel with no ``native/`` directory), or the build
    itself fails.
    """
    package_dir = _repo_native_package_dir()
    swift = shutil.which("swift")
    if swift is None:
        raise AgentUnavailableError(
            "Swift toolchain not found; install Xcode Command Line Tools "
            "(`xcode-select --install`) to build the native accessibility agent"
        )
    if not (package_dir / "Package.swift").exists():
        raise AgentUnavailableError(
            f"Native agent package source not found at {package_dir}"
        )
    build = subprocess.run(
        [swift, "build", "-c", configuration, "--package-path", str(package_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        detail = (build.stderr or build.stdout or "").strip()[-4000:]
        raise AgentUnavailableError(
            f"swift build failed for the native agent:\n{detail}"
        )
    show_bin_path = subprocess.run(
        [
            swift,
            "build",
            "-c",
            configuration,
            "--package-path",
            str(package_dir),
            "--show-bin-path",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    bin_dir = show_bin_path.stdout.strip()
    if show_bin_path.returncode != 0 or not bin_dir:
        raise AgentUnavailableError(
            "could not determine the native agent build output path"
        )
    binary = Path(bin_dir) / _EXECUTABLE_NAME
    if not binary.exists():
        raise AgentUnavailableError(
            f"swift build did not produce the expected binary at {binary}"
        )
    return binary


def _spawn_process(binary: Path, paths: AgentPaths) -> subprocess.Popen[bytes]:
    """Launch the agent executable as a detached background process."""
    log_handle = paths.log_file.open("ab")
    try:
        return subprocess.Popen(
            [str(binary), "--socket", str(paths.socket)],
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_handle.close()


def _process_alive(pid: int) -> bool:
    # A child this module itself spawned that has already exited but was
    # never waited on is a zombie: it still holds `pid` in the process
    # table, so `kill(pid, 0)` below would keep reporting it "alive"
    # forever, and the instant something finally reaps it the kernel is
    # free to hand that exact pid to an unrelated new process — the
    # pid-reuse race this whole module guards against. Reap it first,
    # every time, so a dead child is never mistaken for a live one.
    # `waitpid` raises when `pid` is not our child (the common case: an
    # agent pid merely read from a pidfile, possibly written by an
    # earlier Python process) — that is expected and just falls through
    # to the `kill` probe below.
    if pid > 0:
        with contextlib.suppress(OSError):
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            if reaped_pid == pid:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(
    pid: int,
    *,
    timeout: float,
    revalidate: Callable[[], bool] | None = None,
) -> bool:
    """Send SIGTERM, wait, escalate to SIGKILL. Returns True once it's dead.

    ``revalidate``, when given, is consulted once more immediately before
    the SIGKILL escalation and must return ``True`` only if ``pid`` is
    still confirmed — by the same live identity check that gated the
    original SIGTERM, not merely ``_process_alive`` — to be the process
    this call is trying to stop. The SIGTERM wait is a window in which
    the real process can exit and the kernel can recycle ``pid`` onto
    something unrelated; escalating to SIGKILL without rechecking would
    let that race send a kill signal into a process this call never
    verified. When ``revalidate`` is omitted, escalation proceeds on
    liveness alone — the caller is asserting ``pid`` is already known
    safe to signal outright (e.g. a process this call itself just
    spawned and is cleaning up after a failed readiness wait).
    """
    if not _process_alive(pid):
        return True
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    if _process_alive(pid) and (revalidate is None or revalidate()):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            if not _process_alive(pid):
                return True
            time.sleep(0.05)
    return not _process_alive(pid)


# --- locking ------------------------------------------------------------------


@contextlib.contextmanager
def _lock(path: Path, *, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + max(timeout, 0.1)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AgentUnavailableError(
                        f"Timed out waiting for the native agent lock at {path}"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- status probing -----------------------------------------------------------


def _verify_live_pid(
    paths: AgentPaths, pid: int, *, timeout: float
) -> dict[str, Any] | None:
    """Live socket handshake pinned to exactly ``pid``, ignoring whatever
    the pidfile currently holds.

    Never re-reads ``paths.pid_file``. A caller that has already captured
    a pid and needs to keep checking that *exact* pid across several
    steps (``stop()``'s SIGTERM-then-SIGKILL escalation, in particular)
    must not have that identity silently swapped out from under it: the
    lockfile only serializes this module's own ``start()``/``stop()``
    against each other, and nothing stops a separate, non-cooperating
    same-UID process from rewriting the pidfile mid-call. Re-deriving
    the pid to check from the file on every call — as ``_probe_status``
    correctly does for its own "what's true right now" status reports —
    would let such a rewrite quietly redirect a signal this function's
    caller is about to send onto a different, never-verified process.
    Returns the ping payload on success, or ``None`` if nothing live at
    ``paths.socket`` confirms it is exactly ``pid``.
    """
    if not paths.socket.exists():
        return None
    client = NativeClient(paths.socket, timeout=timeout, expected_pid=pid)
    try:
        return client.ping()
    except (NativeProtocolError, OSError):
        return None
    finally:
        client.close()


def _probe_status(paths: AgentPaths, *, timeout: float) -> dict[str, Any]:
    result = _empty_status(paths)
    pid = _read_pid_file(paths.pid_file)
    if pid is None or not _process_alive(pid):
        return result
    ping = _verify_live_pid(paths, pid, timeout=timeout)
    if ping is None:
        return result
    result.update(
        running=True,
        pid=pid,
        agent_version=ping.get("agent_version"),
        trusted=ping.get("trusted"),
        protocol=ping.get("protocol"),
        uptime_s=ping.get("uptime_s"),
    )
    return result


def _await_ready(paths: AgentPaths, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    delay = 0.05
    status_now = _empty_status(paths)
    while True:
        status_now = _probe_status(paths, timeout=max(0.2, min(1.0, timeout)))
        if status_now["running"]:
            with contextlib.suppress(OSError):
                os.chmod(paths.socket, 0o600)
            return status_now
        if time.monotonic() >= deadline:
            return status_now
        time.sleep(delay)
        delay = min(delay * 1.5, 0.5)


# --- public lifecycle API ------------------------------------------------------


def status(*, timeout: float = _DEFAULT_STATUS_TIMEOUT) -> dict[str, Any]:
    """Report whether the agent is running, read-only (creates nothing)."""
    return _probe_status(agent_paths(), timeout=timeout)


def start(
    *, timeout: float = _DEFAULT_START_TIMEOUT, build: bool = True
) -> dict[str, Any]:
    """Start the native agent if it is not already running. Idempotent.

    Builds the agent on demand (unless ``build=False``), recovers a stale
    socket/pidfile left behind by a killed agent, spawns the executable,
    and blocks until it answers a readiness ping or ``timeout`` elapses.
    Raises ``AgentUnavailableError`` on any failure; never leaves a
    misleading pidfile/socket behind when it does.
    """
    paths = agent_paths()
    _ensure_state_dir(paths.state_dir)
    with _lock(paths.lock_file, timeout=timeout):
        current = _probe_status(paths, timeout=min(2.0, timeout))
        if current["running"]:
            return current
        _clear_stale_files(paths)
        binary = _build_executable() if build else _resolve_prebuilt_executable()
        process = _spawn_process(binary, paths)
        _write_pid_file(paths.pid_file, process.pid)
        ready = _await_ready(paths, timeout=timeout)
        if not ready["running"]:
            _terminate_pid(process.pid, timeout=2.0)
            log_tail = _read_log_tail(paths.log_file)
            _clear_stale_files(paths)
            detail = f"\n{log_tail}" if log_tail else ""
            raise AgentUnavailableError(
                f"Native agent did not become ready within {timeout}s{detail}"
            )
        return ready


def _resolve_prebuilt_executable() -> Path:
    package_dir = _repo_native_package_dir()
    for configuration in ("release", "debug"):
        candidate = package_dir / ".build" / configuration / _EXECUTABLE_NAME
        if candidate.exists():
            return candidate
    raise AgentUnavailableError(
        f"No prebuilt native agent executable found under {package_dir}/.build; "
        "start with build=True or run `swift build` first"
    )


def stop(*, timeout: float = _DEFAULT_STOP_TIMEOUT) -> dict[str, Any]:
    """Stop the native agent if it is running. Idempotent; never errors on an
    already-stopped agent.

    Never signals a pid on the strength of the pidfile alone. A pidfile is
    just a number some past ``start()`` wrote down, and the OS is free to
    hand that exact number to an unrelated process the moment the real
    agent exits — so every signal here is gated on a live socket
    handshake (``_verify_live_pid``'s ``NativeClient(expected_pid=...)``
    check) confirming the process on the other end of the agent socket
    really is ``pid``: once before the initial SIGTERM, and again right
    before any SIGKILL escalation. Both checks are pinned to the exact
    pid read at the top of this call, never re-derived from the pidfile
    mid-call — the lockfile only serializes this module's own
    ``start()``/``stop()`` against each other, and nothing stops a
    separate, non-cooperating same-UID process from rewriting the
    pidfile while this call is in flight. Stale or mismatched state — no
    live agent, or a live agent under a different pid — is cleared
    without ever calling ``kill()``.
    """
    paths = agent_paths()
    with _lock(paths.lock_file, timeout=timeout):
        pid = _read_pid_file(paths.pid_file)
        if pid is None:
            _clear_stale_files(paths)
            return _probe_status(paths, timeout=min(1.0, timeout))
        probe_timeout = min(1.0, timeout)

        def _verified() -> bool:
            return _verify_live_pid(paths, pid, timeout=probe_timeout) is not None

        if not _verified():
            _clear_stale_files(paths)
            return _probe_status(paths, timeout=probe_timeout)
        terminated = _terminate_pid(pid, timeout=timeout, revalidate=_verified)
        _clear_stale_files(paths)
        if not terminated:
            raise AgentUnavailableError(f"Failed to stop native agent process {pid}")
        return _probe_status(paths, timeout=probe_timeout)


def ensure_running(*, timeout: float = _DEFAULT_START_TIMEOUT) -> dict[str, Any]:
    """Return the running status, starting the agent first if needed.

    Raises ``AgentUnavailableError`` under exactly the same conditions as
    ``start()`` — this is the single gate routing code should call before
    dispatching to a native/auto backend.
    """
    current = _probe_status(agent_paths(), timeout=min(2.0, timeout))
    if current["running"]:
        return current
    return start(timeout=timeout)
