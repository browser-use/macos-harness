"""runloop.py: the one way onto the main thread."""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from macos_harness import runloop


def test_with_no_loop_started_both_run_the_function_at_once_in_the_caller():
    ran = []
    runloop.on_main(lambda: ran.append(threading.current_thread()))
    assert runloop.call_on_main(lambda: 42) == 42
    assert ran == [threading.current_thread()]
    runloop.stop()  # nothing to stop: no error


def test_call_on_main_hands_an_error_to_the_caller():
    with pytest.raises(ZeroDivisionError):
        runloop.call_on_main(lambda: 1 / 0)


REAL_LOOP = """
import threading
from macos_harness import runloop

seen = {}

def worker():
    done = threading.Event()
    runloop.on_main(lambda: 1 / 0)  # logged, and the loop lives on
    # A function that returns a value is fine too.
    runloop.on_main(lambda: (seen.update(soon=threading.current_thread().name), done.set()))
    done.wait(5)
    seen["called"] = runloop.call_on_main(lambda: threading.current_thread().name)
    try:
        runloop.call_on_main(lambda: 1 / 0)
    except ZeroDivisionError:
        seen["raised"] = True
    from AppKit import NSApplication
    seen["policy"] = runloop.call_on_main(
        lambda: NSApplication.sharedApplication().activationPolicy()
    )
    seen["nested"] = runloop.call_on_main(lambda: runloop.call_on_main(lambda: "in"))
    runloop.stop()

thread = threading.Thread(target=worker)
runloop.run(then=thread.start)  # returns after stop()
thread.join()
assert not runloop._running.is_set()
print(sorted(seen.items()))
"""


def test_the_real_loop_runs_work_on_the_main_thread_as_an_accessory_app():
    """The helper shows no Dock icon: its activation policy is "accessory" (1)."""
    done = subprocess.run(
        [sys.executable, "-c", REAL_LOOP],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "ZeroDivisionError" in done.stderr  # the failed function was logged
    assert done.stdout.strip() == str(
        [
            ("called", "MainThread"),
            ("nested", "in"),
            ("policy", 1),
            ("raised", True),
            ("soon", "MainThread"),
        ]
    )


EARLY_CALLER = """
import threading
from macos_harness import runloop

seen = []

def caller():
    runloop.on_main(lambda: seen.append(threading.current_thread() is threading.main_thread()))
    runloop.stop()

runloop.run(then=threading.Thread(target=caller).start)
print(seen)
"""


def test_a_thread_started_by_the_loop_never_runs_main_thread_work_itself():
    """A thread started before the loop ran (the helper's server) could call on_main
    first: its work ran at once on that thread, off the main thread. On a slow machine
    the worker of the test above died that way and the loop never stopped."""
    done = subprocess.run(
        [sys.executable, "-c", EARLY_CALLER],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[True]"
