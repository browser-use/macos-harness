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

import contextlib
import itertools
import json
import os
import socket
import threading
import weakref
from collections.abc import Callable, Iterable
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

#: Hard cap on a single NDJSON line -- content plus its terminating
#: newline -- matching the agent's own line cap. Enforced while *reading*
#: (bounds a hostile/buggy agent's response, and the newline is always
#: counted as part of the cap) and while *writing* (bounds an oversized
#: request, newline included, before it ever reaches the socket).
MAX_LINE_BYTES = 8 * 1024 * 1024

_RECV_CHUNK = 65536

#: Default timeout for the initial handshake ping a fresh ``connect()``
#: performs -- generous, because it covers however long the freshly
#: spawned agent process itself takes to come up, not just network I/O.
_DEFAULT_CONNECT_TIMEOUT = 10.0

#: Default timeout for every request made *after* a successful handshake.
#: The agent is already known to be up and answering by then, so this can
#: stay tight without being mistaken for slow process startup.
_DEFAULT_REQUEST_TIMEOUT = 5.0


class NativeProtocolError(MacOSError):
    """Handshake, framing, or transport failure talking to the native agent.

    Distinct from the wire-mapped exceptions below: this is raised for
    problems with the *channel* itself (bad JSON, an oversized line, a
    protocol-version mismatch, an ``expected_pid`` identity mismatch, a
    response id desync, a request this client refused to ever send
    because it was too large) rather than an error the agent deliberately
    reported about an AX operation. ``NativeConnectionError`` below is
    the one subclass raised for a plain failure to ever obtain a usable
    response at the transport level. Every other channel problem here
    means either a complete response frame *was* received and something
    about its content is wrong, or this client rejected a request
    locally before ever transmitting it -- not, as such, that "a real
    request already reached some process": a request too large to send
    never reaches anything. Either way it is a hard failure, never
    grounds for a silent fallback or retry.
    """


