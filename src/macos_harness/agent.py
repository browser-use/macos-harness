"""Launch and own one private, per-caller native accessibility agent child.

The native agent is an optional, separately-built SwiftPM executable (see
``native/macos-harness-agent`` at the repository root) that owns every
Accessibility call on one serial dispatch queue and speaks NDJSON over a
socket (see ``native.py`` for that wire protocol). There is no shared
daemon here, no pidfile, no socket path, no lockfile, no log file, no
LaunchAgent, and nothing else discoverable by another process: every
``MacOS(backend="native"|"auto")`` instance that actually needs the agent
calls ``launch()`` once and gets back its own private child, reachable
only through an unnamed ``socket.socketpair()`` this call creates and
hands to the child as a single inherited file descriptor (``--fd N``).
Nothing survives past whatever calls ``close()`` on the returned
``AgentSession`` -- directly, via ``MacOS.close()``/its context manager,
or automatically once a ``MacOS`` instance is unreachable or the
interpreter exits.

``launch()`` is also the sole place that decides, once, whether a launch
failure is *available-elsewhere* (``AgentUnavailableError`` -- no
executable could be resolved, or one was spawned but never produced a
valid handshake response: a spawn failure, a crash, an EOF, or a timeout)
versus a *hard failure* (any other ``native.NativeProtocolError`` -- a
real response came back and its protocol version or pid was wrong).
Routing code (``MacOS._acquire_native``) uses exactly that distinction to
decide whether ``backend="auto"`` may fall back to the local Accessibility
APIs; once a valid response has been received, nothing here ever falls
back, reconnects, or respawns.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import shutil
import socket
import subprocess
import threading
import weakref
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .native import NativeClient, NativeConnectionError

_EXECUTABLE_NAME = "macos-harness-agent"

#: Explicit override for the agent executable path, checked before the
#: bundled binary or a local SwiftPM build. Unset by default.
_AGENT_BIN_ENV = "MACOS_HARNESS_AGENT_BIN"

_DEFAULT_LAUNCH_TIMEOUT = 10.0
_DEFAULT_CLOSE_TIMEOUT = 5.0
#: Brief window after closing the socket for a well-behaved child to see
#: EOF and exit on its own, before escalating to an explicit signal.
_EOF_GRACE_PERIOD = 0.5
#: Window to reap a SIGKILL, which cannot be ignored or take long.
_KILL_GRACE_PERIOD = 2.0


class AgentUnavailableError(RuntimeError):
    """The native agent could not be resolved, spawned, or ever produced a
    valid handshake response.

    Raised by ``launch()`` for every reason the agent isn't usable
    *before* a real response was ever received: no override, bundled, or
    buildable executable; a Swift toolchain or build failure; a spawn
    failure; or the freshly spawned child crashing, closing its socket,
    or simply never answering the handshake ping before ``timeout``
    elapses. Routing code uses this single type to decide ``auto``
    fallback (catch it, use the Python engine) versus ``native`` hard
    failure (let it propagate) -- never for a failure discovered *after*
    a real response came back (see ``native.NativeProtocolError``,
    always a hard failure regardless of backend).
    """


# --- fd hygiene ---------------------------------------------------------------


def _normalize_child_fd(child_sock: socket.socket, *, min_fd: int = 3) -> socket.socket:
    """Duplicate ``child_sock`` onto a fresh fd at or above ``min_fd`` if
    its own fd landed below that, closing the low-numbered original --
    otherwise returns ``child_sock`` unchanged.

    Only possible when this process had already closed one of its own
    standard streams before the ``socket.socketpair()`` call that
    produced ``child_sock``: the OS always hands out the lowest free fd
    number, so that gap would otherwise go straight to this brand-new
    socket, landing it exactly where the *child's own* (separately
    ``DEVNULL``-redirected) stdin/stdout/stderr will live once ``Popen``
    sets those up during exec. Deliberately local and narrow: this never
    reopens or otherwise touches fds 0/1/2 themselves (unlike filling
    them with ``/dev/null`` would -- a permanent, process-wide change
    nobody asked for), and never touches the *parent* endpoint of the
    same socketpair, which may stay at any fd number.

    The duplicate is made with ``F_DUPFD_CLOEXEC`` (non-inheritable, so
    it can never leak into some other, unrelated child this process
    spawns before ``Popen`` runs for the agent); ``Popen``'s own
    ``pass_fds`` handling clears that flag again for exactly the one fd
    the agent child needs to inherit.

    ``min_fd`` defaults to 3 in production; tests pass a much larger
    value to exercise the real duplication path on an ordinary fd
    without ever touching this process's actual stdio.
    """
    fd = child_sock.fileno()
    if fd >= min_fd:
        return child_sock
    new_fd = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, min_fd)
    child_sock.close()
    return socket.socket(fileno=new_fd)


# --- executable resolution ---------------------------------------------------


def _repo_native_package_dir() -> Path:
    # src/macos_harness/agent.py -> src/macos_harness -> src -> repo root
    return Path(__file__).resolve().parents[2] / "native" / _EXECUTABLE_NAME


def _bundled_executable_path() -> Path:
    return Path(__file__).resolve().parent / "bin" / _EXECUTABLE_NAME


def _local_release_path() -> Path:
    return _repo_native_package_dir() / ".build" / "release" / _EXECUTABLE_NAME


def _native_manifest_and_sources() -> Iterator[Path]:
    """Every file whose mtime determines whether the local release binary
    built from it is stale: the package manifest and its Swift sources.
    Test sources are deliberately excluded -- they never change what the
    built executable itself does.
    """
    package_dir = _repo_native_package_dir()
    manifest = package_dir / "Package.swift"
    if manifest.exists():
        yield manifest
    sources = package_dir / "Sources"
    if sources.is_dir():
        yield from sources.rglob("*.swift")


def _has_production_sources() -> bool:
    """``True`` only when the native package's manifest and at least one
    production Swift source file (under ``Sources/``, never ``Tests/``)
    both exist on disk -- the minimum positive evidence needed before
    ever trusting an existing ``.build/release`` binary as a fresh build
    product instead of a stale or unrelated leftover file. An installed
    wheel or a checkout missing ``native/`` entirely must never treat a
    stray binary at that path as up to date just because it happens to
    exist.
    """
    package_dir = _repo_native_package_dir()
    if not (package_dir / "Package.swift").exists():
        return False
    sources = package_dir / "Sources"
    if not sources.is_dir():
        return False
    return any(sources.rglob("*.swift"))


def _is_local_release_fresh(binary: Path) -> bool:
    """``True`` when ``binary`` exists and is newer than every native
    source file and the package manifest -- i.e. a ``swift build`` right
    now would be a no-op, so skip even invoking ``swift`` to confirm it.
    """
    if not _has_production_sources():
        return False
    try:
        binary_mtime = binary.stat().st_mtime
    except OSError:
        return False
    for source in _native_manifest_and_sources():
        try:
            if source.stat().st_mtime > binary_mtime:
                return False
        except OSError:
            return False
    return True


def _build_executable(*, configuration: str = "release") -> Path:
    """Build the native agent and return its executable path.

    Raises ``AgentUnavailableError`` with a clear, actionable message when
    the Swift toolchain is missing, the package source isn't checked out
    (e.g. an installed wheel with no ``native/`` directory), or the build
    itself fails. After a successful release build, the conventional
    ``.build/release/<name>`` path is trusted directly when present --
    ``swift build --show-bin-path`` is only ever invoked (a second,
    otherwise-unnecessary process) as a fallback for a non-standard
    build configuration or output layout.
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
        raise AgentUnavailableError(f"swift build failed for the native agent:\n{detail}")
    if configuration == "release":
        direct = _local_release_path()
        if direct.is_file():
            return direct
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


