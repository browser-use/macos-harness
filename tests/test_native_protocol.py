"""CI-safe protocol tests for the native agent client (no Swift, no AX).

These exercise ``macos_harness.native.NativeClient`` against a small,
fully-scripted fake agent that speaks the real NDJSON-over-AF_UNIX wire
protocol from a background thread in this process. No subprocess, no Swift
toolchain, no Accessibility permission is required.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from macos_harness.macos import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOSError,
)
from macos_harness.native import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    NativeClient,
    NativeConnectionError,
    NativeProtocolError,
    _NativeHandle,
)

# Sentinel script entry: close the connection without sending a response,
# simulating an agent that dies mid-request.
CLOSE = object()


class _FakeAgent:
    """A scriptable stand-in for the native agent's NDJSON socket.

    ``script`` supplies exactly one outcome per line the fake agent reads,
    in order: raw response bytes to send back, or ``CLOSE`` to drop the
    connection without responding. Every fully-read line (valid JSON or
    not) is recorded in ``received`` before its outcome runs.
    """

    def __init__(self, tmp_path: Path, script: list[Any]) -> None:
        # AF_UNIX socket paths are capped at ~104 bytes on macOS; pytest's
        # per-test ``tmp_path`` can easily exceed that, so the fake agent
        # binds under a short directory created directly in /tmp instead.
        del tmp_path
        self._script = list(script)
        self.received: list[bytes] = []
        self._socket_dir = Path(tempfile.mkdtemp(prefix="nca-", dir="/tmp"))
        self.socket_path = self._socket_dir / "agent.sock"
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        try:
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self.socket_path))
            self._server.listen(1)
            self._server.settimeout(5.0)
            self._thread = threading.Thread(
                target=self._serve, args=(self._server,), daemon=True
            )
            self._thread.start()
        except Exception:
            self.close()
            raise

    def _serve(self, server: socket.socket) -> None:
        index = 0
        while index < len(self._script):
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(5.0)
                    buffer = bytearray()
                    while index < len(self._script):
                        newline = buffer.find(b"\n")
                        while newline == -1:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            buffer += chunk
                            newline = buffer.find(b"\n")
                        if newline == -1:
                            break
                        line = bytes(buffer[: newline + 1])
                        del buffer[: newline + 1]
                        self.received.append(line)
                        outcome = self._script[index]
                        index += 1
                        if outcome is CLOSE:
                            break
                        conn.sendall(outcome)
            except OSError:
                continue

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            with contextlib.suppress(RuntimeError):
                self._thread.join(timeout=timeout)

    def close(self) -> None:
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None
        if self._thread is not None:
            # Thread.start() may itself have raised (e.g. resource limits),
            # leaving a Thread object that was never started; joining that
            # raises RuntimeError instead of a no-op.
            with contextlib.suppress(RuntimeError):
                self._thread.join(timeout=2.0)
            self._thread = None
        shutil.rmtree(self._socket_dir, ignore_errors=True)


def _ok(request_id: int, result: dict[str, Any]) -> bytes:
    payload = {"id": request_id, "ok": True, "result": result}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _err(
    request_id: int, code: str, message: str, ax_error: int | None = None
) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if ax_error is not None:
        error["ax_error"] = ax_error
    payload = {"id": request_id, "ok": False, "error": error}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _ping_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "agent_version": "0.1.0",
        "pid": 4242,
        "trusted": True,
        "uptime_s": 12.5,
    }
    result.update(overrides)
    return result


def test_handshake_rejects_protocol_major_mismatch(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(protocol=2))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError, match="protocol"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_connect_to_missing_agent_raises_protocol_error(tmp_path: Path) -> None:
    client = NativeClient(tmp_path / "does-not-exist.sock", timeout=1.0)
    with pytest.raises(NativeProtocolError, match="Could not connect"):
        client.ping()
    assert client.connected is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("permission.accessibility", AccessibilityPermissionError),
        ("app.not_found", ApplicationNotFoundError),
        ("focus.changed", FocusChangedError),
        ("app.ambiguous", MacOSError),
        ("ax.error", MacOSError),
        ("element.unknown", MacOSError),
        ("timeout", MacOSError),
        ("bad_request", MacOSError),
        ("unsupported_op", MacOSError),
    ],
)
def test_error_codes_map_to_expected_exception(
    tmp_path: Path, code: str, expected: type[MacOSError]
) -> None:
    ax_error = -25200 if code == "ax.error" else None
    agent = _FakeAgent(
        tmp_path,
        [_ok(1, _ping_result()), _err(2, code, f"boom: {code}", ax_error=ax_error)],
    )
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(expected, match="boom"):
            client.query({"reset_elements": True})
        assert (
            client.connected is True
        )  # error responses don't tear down the connection
    finally:
        agent.close()


def test_malformed_json_line_raises_and_connection_survives(tmp_path: Path) -> None:
    agent = _FakeAgent(
        tmp_path,
        [
            _ok(1, _ping_result()),
            b"this is not json\n",
            _ok(3, {"apps": []}),
        ],
    )
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError, match="Malformed JSON"):
            client.list_apps()
        assert client.connected is True
        assert client.generation == 1
        assert client.list_apps() == []
        assert client.generation == 1
    finally:
        agent.close()


def test_oversized_line_rejected_without_oom(tmp_path: Path) -> None:
    oversized = b"x" * (MAX_LINE_BYTES + 2 * 1024 * 1024)  # no trailing newline
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), oversized])
    try:
        client = NativeClient(agent.socket_path, timeout=5.0)
        started = time.monotonic()
        with pytest.raises(NativeProtocolError, match="exceeded"):
            client.list_apps()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, (
            "client must bail out without buffering the full oversized frame"
        )
        assert client.connected is False
    finally:
        agent.close()


def test_oversized_request_is_rejected_before_sending(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result())])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        client.connect()
        huge_text = "x" * (MAX_LINE_BYTES + 1024)
        with pytest.raises(NativeProtocolError, match="over the"):
            client.query({"text": huge_text, "reset_elements": False})
        # Nothing was sent, so the connection is still perfectly usable.
        assert client.connected is True
    finally:
        agent.close()


def test_response_id_mismatch_is_rejected_and_closes(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), _ok(999, {"apps": []})])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError, match="did not match"):
            client.list_apps()
        assert client.connected is False
    finally:
        agent.close()


def test_ok_false_without_error_object_raises_clear_error(tmp_path: Path) -> None:
    bad = (json.dumps({"id": 2, "ok": False}) + "\n").encode()
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), bad])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError, match="Malformed error envelope"):
            client.list_apps()
    finally:
        agent.close()


def test_response_missing_ok_field_is_a_protocol_error(tmp_path: Path) -> None:
    bad = (json.dumps({"id": 2, "result": {}}) + "\n").encode()
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), bad])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError, match="Malformed response envelope"):
            client.list_apps()
    finally:
        agent.close()


def test_no_retry_after_send_when_response_is_lost(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), CLOSE])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError):
            client.press({"text": "Not Now"})
        agent.join(timeout=2.0)
        # Handshake ping + exactly one press attempt: the lost response was
        # never resent.
        assert len(agent.received) == 2
        assert client.connected is False
    finally:
        agent.close()


def test_list_apps_is_sorted_client_side_regardless_of_wire_order(
    tmp_path: Path,
) -> None:
    scrambled = [
        {"name": "Zebra", "bundle_id": "z", "pid": 1, "path": None},
        {"name": "apple", "bundle_id": "a", "pid": 5, "path": None},
        {"name": "Apple", "bundle_id": "a2", "pid": 2, "path": None},
    ]
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result()), _ok(2, {"apps": scrambled})])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        apps = client.list_apps()
        assert [item["pid"] for item in apps] == [2, 5, 1]
    finally:
        agent.close()


def test_stale_generation_handle_rejected_without_wire_call(tmp_path: Path) -> None:
    agent = _FakeAgent(
        tmp_path,
        [
            _ok(1, _ping_result()),  # first connect() handshake -> generation 1
            _ok(2, _ping_result()),  # reconnect handshake -> generation 2
        ],
    )
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        client.connect()
        assert client.generation == 1
        stale_handle = _NativeHandle(client, 7, client.generation)

        client.close()
        client.connect()
        assert client.generation == 2

        with pytest.raises(MacOSError, match="stale"):
            client.get(stale_handle, "AXValue")

        agent.join(timeout=2.0)
        # Only the two handshake pings ever reached the wire; the stale
        # get() was rejected before touching the socket.
        assert len(agent.received) == 2
    finally:
        agent.close()


def test_handle_from_different_client_rejected_without_wire_call(
    tmp_path: Path,
) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result())])
    try:
        client_a = NativeClient(agent.socket_path, timeout=2.0)
        client_a.connect()
        handle = _NativeHandle(client_a, 3, client_a.generation)

        client_b = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(MacOSError, match="different"):
            client_b.get(handle, "AXValue")
        assert client_b.connected is False
    finally:
        agent.close()


def test_full_operation_surface_round_trips(tmp_path: Path) -> None:
    match = {"handle": 11, "role": "AXButton", "title": "Not Now"}
    responses = [
        _ok(1, _ping_result()),  # connect handshake
        _ok(2, {"matches": [match]}),  # query
        _ok(3, {"attributes": {"AXValue": "hello"}}),  # get
        _ok(
            4, {"attributes": {"AXValue": "hello", "AXTitle": "Not Now"}}
        ),  # get_attributes
        _ok(5, {}),  # set
        _ok(6, {}),  # perform
        _ok(7, {"match": match}),  # press
    ]
    agent = _FakeAgent(tmp_path, responses)
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        matches = client.query({"text": "Not Now", "reset_elements": True})
        assert matches == [match]
        handle = _NativeHandle(client, matches[0]["handle"], client.generation)

        assert client.get(handle, "AXValue") == "hello"
        assert client.get_attributes(handle, ["AXValue", "AXTitle"]) == {
            "AXValue": "hello",
            "AXTitle": "Not Now",
        }
        client.set(handle, "AXValue", "world")
        client.perform(handle, "AXPress")
        pressed = client.press({"text": "Not Now"})
        assert pressed == match
        with pytest.raises(MacOSError, match="stale"):
            client.get(handle, "AXValue")

        query_request = json.loads(agent.received[1])
        assert query_request["v"] == PROTOCOL_VERSION
        assert query_request["op"] == "ax_query"
        assert query_request["params"] == {"text": "Not Now", "reset_elements": True}

        get_request = json.loads(agent.received[2])
        assert get_request["op"] == "ax_element_get"
        assert get_request["params"] == {"handle": 11, "attributes": ["AXValue"]}

        get_attrs_request = json.loads(agent.received[3])
        assert get_attrs_request["op"] == "ax_element_get"
        assert get_attrs_request["params"] == {
            "handle": 11,
            "attributes": ["AXValue", "AXTitle"],
        }

        set_request = json.loads(agent.received[4])
        assert set_request["op"] == "ax_element_set"
        assert set_request["params"] == {
            "handle": 11,
            "attribute": "AXValue",
            "value": "world",
        }

        perform_request = json.loads(agent.received[5])
        assert perform_request["op"] == "ax_element_perform"
        assert perform_request["params"] == {"handle": 11, "action": "AXPress"}

        press_request = json.loads(agent.received[6])
        assert press_request["op"] == "ax_press"
        assert press_request["params"] == {"text": "Not Now"}
    finally:
        agent.close()


# --- endpoint binding: expected_pid --------------------------------------


def test_handshake_accepts_matching_expected_pid(tmp_path: Path) -> None:
    # `client.connect()` (not `.ping()`) makes exactly one dispatch — the
    # handshake ping `expected_pid` is actually checked against — so one
    # scripted response is enough; `.ping()` on a fresh connection would
    # dispatch a *second*, unscripted request right after it.
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=4242))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=4242)
        client.connect()
        assert client.connected is True
    finally:
        agent.close()


def test_handshake_rejects_mismatched_expected_pid(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=4242))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=9999)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_handshake_rejects_missing_pid_field_when_expected_pid_given(
    tmp_path: Path,
) -> None:
    payload = {
        "protocol": PROTOCOL_VERSION,
        "agent_version": "0.1.0",
        "trusted": True,
        "uptime_s": 1.0,
    }  # no "pid" key at all
    agent = _FakeAgent(tmp_path, [_ok(1, payload)])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=4242)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_handshake_rejects_non_integer_numeric_pid(tmp_path: Path) -> None:
    # JSON's decimal `4242.0` decodes to a Python float, which compares
    # equal to the int 4242 under `==` -- the exact-type check must still
    # reject it rather than trust a naive equality comparison.
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=4242.0))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=4242)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
    finally:
        agent.close()


def test_handshake_rejects_boolean_pid_masquerading_as_pid_one(
    tmp_path: Path,
) -> None:
    # JSON's `true` decodes to Python's `True`, which compares equal to
    # the int 1 under `==`. A naive `hello["pid"] == expected_pid` check
    # would wrongly accept it when `expected_pid=1`; the type-safe check
    # must not.
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=True))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=1)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_expected_pid_omitted_is_fully_backward_compatible(
    tmp_path: Path,
) -> None:
    """Omitting ``expected_pid`` must behave exactly as before: no
    identity check happens at all, regardless of what pid the agent
    reports."""
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=1))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        client.connect()
        assert client.connected is True
    finally:
        agent.close()


# --- endpoint binding: connect-failure vs. post-dispatch hard failures ----


def test_connect_failure_raises_the_auto_fallback_eligible_subclass(
    tmp_path: Path,
) -> None:
    """``backend="auto"`` callers must be able to catch exactly this: a
    pure failure to connect at all, before any request -- even the
    handshake ping -- was ever dispatched."""
    client = NativeClient(tmp_path / "does-not-exist.sock", timeout=1.0)
    with pytest.raises(NativeConnectionError, match="Could not connect"):
        client.ping()
    assert client.connected is False


def test_protocol_mismatch_is_not_the_connection_error_subclass(
    tmp_path: Path,
) -> None:
    """A protocol-version mismatch happens *after* a real request reached
    the agent; it must stay a plain ``NativeProtocolError`` a
    ``backend="auto"`` caller narrowly catching ``NativeConnectionError``
    will never swallow."""
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(protocol=2))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0)
        with pytest.raises(NativeProtocolError) as excinfo:
            client.ping()
        assert not isinstance(excinfo.value, NativeConnectionError)
    finally:
        agent.close()


def test_expected_pid_mismatch_is_not_the_connection_error_subclass(
    tmp_path: Path,
) -> None:
    agent = _FakeAgent(tmp_path, [_ok(1, _ping_result(pid=4242))])
    try:
        client = NativeClient(agent.socket_path, timeout=2.0, expected_pid=9999)
        with pytest.raises(NativeProtocolError) as excinfo:
            client.ping()
        assert not isinstance(excinfo.value, NativeConnectionError)
    finally:
        agent.close()
