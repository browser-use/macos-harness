"""The command line client: send argv to the helper, print what comes back.

It parses nothing and imports nothing slow, so one call costs about 20 ms. The helper
is started on the first call and reused after that.
"""

from __future__ import annotations

import json
import os
import select
import socket
import stat
import sys
import time

from . import protocol

HELPER = [sys.executable, "-m", "macos_harness.helper"]
QUICK_CONNECT_S = 0.25  # one connect may take this long
START_WINDOW_S = 5.0  # how long a started helper gets to answer
LOG_BYTES = 1024 * 1024  # helper.log is moved to helper.log.1 above this
CONNECT_RETRY_S = 0.1
PING_BUDGET_S = 1.0
STDIN_WAIT_S = 2.0  # for the first byte, and for each later one
PARENT_LOOK_S = 0.2  # how often a call looks whether its parent still runs


class Failure(Exception):
    """The call could not be made. `args[0]` names the text in errors.py."""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--version"]:
        print(protocol.VERSION)
        return 0
    try:
        if argv[:1] == ["helper"]:
            return _helper_command(argv[1:])
        answer = _call(argv)
    except Failure as failure:
        from . import errors  # only on a failure: a call that works never loads it

        error = protocol.failure(getattr(errors, failure.args[0])())["error"]
        return _fail(error, "--json" in argv)
    if "error" in answer:  # a transport error: the helper ran nothing
        error = answer["error"]
        name = error.get("errorName") or "jsonRPCError"
        return _fail({**error, "name": name, "cancelled": False}, "--json" in argv)
    try:
        sys.stdout.write(answer["result"]["stdout"])
        sys.stdout.flush()
    except BrokenPipeError:  # `macos-harness ... | head`: the reader left early
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.stderr.write(answer["result"]["stderr"])
    return answer["result"]["exit"]


def _fail(error: dict, as_json: bool) -> int:
    if as_json:
        shown = {key: error[key] for key in ("code", "name", "message", "cancelled")}
        print(json.dumps({"error": shown}))
    else:
        print(error["message"], file=sys.stderr)
    return 1


def _call(argv: list[str]) -> dict:
    """One tool call. It is sent again only when the helper says it ran nothing."""
    deadline = time.time() + protocol.CALL_TIMEOUT_S
    stdin, restarted, parent = None, False, os.getppid()
    while True:
        sock = _connect(deadline)
        message = protocol.request(argv, stdin, os.environ, deadline)
        try:
            with sock:
                sock.settimeout(max(1.0, deadline - time.time() + 1.0))
                sock.sendall(protocol.pack(message))
                _wait_for_answer(sock, deadline, parent)
                answer = protocol.read(sock)
        except TimeoutError:
            raise Failure("request_timed_out") from None
        except (EOFError, OSError, ValueError):
            # The helper may have run the action. Sending it again could click twice.
            raise Failure("helper_stopped") from None
        refused = answer.get("error", {}).get("errorName")
        if refused in ("versionMismatch", "stopping") and not restarted:
            # It runs other code, or it is on its way out. Either way it ran nothing:
            # wait until it is gone, start our own and send the call once more.
            restarted = True
            _stop_helper()
        elif answer.get("result", {}).get("needStdin") and stdin is None:
            stdin = _read_stdin()
        elif "error" in answer or "exit" in answer.get("result", {}):
            return answer
        else:
            raise Failure("helper_stopped")


def _wait_for_answer(sock: socket.socket, deadline: float, parent: int) -> None:
    """Some hosts leave an interrupted command running (`codex exec`, `pi -p`). Once
    the process that started this one is gone, this one goes too: the helper sees its
    socket close and stops the action at its next event."""
    while not select.select([sock], [], [], PARENT_LOOK_S)[0]:
        if os.getppid() != parent:
            os._exit(130)
        if time.time() > deadline + 1.0:
            raise TimeoutError