def _ensure_local_release(*, force: bool = False) -> Path:
    """Return a ready-to-run local SwiftPM release build of the agent.

    Reuses the existing ``.build/release`` binary when it is already
    newer than every native source file and the package manifest, so
    resolving the agent never pays for a ``swift build`` invocation
    unless the Swift source actually changed. ``force=True`` always
    rebuilds.
    """
    binary = _local_release_path()
    if not force and _is_local_release_fresh(binary):
        return binary
    return _build_executable()


def _validate_executable(path: Path, *, source: str) -> Path:
    """Resolve ``path`` to an absolute, canonical, regular, executable file.

    Applied uniformly to whichever tier resolved the agent binary --
    override, bundled, or a fresh local build -- right before it is ever
    handed to ``Popen``. A symlink chain is fully resolved (never trusted
    as given), and a relative path, a directory, a non-regular file
    (device, FIFO, socket, ...), or a file missing the execute bit for
    this process is rejected here with a clear message naming ``source``
    -- never left to surface as an opaque ``Popen`` failure.
    """
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AgentUnavailableError(
            f"{source} does not name an existing file: {path}"
        ) from exc
    if not resolved.is_file():
        raise AgentUnavailableError(
            f"{source} does not name a regular file: {resolved}"
        )
    if not os.access(resolved, os.X_OK):
        raise AgentUnavailableError(f"{source} is not executable: {resolved}")
    return resolved


