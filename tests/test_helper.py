"""helper.py: who is calling, sessions, the per-app queue, the socket, start and exit."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import types

import fakes_a
import pytest
from fakes import Clock
from fakes_a import RUNNING, TARGET

from macos_harness import errors, helper, protocol, tools
from macos_harness.model import AppTarget

ME = os.getuid()


@pytest.fixture
def world(monkeypatch):
    return fakes_a.install(monkeypatch)


def message(*argv, stdin=None, env=None, **changed):
    made = protocol.request(list(argv), stdin, env or {}, time.time() + 120)
    made["params"].update(changed)
    return made


def answer(*argv, **more):
    return helper.answer_for(message(*argv, **more), os.getpid(), ME)


# --- who is calling ---------------------------------------------------------------------


def tree(*rows):
    """A fake process table: (pid, parent pid, executable path)."""
    table = {pid: (parent, path, 1000.0 + pid) for pid, parent, path in rows}
    return helper.Sessions(process=table.get)


TERMINAL = "/Applications/Term.app/Contents/MacOS/term"
AGENT = "/usr/local/bin/agent"
PLAIN = (
    (1, 0, "/sbin/launchd"),
    (10, 1, TERMINAL),
    (20, 10, "/bin/zsh"),
    (30, 20, AGENT),
    (40, 30, "/bin/zsh"),
    (50, 40, "/usr/bin/python3"),  # the client
    (60, 30, "/bin/bash"),
    (70, 60, "/usr/bin/python3"),  # the client of the next command
)


def test_session_key_from_the_environment():
    sessions = tree(*PLAIN)
    agent = sessions.agent(50, {"MACOS_HARNESS_SESSION": "run-7"})
    assert agent.key == "run-7"
    assert sessions.agent(70, {"MACOS_HARNESS_SESSION": "run-7"}) is agent
    assert sessions.agent(50, {}) is not agent


def test_session_key_is_the_first_parent_that_is_not_a_shell():
    sessions = tree(*PLAIN)
    agent = sessions.agent(50, {})
    assert agent.key == "30:1030.0"  # pid and start time of the agent
    assert sessions.agent(70, {}) is agent  # another shell, the same agent


def test_wrappers_between_the_shell_and_the_agent_do_not_split_the_session():
    assert "sandbox-exec" in helper.WRAPPERS
    wrapped = [(40, 35, "/bin/zsh"), (35, 30, "/usr/bin/sandbox-exec")]
    wrapped += [
        (60, 55, "/bin/bash"),
        (55, 52, "/usr/bin/env"),
        (52, 30, "/usr/bin/time"),
    ]
    rows = [row for row in PLAIN if row[0] not in (40, 60)] + wrapped
    sessions = tree(*rows)
    assert sessions.agent(50, {}).key == "30:1030.0"
    assert sessions.agent(70, {}) is sessions.agent(50, {})


def test_a_login_shell_is_a_shell_and_two_agents_are_two_sessions():
    rows = [(1, 0, "/sbin/launchd"), (20, 1, "-zsh"), (30, 20, AGENT), (31, 20, AGENT)]
    rows += [(50, 30, "/usr/bin/python3"), (51, 31, "/usr/bin/python3")]
    sessions = tree(*rows)
    assert sessions.agent(50, {}).key == "30:1030.0"
    assert sessions.agent(51, {}).key == "31:1031.0"
    assert tree((1, 0, "/sbin/launchd"), (5, 1, "/bin/sh"), (6, 5, "x")).agent(
        6, {}
    ).key == ("1:1001.0")


def test_a_process_that_is_gone_ends_the_walk():
    assert tree((50, 40, "/usr/bin/python3")).agent(50, {}).key == "no-agent"
    assert tree().agent(50, {}).key == "no-agent"


def test_hosts_are_every_app_bundle_on_the_parent_chain():
    nested = (
        "/Applications/Desk.app/Contents/Frameworks/Desk Helper.app/Contents/MacOS/h"
    )
    rows = [(1, 0, "/sbin/launchd"), (10, 1, TERMINAL), (20, 10, nested)]
    rows += [
        (30, 20, "/bin/zsh"),
        (50, 30, "/Applications/Client.app/Contents/MacOS/c"),
    ]
    agent = tree(*rows).agent(50, {})
    assert agent.hosts == {
        "/Applications/Term.app",
        "/Applications/Desk.app",
        "/Applications/Desk.app/Contents/Frameworks/Desk Helper.app",
    }  # the client itself is not a host
    assert tree(*PLAIN).agent(50, {"MACOS_HARNESS_SESSION": "k"}).hosts == {
        "/Applications/Term.app"
    }


def test_the_environment_of_the_call_replaces_the_last_one():
    sessions = tree(*PLAIN)
    agent = sessions.agent(50, {"MACOS_HARNESS_DIFF": "0"})
    assert agent.env == {"MACOS_HARNESS_DIFF": "0"}
    assert sessions.agent(70, {"MACOS_HARNESS_POINTER": "0"}).env == {
        "MACOS_HARNESS_POINTER": "0"
    }


def test_app_sessions_are_per_agent_and_app(world):
    sessions = tree(*PLAIN)
    sessions.revisions = object
    one, two = (
        sessions.agent(50, {}),
        sessions.agent(50, {"MACOS_HARNESS_SESSION": "b"}),
    )
    app = sessions.app(one, TARGET)
    assert sessions.app(one, RUNNING) is app  # the path is the key, not the pid
    assert sessions.app(two, TARGET) is not app
    assert app.revisions is not sessions.app(two, TARGET).revisions
    other = sessions.app(one, AppTarget("/Applications/Other.app", "b", "Other"))
    assert sessions.apps(one) == [app, other]
    sessions.drop(one)
    assert sessions.apps(one) == [] and len(sessions.apps(two)) == 1
    assert sessions.agent(50, {}) is one  # the agent keeps its turn marks


def test_an_agent_whose_process_exited_is_forgotten_once():
    table = {pid: (parent, path, 1000.0 + pid) for pid, parent, path in PLAIN}
    sessions = helper.Sessions(process=table.get)
    agent = sessions.agent(50, {})
    named = sessions.agent(50, {"MACOS_HARNESS_SESSION": "run-7"})
    assert sessions.gone() == []
    table[30] = (20, AGENT, 9999.0)  # the pid was given to a new process
    table[40] = (30, "/bin/zsh", 9998.0)  # which started a shell of its own
    assert sessions.gone() == [agent] and sessions.gone() == []
    assert sessions.agent(50, {}) is not agent
    del table[30]
    assert len(sessions.gone()) == 1
    assert sessions.agent(50, {"MACOS_HARNESS_SESSION": "run-7"}) is named  # no process


def test_a_shell_its_host_left_running_gets_no_session_of_its_own():
    """The host died while its shell ran `get_app_state && click && type_text`. The
    shell lives on under launchd: its next command is refused as a turn that is over,
    not given a session of its own whose host never dies."""
    table = {pid: (parent, path, 1000.0 + pid) for pid, parent, path in PLAIN}
    sessions = helper.Sessions(process=table.get)
    first = sessions.agent(50, {})
    del table[30]
    table[40] = (1, "/bin/zsh", 1040.0)  # the same shell, now under launchd
    table[55] = (40, "/usr/bin/python3", 1055.0)  # its next command
    orphan = sessions.agent(55, {})
    assert (orphan.key, orphan.exited) == (first.key, True)
    table[66] = (1, "/bin/sh", 1066.0)  # a launchd job that runs a shell itself
    table[77] = (66, "/usr/bin/python3", 1077.0)
    assert not sessions.agent(77, {}).exited
    del table[40]
    sessions.gone()
    assert (40, 1040.0) not in sessions._shells  # a shell's record goes with it


# A client that prints its reply, from a process of its own.
REPLYING_CLIENT = """
import json, socket, sys, time
from macos_harness import protocol
message = protocol.request(sys.argv[1:], None, {}, time.time() + 120)
with socket.socket(socket.AF_UNIX) as sock:
    sock.connect(protocol.socket_path())
    print(json.dumps(protocol.call(sock, message)["result"]), flush=True)
