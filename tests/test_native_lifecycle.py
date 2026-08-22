"""CI-safe lifecycle tests for the native agent's Python-side supervisor.

These exercise ``macos_harness.agent``: executable resolution and build
freshness, ``launch()``'s spawn-and-handshake orchestration (via a small,
fully scripted fake agent -- a real subprocess speaking the real wire
protocol over a real inherited socketpair fd, never the Swift toolchain or
a real ``swift build``), and ``_close_session``'s cleanup escalation (via a
lightweight ``Popen``-like double, no subprocess needed at all). Routing
behavior at the ``MacOS`` level (``_acquire_native``, ``close()``, the
context manager) is covered separately in ``test_native_smoke.py``; raw
wire-protocol framing is covered in ``test_native_protocol.py``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import macos_harness.agent as agent_module
import macos_harness.native as native_module

# --- a tiny, real, controllable fake agent subprocess ----------------------
#
# Reads exactly the `--fd FD` argument convention `agent._spawn` invokes,
# adopts that inherited descriptor as a socket, and behaves according to
# the `FAKE_AGENT_MODE` environment variable `_spawn` inherits from this
# test process:
#
#   ok         -- answer every request it receives (its own real pid on
#                 the handshake), looping until the peer closes -- a
#                 real, well-behaved agent that stays usable for more
#                 than just the handshake.
#   wrong_pid  -- answer the handshake with a pid that is deliberately
#                 not its own; behaves like "ok" for anything after.
#   malformed  -- answer the handshake with a line that is not valid
#                 JSON at all, then close.
#   eof        -- close the connection immediately, without answering.
#   hang       -- never answer anything; idle (silently absorbing
#                 input) until the peer closes.
#   hang_after_handshake
#              -- answer the handshake normally, then silently absorb
#                 everything after without ever responding again --
#                 simulates a crash/hang discovered only on a later
#                 request, after the connection was already trusted.
_FAKE_AGENT_BODY = r'''
import json
import os
import socket
import sys


def main():
    args = sys.argv[1:]
    if args[:1] != ["--fd"]:
        sys.exit(2)
    fd = int(args[1])
    sock = socket.socket(fileno=fd)
    mode = os.environ.get("FAKE_AGENT_MODE", "ok")
    if mode == "eof":
        sock.close()
        return
    if mode == "hang":
        while sock.recv(65536):
            pass
        return
    buffer = b""
    first = True
    while True:
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buffer += chunk
        line, _, buffer = buffer.partition(b"\n")
        request = json.loads(line)
        if first and mode == "malformed":
            sock.sendall(b"this is not json\n")
            return
        if not first and mode == "hang_after_handshake":
            while sock.recv(65536):
                pass
            return
        pid = os.getpid()
        if first and mode == "wrong_pid":
            pid += 1
        result = {
            "protocol": 1,
            "agent_version": "fake-e2e",
            "pid": pid,
            "trusted": True,
            "uptime_s": 0.0,
        }
        payload = json.dumps({"id": request["id"], "ok": True, "result": result}) + "\n"
        sock.sendall(payload.encode())
        first = False


main()
'''


@pytest.fixture
def isolated_native_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Isolate executable-resolution tests from this checkout's own real
    bundled binary, real ``native/`` source tree, and any override left in
    the ambient environment -- returns the fake package directory the
    "local SwiftPM release" tier resolves against.
    """
    monkeypatch.delenv(agent_module._AGENT_BIN_ENV, raising=False)
    monkeypatch.setattr(
        agent_module, "_bundled_executable_path", lambda: tmp_path / "bin" / "unbundled"
    )
    package_dir = tmp_path / "native-src"
    monkeypatch.setattr(agent_module, "_repo_native_package_dir", lambda: package_dir)
    return package_dir


@pytest.fixture
def fake_agent_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake-macos-harness-agent"
    script.write_text(f"#!{sys.executable}\n{_FAKE_AGENT_BODY}", encoding="utf-8")
    script.chmod(0o700)
    return script


