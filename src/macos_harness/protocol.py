"""The wire between the client and the helper: the frame, the envelope, the paths.

Standard library only, and nothing slow to import: the client loads this on every call.
"""

from __future__ import annotations

import json
import os
import struct

from . import __version__ as VERSION

API_VERSION = "MacosHarnessIPC-1"  # client and helper must agree on this
MAX_FRAME_BYTES = 8 * 1024 * 1024  # one message on the socket, either way
CALL_TIMEOUT_S = 120.0  # the client gives up after this long
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
SOCKET_PATH_BYTES = 100  # a longer path does not fit a Unix socket address
# Sent with every call besides MACOS_HARNESS_*: the two other telemetry switches, and
# the caller's home folders (HOME and every *_HOME), which `install` writes into.
PASSED_ENV = ("DO_NOT_TRACK", "ANONYMIZED_TELEMETRY", "HOME")

# Transport errors of the helper: name -> (code, text).
ERRORS = {
    "notAuthenticated": (101, "The calling process may not use this socket"),
    "versionMismatch": (
        102,
        (
            "The macOS Harness helper and client have a version mismatch. "
            "Run `macos-harness helper restart`, then call the tool again."
        ),
    ),
    "invalidRequest": (
        -32600,
        "The id of a JSON-RPC message has to be a string or a number.",
    ),
    "methodNotFound": (-32601, "Method not found"),
    "deadlineExceeded": (-32000, "The deadline of the request has passed"),
    "stopping": (-32002, "macOS Harness helper is unavailable."),
    "responseTooLarge": (-32001, "The response is larger than one frame may be"),
}

_ids = iter(range(1, 1 << 62))


class FrameTooLarge(ValueError):
    def __init__(self, size: int):
        super().__init__(f"macOS Harness got a frame over the size limit: {size}")


def pack(obj) -> bytes:
    """One frame: a 4-byte little-endian length, then UTF-8 JSON."""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME_BYTES:
        raise FrameTooLarge(len(body))
    return struct.pack("<I", len(body)) + body


def _exactly(sock, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError("the other side closed the socket")
        data += chunk
    return data


def read(sock) -> dict:
    """The next frame. EOFError when the other side is gone."""
    (size,) = struct.unpack("<I", _exactly(sock, 4))
    if size > MAX_FRAME_BYTES:
        raise FrameTooLarge(size)
    return json.loads(_exactly(sock, size))


def call(sock, message: dict) -> dict:
    """Send one message and read its answer."""
    sock.sendall(pack(message))
    return read(sock)


def request(argv, stdin, env, deadline: float) -> dict:
    """The envelope of one tool call. `deadline` is time.time() seconds."""
    passed = {
        name: value
        for name, value in env.items()
        if name.startswith("MACOS_HARNESS_")
        or name in PASSED_ENV
        or name[-5:] == "_HOME"
    }
    params = {
        "callerApi": API_VERSION,
        "clientPath": PACKAGE_DIR,
        "clientVersion": VERSION,
        "deadlineMs": int(deadline * 1000),
        "argv": list(argv),
        "stdin": stdin,
        "cwd": os.getcwd(),
        "env": passed,
    }
    return {"id": next(_ids), "jsonrpc": "2.0", "method": "request", "params": params}


def control(method: str) -> dict:
    """`ping` or `shutdown`."""
    return {"id": next(_ids), "jsonrpc": "2.0", "method": method}


def result(id, value: dict) -> dict:
    return {"id": id, "jsonrpc": "2.0", "result": value}


def reply(id, exit: int, stdout: str, stderr: str) -> dict:
    return result(id, {"exit": exit, "stdout": stdout, "stderr": stderr})


def error(id, name: str, data: dict | None = None) -> dict:
    code, message = ERRORS[name]
    body = {"code": code, "message": message, "errorName": name, "data": data}
    return {"id": id, "jsonrpc": "2.0", "error": body}


def identity() -> dict:
    """What client and helper compare. Any difference means: not the same code."""
    return {"apiVersion": API_VERSION, "version": VERSION, "path": PACKAGE_DIR}


def failure(error) -> dict:
    """The JSON form of a failed call (`--json`). `error` is a HarnessError."""
    body = {
        "code": error.code,
        "name": error.name,
        "message": error.message,
        "cancelled": error.cancelled,
    }
    if error.tag:
        body["tag"] = error.tag
    return {"error": body}


def _state_dir() -> str:
    home = os.environ.get("MACOS_HARNESS_HOME")
    if home:
        return os.path.expanduser(home)
    return os.path.expanduser("~/Library/Application Support/macos-harness")


def state_dir():
    """The folder for everything kept on disk. Not made here."""
    from pathlib import Path  # slow to import, and the client never needs it

    return Path(_state_dir())


def socket_path() -> str:
    override = os.environ.get("MACOS_HARNESS_SOCKET")
    if override:
        return override
    path = os.path.join(_state_dir(), "helper.sock")
    if len(path.encode()) > SOCKET_PATH_BYTES:
        path = f"/tmp/macos-harness-{os.getuid()}/helper.sock"
    return path


def own_folder(path: str) -> None:
    """The folder of a socket must be this user's alone. One that is too open is
    closed. One another user made first (possible in /tmp, where a long state path
    falls back to), or a link of theirs, or no folder at all, raises PermissionError:
    whoever listens there is not our helper."""
    try:
        link, found = os.lstat(path), os.stat(path)
    except FileNotFoundError:
        return
    # The name itself is looked at too: a link another user put there leads to a
    # folder of ours that they can write into.
    if os.getuid() not in (link.st_uid, found.st_uid) or link.st_uid != found.st_uid:
        raise PermissionError(f"{path} belongs to another user")
    if not os.path.isdir(path):
        raise PermissionError(f"{path} is not a folder")
    if found.st_mode & 0o077:
        os.chmod(path, 0o700)


def peer_uid(sock) -> int:
    """The user of the process at the other end of a Unix socket."""
    return struct.unpack_from("II", sock.getsockopt(0, 1, 76))[1]  # LOCAL_PEERCRED


def lock_path() -> str:
    """Next to the socket: one helper per socket."""
    return os.path.splitext(socket_path())[0] + ".lock"


def log_path() -> str:
    return os.path.join(_state_dir(), "helper.log")