def _resolve_executable() -> Path:
    """Resolve the agent executable: an explicit override, the bundled
    binary, or a local SwiftPM release artifact -- reused as-is when
    fresh, rebuilt when missing or stale. The override and bundled tiers
    are trusted exactly as given and never trigger a rebuild, but every
    tier is validated (canonical, regular, executable) before it is ever
    handed to ``Popen``.
    """
    override = os.environ.get(_AGENT_BIN_ENV)
    if override:
        return _validate_executable(
            Path(override).expanduser(), source=f"{_AGENT_BIN_ENV}={override}"
        )
    bundled = _bundled_executable_path()
    if bundled.is_file():
        return _validate_executable(bundled, source="the bundled agent binary")
    return _validate_executable(
        _ensure_local_release(), source="the local SwiftPM release build"
    )


# --- session -------------------------------------------------------------------


@dataclass
class AgentSession:
    """One private native agent child, bound to exactly the caller that
    launched it -- never shared, discovered, or reused by another
    ``launch()`` call.
    """

    process: subprocess.Popen[bytes]
    client: NativeClient
    #: Serializes ``close()`` against itself: two threads racing to close
    #: the *same* session must never both observe the child as "still
    #: running" and both escalate a signal to it independently.
    _close_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def pid(self) -> int:
        return self.process.pid

    def close(self, *, timeout: float = _DEFAULT_CLOSE_TIMEOUT) -> None:
        with self._close_lock:
            _close_session(self.process, self.client, timeout=timeout)


def _reap_session_on_terminal_failure(session_ref: weakref.ref[AgentSession]) -> None:
    """Weakly-referenced callback armed on a launched session's client.

    ``launch()`` arms this on ``client`` right after a successful
    handshake, via ``functools.partial`` over a plain ``weakref.ref`` --
    never a bound method -- so the *client* never holds a strong
    reference back to its own owning ``AgentSession`` (which in turn
    holds the client strongly): that would be a self-pinning cycle,
    keeping both alive under pure refcounting for as long as anything,
    anywhere, still held the client.

    Fired by ``NativeClient`` itself when a request fails at the
    transport level after that handshake -- the one signal that the
    agent process on the other end is genuinely gone or unreachable, not
    merely that this call site chose to stop talking to it. Reaps the
    still-live ``AgentSession`` exactly like an explicit ``close()``
    would (idempotent, safe to run more than once, never reconnecting or
    falling back); a dead weakref means the session was already closed
    or collected through the normal path, so there is nothing left to do.
    """
    session = session_ref()
    if session is not None:
        session.close()