"""


def test_a_chain_its_dead_host_left_running_is_refused_but_a_launchd_job_is_not(
    served, tmp_path
):
    """The host dies during `get_app_state && sleep && get_app_state`: its shell goes
    on under launchd, and its next command is refused. A job whose shell launchd
    started by itself is served."""
    client = shlex.join(
        [sys.executable, "-c", REPLYING_CLIENT, "get_app_state", "Demo"]
    )
    go = tmp_path / "go"
    chain = f"{client} && until [ -e {go} ]; do sleep 0.02; done && {client}"
    spawn = (
        "import subprocess, sys, time; subprocess.Popen(['/bin/sh', '-c', sys.argv[1]])"
    )
    host = subprocess.Popen(
        [sys.executable, "-c", spawn + "; time.sleep(60)", chain],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert json.loads(host.stdout.readline())["exit"] == 0
        host.kill()
        host.wait()
        go.touch()
        refused = json.loads(host.stdout.readline())
        assert refused["stderr"] == errors.agent_exited().message + "\n"
    finally:
        host.kill()
        go.touch()
    job_go = tmp_path / "job-go"
    job = f"until [ -e {job_go} ]; do sleep 0.02; done && {client}"
    starter = subprocess.Popen(
        [sys.executable, "-c", spawn, job], stdout=subprocess.PIPE, text=True
    )
    starter.wait()  # gone before its shell's first command: that shell is launchd's
    job_go.touch()
    assert json.loads(starter.stdout.readline())["exit"] == 0


def lazy_module(slow=0.05) -> types.ModuleType:
    """A framework module as PyObjC builds it: a name is bound on first use, and its
    table entry is popped then."""
    module = types.ModuleType("Framework")
    names = vars(module)

    def createFrameworkDirAndGetattr():  # the name PyObjC gives it
        funcmap = {"AXUIElementCreateApplication": "b@i"}

        def __getattr__(name):
            if name in funcmap:
                time.sleep(slow)  # loading the function from the bundle
                funcmap.pop(name)
                names[name] = f"bound {name}"
                return names[name]
            raise AttributeError(name)

        return __getattr__

    names["__getattr__"] = createFrameworkDirAndGetattr()
    return module


def first_use_from_eight_threads(module) -> list:
    start, results = threading.Barrier(8), []

    def use():
        start.wait()
        try:
            results.append(module.AXUIElementCreateApplication)
        except Exception as error:  # noqa: BLE001 - what each thread saw
            results.append(error)

    threads = [threading.Thread(target=use) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    return results


def test_eight_threads_that_first_use_one_name_at_once_all_get_it():
    raced = first_use_from_eight_threads(lazy_module())
    assert any(isinstance(r, KeyError) for r in raced)  # the bug, without the lock
    module = lazy_module()
    helper.one_lookup_at_a_time([module, os])  # a plain module is left alone
    assert (
        first_use_from_eight_threads(module)
        == ["bound AXUIElementCreateApplication"] * 8
    )
    with pytest.raises(AttributeError):
        getattr(module, "NoSuchName")  # noqa: B009


def test_the_real_frameworks_get_the_lock():
    pytest.importorskip("ApplicationServices")
    helper.one_lookup_at_a_time()
    import AppKit
    import ApplicationServices
    import CoreFoundation
    import Foundation
    import HIServices
    import Quartz

    for module in (
        AppKit,
        ApplicationServices,
        CoreFoundation,
        Foundation,
        HIServices,
        Quartz,
    ):
        assert vars(module)["__getattr__"].func is helper._looked_up
    assert ApplicationServices.AXUIElementCreateApplication  # still finds names


def test_the_real_process_table_and_the_real_socket_peer():
    parent, path, started = helper.process(os.getpid())
    # A framework build runs as .../Python.app/Contents/MacOS/Python.
    assert parent == os.getppid() and os.path.basename(path).lower().startswith(
        "python"
    )
    assert time.time() - 3600 < started <= time.time()
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert helper.process(child.pid) is None
    a, _other_end = socket.socketpair()
    assert helper.peer(a) == (os.getpid(), ME)


# --- one call at a time per app, and the display stays awake ---------------------------


def test_calls_on_one_app_run_in_arrival_order(world):
    order, gates = [], [threading.Event() for _ in range(4)]

    def call(number):
        gates[number].set()
        with helper.app_queue(TARGET):
            order.append(number)
            time.sleep(0.02)

    with helper.app_queue(TARGET):
        threads = [threading.Thread(target=call, args=(n,)) for n in range(4)]
        for number, thread in enumerate(threads):
            thread.start()
            gates[number].wait()
            time.sleep(0.02)  # let it reach the queue before the next one starts
    for thread in threads:
        thread.join()
    assert order == [0, 1, 2, 3]


def test_calls_on_different_apps_run_at_the_same_time(world):
    other = AppTarget("/Applications/Other.app", "b", "Other")
    entered = threading.Event()

    def call():
        with helper.app_queue(other):
            entered.set()

    with helper.app_queue(TARGET):
        threading.Thread(target=call).start()
        assert entered.wait(2)


def test_the_display_is_kept_awake_from_the_first_call_to_the_last(world):
    other = AppTarget("/Applications/Other.app", "b", "Other")
    with helper.app_queue(TARGET):
        with helper.app_queue(other):
            assert world.names() == ["awake.acquire"]
        assert world.names() == ["awake.acquire"]
    assert world.names() == ["awake.acquire", "awake.release"]
    assert world.args("awake.release") == (1,)
    with helper.app_queue(TARGET):
        pass
    assert world.names().count("awake.acquire") == 2
    assert len(world.activity) == 3  # every call says that the Mac is in use


def test_the_queue_moves_on_when_a_call_raises(world):
    with pytest.raises(RuntimeError), helper.app_queue(TARGET):
        raise RuntimeError("the call failed")
    with helper.app_queue(TARGET):  # would wait for ever if the queue were stuck
        pass
    assert world.names().count("awake.release") == 2


def test_the_awake_texts_name_what_is_going_on():
    kind, name, reason, activity = helper.AWAKE_TEXTS
    assert kind == "PreventUserIdleDisplaySleep"
    assert name == "macOS Harness at work"
    assert reason == "macOS Harness is working on a request and keeps the display on."
    assert activity == "macOS Harness: the agent is active"


def test_a_failing_awake_call_does_not_fail_the_tool_call():
    def fail():
        raise OSError("no power service")

    with helper.Awake(fail, fail, fail):
        pass


# --- one request ------------------------------------------------------------------------


def test_ping_says_who_the_helper_is():
    reply, _ = helper.answer_for(protocol.control("ping"), os.getpid(), ME)
    assert reply["result"] == {**protocol.identity(), "pid": os.getpid()}


def test_a_caller_of_another_user_is_refused(world):
    reply, _ = helper.answer_for(message("get_app_state", "Demo"), 1, ME + 1)
    assert reply["error"]["message"] == "The calling process may not use this socket"
    assert reply["error"]["code"] == 101 and world.log == []


def test_bad_method_and_bad_id():
    reply, _ = helper.answer_for({"id": 3, "method": "listApps"}, 1, ME)
    assert reply["error"]["message"] == "Method not found"
    for bad in (None, 1.5, True, [1]):
        reply, _ = helper.answer_for({"id": bad, "method": "ping"}, 1, ME)
        assert (
            reply["error"]["message"]
            == "The id of a JSON-RPC message has to be a string or a number."
        )
    assert "result" in helper.answer_for({"id": "a-1", "method": "ping"}, 1, ME)[0]


@pytest.mark.parametrize(
    "changed",
    [
        {"callerApi": "MacosHarnessIPC-0"},
        {"clientVersion": "0.1.2"},
        {"clientPath": "/another/checkout/macos_harness"},
    ],
)
def test_other_code_on_the_client_side_is_a_version_mismatch(world, changed):
    reply, _ = answer("get_app_state", "Demo", **changed)
    assert reply["error"]["errorName"] == "versionMismatch"
    assert reply["error"]["data"] == protocol.identity()
    assert world.log == []  # the call was not run: the client may send it again


def test_a_call_past_its_deadline_is_not_run(world):
    reply, _ = answer("get_app_state", "Demo", deadlineMs=1000)
    assert reply["error"]["message"] == "The deadline of the request has passed"
    assert world.log == []


def test_a_request_runs_the_tool_and_carries_its_output(world):
    reply, name = answer("get_app_state", "Demo")
    assert name is None and reply["result"]["exit"] == 0
    assert reply["result"]["stdout"].startswith(
        'Window: "Demo", App: Demo.\n0 button OK\n'
    )
    assert reply["result"]["stderr"] == ""
    assert "apps.resolve" in world.names()


def test_the_deadline_reaches_the_gates_as_seconds(world):
    sent = message("get_app_state", "Demo")
    helper.answer_for(sent, os.getpid(), ME)
    deadline = sent["params"]["deadlineMs"] / 1000
    assert world.args("permissions.gate") == (deadline,)
    assert world.args("approvals.ensure")[3] == deadline


def test_the_call_environment_reaches_the_tool(world):
    env = {"MACOS_HARNESS_SESSION": "s", "MACOS_HARNESS_DIFF": "0"}
    answer("get_app_state", "Demo", env=env)
    agent = world.args("policy.decide")[0]
    assert (agent.key, agent.env) == ("s", env)
    assert world.args("revisions.present")[1] is True  # MACOS_HARNESS_DIFF=0


def test_a_call_that_wants_stdin_is_answered_before_it_does_anything(world):
    reply, _ = answer("type_text", "Demo")
    assert reply["result"] == {"needStdin": True} and world.log == []
    reply, _ = answer("type_text", "Demo", stdin="hi\n")
    assert reply["result"]["exit"] == 1  # run now (and not active: no state read yet)


def test_a_failed_tool_call_names_its_error(world):
    pending = errors.permissions_pending("Terminal", "Accessibility")
    world.permissions.answers["gate"] = pending
    reply, name = answer("get_app_state", "Demo")
    assert name == "permissions_waiting" and reply["result"]["exit"] == 1
    assert reply["result"]["stderr"] == pending.message + "\n"


def test_each_call_leaves_one_line_in_the_log_that_names_no_app(world, capsys):
    answer("get_app_state", "Demo")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert " call get_app_state exit=0 " in line and line.endswith(" ms")
    assert "Demo" not in line


def test_telemetry_gets_the_command_and_nothing_about_the_app(world, monkeypatch):
    seen = []
    monkeypatch.setattr(helper.telemetry, "capture_cli", lambda *a: seen.append(a))
    world.person.answers["gate"] = errors.still_interacting("/Applications/Demo.app")
    answer("get_app_state", "Demo", env={"MACOS_HARNESS_TELEMETRY": "0"})
    command, success, seconds, env, error_name = seen[0]
    assert (command, success, error_name) == (
        "get_app_state",
        False,
        "user_took_over",
    )
    assert env == {"MACOS_HARNESS_TELEMETRY": "0"} and 0 <= seconds < 5


# --- the socket -------------------------------------------------------------------------


@pytest.fixture
def served(world):
    """A helper that serves the test's socket from a thread of this process."""
    lock, shutdown = helper._take_lock(), helper.shutdown  # a test may patch shutdown
    sock = helper._bind()
    thread = threading.Thread(target=helper.serve, args=(sock,), daemon=True)
    thread.start()
    yield world
    shutdown()
    thread.join(2)
    os.close(lock)


