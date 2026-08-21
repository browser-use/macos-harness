"""CI-safe lifecycle tests for the native agent process supervisor.

These exercise ``macos_harness.agent`` (paths, locking, pidfile handling,
stale-socket recovery, readiness polling, start/status/stop/ensure_running)
against injected seams — never a real subprocess, the Swift toolchain, or a
real ``swift build``. The one genuinely OS-level primitive, signal
escalation in ``_terminate_pid``, gets its own focused test that
monkeypatches ``os.kill`` directly rather than touching a real process.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import macos_harness.agent as agent_module
from macos_harness.agent import AgentPaths

# --- a tiny, real-socket fake agent used as the injected spawn seam --------


class _FakeLifecycleAgent:
    """Answers ``ping`` over a real AF_UNIX socket from a background thread.

    Stands in for ``_spawn_process``'s real ``subprocess.Popen`` result:
    ``agent.py`` only ever reads ``.pid`` off what ``_spawn_process``
    returns (termination is always routed through the injected, pid-based
    ``_terminate_pid`` seam), so that is all this needs to expose.
    """

    _pids = itertools.count(900001)

    def __init__(self, paths: AgentPaths) -> None:
        self.pid = next(_FakeLifecycleAgent._pids)
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(paths.socket))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2.0)
            buffer = bytearray()
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    newline = buffer.index(b"\n")
                    line = bytes(buffer[: newline + 1])
                    del buffer[: newline + 1]
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    result = {
                        "protocol": 1,
                        "agent_version": "test-fake",
                        "pid": self.pid,
                        "trusted": True,
                        "uptime_s": time.monotonic() - self._started_at,
                    }
                    payload = json.dumps(
                        {"id": request.get("id"), "ok": True, "result": result}
                    )
                    try:
                        conn.sendall((payload + "\n").encode())
                    except OSError:
                        return

    def terminate(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._server.close()
        self._thread.join(timeout=2.0)


_REGISTRY: dict[int, _FakeLifecycleAgent] = {}


def _fake_spawn_process(binary: Path, paths: AgentPaths) -> _FakeLifecycleAgent:
    fake = _FakeLifecycleAgent(paths)
    _REGISTRY[fake.pid] = fake
    return fake


def _fake_process_alive(pid: int) -> bool:
    return pid in _REGISTRY


def _fake_terminate_pid(
    pid: int, *, timeout: float, revalidate: Callable[[], bool] | None = None
) -> bool:
    # The fake completes instantly -- there is no real SIGTERM-wait-
    # SIGKILL window here for a second identity check to protect against.
    # `_terminate_pid`'s own `revalidate` contract gets its own focused,
    # unmocked tests below.
    del revalidate
    fake = _REGISTRY.pop(pid, None)
    if fake is not None:
        fake.terminate()
    return True


@pytest.fixture
def fake_lifecycle(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short, unique root directly under /tmp keeps the fake agent's
    # AF_UNIX socket path well under the platform's sun_path limit — the
    # nested per-test directories pytest's own tmp_path builds are often
    # too long for socket.bind() to accept. The state dir itself is an
    # as-yet-nonexistent child so a fresh status() sees no state directory.
    root = Path(tempfile.mkdtemp(prefix="mh-", dir="/tmp"))
    state_home = root / "s"
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(state_home))
    _REGISTRY.clear()
    monkeypatch.setattr(
        agent_module, "_build_executable", lambda **kwargs: Path("/fake/agent-binary")
    )
    monkeypatch.setattr(agent_module, "_spawn_process", _fake_spawn_process)
    monkeypatch.setattr(agent_module, "_process_alive", _fake_process_alive)
    monkeypatch.setattr(agent_module, "_terminate_pid", _fake_terminate_pid)
    try:
        yield state_home
    finally:
        try:
            for fake in list(_REGISTRY.values()):
                with contextlib.suppress(OSError):
                    fake.terminate()
        finally:
            _REGISTRY.clear()
            shutil.rmtree(root, ignore_errors=True)


def _pid_file_contents(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


# --- paths --------------------------------------------------------------------


def test_state_dir_honors_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    paths = agent_module.agent_paths()
    assert paths.state_dir == tmp_path
    assert paths.socket == tmp_path / "agent.sock"
    assert paths.pid_file == tmp_path / "agent.pid"
    assert paths.lock_file == tmp_path / "agent.lock"
    assert paths.log_file == tmp_path / "agent.log"
    assert agent_module.socket_path() == paths.socket
    assert agent_module.pid_path() == paths.pid_file


# --- status (read-only) --------------------------------------------------------


def test_status_reports_not_running_on_fresh_state_dir(fake_lifecycle: Path) -> None:
    result = agent_module.status()
    assert result == {
        "running": False,
        "pid": None,
        "agent_version": None,
        "trusted": None,
        "socket_path": str(agent_module.socket_path()),
        "protocol": None,
        "uptime_s": None,
    }
    # status() is read-only: it must not create the state directory.
    assert not agent_module.state_dir().exists()


# --- start ----------------------------------------------------------------------


def test_start_creates_secure_state_dir_and_launches_agent(
    fake_lifecycle: Path,
) -> None:
    result = agent_module.start(timeout=5.0)
    assert result["running"] is True
    assert result["agent_version"] == "test-fake"
    assert result["trusted"] is True
    assert result["protocol"] == 1
    assert isinstance(result["pid"], int)

    paths = agent_module.agent_paths()
    assert paths.state_dir.stat().st_mode & 0o777 == 0o700
    assert paths.socket.stat().st_mode & 0o777 == 0o600
    assert _pid_file_contents(paths.pid_file) == result["pid"]


def test_start_is_idempotent(fake_lifecycle: Path) -> None:
    first = agent_module.start(timeout=5.0)
    assert len(_REGISTRY) == 1
    second = agent_module.start(timeout=5.0)
    assert second["pid"] == first["pid"]
    assert len(_REGISTRY) == 1  # no second agent spawned


def test_start_recovers_stale_pidfile_and_socket(fake_lifecycle: Path) -> None:
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text("999999\n")  # not in the fake registry -> reads as dead
    paths.socket.write_bytes(b"")  # leftover regular file where the socket should be

    result = agent_module.start(timeout=5.0)

    assert result["running"] is True
    assert result["pid"] != 999999
    assert paths.socket.is_socket()


def test_start_raises_agent_unavailable_when_build_fails(
    fake_lifecycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**kwargs: Any) -> Path:
        raise agent_module.AgentUnavailableError("no toolchain")

    monkeypatch.setattr(agent_module, "_build_executable", _boom)

    with pytest.raises(agent_module.AgentUnavailableError, match="no toolchain"):
        agent_module.start(timeout=2.0)

    paths = agent_module.agent_paths()
    assert not paths.pid_file.exists()
    assert not paths.socket.exists()


def test_start_raises_agent_unavailable_when_never_ready(
    fake_lifecycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _DeadOnArrival:
        pid = 987654321  # never a real pid; never binds a socket

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(
        agent_module, "_spawn_process", lambda binary, paths: _DeadOnArrival()
    )

    with pytest.raises(
        agent_module.AgentUnavailableError, match="did not become ready"
    ):
        agent_module.start(timeout=0.3)

    paths = agent_module.agent_paths()
    assert not paths.pid_file.exists()
    assert not paths.socket.exists()


def test_concurrent_start_is_serialized_by_the_lock(fake_lifecycle: Path) -> None:
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            agent_module.AgentUnavailableError, match="Timed out waiting"
        ):
            agent_module.start(timeout=0.3)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- stop -------------------------------------------------------------------------


def test_stop_terminates_and_removes_socket_and_pidfile(fake_lifecycle: Path) -> None:
    started = agent_module.start(timeout=5.0)
    paths = agent_module.agent_paths()
    assert paths.pid_file.exists()
    assert paths.socket.exists()

    stopped = agent_module.stop(timeout=5.0)

    assert stopped["running"] is False
    assert not paths.pid_file.exists()
    assert not paths.socket.exists()
    assert started["pid"] not in _REGISTRY
    assert agent_module.status()["running"] is False


def test_stop_is_idempotent_when_never_started(fake_lifecycle: Path) -> None:
    result = agent_module.stop(timeout=2.0)
    assert result["running"] is False


def test_stop_is_idempotent_when_already_stopped(fake_lifecycle: Path) -> None:
    agent_module.start(timeout=5.0)
    agent_module.stop(timeout=5.0)
    # Calling stop() again on an already-clean state must not raise.
    result = agent_module.stop(timeout=2.0)
    assert result["running"] is False


# --- ensure_running -----------------------------------------------------------------


def test_ensure_running_starts_once_and_reuses_thereafter(fake_lifecycle: Path) -> None:
    first = agent_module.ensure_running(timeout=5.0)
    assert first["running"] is True
    assert len(_REGISTRY) == 1

    second = agent_module.ensure_running(timeout=5.0)
    assert second["pid"] == first["pid"]
    assert len(_REGISTRY) == 1


def test_ensure_running_propagates_agent_unavailable(
    fake_lifecycle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**kwargs: Any) -> Path:
        raise agent_module.AgentUnavailableError("no toolchain")

    monkeypatch.setattr(agent_module, "_build_executable", _boom)

    with pytest.raises(agent_module.AgentUnavailableError):
        agent_module.ensure_running(timeout=2.0)


# --- _terminate_pid escalation (direct, no fake process needed) ---------------------


def test_terminate_pid_escalates_to_sigkill_when_sigterm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    def fake_alive(pid: int) -> bool:
        # "Ignores" SIGTERM: stays alive until a SIGKILL has been recorded.
        return not any(sig == signal.SIGKILL for _, sig in calls)

    monkeypatch.setattr(agent_module.os, "kill", fake_kill)
    monkeypatch.setattr(agent_module, "_process_alive", fake_alive)

    result = agent_module._terminate_pid(4321, timeout=0.1)

    assert result is True
    assert calls[0] == (4321, signal.SIGTERM)
    assert (4321, signal.SIGKILL) in calls


def test_terminate_pid_is_a_noop_when_already_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        agent_module.os, "kill", lambda pid, sig: calls.append((pid, sig))
    )
    monkeypatch.setattr(agent_module, "_process_alive", lambda pid: False)

    result = agent_module._terminate_pid(4321, timeout=0.1)

    assert result is True
    assert calls == []


# --- _process_alive: reap-before-kill -------------------------------------------


def test_process_alive_reaps_exited_child_via_waitpid_before_kill_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that already exited but was never waited on is a zombie:
    kill(pid, 0) alone keeps reporting it alive forever. `_process_alive`
    must call waitpid(WNOHANG) first and trust that over a bare kill
    probe -- and must not even need to call kill() once waitpid reaps it.
    """
    calls: list[str] = []

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        calls.append("waitpid")
        assert options == os.WNOHANG
        return (pid, 0)  # already exited; successfully reaped just now

    def fake_kill(pid: int, sig: int) -> None:
        calls.append("kill")

    monkeypatch.setattr(agent_module.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(agent_module.os, "kill", fake_kill)

    assert agent_module._process_alive(4321) is False
    assert calls == ["waitpid"]


def test_process_alive_tolerates_a_pid_that_is_not_its_own_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most pids `_process_alive` is asked about are not children of this
    process (e.g. an agent pid merely read from a pidfile, possibly
    written by an earlier Python process). waitpid(WNOHANG) on a
    non-child pid raises ChildProcessError, which must be swallowed and
    fall through to the ordinary kill(pid, 0) probe rather than
    propagating or misreporting liveness.
    """

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        raise ChildProcessError(10, "No child processes")

    def fake_kill(pid: int, sig: int) -> None:
        return None  # process exists and is signalable

    monkeypatch.setattr(agent_module.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(agent_module.os, "kill", fake_kill)

    assert agent_module._process_alive(4321) is True


def test_process_alive_does_not_mistake_a_running_child_for_a_reaped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """waitpid(WNOHANG) returns ``(0, 0)`` -- not ``pid`` -- while a real
    child is still running; that must never be read as "reaped and dead".
    """

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        return (0, 0)

    def fake_kill(pid: int, sig: int) -> None:
        return None

    monkeypatch.setattr(agent_module.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(agent_module.os, "kill", fake_kill)

    assert agent_module._process_alive(4321) is True


# --- _read_pid_file: pid <= 1 rejection ------------------------------------------


@pytest.mark.parametrize("raw", ["0", "1", "-1", "-999999"])
def test_read_pid_file_rejects_pid_le_1(tmp_path: Path, raw: str) -> None:
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text(f"{raw}\n", encoding="utf-8")
    assert agent_module._read_pid_file(pid_file) is None


def test_read_pid_file_accepts_a_real_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("2\n", encoding="utf-8")
    assert agent_module._read_pid_file(pid_file) == 2


# --- _terminate_pid: revalidate gates the SIGKILL escalation ---------------------


def test_terminate_pid_skips_sigkill_when_revalidate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``revalidate`` is the identity recheck gating the SIGKILL
    escalation: if it reports the pid no longer verified (e.g. the
    original process already exited and the kernel handed the pid to
    something else during the SIGTERM wait), no SIGKILL may be sent.
    """
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    def fake_alive(pid: int) -> bool:
        # Stays "alive" throughout: SIGTERM is ignored, and nothing ever
        # sends a SIGKILL to make it exit either.
        return True

    monkeypatch.setattr(agent_module.os, "kill", fake_kill)
    monkeypatch.setattr(agent_module, "_process_alive", fake_alive)

    result = agent_module._terminate_pid(4321, timeout=0.1, revalidate=lambda: False)

    assert result is False
    assert calls == [(4321, signal.SIGTERM)]  # SIGTERM only, never SIGKILL


def test_terminate_pid_sends_sigkill_when_revalidate_confirms_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    def fake_alive(pid: int) -> bool:
        return not any(sig == signal.SIGKILL for _, sig in calls)

    monkeypatch.setattr(agent_module.os, "kill", fake_kill)
    monkeypatch.setattr(agent_module, "_process_alive", fake_alive)

    result = agent_module._terminate_pid(4321, timeout=0.1, revalidate=lambda: True)

    assert result is True
    assert calls[0] == (4321, signal.SIGTERM)
    assert (4321, signal.SIGKILL) in calls


# --- stop(): identity-gated signaling (stale/reused/mismatched pids) -------------


def test_stop_clears_a_zero_or_negative_pidfile_without_signaling_anything(
    fake_lifecycle: Path,
) -> None:
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text("0\n", encoding="utf-8")

    result = agent_module.stop(timeout=2.0)

    assert result["running"] is False
    assert not paths.pid_file.exists()


def test_stop_refuses_to_signal_a_stale_reused_pid_with_no_live_agent(
    fake_lifecycle: Path,
) -> None:
    """A pidfile can outlive the agent it named -- the OS is free to hand
    that exact number to a totally unrelated process later. If nothing is
    listening on the agent socket to vouch for it, `stop()` must never
    send that pid a signal, only clear the stale record.
    """
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    reused_pid = 424242
    # Deliberately registered as "alive" -- proving the point that even a
    # pid a liveness probe would call live is never enough on its own;
    # only a verified live socket handshake may license a signal.
    _REGISTRY[reused_pid] = object()
    paths.pid_file.write_text(f"{reused_pid}\n", encoding="utf-8")
    try:
        result = agent_module.stop(timeout=2.0)

        assert result["running"] is False
        assert not paths.pid_file.exists()
        # `_terminate_pid` (the fake) was never invoked for this pid: it
        # is still "alive" in the registry because nothing signaled it.
        assert reused_pid in _REGISTRY
    finally:
        _REGISTRY.pop(reused_pid, None)


def test_stop_refuses_to_signal_a_stale_reused_pid_behind_an_orphaned_socket(
    fake_lifecycle: Path,
) -> None:
    """The more realistic flavor of pid reuse: the crashed agent's own
    socket file is still sitting on disk, but nothing is listening on it
    any more, while the pidfile's number now happens to belong to some
    unrelated, still-alive process.
    """
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    reused_pid = 424243
    _REGISTRY[reused_pid] = object()
    paths.pid_file.write_text(f"{reused_pid}\n", encoding="utf-8")
    paths.socket.write_bytes(b"")  # a leftover, non-socket file at the path
    try:
        result = agent_module.stop(timeout=2.0)

        assert result["running"] is False
        assert not paths.pid_file.exists()
        assert not paths.socket.exists()
        assert reused_pid in _REGISTRY
    finally:
        _REGISTRY.pop(reused_pid, None)


def test_stop_refuses_to_signal_on_live_socket_ping_pid_mismatch(
    fake_lifecycle: Path,
) -> None:
    """A live, real agent is answering on the socket, but its own ping
    reports a *different* pid than the one the pidfile names -- e.g. a
    fast restart reused the socket path under a new pid and left the old
    pidfile behind. `stop()` must not treat "some agent is up" as license
    to signal whatever pid happens to be on file.
    """
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeLifecycleAgent(paths)
    stale_pid = fake.pid + 1
    _REGISTRY[stale_pid] = fake  # `_process_alive(stale_pid)` reads as alive
    paths.pid_file.write_text(f"{stale_pid}\n", encoding="utf-8")
    try:
        result = agent_module.stop(timeout=2.0)

        assert result["running"] is False
        assert not paths.pid_file.exists()
        assert not paths.socket.exists()
        assert stale_pid in _REGISTRY  # never signaled/popped
    finally:
        _REGISTRY.pop(stale_pid, None)
        fake.terminate()


def test_stop_signals_when_live_socket_ping_confirms_the_exact_pid(
    fake_lifecycle: Path,
) -> None:
    """The clean, expected case: the pidfile's pid is exactly what the
    live agent's own ping reports back, so `stop()` proceeds to
    terminate it -- the mirror image of the mismatch/stale-pid tests
    above.
    """
    paths = agent_module.agent_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeLifecycleAgent(paths)
    _REGISTRY[fake.pid] = fake
    paths.pid_file.write_text(f"{fake.pid}\n", encoding="utf-8")

    result = agent_module.stop(timeout=2.0)

    assert result["running"] is False
    assert not paths.pid_file.exists()
    assert not paths.socket.exists()
    assert fake.pid not in _REGISTRY  # _terminate_pid really ran
