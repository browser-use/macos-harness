"""The helper: one long-lived process per user. Every session lives here.

The main thread runs the AppKit loop. The socket and the tool calls run on worker
threads. `main()` holds the start order and the exit order, and nobody else does.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import functools
import os
import re
import select
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable

from . import protocol, runloop, telemetry, tools
from .model import AgentSession, AppSession, AppTarget

IDLE_EXIT_S = 15 * 60.0  # no call for this long: exit
IDLE_TICK_S = 5.0
HOST_LOOK_S = 0.25  # how often a running call looks whether its agent still runs
IO_TIMEOUT_S = 10.0  # per frame, read and write
LOCK_WAIT_S = 5.0  # a helper that is on its way out still holds the lock
EXIT_CAP_S = protocol.CALL_TIMEOUT_S + 10.0  # a stuck exit ends here
# After one of these errors the helper exits, so the next one sees the new grant.
EXIT_AFTER = ("permissions_waiting", "permissions_missing")
# Processes between the client and the agent that are not the agent.
WRAPPERS = (
    "sh",
    "bash",
    "zsh",
    "dash",
    "fish",
    "env",
    "time",
    "timeout",
    "nice",
    "nohup",
    "uv",
    "xargs",
    "sudo",
    "sandbox-exec",
)
APP_BUNDLE = re.compile(r"(?<=\.app)(?=/)")  # after every ".app" path part


def log(*words: object) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *words, file=sys.stderr, flush=True)


# --- who is calling ------------------------------------------------------------------

_libproc = None


def process(pid: int) -> tuple[int, str, float] | None:
    """(parent pid, executable path, start time) of a process. None when it is gone."""
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo
    if _libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), info, 136) != 136:
        return None
    path = ctypes.create_string_buffer(4096)
    _libproc.proc_pidpath(pid, path, 4096)
    name = path.value or info.raw[64:96].split(b"\0")[0]  # no path: the short name
    seconds, micros = struct.unpack_from("QQ", info, 120)
    parent = struct.unpack_from("I", info, 16)[0]
    return parent, name.decode("utf-8", "replace"), seconds + micros / 1e6


def peer(conn: socket.socket) -> tuple[int, int]:
    """(pid, uid) of the process at the other end of a Unix socket."""
    cred = conn.getsockopt(0, 1, 76)  # SOL_LOCAL, LOCAL_PEERCRED: struct xucred
    pid = struct.unpack("i", conn.getsockopt(0, 2, 4))[0]  # LOCAL_PEERPID
    return pid, struct.unpack_from("II", cred)[1]


class Sessions:
    """One AgentSession per calling agent, one AppSession per agent and app."""

    def __init__(self, process=process):
        self.process = process
        self.revisions = None  # the Revisions class. None: elements.Revisions
        self._agents: dict[str, AgentSession] = {}
        self._born: dict[str, tuple[int, float]] = {}  # key -> the agent's process
        self._apps: dict[tuple[str, str], AppSession] = {}
        self._lock = threading.Lock()

    def _parents(self, pid: int) -> list[tuple[int, str, float]]:
        """(pid, path, start) of every parent of `pid`, nearest first."""
        chain, seen = [], {pid}
        found = self.process(pid)
        while found and found[0] > 0 and found[0] not in seen:
            pid = found[0]
            seen.add(pid)
            found = self.process(pid)
            if found:
                chain.append((pid, found[1], found[2]))
        return chain

    def agent(self, peer_pid: int, env: dict[str, str]) -> AgentSession:
        """The session of the agent behind one call. `env` replaces the last one."""
        key = env.get("MACOS_HARNESS_SESSION")
        with self._lock:
            agent = self._agents.get(key) if key else None
            parents = self._parents(peer_pid)
            if agent is None:
                born = None if key else _agent_process(parents)
                key = key or (f"{born[0]}:{born[1]}" if born else "no-agent")
                agent = self._agents.get(key)
            if agent is None:
                hosts = (b for _, path, _ in parents for b in _bundles(path))
                agent = AgentSession(key=key, hosts=frozenset(hosts))
                self._agents[key] = agent
                if born:
                    self._born[key] = born
            agent.env = dict(env)
        return agent

    def gone(self) -> list[AgentSession]:
        """Forget the agents whose process has exited, and return them."""
        with self._lock:
            born = dict(self._born)
        dead = [key for key, started in born.items() if not self._runs(started)]
        with self._lock:
            for key in dead:
                del self._born[key]
            return [self._agents.pop(key) for key in dead]

    def _runs(self, born: tuple[int, float]) -> bool:
        return (self.process(born[0]) or (0, "", None))[2] == born[1]

    def left(self, agent: AgentSession) -> Callable[[], bool]:
        """A check that turns True once the agent's turn ended (its turn-ended hook,
        Esc in Codex) or its process is gone. Some hosts leave an interrupted command
        running, so the socket of its call stays open."""
        turn, born = agent.turns_ended, self._born.get(agent.key)
        looked, gone = 0.0, False

        def check() -> bool:
            nonlocal looked, gone
            if born and not gone and time.monotonic() - looked >= HOST_LOOK_S:
                looked, gone = time.monotonic(), not self._runs(born)
            return gone or agent.turns_ended != turn

        return check

    def app(self, agent: AgentSession, target: AppTarget) -> AppSession:
        with self._lock:
            app = self._apps.get((agent.key, target.path))
            if app is None:
                revisions = self.revisions or tools.mods.elements.Revisions
                app = AppSession(agent, target, revisions=revisions())
                self._apps[agent.key, target.path] = app
        return app

    def apps(self, agent: AgentSession) -> list[AppSession]:
        with self._lock:
            return [app for key, app in self._apps.items() if key[0] == agent.key]

    def drop(self, agent: AgentSession) -> None:
        """Forget the agent's app sessions. The agent itself keeps its turn marks."""
        with self._lock:
            for key in [key for key in self._apps if key[0] == agent.key]:
                del self._apps[key]