def _try_connect() -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if not os.environ.get(
            "MACOS_HARNESS_SOCKET"
        ):  # never talk into a stranger's folder
            protocol.own_folder(os.path.dirname(protocol.socket_path()))
        sock.settimeout(QUICK_CONNECT_S)
        sock.connect(protocol.socket_path())
        if protocol.peer_uid(sock) != os.getuid():  # whoever listens is not our helper
            raise PermissionError(protocol.socket_path())
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
        sock.close()
        return None
    except OSError:
        sock.close()
        raise Failure("helper_unavailable") from None
    return sock


def _connect(deadline: float) -> socket.socket:
    """Connect, starting the helper when nobody listens."""
    sock = _try_connect()
    if sock is not None:
        return sock
    _start_helper()
    until = time.time() + START_WINDOW_S
    while time.time() < until:
        time.sleep(CONNECT_RETRY_S)
        sock = _try_connect()
        if sock is not None and _ping(sock, deadline) is not None:
            return _try_connect() or _unavailable()
    return _unavailable()


def _unavailable():
    raise Failure("helper_unavailable")


def _ping(sock: socket.socket, deadline: float = float("inf")) -> dict | None:
    """What the helper says about itself, or None when it does not answer in time."""
    try:
        with sock:
            sock.settimeout(min(PING_BUDGET_S, max(0.001, deadline - time.time())))
            return protocol.call(sock, protocol.control("ping")).get("result")
    except (EOFError, OSError, ValueError):
        return None


def _start_helper() -> None:
    """Start the helper fully detached, so it holds none of our pipes.

    New session, stdin from /dev/null, output appended to helper.log, no other open
    file, working folder /. A helper that kept our stdout would keep the agent's
    shell waiting for it.
    """
    log = protocol.log_path()
    os.makedirs(os.path.dirname(log), mode=0o700, exist_ok=True)
    if os.path.exists(log) and os.path.getsize(log) > LOG_BYTES:
        os.replace(log, log + ".1")  # one old file is kept
    # The helper must find this package, also when we run from a checkout.
    paths = [os.path.dirname(protocol.PACKAGE_DIR), os.environ.get("PYTHONPATH")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))}
    child = os.fork()
    if child:
        os.waitpid(child, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)  # the helper's parent is gone: nobody has to wait for it
        os.chdir("/")
        out = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        os.closerange(3, min(os.sysconf("SC_OPEN_MAX"), 65536))
        os.execve(HELPER[0], HELPER, env)
    finally:
        os._exit(127)


def _stop_helper() -> bool:
    """Ask the helper to exit and wait until it is gone. False when none runs."""
    sock = _try_connect()
    if sock is None:
        return False
    try:
        with sock:
            sock.settimeout(protocol.CALL_TIMEOUT_S + 15.0)
            protocol.call(sock, protocol.control("shutdown"))
            while sock.recv(4096):  # it closes the socket when it has exited
                pass
    except (EOFError, OSError, ValueError):
        pass
    return True


def _read_stdin() -> str:
    """The text the helper asked for. Only a pipe or a file is read, and never for long.

    An agent shell may leave stdin open as a pipe that never ends, so every wait for
    a byte ends after STDIN_WAIT_S.
    """
    try:
        mode = os.fstat(0).st_mode
    except OSError:
        return ""
    if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
        return ""
    data = b""
    while len(data) < protocol.MAX_FRAME_BYTES:
        if not select.select([0], [], [], STDIN_WAIT_S)[0]:
            break
        chunk = os.read(0, 1 << 16)
        if not chunk:
            break
        data += chunk
    return data[: protocol.MAX_FRAME_BYTES].decode("utf-8", "replace")


def _helper_command(words: list[str]) -> int:
    """`helper status`, `helper stop`, `helper restart`. For the person, not the model."""
    action = words[0] if words else "status"
    if action not in ("status", "stop", "restart"):
        print("usage: macos-harness helper [status|stop|restart]", file=sys.stderr)
        return 2
    if action != "status":
        stopped = _stop_helper()
        if action == "stop":
            print("stopped" if stopped else "not running")
            return 0
    sock = _try_connect() if action == "status" else _connect(float("inf"))
    info = _ping(sock) if sock is not None else None
    if info is None:
        print("not running")
        return 1
    print(f"running: pid {info['pid']}, version {info['version']}, {info['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