def _spawn(binary: Path, child_sock: socket.socket) -> subprocess.Popen[bytes]:
    """Launch ``binary --fd <child_sock's fd>`` with only that one fd
    inherited (``pass_fds``; every other descriptor stays closed), its
    own stdin/stdout/stderr all redirected to ``DEVNULL`` -- nothing it
    ever writes can land in this process's own streams, and nothing here
    can ever write into its stdin -- in its own session so a signal
    delivered to this process's controlling terminal or process group
    never reaches it directly -- only this module's own explicit cleanup
    ever signals it.
    """
    fd = child_sock.fileno()
    return subprocess.Popen(
        [str(binary), "--fd", str(fd)],
        pass_fds=(fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch(*, timeout: float = _DEFAULT_LAUNCH_TIMEOUT) -> AgentSession:
    """Launch a brand-new, private native agent child and verify it.

    Resolves the executable, creates an ``AF_UNIX`` ``socket.socketpair()``,
    spawns the executable with only the child's endpoint inherited, closes
    that endpoint in this process immediately, and performs the protocol
    handshake over the parent's own endpoint: the ping response's own
    ``pid`` field must equal exactly the pid ``Popen`` itself reports for
    the process this call just spawned. ``timeout`` bounds only that
    handshake; every request the returned session's client makes
    afterward uses its own, tighter steady-state default instead.

    Ownership is total, including across a partial launch and even a
    ``BaseException`` (a ``KeyboardInterrupt`` landing mid-handshake,
    say): on any failure, both socketpair endpoints are closed and the
    spawned process (if any) is terminated and reaped before the
    exception ever reaches the caller -- nothing is left for it to clean
    up. Once a session is successfully returned, a later transport
    failure discovered by that session's own client (never a fresh
    ``connect()`` -- there is no reconnect or respawn here) reaps it the
    same way, on its own, without the caller having to notice.

    Raises ``AgentUnavailableError`` for every failure discovered
    *before* a real, valid handshake response came back (no usable
    executable, a spawn failure, or the child crashing, closing its
    socket, or simply never answering before ``timeout``) -- the one
    case routing code may treat as "try the local Accessibility APIs
    instead". Any other failure -- a response arrived but its protocol
    version or pid was wrong -- raises the underlying
    ``native.NativeProtocolError`` unchanged; that is always a hard
    failure, never grounds for falling back, reconnecting, or
    respawning.
    """
    binary = _resolve_executable()
    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_sock = _normalize_child_fd(child_sock)
    try:
        process = _spawn(binary, child_sock)
    except BaseException as exc:
        parent_sock.close()
        if isinstance(exc, OSError):
            raise AgentUnavailableError(f"Could not launch {binary}: {exc}") from exc
        raise
    finally:
        child_sock.close()

    client = NativeClient(sock=parent_sock, connect_timeout=timeout, expected_pid=process.pid)
    try:
        client.connect()
    except BaseException as exc:
        _close_session(process, client)
        if isinstance(exc, NativeConnectionError):
            raise AgentUnavailableError(
                f"Native agent {binary} did not answer within {timeout}s: {exc}"
            ) from exc
        raise

    session = AgentSession(process=process, client=client)
    client._arm_terminal_failure_hook(
        functools.partial(_reap_session_on_terminal_failure, weakref.ref(session))
    )
    return session


def _close_session(
    process: subprocess.Popen[bytes],
    client: NativeClient,
    *,
    timeout: float = _DEFAULT_CLOSE_TIMEOUT,
) -> None:
    """Total, idempotent teardown of one launched child.

    Closes the socket first, giving an already-running child a chance to
    see EOF and exit on its own; only escalates to an explicit
    terminate/kill for a child still alive after that brief wait -- and
    always through the exact ``Popen`` this call was given, never a bare
    pid re-derived from anywhere else, so cleanup can never land on an
    unrelated process. Safe to call more than once: a process this
    already reaped makes every subsequent ``poll()`` here an immediate,
    syscall-free no-op.

    Every signal is sent defensively: ``Popen.terminate()``/``.kill()``
    already refuse to signal a pid this same ``Popen`` object already
    knows has exited, but a child that exits in the narrow window
    between our own ``poll()`` check and the signal call itself is still
    possible -- ``ProcessLookupError`` from that race is swallowed
    rather than left to propagate, since it means there is nothing left
    to signal.
    """
    client.close()
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_EOF_GRACE_PERIOD)
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_KILL_GRACE_PERIOD)