def _agent_process(parents: list[tuple[int, str, float]]) -> tuple[int, float] | None:
    """(pid, start time) of the first parent that is not a shell or a wrapper."""
    for pid, path, start in parents:
        if os.path.basename(path).lstrip("-") not in WRAPPERS:
            return pid, start
    return None


def _bundles(path: str) -> list[str]:
    """Every app bundle a process path lies in: /A.app/Contents/MacOS/A -> /A.app."""
    return [path[: match.start()] for match in APP_BUNDLE.finditer(path)]


SESSIONS = Sessions()

# --- one call at a time per app, and the display stays awake --------------------------


class _Fifo:
    """A lock that serves waiters in arrival order."""

    def __init__(self):
        self._lock, self._waiting, self._busy = threading.Lock(), deque(), False

    def __enter__(self):
        with self._lock:
            if not self._busy:
                self._busy = True
                return
            turn = threading.Event()
            self._waiting.append(turn)
        turn.wait()

    def __exit__(self, *exc):
        with self._lock:
            if self._waiting:
                self._waiting.popleft().set()
            else:
                self._busy = False


class Awake:
    """One keep-the-display-awake assertion while any call runs. Holders are counted.

    Every call also tells the system that the Mac is in use: a sleeping display wakes.
    """

    def __init__(self, acquire=None, release=None, activity=None):
        self.acquire = acquire or _assert_awake
        self.release = release or _release_awake
        self.activity = activity or _declare_activity
        self._lock, self._holders, self._token = threading.Lock(), 0, None

    def __enter__(self):
        with self._lock:
            self._holders += 1
            if self._holders == 1:
                self._token = _quietly(self.acquire)
            _quietly(self.activity)

    def __exit__(self, *exc):
        with self._lock:
            self._holders -= 1
            if self._holders == 0 and self._token is not None:
                _quietly(self.release, self._token)
                self._token = None


AWAKE_TEXTS = (
    "PreventUserIdleDisplaySleep",
    "macOS Harness at work",
    "macOS Harness is working on a request and keeps the display on.",
    "macOS Harness: the agent is active",
)
_activity = ctypes.c_uint32(0)  # the system wants the same id again on the next call


@functools.cache
def _power():
    """IOKit's power calls, and AWAKE_TEXTS as the strings they take."""
    void, u32 = ctypes.c_void_p, ctypes.c_uint32
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    cf.CFStringCreateWithCString.restype = void
    cf.CFStringCreateWithCString.argtypes = [void, ctypes.c_char_p, u32]
    create = iokit.IOPMAssertionCreateWithDescription
    create.argtypes = [void] * 5 + [ctypes.c_double, void, ctypes.POINTER(u32)]
    iokit.IOPMAssertionDeclareUserActivity.argtypes = [void, u32, ctypes.POINTER(u32)]
    utf8 = 0x08000100
    texts = [cf.CFStringCreateWithCString(None, t.encode(), utf8) for t in AWAKE_TEXTS]
    return iokit, texts


def _assert_awake() -> int:
    iokit, (kind, name, reason, _) = _power()
    token = ctypes.c_uint32(0)
    iokit.IOPMAssertionCreateWithDescription(
        kind, name, None, reason, None, 0.0, None, ctypes.byref(token)
    )
    return token.value


def _release_awake(token: int) -> None:
    _power()[0].IOPMAssertionRelease(ctypes.c_uint32(token))


