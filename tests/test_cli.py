"""cli.py: the client. A fake helper on the test's socket, or a real one with fakes."""

from __future__ import annotations

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import fakes_a
import pytest

from macos_harness import cli, errors, protocol

SRC = Path(cli.__file__).parent
STATE = 'Window: "Demo", App: Demo.\n0 button OK\n\n' + (
    f"Screenshot (800x600 px): {fakes_a.SHOT.path}\n"
)


class Server:
    """A helper made of one thread. `script` answers each request in turn."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(protocol.socket_path())
        self.sock.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    request = protocol.read(conn)
                except EOFError:
                    continue
                self.requests.append(request)
                answer = self.script.pop(0) if self.script else "hang up"
                if callable(answer):
                    answer = answer(request)
                if isinstance(answer, dict):
                    conn.sendall(protocol.pack({"id": request["id"], **answer}))

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    made = []

    def make(*script):
        made.append(Server(*script))
        return made[-1]

    yield make
    for one in made:
        one.close()


def ok(stdout="", stderr="", exit=0):
    return {"result": {"exit": exit, "stdout": stdout, "stderr": stderr}}


def env(**more):
    """The test's environment, with telemetry off for every process it starts."""
    return {**os.environ, "MACOS_HARNESS_TELEMETRY": "0", **more}


def client(*argv, stdin=subprocess.DEVNULL, timeout=30, **more):
    """The real client as its own process. It starts the faked helper when needed."""
    return subprocess.run(
        [*fakes_a.CLIENT, *argv],
        check=False,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(**more),
    )


@pytest.fixture
def helper_process(private_state):
    """Stops whatever helper a test left on the test's socket."""
    yield
    cli._stop_helper()


def test_the_client_talks_only_to_a_helper_of_the_same_user(server, monkeypatch):
    """Whoever listens on the socket got the arguments, the stdin text and the
    environment: now the user at the other end is asked first."""
    server()
    sock = cli._try_connect()
    assert sock is not None
    sock.close()
    monkeypatch.setattr(protocol, "peer_uid", lambda sock: os.getuid() + 1)
    with pytest.raises(cli.Failure, match="helper_unavailable"):
        cli._try_connect()


def helper_pid() -> int | None:
    sock = cli._try_connect()
    info = cli._ping(sock) if sock else None
    return info["pid"] if info else None  # a helper that is dying may not answer


# --- light and fast ---------------------------------------------------------------------


def imports(path: Path) -> tuple[set, set]:
    """(modules imported at the top of a file, modules imported inside functions)."""
    top, inner = set(), set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            (top if node in tree.body else inner).update(names)
    return top, inner


def test_the_client_imports_only_what_it_was_given():
    top, inner = imports(SRC / "cli.py")
    allowed = {"json", "os", "select", "socket", "stat", "sys", "time", "protocol"}
    assert top - {"__future__"} == allowed
    assert inner == {"errors"}  # only when the call could not be made
    top, inner = imports(SRC / "protocol.py")
    assert top - {"__future__"} == {"json", "os", "struct", "__version__"}
    assert inner == {"pathlib"}
    assert (SRC / "__init__.py").read_text().strip() == '__version__ = "0.2.0"'


POISON = """
import sys
heavy = ("macos_harness.model macos_harness.errors macos_harness.tools "
         "macos_harness.helper macos_harness.telemetry objc AppKit Foundation Quartz "
         "ApplicationServices CoreFoundation argparse subprocess pathlib dataclasses "
         "typing threading ctypes importlib.metadata logging").split()
before = set(sys.modules)
for name in heavy:
    sys.modules.setdefault(name, None)  # importing it now raises ImportError
from macos_harness import cli
code = cli.main(sys.argv[1:])
loaded = sorted(n for n in set(sys.modules) - before if sys.modules[n] is not None)
print("LOADED " + " ".join(loaded), file=sys.stderr)
raise SystemExit(code)
"""


