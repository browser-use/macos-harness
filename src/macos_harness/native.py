"""Synchronous client for the native macOS accessibility agent's wire protocol.

The native agent is an optional SwiftPM executable that owns every
Accessibility (AX) call on one serial dispatch queue and speaks NDJSON
(newline-delimited JSON) over an ``AF_UNIX`` socket: one JSON object per
line, request in, response out, no pipelining. This module is the Python
half of that protocol only — it knows nothing about starting, stopping, or
supervising the agent process (see ``agent.py`` for that) and it never
imports ``agent.py``, so ``agent.py`` can import *this* module for its own
readiness probes without creating an import cycle.

Wire envelope (protocol v1)::

    request:  {"v": 1, "id": <int>, "op": <str>, "params": {...}}
    success:  {"id": <int>, "ok": true, "result": {...}}
    error:    {"id": <int>, "ok": false,
               "error": {"code": <str>, "message": <str>, "ax_error": <int?>}}

Every request is dispatched exactly once. If a response is lost (timeout,
malformed frame, dropped connection) the client never resends it — for a
mutating op such as ``ax_press`` a silent retry could fire the action twice.
The caller sees a clear error and, on their own, decides whether to retry
from scratch (a fresh snapshot, a fresh press).
"""

from __future__ import annotations

import itertools
import json
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Self

from .macos import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOSError,
)

#: Major protocol version this client speaks. The agent's ``ping`` result
#: must echo the same value or the handshake fails closed.
PROTOCOL_VERSION = 1

#: Hard cap on a single NDJSON line, matching the agent's own line cap.
#: Enforced while *reading* (bounds a hostile/buggy agent's response) and
#: while *writing* (bounds an oversized request before it ever reaches the
#: socket).
MAX_LINE_BYTES = 8 * 1024 * 1024

_RECV_CHUNK = 65536


class NativeProtocolError(MacOSError):
    """Handshake, framing, or transport failure talking to the native agent.

    Distinct from the wire-mapped exceptions below: this is raised for
    problems with the *channel* itself (bad JSON, oversized line,
    protocol-version mismatch, ``expected_pid`` identity mismatch,
    response id desync) rather than an error the agent deliberately
    reported about an AX operation. ``NativeConnectionError`` below is
    the one subclass raised for a plain failure to connect at all —
    every other channel problem here means a real request already
    reached *some* process, a harder failure than "no agent available".
    """


class NativeConnectionError(NativeProtocolError):
    """The native agent's socket could not be reached at all.

    Raised only for the transport-level failure connecting the
    ``AF_UNIX`` socket itself (no agent listening, permission denied, a
    stale/missing socket path, ...) — strictly *before* any request,
    including the handshake ping, has been dispatched. This is the one
    native-agent failure ``backend="auto"`` callers may treat as "no
    native agent available, fall back to the local Accessibility APIs".
    Every other ``NativeProtocolError`` (protocol-version mismatch,
    ``expected_pid`` identity mismatch, malformed or desynced
    responses, a connection that dropped mid-request, ...) means a real
    request already reached *some* process on the other end, and
    silently falling back at that point could paper over a wrong,
    incompatible, or unexpected agent instead of surfacing it — so
    those stay hard failures even under ``auto``.
    """


#: Wire error code -> exception class, matching the existing Python
#: exception taxonomy 1:1. Codes not listed here (``app.ambiguous``,
#: ``ax.error``, ``element.unknown``, ``timeout``, ``bad_request``,
#: ``unsupported_op``, and any future code) fall back to plain MacOSError.
_ERROR_CLASSES: dict[str, type[MacOSError]] = {
    "permission.accessibility": AccessibilityPermissionError,
    "app.not_found": ApplicationNotFoundError,
    "focus.changed": FocusChangedError,
}


def _error_from_payload(response: dict[str, Any]) -> MacOSError:
    error = response.get("error")
    if not isinstance(error, dict):
        return NativeProtocolError(
            f"Malformed error envelope from native agent: {response!r}"
        )
    code = str(error.get("code") or "ax.error")
    message = str(error.get("message") or code)
    ax_error = error.get("ax_error")
    if isinstance(ax_error, int):
        message = f"{message} (AXError {ax_error})"
    exc_type = _ERROR_CLASSES.get(code, MacOSError)
    return exc_type(message)