class NativeConnectionError(NativeProtocolError):
    """The native agent never produced a usable response at the transport level.

    Raised for every failure that means no complete, parseable response
    frame was ever obtained from the peer over the adopted socket: the
    peer was already gone (or never listening) by the time this client
    tried to use it, sending or reading a request/response failed
    outright, the connection was closed (EOF) before a full line came
    back, it timed out before one did, or the peer sent more than the
    line cap (including its own terminating newline) without ever
    completing a line. This is the one native-agent failure
    ``backend="auto"`` callers may treat as "no native agent available,
    fall back to the local Accessibility APIs" — including during the
    very first handshake ping ``NativeClient.connect()`` performs, so a
    freshly spawned agent that crashes or never answers before
    responding is exactly as fallback-eligible as a socket whose peer
    was already gone when adopted.

    Every other ``NativeProtocolError`` (a malformed or wrongly-shaped
    JSON body, a protocol-version mismatch, an ``expected_pid`` identity
    mismatch, a desynced response id, a request rejected locally for
    being too large to ever send, ...) means either a complete response
    frame *was* received and something about its content is wrong, or a
    request was deliberately never sent at all — and silently falling
    back at that point could paper over a wrong, incompatible, or
    unexpected agent (or hide a real bug in the caller) instead of
    surfacing it — so those stay hard failures even under ``auto``.
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


#: Every ``NativeClient`` that currently holds (or might still hold) a live
#: socket, tracked weakly so membership here never keeps a client -- or
#: anything it in turn keeps alive -- reachable a moment longer than it
#: otherwise would be. Existing solely so a ``fork()`` elsewhere in the
#: embedding process (a direct ``os.fork()``, ``multiprocessing`` with the
#: default "fork" start method, ...) can be cleaned up after: without this,
#: every live client's socket fd is duplicated verbatim into the child,
#: handing an unrelated, never-asked-for process a live, working channel to
#: this agent.
_live_clients: weakref.WeakSet[NativeClient] = weakref.WeakSet()


def _close_inherited_sockets_after_fork() -> None:
    """Slam shut every live client's raw socket in a freshly forked child.

    Registered once, process-wide, via ``os.register_at_fork`` below.
    Runs in the child with exactly one thread alive (the thread that
    called ``fork()``), before any of the child's own code resumes, so it
    deliberately never acquires a client's own lock: some *other* thread
    could have held it at the exact moment of ``fork()``, and that thread
    does not exist in this child at all -- blocking to acquire its lock
    here would hang forever. It never touches the agent's ``Popen``
    either, only the raw socket object: the ``Popen`` is owned solely by
    the original parent process, and nothing here signals, waits on, or
    otherwise "knows about" it -- this only closes the *file descriptor*
    this child happened to inherit by virtue of being forked from a
    process that had it open.
    """
    for client in list(_live_clients):
        for attr in ("_sock", "_preconnected"):
            sock = getattr(client, attr, None)
            if sock is not None:
                setattr(client, attr, None)
                with contextlib.suppress(OSError):
                    sock.close()
        client._recv_buffer.clear()


if hasattr(os, "register_at_fork"):  # pragma: no branch - always true on macOS
    os.register_at_fork(after_in_child=_close_inherited_sockets_after_fork)


class NativeClient:
    """One synchronous connection to the native agent's NDJSON socket.

    Construction is cheap and never touches the network: it merely adopts
    an already-connected ``sock`` — the parent's own end of the
    ``socket.socketpair()`` the lifecycle module's ``launch()`` creates
    for its private per-instance agent child. The socket is adopted
    lazily on first use (or via an explicit ``connect()``), and that
    first connection performs the protocol handshake: it pings the agent
    and refuses to proceed if the agent's protocol major version does
    not match ``PROTOCOL_VERSION`` or its pid does not match
    ``expected_pid``. The handshake itself runs with ``connect_timeout``;
    every request afterward uses the tighter ``request_timeout`` instead
    — the agent is already known to be up by then, so a slow *response*
    is no longer indistinguishable from a slow *process start*.

    ``expected_pid`` binds the handshake to one exact agent process: the
    ping's ``pid`` field must equal it exactly (as a real, non-boolean
    integer) or the handshake fails closed and the socket is closed
    before returning. An already-open socket alone never proves *which*
    process is listening on it, so every caller (``agent.launch()``
    binding to the ``Popen`` it just spawned) must always pass the pid
    it actually means to talk to.

    Every socket-touching call fails closed if made from a different OS
    process than the one that constructed this client — the one way a
    ``fork()`` elsewhere in the embedding application could otherwise
    hand an unrelated child process a live, working handle to this
    agent. A background ``os.register_at_fork()`` hook (module level,
    registered once) also proactively closes every live client's raw
    socket in a freshly forked child, before any of the child's own code
    resumes, regardless of whether that child ever calls anything here.
    """

    def __init__(
        self,
        *,
        sock: socket.socket,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        expected_pid: int,
    ) -> None:
        self._preconnected: socket.socket | None = sock
        self._label = f"<agent socket, expected pid {expected_pid}>"
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._expected_pid = expected_pid
        self._creator_pid = os.getpid()
        self._sock: socket.socket | None = None
        self._recv_buffer = bytearray()
        self._ids = itertools.count(1)
        self._generation = 0
        self._lock = threading.RLock()
        self._terminal_failure_hook: Callable[[], None] | None = None
        _live_clients.add(self)

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        state = "connected" if self._sock is not None else "lazy"
        return f"NativeClient({self._label}, {state})"

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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

    # --- fork / process-identity safety -----------------------------------

    def _check_owner(self) -> None:
        """Fail closed if used from a different process than created it.

        The only way this can happen is a ``fork()`` elsewhere in the
        embedding application: the forked child inherits a byte-for-byte
        copy of this object, including ``self._sock``, without ever
        having gone through ``__init__`` itself. Checked first, before
        the "already connected" fast path, so an inherited-but-live
        socket in a forked child is never silently reused.
        """
        pid = os.getpid()
        if pid != self._creator_pid:
            raise NativeProtocolError(
                f"This NativeClient was created in pid {self._creator_pid} "
                f"and cannot be used from pid {pid} (a fork boundary was "
                "crossed); construct a fresh client in this process instead"
            )

    def _arm_terminal_failure_hook(self, hook: Callable[[], None]) -> None:
        """Wire a callback fired on a post-handshake transport failure.

        ``agent.launch()`` arms this exactly once, right after a
        successful handshake, with a hook that weakly reaps the owning
        ``AgentSession`` — this module never imports ``agent.py`` or
        knows what the hook actually does, and never reconnects or falls
        back on its own. Any exception the hook itself raises is
        swallowed so it can never mask the transport error that
        triggered it.
        """
        self._terminal_failure_hook = hook

    def _fail_transport(self) -> None:
        """Close the socket and, if armed, fire the terminal-failure hook."""
        self.close()
        hook = self._terminal_failure_hook
        if hook is not None:
            with contextlib.suppress(Exception):
                hook()

    # --- connection lifecycle -------------------------------------------

    def connect(self) -> None:
        """Open (or adopt) the socket and verify the handshake.

        Safe to call repeatedly once connected — a no-op after the first
        success. Adopts its already-connected socket exactly once; a
        closed socketpair endpoint can never be reopened, so calling this
        again after ``close()`` raises instead of silently doing nothing
        — a fresh connection needs a fresh ``launch()``, not a retry
        here. Any failure during setup — including one raised while
        merely configuring the freshly adopted socket, before the
        handshake even begins — closes that socket before propagating,
        so a partially set up client never leaks its file descriptor.
        """
        with self._lock:
            self._check_owner()
            if self._sock is not None:
                return
            if self._preconnected is None:
                raise NativeProtocolError(
                    "This client's socket was already consumed and closed; "
                    "it cannot be reconnected"
                )
            sock = self._preconnected
            self._preconnected = None
            self._sock = sock
            try:
                sock.settimeout(self._connect_timeout)
                self._recv_buffer.clear()
                self._generation += 1
                hello = self._dispatch("ping", {})
                protocol = hello.get("protocol")
                if protocol != PROTOCOL_VERSION:
                    raise NativeProtocolError(
                        f"Native agent protocol {protocol!r} at {self._label} "
                        f"does not match client protocol {PROTOCOL_VERSION}; "
                        "refusing to proceed"
                    )
                hello_pid = _exact_pid(hello.get("pid"))
                if hello_pid != self._expected_pid:
                    raise NativeProtocolError(
                        f"Native agent at {self._label} reported pid "
                        f"{hello.get('pid')!r}, expected {self._expected_pid}; "
                        "refusing to proceed"
                    )
                sock.settimeout(self._request_timeout)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
            self._recv_buffer.clear()

    # --- wire transport ---------------------------------------------------

    def _request(self, op: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.connect()
            return self._dispatch(op, params or {})

    def _dispatch(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send exactly one request and read exactly one response.

        Never retried internally: once ``sendall`` returns, the request is
        considered dispatched, and any failure reading its response is
        surfaced to the caller rather than silently resent. Always
        called with ``self._lock`` already held by ``connect()`` or
        ``_request()``.
        """
        assert self._sock is not None
        request_id = next(self._ids)
        frame = (
            json.dumps(
                {"v": PROTOCOL_VERSION, "id": request_id, "op": op, "params": params},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(frame) > MAX_LINE_BYTES:
            raise NativeProtocolError(
                f"Request for {op!r} is {len(frame)} bytes including its "
                f"newline, over the {MAX_LINE_BYTES} cap"
            )
        try:
            self._sock.sendall(frame)
        except OSError as exc:
            self._fail_transport()
            raise NativeConnectionError(
                f"Failed sending {op!r} to the native agent: {exc}"
            ) from exc
        try:
            raw = self._recv_line()
        except NativeProtocolError:
            self._fail_transport()
            raise
        except OSError as exc:
            self._fail_transport()
            raise NativeConnectionError(
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
            self._fail_transport()
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
        """Read one ``\\n``-terminated frame, bounded to ``MAX_LINE_BYTES``
        including its own terminating newline.

        ``self._recv_buffer`` never grows past ``MAX_LINE_BYTES`` bytes:
        each ``recv()`` is itself capped to the remaining room, so once a
        newline lands in the buffer the completed line (content plus
        that newline) is automatically within cap with no separate
        length check needed, and a peer that never sends a newline at
        all is rejected the instant no room is left for one — without
        ever buffering more than the cap to find that out.
        """
        assert self._sock is not None
        while True:
            newline = self._recv_buffer.find(b"\n")
            if newline != -1:
                line = bytes(self._recv_buffer[: newline + 1])
                del self._recv_buffer[: newline + 1]
                return line
            if len(self._recv_buffer) >= MAX_LINE_BYTES:
                raise NativeConnectionError(
                    f"Native agent response exceeded the {MAX_LINE_BYTES}-byte "
                    "cap (including its newline) without ever sending one"
                )
            room = MAX_LINE_BYTES - len(self._recv_buffer)
            chunk = self._sock.recv(min(_RECV_CHUNK, room))
            if not chunk:
                raise NativeConnectionError("Native agent closed the connection")
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