def call(sent):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(protocol.socket_path())
        return protocol.call(sock, sent)


def test_the_socket_and_its_folder_are_private(served):
    path = protocol.socket_path()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700


def test_a_call_through_the_real_socket(served):
    reply = call(message("get_app_state", "--app", "Demo"))
    assert reply["result"]["exit"] == 0
    assert reply["result"]["stdout"].endswith(fakes_a.SHOT.path + "\n")
    assert call(protocol.control("ping"))["result"]["pid"] == os.getpid()


def test_one_connection_carries_one_request(served):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(protocol.socket_path())
        assert "result" in protocol.call(sock, protocol.control("ping"))
        with pytest.raises((EOFError, OSError)):  # the helper closed it after one reply
            protocol.call(sock, protocol.control("ping"))


def test_a_silent_client_is_dropped_and_blocks_nobody(served, monkeypatch):
    monkeypatch.setattr(helper, "IO_TIMEOUT_S", 0.1)
    assert helper.IO_TIMEOUT_S == 0.1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.connect(protocol.socket_path())
        assert "result" in call(protocol.control("ping"))  # served meanwhile
        silent.settimeout(2)
        assert silent.recv(1) == b""  # the helper closed it


def test_the_io_timeout_is_ten_seconds_per_frame():
    assert helper.IO_TIMEOUT_S == 10.0


