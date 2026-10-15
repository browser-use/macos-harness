"""protocol.py: the frame, the envelope, the transport errors, the paths."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import types

import pytest

from macos_harness import errors, model, protocol


def pair():
    return socket.socketpair()


def test_frame_is_a_4_byte_little_endian_length_then_utf8_json():
    frame = protocol.pack({"text": "héllo €"})
    body = json.dumps({"text": "héllo €"}, ensure_ascii=False, separators=(",", ":"))
    assert frame[:4] == struct.pack("<I", len(body.encode()))
    assert frame[4:].decode() == body


def test_frame_round_trip_also_in_small_pieces():
    a, b = pair()
    message = {"id": 1, "text": "x" * 100_000, "list": [1, 2.5, None, True]}
    frame = protocol.pack(message)

    def send():
        for start in range(0, len(frame), 7001):
            a.sendall(frame[start : start + 7001])

    threading.Thread(target=send).start()
    assert protocol.read(b) == message


def test_the_8_mb_limit_holds_both_ways():
    assert protocol.MAX_FRAME_BYTES == 8388608
    fits = "x" * (protocol.MAX_FRAME_BYTES - 2)  # two bytes for the quotes
    assert len(protocol.pack(fits)) == protocol.MAX_FRAME_BYTES + 4
    with pytest.raises(protocol.FrameTooLarge) as too_large:
        protocol.pack(fits + "x")
    assert (
        str(too_large.value) == "macOS Harness got a frame over the size limit: 8388609"
    )
    a, b = pair()
    a.sendall(struct.pack("<I", protocol.MAX_FRAME_BYTES + 1))
    with pytest.raises(protocol.FrameTooLarge):
        protocol.read(b)  # refused from the length alone: nothing more is read


def test_a_closed_socket_is_end_of_file_also_in_the_middle_of_a_frame():
    a, b = pair()
    a.close()
    with pytest.raises(EOFError):
        protocol.read(b)
    a, b = pair()
    a.sendall(protocol.pack({"id": 1})[:6])
    a.close()
    with pytest.raises(EOFError):
        protocol.read(b)


def test_envelope_is_json_rpc_with_argv_and_an_absolute_deadline(monkeypatch):
    monkeypatch.setenv("MACOS_HARNESS_DIFF", "0")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("SECRET_TOKEN", "never sent")
    deadline = time.time() + protocol.CALL_TIMEOUT_S
    message = protocol.request(["click", "--app", "X"], None, os.environ, deadline)
    assert set(message) == {"id", "jsonrpc", "method", "params"}
    assert (message["jsonrpc"], message["method"]) == ("2.0", "request")
    params = message["params"]
    assert set(params) == {
        "callerApi",
        "clientPath",
        "clientVersion",
        "deadlineMs",
        "argv",
        "stdin",
        "cwd",
        "env",
    }
    assert params["callerApi"] == "MacosHarnessIPC-1"
    assert params["clientVersion"] == "0.2.1"
    assert params["clientPath"] == os.path.dirname(protocol.__file__)
    assert params["deadlineMs"] == int(deadline * 1000)
    assert params["argv"] == ["click", "--app", "X"] and params["stdin"] is None
    assert params["cwd"] == os.getcwd()
    assert params["env"]["MACOS_HARNESS_DIFF"] == "0"
    assert params["env"]["DO_NOT_TRACK"] == "1"
    # The caller's home folders travel too: `install` writes into them, not into the
    # helper's. Nothing else does.
    monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere")
    sent = protocol.request([], None, os.environ, deadline)["params"]["env"]
    assert (
        sent["HOME"] == os.environ["HOME"] and sent["XDG_CONFIG_HOME"] == "/somewhere"
    )
    assert "SECRET_TOKEN" not in sent
    assert all(
        n.startswith("MACOS_HARNESS_")
        or n in ("DO_NOT_TRACK", "HOME")
        or n.endswith("_HOME")
        for n in sent
    )


def test_request_timeout_is_120_s():
    assert protocol.CALL_TIMEOUT_S == 120.0


def test_every_tool_goes_through_the_one_request_method():
    first = protocol.request(["click"], None, {}, 0)
    second = protocol.request(["turn-ended"], "text", {}, 0)
    assert first["method"] == second["method"] == "request"
    assert second["id"] == first["id"] + 1 and second["params"]["stdin"] == "text"
    assert protocol.control("ping")["method"] == "ping"
    assert "params" not in protocol.control("shutdown")


def test_reply_shapes():
    assert protocol.reply(7, 1, "out", "err") == {
        "id": 7,
        "jsonrpc": "2.0",
        "result": {"exit": 1, "stdout": "out", "stderr": "err"},
    }
    assert protocol.result(7, {"needStdin": True})["result"] == {"needStdin": True}


def test_transport_error_texts():
    said = {name: protocol.error(1, name)["error"] for name in protocol.ERRORS}
    assert said["methodNotFound"]["message"] == "Method not found"
    assert (
        said["responseTooLarge"]["message"]
        == "The response is larger than one frame may be"
    )
    assert (
        said["deadlineExceeded"]["message"] == "The deadline of the request has passed"
    )
    assert (
        said["invalidRequest"]["message"]
        == "The id of a JSON-RPC message has to be a string or a number."
    )
    assert said["notAuthenticated"] == {
        "code": 101,
        "message": "The calling process may not use this socket",
        "errorName": "notAuthenticated",
        "data": None,
    }
    assert (
        said["stopping"]["message"]
        == "The macOS Harness helper is shutting down. Run the command again."
    )


def test_version_mismatch_text_says_what_to_do():
    error = protocol.error(1, "versionMismatch", protocol.identity())["error"]
    assert error["code"] == 102
    assert error["message"].startswith(
        "The macOS Harness helper and client have a version mismatch."
    )
    assert "macos-harness helper restart" in error["message"]
    assert error["data"] == {
        "apiVersion": "MacosHarnessIPC-1",
        "version": "0.2.1",
        "path": os.path.dirname(protocol.__file__),
    }


def test_the_json_form_of_a_failed_call():
    took_over = errors.still_interacting("/Applications/Notes.app")
    assert protocol.failure(took_over)["error"] == {
        "code": 110,
        "name": "user_took_over",
        "message": took_over.message,
        "cancelled": True,
    }
    assert (
        protocol.failure(errors.stale_element())["error"]["tag"] == "invalid_element_id"
    )


def test_the_shared_numbers_agree_with_model():
    assert protocol.API_VERSION == model.API_VERSION
    assert protocol.MAX_FRAME_BYTES == model.MAX_FRAME_BYTES
    assert protocol.CALL_TIMEOUT_S == model.CALL_TIMEOUT_S


def test_socket_path_default_override_and_long_path(monkeypatch, tmp_path):
    monkeypatch.delenv("MACOS_HARNESS_SOCKET")
    monkeypatch.delenv("MACOS_HARNESS_HOME")
    home = os.path.expanduser("~/Library/Application Support/macos-harness")
    assert protocol.socket_path() == f"{home}/helper.sock"
    assert protocol.lock_path() == f"{home}/helper.lock"
    assert protocol.log_path() == f"{home}/helper.log"
    assert str(protocol.state_dir()) == home
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / ("deep" * 30)))
    assert protocol.socket_path() == f"/tmp/macos-harness-{os.getuid()}/helper.sock"
    assert protocol.lock_path() == f"/tmp/macos-harness-{os.getuid()}/helper.lock"
    monkeypatch.setenv("MACOS_HARNESS_SOCKET", "/tmp/rig/one.sock")
    assert protocol.socket_path() == "/tmp/rig/one.sock"
    assert protocol.lock_path() == "/tmp/rig/one.lock"  # one helper per socket
    assert protocol.log_path().startswith(str(tmp_path))


def test_a_socket_folder_must_be_this_user_s_alone(tmp_path, monkeypatch):
    """A long state path falls back to /tmp, where another user can make the folder
    first and listen in it: the client then sent its arguments, stdin text and
    environment there."""
    folder = tmp_path / "macos-harness-501"
    protocol.own_folder(str(folder))  # not there yet: the helper will make it
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    protocol.own_folder(str(folder))
    assert folder.stat().st_mode & 0o777 == 0o700  # ours but open: closed
    me = folder.stat().st_uid
    monkeypatch.setattr(protocol.os, "getuid", lambda: me + 1)
    with pytest.raises(PermissionError, match="belongs to another user"):
        protocol.own_folder(str(folder))


def test_a_link_another_user_made_to_a_folder_of_ours_is_no_socket_folder(
    tmp_path, monkeypatch
):
    """The check followed links: another user linked the /tmp name to a folder of the
    victim that others may write into and listened there. The link was accepted, and
    the folder it led to was closed to 0700."""
    target, name = tmp_path / "drop-box", tmp_path / "macos-harness-501"
    target.mkdir()
    target.chmod(0o733)
    name.symlink_to(target)
    real, me = os.lstat, os.getuid()
    theirs = lambda path: types.SimpleNamespace(
        st_uid=me + 1, st_mode=real(path).st_mode
    )
    monkeypatch.setattr(protocol.os, "lstat", theirs)
    with pytest.raises(PermissionError, match="belongs to another user"):
        protocol.own_folder(str(name))
    assert target.stat().st_mode & 0o777 == 0o733  # untouched
    monkeypatch.setattr(protocol.os, "lstat", real)
    protocol.own_folder(str(name))  # a link of our own to a folder of our own is fine
    file = tmp_path / "file"
    file.write_text("")
    with pytest.raises(PermissionError, match="is not a folder"):
        protocol.own_folder(str(file))
