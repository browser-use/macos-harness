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

timer = threading.Timer(0.2, worker)
timer.start()
runloop.run()  # returns after stop()
timer.join()
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