def test_a_reply_that_is_too_large_becomes_an_error(served):
    served.install.answers["skill_text"] = "x" * (protocol.MAX_FRAME_BYTES + 1)
    reply = call(message("skill"))
    assert reply["error"]["message"] == "The response is larger than one frame may be"


def test_a_garbage_frame_is_dropped(served):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(protocol.socket_path())
        sock.sendall(b"\x05\x00\x00\x00nope!")
        assert sock.recv(1) == b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(protocol.socket_path())
        sock.sendall(protocol.pack(["not", "a", "message"]))
        assert sock.recv(1) == b""
    assert "result" in call(protocol.control("ping"))


def test_shutdown_waits_for_the_running_call_and_refuses_new_ones(served):
    """`macos-harness helper stop` also ends what runs: the call stops at its next
    step and says so, then the helper goes."""
    entered, leave, done = threading.Event(), threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        leave.wait(5)

    served.person.answers["gate"] = slow
    replies = []
    running = threading.Thread(
        target=lambda: replies.append(call(message("get_app_state", "Demo")))
    )
    running.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=lambda: (helper.shutdown(), done.set()))
    stopper.start()
    time.sleep(0.2)
    assert not done.is_set()  # the running call is not finished yet
    late, _ = answer("get_app_state", "Demo")
    assert late["error"]["errorName"] == "stopping"
    leave.set()
    assert done.wait(2)
    running.join(2)
    result = replies[0]["result"]  # the running call got its answer: cancelled
    assert result["exit"] == 1 and result["stderr"] == (
        errors.helper_stopped().message + "\n"  # not "did not answer within 2 minutes"
    )
    with pytest.raises(OSError), socket.socket(socket.AF_UNIX) as sock:
        sock.connect(protocol.socket_path())  # nobody accepts any more


