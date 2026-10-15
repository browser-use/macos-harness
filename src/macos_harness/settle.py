"""Wait until an app's UI has gone quiet and shows no loading sign.

One poll loop on an accessibility observer. The caller decides when to wait; this file
never reads or sets `needs_settle`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from . import ax, errors, windows
from .model import AppKind, AppTarget, Capture, WindowRef

POLL_NATIVE = 0.1  # seconds between two looks at a native app
POLL_WEB_LIKE = 0.25  # browsers and apps built on a browser engine
QUIET = 0.25  # this long with no counted notification is quiet
LIMIT = 5.0  # hard stop, checked before each sleep: one more poll and read may follow

TITLE_CHANGED = "AXTitleChanged"
NOTIFICATIONS = (
    TITLE_CHANGED,
    "AXLayoutChanged",
    "AXFocusedWindowChanged",
    "AXFocusedUIElementChanged",
    "AXElementBusyChanged",
    "AXMenuOpened",
    "AXSheetCreated",
    "AXWindowCreated",
    "AXCreated",
    "AXUIElementDestroyed",
)


class _Heard:
    """When the app last sent a notification that counts."""

    def __init__(self, clock: Any):
        self.clock, self.lock = clock, threading.Lock()
        self.last = (
            clock.monotonic()
        )  # quiet is counted from before the observer exists
        self.title_changes: list[tuple[float, Any]] = []
        self.titles: list[list[Any]] = []  # [element, its last title]

    def note(self, name: str, element: Any) -> None:
        """The observer callback. It runs on the main run loop, so it only stores."""
        now = self.clock.monotonic()
        with self.lock:
            if name == TITLE_CHANGED:
                self.title_changes.append((now, element))
            else:
                self.last = max(self.last, now)

    def quiet_for(self) -> float:
        with self.lock:
            changes, self.title_changes = self.title_changes, []
        # An app that sends the same title forever must still go quiet.
        counted = [at for at, element in changes if self._new_title(element)]
        with self.lock:
            self.last = max([self.last, *counted])
            return self.clock.monotonic() - self.last

    def _new_title(self, element: Any) -> bool:
        try:
            title = ax.get(element, "AXTitle")
        except errors.HarnessError:
            return False
        if not isinstance(title, str):
            return False
        for known in self.titles:
            if ax.same(known[0], element):
                changed, known[1] = known[1] != title, title
                return changed
        self.titles.append([element, title])
        return True  # the first title seen for an element counts


def _key_window(target: AppTarget) -> WindowRef | None:
    try:
        return windows.key_window(target)
    except errors.HarnessError as error:
        if error.message != errors.no_windows().message:
            raise
        return None  # keep polling: a window may still come


def wait(
    target: AppTarget,
    kind: AppKind,
    read: Callable[[WindowRef], Capture],
    poll: float | None = None,
    quiet: float = QUIET,
    limit: float = LIMIT,
    clock: Any = time,
    abandoned: Callable[[], bool] = lambda: False,
) -> Capture | None:
    """The capture `read` built once the UI is quiet and not loading. None at the limit."""
    poll = poll or (POLL_WEB_LIKE if kind.web_like else POLL_NATIVE)
    heard = _Heard(clock)
    observer = ax.observe(target.pid, ax.app(target.pid), NOTIFICATIONS, heard.note)
    try:
        start = clock.monotonic()
        while clock.monotonic() - start < limit:
            clock.sleep(poll)
            if abandoned():  # nobody waits for this read any more
                raise errors.request_timed_out()
            if heard.quiet_for() < quiet:
                continue
            window = _key_window(target)
            capture = read(window) if window else None
            if capture and not capture.loading:
                return capture  # the caller prints this one: the window is walked once
        return None
    finally:
        observer.stop()