@pytest.fixture
def launch_with(monkeypatch: pytest.MonkeyPatch, fake_agent_script: Path):
    """Point ``agent._resolve_executable()`` at the fake agent script (the
    explicit-override tier) and run a real ``launch()`` against it under a
    given ``FAKE_AGENT_MODE``.
    """

    def _launch(mode: str, *, timeout: float = 5.0) -> agent_module.AgentSession:
        monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(fake_agent_script))
        monkeypatch.setenv("FAKE_AGENT_MODE", mode)
        return agent_module.launch(timeout=timeout)

    return _launch


# --- _is_local_release_fresh ------------------------------------------------


def _touch(path: Path, *, age_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"x")
    when = time.time() - age_s
    os.utime(path, (when, when))


def test_is_local_release_fresh_when_binary_newer_than_manifest_and_sources(
    isolated_native_paths: Path,
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Package.swift", age_s=20)
    _touch(package_dir / "Sources" / "Main.swift", age_s=20)
    _touch(binary, age_s=5)

    assert agent_module._is_local_release_fresh(binary) is True


def test_is_local_release_stale_when_a_source_file_is_newer_than_binary(
    isolated_native_paths: Path,
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(binary, age_s=20)
    _touch(package_dir / "Package.swift", age_s=15)
    _touch(package_dir / "Sources" / "Main.swift", age_s=1)  # newer than binary

    assert agent_module._is_local_release_fresh(binary) is False


def test_is_local_release_stale_when_manifest_is_newer_than_binary(
    isolated_native_paths: Path,
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(binary, age_s=20)
    _touch(package_dir / "Package.swift", age_s=1)  # newer than binary
    _touch(package_dir / "Sources" / "Main.swift", age_s=15)

    assert agent_module._is_local_release_fresh(binary) is False


def test_is_local_release_stale_when_binary_is_missing(
    isolated_native_paths: Path,
) -> None:
    binary = isolated_native_paths / ".build" / "release" / agent_module._EXECUTABLE_NAME
    assert agent_module._is_local_release_fresh(binary) is False


def test_is_local_release_fresh_ignores_test_sources(
    isolated_native_paths: Path,
) -> None:
    """A change under ``Tests/`` never invalidates freshness -- it can
    never affect what the built executable does."""
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Package.swift", age_s=20)
    _touch(package_dir / "Sources" / "Main.swift", age_s=20)
    _touch(binary, age_s=5)
    _touch(package_dir / "Tests" / "AgentTests" / "MainTests.swift", age_s=0)

    assert agent_module._is_local_release_fresh(binary) is True


def test_is_local_release_stale_when_manifest_missing_even_if_binary_and_sources_exist(
    isolated_native_paths: Path,
) -> None:
    """No ``Package.swift`` at all -- e.g. an installed wheel with no
    ``native/`` directory -- must never trust a stray binary as fresh,
    even if a ``Sources/`` tree happens to exist alongside it."""
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Sources" / "Main.swift", age_s=20)
    _touch(binary, age_s=5)

    assert agent_module._is_local_release_fresh(binary) is False


def test_is_local_release_stale_when_sources_dir_missing_or_empty(
    isolated_native_paths: Path,
) -> None:
    """A manifest with no production Swift source at all (missing
    ``Sources/``, or an empty one) must never trust a stray binary as
    fresh."""
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Package.swift", age_s=20)
    _touch(binary, age_s=5)

    assert agent_module._is_local_release_fresh(binary) is False

    (package_dir / "Sources").mkdir(parents=True, exist_ok=True)
    assert agent_module._is_local_release_fresh(binary) is False


# --- _ensure_local_release ---------------------------------------------------


def test_ensure_local_release_reuses_a_fresh_binary_without_building(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Package.swift", age_s=20)
    _touch(package_dir / "Sources" / "Main.swift", age_s=20)
    _touch(binary, age_s=5)

    def _boom(**kwargs: object) -> Path:
        raise AssertionError("must not build a binary already known fresh")

    monkeypatch.setattr(agent_module, "_build_executable", _boom)

    assert agent_module._ensure_local_release() == binary


def test_ensure_local_release_builds_when_stale(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(binary, age_s=20)
    _touch(package_dir / "Sources" / "Main.swift", age_s=1)  # newer -> stale
    rebuilt = package_dir / ".build" / "release" / "rebuilt-marker"
    calls: list[str] = []
    monkeypatch.setattr(
        agent_module, "_build_executable", lambda **kw: calls.append("build") or rebuilt
    )

    assert agent_module._ensure_local_release() == rebuilt
    assert calls == ["build"]


def test_ensure_local_release_builds_when_binary_missing(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = isolated_native_paths
    rebuilt = package_dir / ".build" / "release" / "built-marker"
    calls: list[str] = []
    monkeypatch.setattr(
        agent_module, "_build_executable", lambda **kw: calls.append("build") or rebuilt
    )

    assert agent_module._ensure_local_release() == rebuilt
    assert calls == ["build"]


def test_ensure_local_release_force_rebuilds_even_when_fresh(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = isolated_native_paths
    binary = package_dir / ".build" / "release" / agent_module._EXECUTABLE_NAME
    _touch(package_dir / "Package.swift", age_s=20)
    _touch(package_dir / "Sources" / "Main.swift", age_s=20)
    _touch(binary, age_s=5)  # objectively fresh
    calls: list[str] = []
    monkeypatch.setattr(
        agent_module, "_build_executable", lambda **kw: calls.append("build") or binary
    )

    agent_module._ensure_local_release(force=True)

    assert calls == ["build"]


# --- _build_executable: local release path reused before --show-bin-path ----


def test_build_executable_returns_local_release_path_directly_without_show_bin_path(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = isolated_native_paths
    manifest = package_dir / "Package.swift"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("// swift-tools-version:5.9\n")
    monkeypatch.setattr(agent_module.shutil, "which", lambda name: "/usr/bin/swift")

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--show-bin-path" in cmd:
            raise AssertionError(
                "must not invoke --show-bin-path when the conventional "
                "release path already exists after a successful build"
            )
        binary = agent_module._local_release_path()
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"fake-binary")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_module.subprocess, "run", _fake_run)

    result = agent_module._build_executable()

    assert result == agent_module._local_release_path()
    assert len(calls) == 1


def test_build_executable_falls_back_to_show_bin_path_when_conventional_path_missing(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_dir = isolated_native_paths
    manifest = package_dir / "Package.swift"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("// swift-tools-version:5.9\n")
    monkeypatch.setattr(agent_module.shutil, "which", lambda name: "/usr/bin/swift")

    alt_bin_dir = tmp_path / "alt-bin"
    alt_binary = alt_bin_dir / agent_module._EXECUTABLE_NAME
    alt_bin_dir.mkdir(parents=True, exist_ok=True)
    alt_binary.write_bytes(b"fake-binary")

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--show-bin-path" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(alt_bin_dir) + "\n", stderr="")
        # The conventional `.build/release/<name>` path is deliberately
        # never created here, forcing the fallback.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_module.subprocess, "run", _fake_run)

    result = agent_module._build_executable()

    assert result == alt_binary


# --- _resolve_executable: override / bundled / local-release order ----------


def test_resolve_executable_prefers_explicit_override_over_everything(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "override-agent"
    override.write_bytes(b"binary")
    override.chmod(0o700)
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(override))
    monkeypatch.setattr(
        agent_module,
        "_bundled_executable_path",
        lambda: (_ for _ in ()).throw(AssertionError("bundled tier must not run")),
    )
    monkeypatch.setattr(
        agent_module,
        "_ensure_local_release",
        lambda **kw: (_ for _ in ()).throw(AssertionError("local-release tier must not run")),
    )

    assert agent_module._resolve_executable() == override.resolve()


def test_resolve_executable_raises_when_override_path_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(missing))

    with pytest.raises(agent_module.AgentUnavailableError, match="MACOS_HARNESS_AGENT_BIN"):
        agent_module._resolve_executable()


def test_resolve_executable_rejects_a_non_executable_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    not_executable = tmp_path / "no-exec-bit"
    not_executable.write_bytes(b"binary")
    not_executable.chmod(0o600)
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(not_executable))

    with pytest.raises(agent_module.AgentUnavailableError, match="not executable"):
        agent_module._resolve_executable()


def test_resolve_executable_rejects_a_directory_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a_directory = tmp_path / "im-a-directory"
    a_directory.mkdir()
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(a_directory))

    with pytest.raises(agent_module.AgentUnavailableError, match="regular file"):
        agent_module._resolve_executable()


def test_resolve_executable_canonicalizes_a_symlinked_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_binary = tmp_path / "real-agent"
    real_binary.write_bytes(b"binary")
    real_binary.chmod(0o700)
    link = tmp_path / "link-to-agent"
    link.symlink_to(real_binary)
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(link))

    resolved = agent_module._resolve_executable()

    assert resolved == real_binary.resolve()
    assert resolved != link


def test_resolve_executable_prefers_bundled_over_local_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(agent_module._AGENT_BIN_ENV, raising=False)
    bundled = tmp_path / "bundled-agent"
    bundled.write_bytes(b"binary")
    bundled.chmod(0o700)
    monkeypatch.setattr(agent_module, "_bundled_executable_path", lambda: bundled)
    monkeypatch.setattr(
        agent_module,
        "_ensure_local_release",
        lambda **kw: (_ for _ in ()).throw(AssertionError("local-release tier must not run")),
    )

    assert agent_module._resolve_executable() == bundled.resolve()


def test_resolve_executable_falls_through_to_local_release(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_binary = tmp_path / "local-release-agent"
    real_binary.write_bytes(b"binary")
    real_binary.chmod(0o700)
    monkeypatch.setattr(agent_module, "_ensure_local_release", lambda **kw: real_binary)

    assert agent_module._resolve_executable() == real_binary.resolve()


def test_resolve_executable_raises_when_nothing_resolves(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**kwargs: object) -> Path:
        raise agent_module.AgentUnavailableError("no toolchain")

    monkeypatch.setattr(agent_module, "_build_executable", _boom)

    with pytest.raises(agent_module.AgentUnavailableError, match="no toolchain"):
        agent_module._resolve_executable()


# --- _normalize_child_fd: local child-endpoint fd hygiene -------------------


def test_normalize_child_fd_is_a_noop_when_already_at_or_above_min_fd() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        result = agent_module._normalize_child_fd(sock, min_fd=0)
        assert result is sock
    finally:
        sock.close()


def test_normalize_child_fd_duplicates_and_closes_the_original_below_min_fd() -> None:
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_fd = original.fileno()

    normalized = agent_module._normalize_child_fd(original, min_fd=50)
    try:
        assert normalized.fileno() >= 50
        assert normalized.fileno() != original_fd
        # The low-numbered original fd is really closed, not merely
        # abandoned -- using it now must fail.
        with pytest.raises(OSError):
            os.fstat(original_fd)
    finally:
        normalized.close()


# --- launch(): real subprocess, real socketpair fd handoff, real wire ------


def test_launch_succeeds_verifies_popen_pid_and_closes_cleanly(launch_with) -> None:
    session = launch_with("ok")
    try:
        assert session.process.poll() is None
        assert session.pid == session.process.pid
        assert session.client.connected is True
        assert session.client.ping()["agent_version"] == "fake-e2e"
    finally:
        session.close()

    assert session.process.poll() is not None  # reaped, not left running
    assert session.client.connected is False
    session.close()  # idempotent


def test_launch_raises_agent_unavailable_when_no_executable_resolves(
    isolated_native_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**kwargs: Any) -> Path:
        raise agent_module.AgentUnavailableError("no toolchain")

    monkeypatch.setattr(agent_module, "_build_executable", _boom)

    with pytest.raises(agent_module.AgentUnavailableError, match="no toolchain"):
        agent_module.launch()


def test_launch_raises_agent_unavailable_when_the_binary_cannot_be_spawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resolved path that is a regular, executable-bit-set file but not
    an actual executable format fails inside ``Popen`` itself -- a
    pre-handshake, spawn-level ``OSError`` -- not a resolution/permission
    problem (that's covered by the ``_validate_executable`` tests above)
    or a build problem."""
    unusable = tmp_path / "not-a-real-binary"
    unusable.write_text("#!/nonexistent-interpreter-xyz\nnot a real binary\n")
    unusable.chmod(0o700)
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(unusable))

    with pytest.raises(agent_module.AgentUnavailableError, match="Could not launch"):
        agent_module.launch(timeout=2.0)


def test_launch_falls_back_eligible_on_eof_before_a_valid_ping(launch_with) -> None:
    """The child answered by closing the connection outright, without a
    response -- exactly as unavailable as no agent listening at all."""
    with pytest.raises(agent_module.AgentUnavailableError):
        launch_with("eof")


def test_launch_falls_back_eligible_on_timeout_before_a_valid_ping(launch_with) -> None:
    with pytest.raises(agent_module.AgentUnavailableError):
        launch_with("hang", timeout=0.3)


def test_launch_hard_fails_on_pid_mismatch_and_still_reaps_the_child(
    launch_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete, well-formed response *did* come back -- just reporting
    the wrong pid. That is never fallback-eligible, unlike EOF/timeout."""
    captured: list[subprocess.Popen] = []
    real_spawn = agent_module._spawn

    def _spy_spawn(binary, child_sock):
        process = real_spawn(binary, child_sock)
        captured.append(process)
        return process

    monkeypatch.setattr(agent_module, "_spawn", _spy_spawn)

    with pytest.raises(native_module.NativeProtocolError) as excinfo:
        launch_with("wrong_pid")

    assert not isinstance(excinfo.value, native_module.NativeConnectionError)
    assert not isinstance(excinfo.value, agent_module.AgentUnavailableError)
    assert len(captured) == 1
    # The child must already be reaped (or reapable within a beat) -- not
    # left running because the pid check failed to clean up after itself.
    captured[0].wait(timeout=2.0)


def test_launch_hard_fails_on_malformed_ping_and_still_reaps_the_child(
    launch_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[subprocess.Popen] = []
    real_spawn = agent_module._spawn

    def _spy_spawn(binary, child_sock):
        process = real_spawn(binary, child_sock)
        captured.append(process)
        return process

    monkeypatch.setattr(agent_module, "_spawn", _spy_spawn)

    with pytest.raises(native_module.NativeProtocolError) as excinfo:
        launch_with("malformed")

    assert not isinstance(excinfo.value, native_module.NativeConnectionError)
    assert not isinstance(excinfo.value, agent_module.AgentUnavailableError)
    assert len(captured) == 1
    captured[0].wait(timeout=2.0)


def test_launch_never_leaks_the_child_socket_endpoint(launch_with) -> None:
    """A second, independent launch works fine -- proves the first launch's
    child-side fd was actually closed in the parent, not merely dropped."""
    first = launch_with("ok")
    try:
        second = agent_module.launch(timeout=5.0)
        try:
            assert second.pid != first.pid
        finally:
            second.close()
    finally:
        first.close()


def test_launch_reraises_a_non_oserror_baseexception_from_spawn_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``BaseException`` that is not ``OSError`` (e.g. a
    ``KeyboardInterrupt`` landing mid-spawn) must propagate completely
    unchanged -- never swallowed or reframed as ``AgentUnavailableError``
    -- while still closing the parent-side socketpair endpoint already
    created before the exception hit."""
    unusable = tmp_path / "whatever"
    unusable.write_bytes(b"x")
    unusable.chmod(0o700)
    monkeypatch.setenv(agent_module._AGENT_BIN_ENV, str(unusable))

    created: list[socket.socket] = []
    real_socketpair = socket.socketpair

    def _spy_socketpair(*args: object, **kwargs: object) -> tuple[socket.socket, socket.socket]:
        pair = real_socketpair(*args, **kwargs)
        created.append(pair[0])
        return pair

    monkeypatch.setattr(agent_module.socket, "socketpair", _spy_socketpair)
    monkeypatch.setattr(
        agent_module,
        "_spawn",
        lambda binary, child_sock: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agent_module.launch(timeout=2.0)

    assert len(created) == 1
    assert created[0].fileno() == -1  # the parent-side endpoint was closed


def test_launch_reraises_a_non_connection_baseexception_from_handshake_and_reaps_child(
    launch_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``BaseException`` from the handshake that is not
    ``NativeConnectionError`` must propagate unchanged -- never reframed
    as ``AgentUnavailableError``, which is reserved for
    ``NativeConnectionError`` alone -- while still reaping the spawned
    child."""
    captured: list[subprocess.Popen] = []
    real_spawn = agent_module._spawn

    def _spy_spawn(binary, child_sock):
        process = real_spawn(binary, child_sock)
        captured.append(process)
        return process

    monkeypatch.setattr(agent_module, "_spawn", _spy_spawn)

    class _SimulatedBug(RuntimeError):
        pass

    def _boom_connect(self) -> None:
        raise _SimulatedBug("simulated bug during connect")

    monkeypatch.setattr(native_module.NativeClient, "connect", _boom_connect)

    with pytest.raises(_SimulatedBug):
        launch_with("ok", timeout=2.0)

    assert len(captured) == 1
    captured[0].wait(timeout=2.0)


def test_terminal_transport_failure_after_handshake_reaps_session_without_explicit_close(
    launch_with,
) -> None:
    """A post-handshake transport failure -- here, the fake agent
    (in ``hang_after_handshake`` mode) answering the handshake normally
    but silently absorbing a follow-up request forever, indistinguishable
    from a crash -- must reap the session's child process on its own via
    the armed weak hook, without the caller ever calling
    ``session.close()``."""
    session = launch_with("hang_after_handshake", timeout=5.0)
    # Shrink the already-applied socket timeout directly so the
    # unanswered follow-up request below times out quickly instead of
    # waiting on the real (5s) request-timeout default.
    session.client._sock.settimeout(0.3)

    with pytest.raises(native_module.NativeConnectionError):
        session.client.list_apps()

    # No explicit session.close() anywhere above.
    session.process.wait(timeout=2.0)
    assert session.process.poll() is not None
    assert session.client.connected is False


# --- _close_session: cleanup escalation, idempotency, PID scoping ----------


class _FakeProcess:
    """Minimal stand-in for ``subprocess.Popen`` exposing only what
    ``_close_session`` touches: ``poll``/``wait``/``terminate``/``kill``.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exits_on_terminate = False
        self._exits_on_kill = False
        self._exited = False
        self._raise_on_terminate: BaseException | None = None
        self._raise_on_kill: BaseException | None = None

    def exit_on_terminate(self) -> None:
        self._exits_on_terminate = True

    def exit_on_kill(self) -> None:
        self._exits_on_kill = True

    def mark_exited(self) -> None:
        self._exited = True

    def raise_on_terminate(self, exc: BaseException) -> None:
        self._raise_on_terminate = exc

    def raise_on_kill(self, exc: BaseException) -> None:
        self._raise_on_kill = exc

    def poll(self) -> int | None:
        if self.returncode is None and self._exited:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        # A real `Popen.wait()` actively reaps and captures the exit
        # status itself -- it does not merely check a cache `poll()`
        # happens to have populated. Mirror that: `_exited` becoming
        # True (via `terminate()`/`kill()`/`mark_exited()`) must be
        # enough for `wait()` alone to observe and report it, with no
        # intervening `poll()` call required.
        if self.returncode is None:
            if self._exited:
                self.returncode = 0
            else:
                raise subprocess.TimeoutExpired(cmd="fake-agent", timeout=timeout or 0.0)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._raise_on_terminate is not None:
            raise self._raise_on_terminate
        if self._exits_on_terminate:
            self._exited = True

    def kill(self) -> None:
        self.kill_calls += 1
        if self._raise_on_kill is not None:
            raise self._raise_on_kill
        if self._exits_on_kill:
            self._exited = True


class _FakeClient:
    def __init__(self, *, on_close=None) -> None:
        self.close_calls = 0
        self._on_close = on_close

    def close(self) -> None:
        self.close_calls += 1
        if self._on_close is not None:
            self._on_close()


def test_close_session_skips_signaling_a_child_that_exits_on_eof_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=11111)
    client = _FakeClient(on_close=process.mark_exited)

    agent_module._close_session(process, client)

    assert client.close_calls == 1
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert process.returncode is not None


def test_close_session_terminates_a_child_still_alive_after_eof_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=22222)
    process.exit_on_terminate()
    client = _FakeClient()

    agent_module._close_session(process, client, timeout=2.0)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.returncode is not None


def test_close_session_escalates_to_kill_when_terminate_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    monkeypatch.setattr(agent_module, "_KILL_GRACE_PERIOD", 0.2)
    process = _FakeProcess(pid=33333)
    process.exit_on_kill()
    client = _FakeClient()

    agent_module._close_session(process, client, timeout=0.05)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode is not None


def test_close_session_is_fully_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=44444)
    process.exit_on_terminate()
    client = _FakeClient()

    agent_module._close_session(process, client, timeout=2.0)
    agent_module._close_session(process, client, timeout=2.0)

    assert client.close_calls == 2  # NativeClient.close() is itself a no-op the 2nd time
    assert process.terminate_calls == 1  # never re-signaled once reaped
    assert process.kill_calls == 0


def test_close_session_never_signals_a_different_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup is always scoped to the exact ``Popen`` object it is given
    -- never a bare pid that could, by construction or coincidence,
    collide with some other process this call has no business touching.
    """
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    target = _FakeProcess(pid=55555)
    target.exit_on_terminate()
    unrelated = _FakeProcess(pid=55555)  # same pid value, a distinct object
    client = _FakeClient()

    agent_module._close_session(target, client, timeout=2.0)

    assert target.terminate_calls == 1
    assert unrelated.terminate_calls == 0
    assert unrelated.kill_calls == 0
    assert unrelated.returncode is None


def test_close_session_tolerates_terminate_racing_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child can legitimately exit in the narrow window between our
    own ``poll()`` check and the ``terminate()`` call itself;
    ``ProcessLookupError`` from that race must never propagate."""
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=66601)
    process.raise_on_terminate(ProcessLookupError())
    client = _FakeClient()

    agent_module._close_session(process, client, timeout=0.2)  # must not raise

    assert process.terminate_calls == 1


def test_close_session_tolerates_kill_racing_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=66602)
    process.raise_on_kill(ProcessLookupError())
    client = _FakeClient()

    agent_module._close_session(process, client, timeout=0.05)  # must not raise

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_agent_session_close_delegates_to_close_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=66666)
    process.exit_on_terminate()
    client = _FakeClient()
    session = agent_module.AgentSession(process=process, client=client)

    assert session.pid == 66666
    session.close(timeout=2.0)

    assert client.close_calls == 1
    assert process.terminate_calls == 1


def test_agent_session_close_serializes_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads racing to close the *same* session must never both
    observe the child as "still running" and both escalate a signal to
    it independently."""
    monkeypatch.setattr(agent_module, "_EOF_GRACE_PERIOD", 0.05)
    process = _FakeProcess(pid=77777)
    process.exit_on_terminate()
    client = _FakeClient()
    session = agent_module.AgentSession(process=process, client=client)

    barrier = threading.Barrier(2)

    def _close() -> None:
        barrier.wait(timeout=2.0)
        session.close(timeout=2.0)

    threads = [threading.Thread(target=_close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert process.terminate_calls == 1
    assert client.close_calls == 2


# --- fork safety: never deadlock on an inherited lock, never signal a parent-owned Popen ---
#
# Both regressions below run `os.fork()` inside a small helper script launched
# via `subprocess.run`, never inside the pytest worker process itself. The
# worker process (this one) has other threads besides the one deliberate
# lock-holder the scenario needs -- pytest's own capture machinery, etc. --
# so forking it directly triggers CPython's "process is multi-threaded, fork()
# may lead to deadlocks" `DeprecationWarning` on every run, unrelated to
# anything this test is actually checking. A fresh, single-purpose child
# interpreter has exactly the threads this scenario deliberately starts, and
# its own `os.fork()` call still exercises the exact same production code
# against a real fork boundary; only that warning (emitted to the *helper's*
# own stderr, which the outer test never inspects) is what fork() moving
# there avoids.

_AGENT_SESSION_FORK_SCRIPT = r'''
import os
import sys
import threading
import time

import macos_harness.agent as agent_module


class _Process:
    def __init__(self, pid):
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


class _Client:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


process = _Process(pid=88888)
client = _Client()
session = agent_module.AgentSession(process=process, client=client)

# A background thread holds `_close_lock` across the fork, exactly like a
# real concurrent `close()` caller could -- the one scenario that would
# deadlock a forked child if `close()` acquired the lock before checking
# pid identity first.
lock_held = threading.Event()
release_lock = threading.Event()


def _hold_lock():
    with session._close_lock:
        lock_held.set()
        release_lock.wait(timeout=5.0)


holder = threading.Thread(target=_hold_lock)
holder.start()
if not lock_held.wait(timeout=5.0):
    print("SETUP_FAILED", flush=True)
    sys.exit(2)

child_pid = os.fork()
if child_pid == 0:
    # `close()` on a forked child's copy of this session must return
    # (never raise) without ever touching `process`/`client` -- nothing
    # here needs to catch an exception it should never throw in the
    # first place; an unexpected one crashes this child loudly instead
    # of being hidden, which os.waitpid() below still observes as a
    # (non-hanging) exit.
    start = time.monotonic()
    session.close(timeout=2.0)
    elapsed = time.monotonic() - start
    print(
        "CHILD_OK elapsed=%.3f terminate=%d kill=%d client_close=%d"
        % (elapsed, process.terminate_calls, process.kill_calls, client.close_calls),
        flush=True,
    )
    os._exit(0)

deadline = time.monotonic() + 5.0
exited = False
status = 0
while time.monotonic() < deadline:
    done_pid, status = os.waitpid(child_pid, os.WNOHANG)
    if done_pid == child_pid:
        exited = True
        break
    time.sleep(0.01)

release_lock.set()
holder.join(timeout=5.0)

if not exited:
    os.kill(child_pid, 9)
    os.waitpid(child_pid, 0)
    print("CHILD_TIMED_OUT", flush=True)
    sys.exit(3)

print(
    "PARENT_VIEW terminate=%d kill=%d child_exit_ok=%s"
    % (
        process.terminate_calls,
        process.kill_calls,
        os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0,
    ),
    flush=True,
)
'''


def test_agent_session_close_after_fork_never_hangs_or_signals_the_parent_popen() -> None:
    """A forked child inherits a byte-for-byte copy of a live ``AgentSession``,
    including a ``_close_lock`` some other thread might hold at the exact
    instant of ``fork()`` and a ``process`` that names a real pid the child
    does not actually own. ``close()`` must recognize the fork boundary
    before ever touching either: no hang waiting on a lock only a
    now-nonexistent thread could release, and no ``terminate()``/``kill()``
    aimed at a process this child has no business signaling -- the exact
    two failure modes ``_close_session`` exists to avoid even *within* one
    process, now also required *across* a fork. See ``_AGENT_SESSION_FORK_SCRIPT``
    above for why the fork itself happens in a helper subprocess rather than
    inline here.
    """
    if not hasattr(os, "fork"):
        pytest.skip("os.fork() is not available on this platform")

    result = subprocess.run(
        [sys.executable, "-c", _AGENT_SESSION_FORK_SCRIPT],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert result.returncode == 0, (
        f"helper subprocess failed (exit {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    lines = {line.split(" ", 1)[0]: line for line in result.stdout.strip().splitlines()}

    assert "CHILD_OK" in lines, result.stdout
    child_line = lines["CHILD_OK"]
    assert "terminate=0" in child_line, child_line
    assert "kill=0" in child_line, child_line
    assert "client_close=0" in child_line, child_line

    # The parent's own view is completely unaffected: the real (fake)
    # process it still owns was never signaled by the fork at all.
    assert "PARENT_VIEW" in lines, result.stdout
    parent_line = lines["PARENT_VIEW"]
    assert "terminate=0" in parent_line, parent_line
    assert "kill=0" in parent_line, parent_line
    assert "child_exit_ok=True" in parent_line, parent_line