# A client that sends one call and waits for its reply, as the real one does.
WAITING_CLIENT = """
import socket, sys, time
from macos_harness import protocol
env = {"MACOS_HARNESS_SESSION": "cut"}
message = protocol.request(sys.argv[1:], None, env, time.time() + 120)
with socket.socket(socket.AF_UNIX) as sock:
    sock.connect(protocol.socket_path())
    protocol.call(sock, message)
"""


def slow_typing(served) -> list[str]:
    """type_text types one key per 10 ms, 5 s for 500, and notes where it stopped."""
    typed = []

    def type_text(app, text):
        for key in text:
            if app.cancel():
                typed.append("cancelled")
                raise errors.user_changed(app.target.path)  # what a cancelled one says
            typed.append(key)
            time.sleep(0.01)

    served.actions.answers["type_text"] = type_text
    return typed


def typing_began(typed: list[str]) -> float:
    deadline = time.time() + 10
    while len(typed) < 5:
        assert time.time() < deadline, "typing never began"
        time.sleep(0.005)
    return deadline


def keys_until_cancelled(typed: list[str], deadline: float) -> int:
    at = len(typed)
    while typed[-1] != "cancelled":
        assert time.time() < deadline, "typing went on to the end"
        time.sleep(0.005)
    return len(typed) - at