def test_a_whole_call_loads_nothing_heavy(server):
    served = server(ok("fine\n"))
    done = subprocess.run(
        [sys.executable, "-c", POISON, "get_app_state", "Demo"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env(),
        timeout=30,
    )
    assert (done.returncode, done.stdout) == (0, "fine\n")
    loaded = done.stderr.split("LOADED ")[1].split()
    assert "macos_harness.protocol" in loaded and len(served.requests) == 1
    assert not [
        name for name in loaded if name.startswith(("objc", "AppKit", "Quartz"))
    ]


def test_version_needs_no_helper():
    done = subprocess.run(
        [sys.executable, "-m", "macos_harness.cli", "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=env(),
        timeout=30,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "0.2.0\n", "")
    assert not os.path.exists(protocol.socket_path())  # none was started


# --- one call against a fake helper -----------------------------------------------------


def test_the_reply_is_printed_and_its_exit_code_returned(server, capsys):
    served = server(ok("the state\n", "a warning\n", 0))
    assert cli.main(["get_app_state", "--app", "Demo"]) == 0
    assert capsys.readouterr() == ("the state\n", "a warning\n")
    params = served.requests[0]["params"]
    assert params["argv"] == ["get_app_state", "--app", "Demo"]
    assert params["stdin"] is None  # stdin is not read until the helper asks
    assert params["callerApi"] == "MacosHarnessIPC-1"
    left = params["deadlineMs"] / 1000 - time.time()
    assert 118 < left <= 120


def test_a_failed_call_is_sent_once_and_ends_the_chain(server, capsys):
    served = server(
        ok(
            "",
            "No app called Nope was found on this Mac. Call `list_apps` and use a name or bundle id from it.\n",
            1,
        )
    )
    assert cli.main(["click", "--app", "Nope", "1"]) == 1  # `&&` stops here
    assert capsys.readouterr() == (
        "",
        "No app called Nope was found on this Mac. Call `list_apps` and use a name or bundle id from it.\n",
    )
    assert len(served.requests) == 1  # no retry of an action


def test_usage_errors_keep_exit_code_2(server):
    server(ok("", "Give the app: --app <name, bundle id or path>\n", 2))
    assert cli.main(["click"]) == 2


def test_helper_gone_in_the_middle_of_a_call_is_said_and_not_sent_again(server, capsys):
    served = server("hang up")
    assert cli.main(["click", "--app", "Demo", "1"]) == 1
    assert capsys.readouterr() == ("", errors.helper_stopped().message + "\n")
    assert len(served.requests) == 1  # sending it again could click twice


def test_client_errors_as_json(server, capsys):
    server("hang up")
    assert cli.main(["click", "--app", "Demo", "1", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error == {
        "code": 103,
        "name": "helper_stopped",
        "message": errors.helper_stopped().message,
        "cancelled": False,
    }


def test_a_transport_error_is_printed_and_named(server, capsys):
    server(protocol.error(0, "methodNotFound"), protocol.error(0, "deadlineExceeded"))
    assert cli.main(["click"]) == 1
    assert capsys.readouterr() == ("", "Method not found\n")
    assert cli.main(["--json", "click"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == "The deadline of the request has passed"
    assert (error["name"], error["cancelled"]) == ("deadlineExceeded", False)


def test_a_transport_error_with_no_name_gets_the_fallback_name(server, capsys):
    server({"error": {"code": -32700, "message": "Parse error"}})
    assert cli.main(["--json", "click"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["name"] == "jsonRPCError"


def test_a_helper_that_never_answers_is_a_timeout(server, capsys, monkeypatch):
    monkeypatch.setattr(protocol, "CALL_TIMEOUT_S", 0.2)
    release = threading.Event()
    server(lambda request: release.wait(10))
    assert cli.main(["get_app_state", "Demo"]) == 1
    assert (
        capsys.readouterr().err
        == "macOS Harness did not answer within 2 minutes, so the action may or may not have run. Call `get_app_state` to see the app before you try again.\n"
    )
    release.set()


def test_a_helper_on_its_way_out_ran_nothing_so_the_call_is_sent_to_the_next(
    server, capsys
):
    served = server(protocol.error(0, "stopping"), {"result": {}}, ok("done\n"))
    assert cli.main(["click", "--app", "Demo", "1"]) == 0
    assert capsys.readouterr().out == "done\n"
    assert [r["method"] for r in served.requests] == ["request", "shutdown", "request"]


def test_a_second_refusal_is_printed_and_not_tried_again(server, capsys):
    refusal = protocol.error(0, "versionMismatch")
    served = server(refusal, {"result": {}}, refusal)
    assert cli.main(["click", "--app", "Demo", "1"]) == 1
    assert "version mismatch" in capsys.readouterr().err
    assert len(served.requests) == 3


def test_a_reader_that_left_early_is_no_error(server):
    release = threading.Event()
    served = server(lambda request: (release.wait(10), ok("x" * 200_000 + "\n"))[1])
    process = script_client("get_app_state", "Demo", stdin=subprocess.DEVNULL)
    deadline = time.time() + 10
    while not served.requests and time.time() < deadline:
        time.sleep(0.01)
    process.stdout.close()  # like `macos-harness get_app_state Demo | head -1`
    release.set()
    assert process.wait(10) == 0
    assert process.stderr.read() == ""


def test_a_reply_that_is_no_reply_counts_as_helper_stopped(server, capsys):
    server({"result": {}})
    assert cli.main(["click"]) == 1
    assert capsys.readouterr().err == errors.helper_stopped().message + "\n"


# --- stdin: read only when the helper asks ----------------------------------------------

NEED = {"result": {"needStdin": True}}


def script_client(*argv, stdin):
    return subprocess.Popen(
        [sys.executable, "-m", "macos_harness.cli", *argv],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env(),
    )


def test_a_heredoc_reaches_the_helper_in_the_second_request(server):
    served = server(NEED, ok("typed\n"))
    process = script_client("type_text", "--app", "Demo", stdin=subprocess.PIPE)
    out, err = process.communicate("line one\nline two\n", timeout=30)
    assert (process.returncode, out, err) == (0, "typed\n", "")
    first, second = (request["params"] for request in served.requests)
    assert first["stdin"] is None and second["stdin"] == "line one\nline two\n"
    assert first["argv"] == second["argv"]


def test_an_open_silent_stdin_costs_nothing_until_the_helper_asks(server):
    server(ok("state\n"))
    started = time.time()
    process = script_client("get_app_state", "Demo", stdin=subprocess.PIPE)
    assert process.stdout.read() == "state\n"  # answered while stdin is still open
    assert time.time() - started < 1.5
    process.stdin.close()
    assert process.wait(10) == 0


def test_an_open_silent_stdin_costs_two_seconds_once_and_never_hangs(server):
    served = server(
        NEED, ok("", "macOS Harness error 100: No text was given to type\n", 1)
    )
    started = time.time()
    process = script_client("type_text", "--app", "Demo", stdin=subprocess.PIPE)
    assert process.stderr.read().endswith("No text was given to type\n")
    assert 1.9 < time.time() - started < 5
    assert served.requests[1]["params"]["stdin"] == ""
    process.stdin.close()
    assert process.wait(10) == 1


def test_a_stdin_that_is_no_pipe_and_no_file_is_not_read(server):
    served = server(NEED, ok("done\n"))
    started = time.time()
    process = script_client("paste", "Demo", stdin=subprocess.DEVNULL)
    assert process.communicate(timeout=30)[0] == "done\n"
    assert served.requests[1]["params"]["stdin"] == "" and time.time() - started < 1.5


def test_a_file_on_stdin_is_read(server, tmp_path):
    served = server(NEED, ok("done\n"))
    (tmp_path / "text.txt").write_text("from a file\n")
    with open(tmp_path / "text.txt") as file:
        assert (
            script_client("paste", "Demo", stdin=file).communicate(timeout=30)[0]
            == "done\n"
        )
    assert served.requests[1]["params"]["stdin"] == "from a file\n"


# --- starting, reusing and replacing the helper -----------------------------------------


def test_first_call_starts_a_helper_and_the_next_call_reuses_it(helper_process):
    assert helper_pid() is None
    first = client("get_app_state", "--app", "Demo")
    assert (first.returncode, first.stdout, first.stderr) == (0, STATE, "")
    pid = helper_pid()
    assert pid and pid != os.getpid()
    second = client("click", "--app", "Demo", "--element_index", "0")
    assert (
        second.stdout == "Done. Call `get_app_state` to see the new state of the UI.\n"
    )
    assert helper_pid() == pid  # the same helper: it knew the number
    log = Path(protocol.log_path()).read_text()
    assert log.count("helper started") == 1


def test_the_helper_holds_none_of_the_clients_pipes_and_is_detached(helper_process):
    done = client("get_app_state", "Demo")  # returns only when stdout reached its end
    assert done.returncode == 0
    pid = helper_pid()
    listed = subprocess.run(
        ["ps", "-o", "ppid=,tty=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert listed[0] == "1"  # nobody waits for it
    assert listed[-1] in ("??", "-")  # no terminal
    assert os.getsid(pid) != os.getsid(0)  # a session of its own
    cwd = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    assert "n/\n" in cwd  # working folder /


def test_a_stale_socket_is_replaced(helper_process):
    assert client("get_app_state", "Demo").returncode == 0
    old = helper_pid()
    os.kill(old, signal.SIGKILL)  # no clean exit: the socket file stays
    deadline = time.time() + 5
    while helper_pid() is not None and time.time() < deadline:
        time.sleep(0.05)
    assert os.path.exists(protocol.socket_path())
    again = client("get_app_state", "Demo")
    assert (again.returncode, again.stdout) == (0, STATE)
    assert helper_pid() not in (None, old)


def test_two_clients_starting_at_once_leave_one_helper(helper_process):
    runs = [
        subprocess.Popen(
            [*fakes_a.CLIENT, "get_app_state", "Demo"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env(),
        )
        for _ in range(4)
    ]
    numbers = []
    for run in runs:
        out, err = run.communicate(timeout=30)
        assert (run.returncode, err) == (0, "")
        numbers.append(out.splitlines()[1])
    # One helper, and one session: the four reads of this one agent count on.
    assert sorted(numbers) == [f"{n} button OK" for n in range(4)]
    log = Path(protocol.log_path()).read_text()
    assert log.count("helper started") == 1
    assert helper_pid() is not None


def test_a_helper_of_another_version_is_replaced_and_the_call_runs_once(helper_process):
    old = subprocess.Popen(fakes_a.HELPER, env=env(FAKES_A_VERSION="0.1.9"))
    deadline = time.time() + 10
    while helper_pid() is None and time.time() < deadline:
        time.sleep(0.05)
    assert helper_pid() == old.pid
    done = client("get_app_state", "Demo")
    assert (done.returncode, done.stdout, done.stderr) == (0, STATE, "")
    assert old.wait(10) == 0  # asked to stop, it left cleanly
    new = helper_pid()
    assert new not in (None, old.pid)
    info = cli._ping(cli._try_connect())
    assert info["version"] == "0.2.0"


def test_a_helper_that_cannot_start_gives_the_text_with_the_retry_count(private_state):
    done = subprocess.run(
        [sys.executable, "-c", NO_HELPER, "get_app_state", "Demo"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env(),
        timeout=30,
    )
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr == errors.helper_unavailable().message + "\n"
    assert "After 3 failures in a row" in done.stderr


NO_HELPER = """
import sys, time
from macos_harness import cli
cli.HELPER = [sys.executable, "-c", "pass"]
cli.START_WINDOW_S = 0.35
started = time.time()
code = cli.main(sys.argv[1:])
assert 0.3 < time.time() - started < 3, time.time() - started
raise SystemExit(code)
"""


def test_connect_is_retried_every_100_ms_and_the_ping_has_one_second():
    assert (cli.CONNECT_RETRY_S, cli.START_WINDOW_S) == (0.1, 5.0)
    assert (cli.PING_BUDGET_S, cli.QUICK_CONNECT_S) == (1.0, 0.25)


def test_a_long_log_is_moved_aside_before_a_helper_starts(helper_process):
    log = Path(protocol.log_path())
    log.parent.mkdir(parents=True)
    log.write_text("x" * (cli.LOG_BYTES + 1))
    assert client("get_app_state", "Demo").returncode == 0
    assert len(Path(str(log) + ".1").read_text()) == cli.LOG_BYTES + 1
    assert "helper started" in log.read_text() and log.stat().st_size < 10_000


def test_the_helper_finds_the_package_when_the_client_ran_from_a_checkout(
    helper_process,
):
    assert client("get_app_state", "Demo").returncode == 0
    shown = subprocess.run(
        ["ps", "eww", "-o", "command=", "-p", str(helper_pid())],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    assert f"PYTHONPATH={SRC.parent}" in shown


def test_ping_gives_up_at_its_budget(server, monkeypatch):
    monkeypatch.setattr(cli, "PING_BUDGET_S", 0.1)
    release = threading.Event()
    server(lambda request: release.wait(5))
    started = time.time()
    assert cli._ping(cli._try_connect()) is None
    assert time.time() - started < 1
    release.set()


# --- helper status, stop, restart -------------------------------------------------------


def test_helper_status_stop_restart(helper_process):
    status = client("helper", "status")
    assert (status.returncode, status.stdout) == (1, "not running\n")
    assert helper_pid() is None  # status starts none
    assert client("helper", "stop").stdout == "not running\n"
    restarted = client("helper", "restart")
    pid = helper_pid()
    assert restarted.stdout.startswith(f"running: pid {pid}, version 0.2.0, ")
    assert client("helper", "status").stdout == restarted.stdout
    assert client("helper", "restart").returncode == 0
    assert helper_pid() not in (None, pid)
    assert client("helper", "stop").stdout == "stopped\n"
    assert helper_pid() is None and not os.path.exists(protocol.socket_path())
    assert client("helper", "dance").returncode == 2


def test_sigterm_is_a_clean_exit(helper_process):
    assert client("get_app_state", "Demo").returncode == 0
    pid = helper_pid()
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while os.path.exists(protocol.socket_path()) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(protocol.socket_path())  # it unlinked its socket
    assert "helper stopped" in Path(protocol.log_path()).read_text()