class _NativeHandle:
    """Opaque sentinel for an AX element the agent — not Python — resolved.

    ``MacOS`` interns this exactly like a raw ``AXUIElementRef`` returned by
    the local AX APIs: it stores the sentinel in ``self._elements`` and
    hands back the same monotonic ``element_index`` it always has. ``MacOS``
    never inspects a sentinel's contents.

    ``generation`` pins the handle to the agent-side element registry epoch
    it was minted under (the registry is per-connection and resets whenever
    a caller asks for it, e.g. ``reset_elements=True``). A handle whose
    generation no longer matches its client's current generation is stale;
    that is detected and rejected client-side, before any socket I/O, so a
    stale handle never silently addresses the wrong element.
    """

    __slots__ = ("client", "generation", "handle")

    def __init__(self, client: NativeClient, handle: int, generation: int) -> None:
        self.client = client
        self.handle = handle
        self.generation = generation

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"_NativeHandle(handle={self.handle!r}, generation={self.generation!r})"


def _exact_pid(value: Any) -> int | None:
    """Return ``value`` as a real OS process id, or ``None`` if it isn't one.

    JSON's ``number`` type carries no distinction between ``int``, ``float``,
    and (via Python's decoder) even ``bool`` — ``true`` compares equal to
    ``1`` under a naive ``==``. A wire payload is untrusted input: only an
    exact, non-boolean ``int`` greater than 1 (every real pid; ``0`` and
    ``1`` are never a user agent's own pid) is accepted as a pid to match
    against.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 1:
        return None
    return value


class NativeClient:
    """One synchronous connection to the native agent's NDJSON socket.

    Construction is cheap and never touches the network. The socket is
    opened lazily on first use (or via an explicit ``connect()``), and that
    first connection performs the protocol handshake: it pings the agent
    and refuses to proceed if the agent's protocol major version does not
    match ``PROTOCOL_VERSION``.

    ``expected_pid``, when given, binds the handshake to one exact agent
    process: the ping's ``pid`` field must equal it exactly (as a real,
    non-boolean integer) or the handshake fails closed and the socket is
    closed before returning. A socket path alone never proves *which*
    process is listening on it — the OS is free to hand a stale path or a
    reused pid to something else entirely — so callers that already know
    which pid they mean to talk to (anything derived from the lifecycle
    module's own pidfile-verified status) should always pass it.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        timeout: float = 5.0,
        expected_pid: int | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._expected_pid = expected_pid
        self._sock: socket.socket | None = None
        self._recv_buffer = bytearray()
        self._ids = itertools.count(1)
        self._generation = 0

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        state = "connected" if self._sock is not None else "lazy"
        return f"NativeClient({self._socket_path!s}, {state})"

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def generation(self) -> int:
        """Current agent-side element registry epoch known to this client.

        Bumps on every fresh connection and on every ``query()`` call whose
        effective ``reset_elements`` is truthy — mirroring the local
        ``MacOS`` code's own ``self._elements = {}`` reset one for one.
        """
        return self._generation

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # --- connection lifecycle -------------------------------------------

    def connect(self) -> None:
        """Open the socket and verify the handshake. Safe to call repeatedly."""
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(str(self._socket_path))
        except OSError as exc:
            sock.close()
            raise NativeConnectionError(
                f"Could not connect to the native agent at {self._socket_path}: {exc}"
            ) from exc
        self._sock = sock
        self._recv_buffer.clear()
        self._generation += 1
        try:
            hello = self._dispatch("ping", {})
        except Exception:
            self.close()
            raise
        protocol = hello.get("protocol")
        if protocol != PROTOCOL_VERSION:
            self.close()
            raise NativeProtocolError(
                f"Native agent protocol {protocol!r} at {self._socket_path} does not "
                f"match client protocol {PROTOCOL_VERSION}; refusing to proceed"
            )
        if self._expected_pid is not None:
            hello_pid = _exact_pid(hello.get("pid"))
            if hello_pid != self._expected_pid:
                self.close()
                raise NativeProtocolError(
                    f"Native agent at {self._socket_path} reported pid "
                    f"{hello.get('pid')!r}, expected {self._expected_pid}; "
                    "refusing to proceed"
                )

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._recv_buffer.clear()

    # --- wire transport ---------------------------------------------------

    def _request(self, op: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.connect()
        return self._dispatch(op, params or {})

    def _dispatch(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send exactly one request and read exactly one response.

        Never retried internally: once ``sendall`` returns, the request is
        considered dispatched, and any failure reading its response is
        surfaced to the caller rather than silently resent.
        """
        assert self._sock is not None
        request_id = next(self._ids)
        line = json.dumps(
            {"v": PROTOCOL_VERSION, "id": request_id, "op": op, "params": params},
            separators=(",", ":"),
        ).encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            raise NativeProtocolError(
                f"Request for {op!r} is {len(line)} bytes, over the {MAX_LINE_BYTES} cap"
            )
        try:
            self._sock.sendall(line + b"\n")
        except OSError as exc:
            self.close()
            raise NativeProtocolError(
                f"Failed sending {op!r} to the native agent: {exc}"
            ) from exc
        try:
            raw = self._recv_line()
        except NativeProtocolError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise NativeProtocolError(
                f"Failed reading the {op!r} response: {exc}"
            ) from exc
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NativeProtocolError(
                f"Malformed JSON response to {op!r}: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise NativeProtocolError(
                f"Malformed response shape to {op!r}: {response!r}"
            )
        if response.get("id") != request_id:
            self.close()
            raise NativeProtocolError(
                f"Response id {response.get('id')!r} for {op!r} did not match "
                f"request id {request_id}; connection desynced"
            )
        ok = response.get("ok")
        if ok is True:
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        if ok is False:
            raise _error_from_payload(response)
        raise NativeProtocolError(
            f"Malformed response envelope for {op!r}: {response!r}"
        )

    def _recv_line(self) -> bytes:
        """Read one ``\\n``-terminated frame, bounded to ``MAX_LINE_BYTES``.

        Buffers any bytes read past the newline for the next call instead of
        discarding them, and never grows the buffer past the cap — a peer
        that never sends a newline is rejected without unbounded memory use.
        """
        assert self._sock is not None
        while True:
            newline = self._recv_buffer.find(b"\n")
            if newline != -1:
                line = bytes(self._recv_buffer[: newline + 1])
                del self._recv_buffer[: newline + 1]
                return line
            if len(self._recv_buffer) > MAX_LINE_BYTES:
                raise NativeProtocolError(
                    f"Native agent response exceeded {MAX_LINE_BYTES} bytes without a newline"
                )
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                raise NativeProtocolError("Native agent closed the connection")
            self._recv_buffer += chunk

    # --- element handles ---------------------------------------------------

    def _resolve_handle(self, handle: _NativeHandle) -> int:
        """Validate a sentinel client-side and return its raw wire handle.

        Fails fast — no socket I/O — on a handle from a different client or
        a stale registry generation, exactly the two ways a native handle
        can go bad.
        """
        if not isinstance(handle, _NativeHandle):
            raise MacOSError(f"Not a native element handle: {handle!r}")
        if handle.client is not self:
            raise MacOSError(
                f"Native element handle {handle.handle!r} belongs to a different "
                "agent connection"
            )
        if handle.generation != self._generation:
            raise MacOSError(
                f"Native element handle {handle.handle!r} is stale (agent registry "
                "reset); take a fresh snapshot first"
            )
        return handle.handle

    # --- operations ---------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """Return ``{protocol, agent_version, pid, trusted, uptime_s}``."""
        return self._request("ping")

    def list_apps(self) -> list[dict[str, Any]]:
        result = self._request("list_apps")
        apps = result.get("apps")
        if not isinstance(apps, list):
            raise NativeProtocolError(f"Malformed list_apps result: {result!r}")
        # Trust nothing about wire ordering: sort exactly like the local
        # MacOS.list_apps() so backend parity holds regardless of the
        # agent's own iteration order.
        return sorted(
            apps,
            key=lambda item: (str(item.get("name", "")).casefold(), item.get("pid", 0)),
        )

    def query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run ``ax_query`` and return its match descriptors, ``handle`` intact."""
        resets = bool(params.get("reset_elements", True))
        result = self._request("ax_query", params)
        matches = result.get("matches")
        if not isinstance(matches, list):
            raise NativeProtocolError(f"Malformed ax_query result: {result!r}")
        if resets:
            self._generation += 1
        return matches

    def press(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run ``ax_press`` and return the single match it acted on."""
        result = self._request("ax_press", params)
        match = result.get("match")
        if not isinstance(match, dict):
            raise NativeProtocolError(f"Malformed ax_press result: {result!r}")
        self._generation += 1
        return match

    def get(self, handle: _NativeHandle, attribute: str) -> Any:
        return self.get_attributes(handle, (attribute,))[attribute]

    def get_attributes(
        self, handle: _NativeHandle, attributes: Iterable[str]
    ) -> dict[str, Any]:
        """Read multiple raw AX attributes with the agent's batch op."""
        wire_handle = self._resolve_handle(handle)
        names = list(dict.fromkeys(str(name) for name in attributes))
        result = self._request(
            "ax_element_get", {"handle": wire_handle, "attributes": names}
        )
        values = result.get("attributes")
        if not isinstance(values, dict):
            raise NativeProtocolError(f"Malformed ax_element_get result: {result!r}")
        return values

    def set(self, handle: _NativeHandle, attribute: str, value: Any) -> None:
        wire_handle = self._resolve_handle(handle)
        self._request(
            "ax_element_set",
            {"handle": wire_handle, "attribute": attribute, "value": value},
        )

    def perform(self, handle: _NativeHandle, action: str) -> None:
        wire_handle = self._resolve_handle(handle)
        self._request("ax_element_perform", {"handle": wire_handle, "action": action})
