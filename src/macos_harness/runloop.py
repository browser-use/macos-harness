"""The one way onto the helper's main thread, where AppKit lives.

With no run loop started (tests, one-off scripts) `on_main` and `call_on_main` run the
function at once in the caller's thread.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from typing import Any

_running = threading.Event()


def on_main(fn: Callable[[], Any]) -> None:
    """Run `fn` soon on the main thread. Do not wait for it."""
    if not _running.is_set():
        fn()
        return
    from Foundation import NSOperationQueue

    def block() -> None:  # AppKit wants None back, and an error in it ends the app
        try:
            fn()
        except Exception:  # noqa: BLE001 - logged: the helper must live on
            traceback.print_exc()

    NSOperationQueue.mainQueue().addOperationWithBlock_(block)


def call_on_main(fn: Callable[[], Any]) -> Any:
    """Run `fn` on the main thread, wait, and return or raise what it did."""
    on_main_already = threading.current_thread() is threading.main_thread()
    if not _running.is_set() or on_main_already:
        return fn()
    done: queue.SimpleQueue = queue.SimpleQueue()

    def job() -> None:
        try:
            done.put((fn(), None))
        except Exception as error:  # noqa: BLE001 - handed to the caller
            done.put((None, error))

    on_main(job)
    value, error = done.get()
    if error is not None:
        raise error
    return value


def run() -> None:
    """Run the AppKit loop as an accessory app (no Dock icon) until `stop()`.

    Only `helper.main` calls this, on the main thread.
    """
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from Foundation import NSOperationQueue

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    NSOperationQueue.mainQueue().addOperationWithBlock_(_running.set)
    try:
        app.run()
    finally:
        _running.clear()


def stop() -> None:
    """Make `run()` return. Any thread may call it. Nothing happens with no loop."""
    if not _running.is_set():
        return
    from AppKit import NSApplication, NSEvent, NSEventTypeApplicationDefined

    def halt() -> None:
        app = NSApplication.sharedApplication()
        app.stop_(None)
        # stop_ takes effect after the next event, so send one.
        wake = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0
        )
        app.postEvent_atStart_(wake, True)

    on_main(halt)