def _declare_activity() -> None:
    iokit, texts = _power()
    iokit.IOPMAssertionDeclareUserActivity(texts[3], 0, ctypes.byref(_activity))


AWAKE = Awake()
_queues: dict[str, _Fifo] = {}
_queues_lock = threading.Lock()


@contextlib.contextmanager
def app_queue(target: AppTarget):
    """Calls on one app run one at a time, in arrival order. Other apps do not wait."""
    with _queues_lock:
        queue = _queues.setdefault(target.path, _Fifo())
    with queue, AWAKE:
        yield


# --- the socket -----------------------------------------------------------------------


class _State:
    def __init__(self):
        self.changed = threading.Condition()
        self.running = 0  # tool calls in flight
        self.last_call = time.time()
        self.stopping = False
        self.listener: socket.socket | None = None
        self.stop_started = threading.Event()
        self.left = threading.Event()  # shutdown is done: the exit steps may run
        self.exited = threading.Event()  # the exit steps are done


STATE = _State()


def _quietly(fn, *args):
    """Run one step that must not stop the steps after it."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - logged, and the helper goes on
        log("step failed:", traceback.format_exc())
        return None


def serve(sock: socket.socket) -> None:
    """Accept clients until shutdown. One thread per connection."""
    STATE.listener = sock
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        if STATE.stopping:  # shutdown woke us with a connection of its own
            conn.close()
            sock.close()
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _handle(conn: socket.socket) -> None:
    """One connection carries one request and its reply."""
    with conn:
        try:
            conn.settimeout(IO_TIMEOUT_S)
            pid, uid = peer(conn)
            message = protocol.read(conn)
        except (EOFError, OSError, ValueError):
            return
        if not isinstance(message, dict):
            return
        answer, error_name = answer_for(message, pid, uid, _gone(conn))
        try:
            frame = protocol.pack(answer)
        except protocol.FrameTooLarge:
            frame = protocol.pack(protocol.error(message.get("id"), "responseTooLarge"))
        with contextlib.suppress(OSError):
            conn.sendall(frame)
        if message.get("method") == "shutdown":
            shutdown()
            threading.Event().wait()  # the client reads this socket until we are gone
        if error_name in EXIT_AFTER:
            shutdown()


def _gone(conn: socket.socket):
    """A check, asked before every event of an action, whether the client has gone. It
    sends nothing after its request, so its socket turns readable only when it closes:
    the host interrupted the tool (Ctrl+C, Esc) and the action stops right there. So
    does every action once the helper is told to stop (`macos-harness helper stop`)."""
    poller = select.poll()
    poller.register(conn, select.POLLIN | select.POLLHUP)
    return lambda: STATE.stopping or bool(poller.poll(0))


def answer_for(
    message: dict, pid: int, uid: int, abandoned=lambda: False
) -> tuple[dict, str | None]:
    """The reply to one message, and the name of the tool error in it, if any."""
    id, method = message.get("id"), message.get("method")
    if uid != os.getuid():
        return protocol.error(id, "notAuthenticated"), None
    if isinstance(id, bool) or not isinstance(id, (int, str)):
        return protocol.error(None, "invalidRequest"), None
    if method in ("ping", "shutdown"):
        return protocol.result(id, {**protocol.identity(), "pid": os.getpid()}), None
    if method != "request":
        return protocol.error(id, "methodNotFound"), None
    params = message.get("params") or {}
    theirs = {
        "apiVersion": params.get("callerApi"),
        "version": params.get("clientVersion"),
        "path": params.get("clientPath"),
    }
    if theirs != protocol.identity():
        return protocol.error(id, "versionMismatch", protocol.identity()), None
    deadline = params.get("deadlineMs", 0) / 1000
    if time.time() >= deadline:
        return protocol.error(id, "deadlineExceeded"), None
    env = params.get("env") or {}
    with _counted() as accepted:
        if not accepted:
            return protocol.error(id, "stopping"), None
        agent = SESSIONS.agent(pid, env)
        left, started = SESSIONS.left(agent), time.monotonic()
        try:
            outcome = tools.execute(
                params.get("argv") or [],
                params.get("stdin"),
                agent,
                params.get("cwd") or "/",
                deadline,
                lambda: abandoned() or left(),
            )
        except tools.NeedStdin:
            return protocol.result(id, {"needStdin": True}), None
    error, seconds = outcome.error, time.monotonic() - started
    # One line per call in helper.log. It names the command, never the app.
    log(f"call {outcome.command} exit={outcome.exit} {seconds * 1000:.0f} ms")
    _quietly(
        telemetry.capture_cli,
        outcome.command,
        outcome.exit == 0,
        seconds,
        env,
        error.name if error else None,
    )
    reply = protocol.reply(id, outcome.exit, outcome.stdout, outcome.stderr)
    return reply, error.name if error else None


@contextlib.contextmanager
def _counted():
    """Count one running call. Yields False, and counts nothing, once we are stopping."""
    with STATE.changed:
        accepted = not STATE.stopping
        STATE.running += accepted
        STATE.last_call = time.time()
    try:
        yield accepted
    finally:
        with STATE.changed:
            STATE.running -= accepted
            STATE.last_call = time.time()
            STATE.changed.notify_all()


def shutdown() -> None:
    """Stop accepting, let running calls finish, then let `main` leave its loop."""
    with STATE.changed:
        first, STATE.stopping = not STATE.stopping, True
    if first:
        STATE.stop_started.set()
        if STATE.listener is not None:
            # A close from here would not wake accept(). A connection does.
            waker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with waker, contextlib.suppress(OSError):
                waker.connect(STATE.listener.getsockname())
    with STATE.changed:
        STATE.changed.wait_for(lambda: STATE.running == 0)
    runloop.stop()
    STATE.left.set()


def idle_loop(clock=time) -> None:
    """Exit after IDLE_EXIT_S with no call, and let go of agents that have exited."""
    state = STATE
    while not state.stopping:
        clock.sleep(IDLE_TICK_S)
        for agent in SESSIONS.gone():  # its process exited: the same as a turn end
            _quietly(tools.let_go, agent)
        _quietly(lambda: tools.mods.screenshot.cleanup())  # pictures go after an hour
        idle = clock.time() - state.last_call
        if state.running == 0 and idle >= IDLE_EXIT_S:
            shutdown()


# --- start and exit -------------------------------------------------------------------


def _take_lock() -> int | None:
    """The lock that makes this the one helper of its socket. None: another one lives."""
    path = protocol.lock_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if not os.environ.get("MACOS_HARNESS_SOCKET"):
        protocol.own_folder(os.path.dirname(path))  # ours alone, or no helper here
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    until = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            pass
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with probe, contextlib.suppress(OSError):
            probe.connect(protocol.socket_path())
            until = 0  # the holder serves clients: it is not on its way out
        if time.time() >= until:
            os.close(fd)
            return None
        time.sleep(0.05)


def _bind() -> socket.socket:
    """A socket file with no lock holder is stale: remove it and bind. Mode 0600."""
    path = protocol.socket_path()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(64)
    return sock


def _on_signals() -> None:
    """SIGTERM and SIGINT start a clean exit, also while the main thread is in AppKit."""
    reader, writer = socket.socketpair()
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno())
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: None)

    STATE.wakeup = writer  # the signal handler writes to it: it must stay open
    threading.Thread(target=lambda: (reader.recv(1), shutdown()), daemon=True).start()


def _exit_cap() -> None:
    """An exit that hangs (an app that never answers) must not hold the lock for ever."""
    STATE.stop_started.wait()
    if not STATE.exited.wait(EXIT_CAP_S):
        os._exit(1)


def main() -> None:
    """Start order and exit order of the helper (one place, on purpose)."""
    mods = tools.mods
    lock = _take_lock()
    if lock is None:
        return
    sock = None
    try:
        sock = _bind()
        log("helper started, pid", os.getpid(), protocol.socket_path())
        _quietly(lambda: mods.pasteboard.restore_saved())
        _quietly(lambda: mods.screenshot.cleanup())
        _quietly(lambda: mods.person.start_watch(os.getpid()))
        prewarm = lambda: _quietly(lambda: mods.apps.prewarm())

        def start_threads() -> None:  # once the loop runs: their work reaches it
            threading.Thread(target=prewarm, daemon=True).start()
            threading.Thread(target=serve, args=(sock,), daemon=True).start()
            threading.Thread(target=idle_loop, daemon=True).start()
            threading.Thread(target=_exit_cap, daemon=True).start()

        _on_signals()
        runloop.run(then=start_threads)
        STATE.left.wait()  # with no AppKit loop, run() returns at once: wait for shutdown
    finally:
        shutdown()
        _quietly(lambda: mods.focus.release_all())
        _quietly(lambda: mods.tree.disable_all())
        _quietly(lambda: mods.pasteboard.restore_saved())
        _quietly(lambda: mods.presence.turn_done())
        _quietly(lambda: mods.screenshot.cleanup())
        if sock is not None:
            with contextlib.suppress(OSError):
                os.unlink(protocol.socket_path())
        os.close(lock)
        STATE.exited.set()
        log("helper stopped")


if __name__ == "__main__":
    main()