def test_a_killed_client_stops_its_running_action_at_the_next_key(served):
    """The host's interrupt is the stop button: Ctrl+C or Esc ends the tool's process,
    and a long type_text, hold or drag stops at its next event."""
    typed = slow_typing(served)
    session = {"MACOS_HARNESS_SESSION": "cut"}
    assert call(message("get_app_state", "Demo", env=session))["result"]["exit"] == 0
    client = subprocess.Popen(
        [sys.executable, "-c", WAITING_CLIENT, "type_text", "Demo", "x" * 500]
    )
    deadline = typing_began(typed)
    client.kill()
    client.wait()
    assert keys_until_cancelled(typed, deadline) <= 2  # the key on its way, no other


# What a host runs: two calls, the second one left running. It prints that one's pid.
HOST = """
import json, subprocess, sys, time
client = json.loads(sys.argv[1])
subprocess.run(client + ["get_app_state", "Demo"], stdout=subprocess.DEVNULL)
print(subprocess.Popen(client + ["type_text", "Demo", "x" * 500]).pid, flush=True)
time.sleep(60)
"""


def keys_after_the_host_is_killed(served, client: list[str], env: dict) -> int:
    """`codex exec` and `pi -p` die on Ctrl+C and leave their command running."""
    typed = slow_typing(served)
    host = subprocess.Popen(
        [sys.executable, "-c", HOST, json.dumps(client)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    pid = None
    try:
        pid = int(host.stdout.readline())
        deadline = typing_began(typed)
        host.kill()
        host.wait()
        return keys_until_cancelled(typed, deadline)
    finally:
        host.kill()
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_a_client_whose_host_died_ends_and_its_typing_stops(served):
    """The client is a child of the host: it sees its parent go and exits."""
    env = {**os.environ, "MACOS_HARNESS_SESSION": "cut"}  # the helper knows no process
    assert keys_after_the_host_is_killed(served, fakes_a.CLIENT, env) < 40  # 0.4 s


def test_a_call_whose_agent_process_died_stops_although_its_client_lives(served):
    """A client that does not watch its parent (one with a shell between them): the
    helper sees the agent's process go."""
    waiting = WAITING_CLIENT.replace('{"MACOS_HARNESS_SESSION": "cut"}', "{}")
    client = [sys.executable, "-c", waiting]
    keys = keys_after_the_host_is_killed(served, client, dict(os.environ))
    assert keys < 40  # 0.4 s: the helper looks every 0.25 s


def test_a_cancelling_turn_end_stops_the_action_its_agent_left_running(served):
    """Codex's Interrupt, Pi's agent_end, a session's end: typing stops at the next
    key."""
    typed, replies = slow_typing(served), []
    session = {"MACOS_HARNESS_SESSION": "cut"}
    assert call(message("get_app_state", "Demo", env=session))["result"]["exit"] == 0
    typing = threading.Thread(
        target=lambda: replies.append(
            call(message("type_text", "Demo", "x" * 500, env=session))
        )
    )
    typing.start()
    deadline = typing_began(typed)
    cancel = message("turn-ended", "--cancel", env=session)
    ended = threading.Thread(target=lambda: call(cancel))
    ended.start()
    assert keys_until_cancelled(typed, deadline) <= 2
    ended.join(2)
    typing.join(2)
    assert not ended.is_alive()  # it let go of the app once the action stopped
    # The host left the command running: its output may still be read.
    assert replies[0]["result"]["stderr"] == errors.agent_left().message + "\n"


def test_claude_code_s_stop_leaves_a_background_agent_s_action_running(served):
    """Stop comes when the main turn ends, while background agents still type."""
    typed = slow_typing(served)
    session = {"MACOS_HARNESS_SESSION": "cut"}
    assert call(message("get_app_state", "Demo", env=session))["result"]["exit"] == 0
    text, replies = "x" * 60, []
    typing = threading.Thread(
        target=lambda: replies.append(
            call(message("type_text", "Demo", text, env=session))
        )
    )
    typing.start()
    typing_began(typed)
    assert call(message("turn-ended", env=session))["result"]["exit"] == 0
    typing.join(5)
    assert typed == list(text) and replies[0]["result"]["exit"] == 0


def test_a_killed_client_waiting_for_the_person_frees_the_app_within_a_second(served):
    """Ctrl+C on a call that waits, inside the app's queue, for the person to finish:
    the next call on that app got in only when the wait ran out (up to 100 s)."""
    calls, waiting = [], threading.Event()

    def gate(agent, target, state_read, abandoned=lambda: False):
        calls.append(1)
        if len(calls) == 2:  # the second gate, in the queue: the person is busy
            waiting.set()
            while not abandoned():
                time.sleep(0.01)
            raise errors.request_timed_out()

    served.person.answers["gate"] = gate
    client = subprocess.Popen(
        [sys.executable, "-c", WAITING_CLIENT, "get_app_state", "Demo"]
    )
    assert waiting.wait(10), "the call never reached the person's wait"
    client.kill()
    client.wait()
    began = time.monotonic()
    with helper.app_queue(TARGET):
        assert time.monotonic() - began < 1.0


def test_a_permission_error_makes_the_helper_exit_after_the_reply(served, monkeypatch):
    stops = []
    monkeypatch.setattr(helper, "shutdown", lambda: stops.append(helper.STATE.running))
    reply = call(message("get_app_state", "Demo"))
    assert reply["result"]["exit"] == 0 and stops == []
    for error in (
        errors.permissions_pending("Terminal", "Accessibility"),
        errors.permissions_not_granted("Accessibility", "Terminal"),
    ):
        served.permissions.answers["gate"] = error
        reply = call(message("get_app_state", "Demo"))
        assert reply["result"]["stderr"] == error.message + "\n"  # sent first
    deadline = time.time() + 2
    while len(stops) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert stops == [0, 0]  # after the call was counted out


def test_other_errors_do_not_end_the_helper(served, monkeypatch):
    stops = []
    monkeypatch.setattr(helper, "shutdown", lambda: stops.append(1))
    served.person.answers["gate"] = errors.still_interacting("/Applications/Demo.app")
    call(message("get_app_state", "Demo"))
    time.sleep(0.05)
    assert stops == []


# --- idle exit, the lock, start and exit ------------------------------------------------


def test_idle_exit_after_fifteen_minutes_with_no_call(world, monkeypatch):
    assert helper.IDLE_EXIT_S == 900
    clock, stops = Clock(), []

    def stop():
        stops.append(clock.now)
        helper.STATE.stopping = True

    monkeypatch.setattr(helper, "shutdown", stop)
    helper.STATE.last_call = clock.now
    helper.idle_loop(clock)
    assert stops == [clock.now] and 900 <= stops[0] - 1000 < 900 + helper.IDLE_TICK_S


def test_an_agent_that_exited_is_let_go_like_a_turn_end(world, monkeypatch):
    clock = Clock()
    agent = helper.SESSIONS.agent(os.getpid(), {})  # found by its process
    world.run("get_app_state", "Demo", agent=agent)
    world.log.clear()
    helper.SESSIONS.process = lambda pid: None  # every process is gone now
    monkeypatch.setattr(helper, "IDLE_EXIT_S", 5)
    helper.STATE.last_call = clock.now
    helper.idle_loop(clock)  # one tick, then the idle exit
    released = ["focus.release", "tree.disable", "windows.unwatch", "revisions.reset"]
    assert world.names()[:1] == ["person.turn_ended"]
    assert [name for name in world.names() if name in released] == released
    assert helper.SESSIONS.apps(agent) == []
    helper.STATE.left.wait(2)


def test_a_running_call_keeps_the_helper_alive(world, monkeypatch):
    class Busy(Clock):
        def sleep(self, seconds):
            super().sleep(seconds)
            if self.now >= 3000 and helper.STATE.running:  # the long call ends here
                helper.STATE.running, helper.STATE.last_call = 0, self.now

    clock, stops = Busy(), []

    def stop():
        stops.append(clock.now)
        helper.STATE.stopping = True

    monkeypatch.setattr(helper, "shutdown", stop)
    helper.STATE.running, helper.STATE.last_call = 1, clock.now
    helper.idle_loop(clock)
    assert stops == [3000 + 900]


def test_a_second_helper_cannot_take_the_lock_of_a_serving_one(served):
    started = time.time()
    assert helper._take_lock() is None
    assert time.time() - started < 1  # it saw a live helper and left at once


def test_a_lock_that_is_being_given_up_is_waited_for(world):
    first = helper._take_lock()
    threading.Timer(0.3, os.close, [first]).start()
    second = helper._take_lock()  # nobody serves: the holder is on its way out
    assert second is not None
    os.close(second)


def test_a_socket_file_with_no_lock_holder_is_stale(world):
    path = protocol.socket_path()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.close()  # the file stays, nobody listens: a helper that was killed
    lock = helper._take_lock()
    sock = helper._bind()
    assert lock is not None and os.path.exists(path)
    sock.close()
    os.close(lock)


def test_start_and_exit_order(world, monkeypatch):
    monkeypatch.setattr(helper, "_on_signals", lambda: None)
    running = []  # the helper's own threads at the moment the loop starts

    def run(then=lambda: None):
        names = [thread.name for thread in threading.enumerate()]
        running.extend(
            n for n in names if n.endswith(("(serve)", "(idle_loop)", "(_exit_cap)"))
        )
        world.log.append(("runloop.run", (), {}))
        then()

    monkeypatch.setattr(helper.runloop, "run", run)
    seen = {}

    def client():
        deadline = time.time() + 5
        while not os.path.exists(protocol.socket_path()) and time.time() < deadline:
            time.sleep(0.01)
        seen["ping"] = call(protocol.control("ping"))
        seen["call"] = call(message("get_app_state", "Demo"))
        seen["before"] = world.names()
        helper.shutdown()

    thread = threading.Thread(target=client)
    thread.start()
    helper.main()
    # No thread of the helper was started before the loop ran: their first calls to
    # the main thread would have run on their own thread.
    assert running == []
    thread.join(5)
    assert seen["ping"]["result"]["pid"] == os.getpid()
    assert seen["call"]["result"]["exit"] == 0
    start = [n for n in seen["before"] if n not in ("apps.prewarm", "runloop.run")][:4]
    assert start == [
        "pasteboard.restore_saved",
        "screenshot.cleanup",
        "person.start_watch",
        "apps.resolve",  # the first call: served only after the three steps
    ]
    assert {"apps.prewarm", "runloop.run"} <= set(seen["before"])  # each on its thread
    assert world.args("person.start_watch") == (os.getpid(),)
    after = world.names()[len(seen["before"]) :]
    assert after == [
        "focus.release_all",
        "tree.disable_all",
        "pasteboard.restore_saved",
        "presence.turn_done",
        "screenshot.cleanup",  # the last session's window pictures do not stay
    ]
    assert not os.path.exists(protocol.socket_path())  # unlinked
    fd = os.open(protocol.lock_path(), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the lock was dropped
    os.close(fd)


def test_a_failing_start_step_does_not_stop_the_helper(world, monkeypatch):
    monkeypatch.setattr(helper, "_on_signals", lambda: None)
    monkeypatch.setattr(helper.runloop, "run", lambda then: then())
    world.pasteboard.answers["restore_saved"] = RuntimeError("no pasteboard")
    world.focus.answers["release_all"] = RuntimeError("no focus")
    threading.Timer(0.2, helper.shutdown).start()
    helper.main()
    assert world.names()[-4:] == [
        "tree.disable_all",
        "pasteboard.restore_saved",
        "presence.turn_done",
        "screenshot.cleanup",
    ]


def test_a_second_helper_exits_at_once(served, monkeypatch):
    monkeypatch.setattr(helper, "_bind", lambda: pytest.fail("it must not bind"))
    helper.main()
    assert "pasteboard.restore_saved" not in served.names()
    assert os.path.exists(protocol.socket_path())  # the first helper's socket stays


def test_the_helper_is_a_long_lived_process_that_keeps_every_session(served):
    call(message("get_app_state", "Demo", env={"MACOS_HARNESS_SESSION": "s"}))
    reply = call(message("click", "Demo", "0", env={"MACOS_HARNESS_SESSION": "s"}))
    assert (
        reply["result"]["stdout"] == tools.ACTION_LINE + "\n"
    )  # the numbers were kept
    other = call(message("click", "Demo", "0", env={"MACOS_HARNESS_SESSION": "t"}))
    assert other["result"]["exit"] == 1
