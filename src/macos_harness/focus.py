"""Makes one app believe it is active and has key focus.

The system's front app, the real keyboard focus and the pointer stay where the
person left them. The app is told it became active and its window gets one click at
its activation point; nothing is raised. If the system makes the app really
frontmost by accident, the app that was in front is put back.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import threading
import time

from . import ax, errors, events
from .model import AppTarget, WindowRef

# Held from "make the app believe it is active" until the events of one action are
# sent: two apps cannot both be told they are frontmost at the same instant.
INPUT_LOCK = threading.RLock()

APPKIT_DEFINED, PROCESS_NOTICE = 13, 21  # event types
ACTIVATED, DEACTIVATED = 1, 2  # subtypes of the first
KEY_FOCUS_RETURNED = 0x8000  # subtype of the second
KEY_FOCUS_REMOVED = 0x4000  # measured to work
ACTIVATION_FLAGS = 0xC0000  # on the activation event when it names a window
BELIEVE_WAIT = 0.25  # seconds the app gets to report it is frontmost
RESTORE_WAIT = 2.0  # seconds the earlier front app gets to be in front again
# For this long after we told an app it is active, a real switch to it is undone
# unless the person clicked or typed since.
# Seen: Pages came to the front 1.9 s after we told it it is active for a key.
LEAK_WATCH = 3.0  # 1.9 s measured on Pages
POLL = 0.01

_ENFORCERS: dict[int, Enforcer] = {}


class _ProcessNumber(ctypes.Structure):
    _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]


@functools.cache
def _services():
    name = "ApplicationServices"
    return ctypes.CDLL(f"/System/Library/Frameworks/{name}.framework/{name}")


def front_pid() -> int:
    """The really frontmost process, asked fresh each time. 0 when unknown."""
    services, number, pid = _services(), _ProcessNumber(), ctypes.c_int(0)
    services.GetFrontProcess(ctypes.byref(number))
    services.GetProcessPID(ctypes.byref(number), ctypes.byref(pid))
    return pid.value


def no_attention(pid: int) -> None:
    """Tell the system that an app we drive does not want attention, so that a new
    window or sheet of it does not bring it forward. The call is made
    each time a window or sheet of the target is created. A missing call is skipped."""
    services, number = _services(), _ProcessNumber()
    desires = getattr(services, "_SetApplicationDesiresAttention", None)
    if (
        desires is not None
        and services.GetProcessForPID(pid, ctypes.byref(number)) == 0
    ):
        desires(ctypes.byref(number), False)


def _says(element, name: str) -> bool | None:
    """What the app itself says about one of its elements. None for no answer."""
    try:
        return ax.get(element, name)
    except Exception:  # noqa: BLE001
        return None


def _reports_frontmost(pid: int) -> bool | None:
    return _says(ax.app(pid), "AXFrontmost")


@contextlib.contextmanager
def _screen_frozen():
    """No screen update between telling the app it is active and the click that
    makes its window key."""
    connection = events.private("CGSMainConnectionID", b"i")
    freeze = events.private("CGSDisableUpdate", b"ii")
    thaw = events.private("CGSReenableUpdate", b"ii")
    usable = connection and freeze and thaw
    if usable:
        freeze(connection())
    try:
        yield
    finally:
        if usable:
            thaw(connection())


def _bring_back(pid: int) -> None:
    """Our activation leaked: put the app that was in front back in front."""
    ax.set(ax.app(pid), "AXFrontmost", True)
    deadline = time.monotonic() + RESTORE_WAIT
    while front_pid() != pid:
        if time.monotonic() >= deadline:
            raise errors.timeout_reached()
        time.sleep(POLL)


def _watch_leak(pid: int, before: int, since: float) -> None:
    """In the background: if `pid` really comes to the front before LEAK_WATCH ends
    and the person did not go there themselves, put `before` back in front."""

    def watch() -> None:
        while time.monotonic() < since + LEAK_WATCH:
            if front_pid() == pid:
                if not _person_went(pid, since):
                    with contextlib.suppress(errors.HarnessError):
                        _bring_back(before)
                return
            time.sleep(POLL)

    _background(watch)


def _person_went(pid: int, since: float) -> bool:
    from . import person

    return person.went_to(pid, since)


def _background(call) -> None:
    threading.Thread(target=call, daemon=True).start()


def _point(value) -> tuple[float, float] | None:
    """A point as the app gives it: wrapped, a struct or a plain pair."""
    if value is None or isinstance(value, tuple):
        return value
    if not hasattr(value, "x"):
        import ApplicationServices as api

        found, value = api.AXValueGetValue(value, api.kAXValueCGPointType, None)
        if not found:
            return None
    return (value.x, value.y)


class Enforcer:
    """What one app believes. Three separate truths: it is really active, it
    believes it is active, it believes it has key focus."""

    def __init__(self, pid: int):
        self.pid = pid
        self._active = self._focus = False

    @property
    def really_active(self) -> bool:
        return front_pid() == self.pid

    @property
    def believes_active(self) -> bool:
        if self.really_active:
            return True
        # The app's own word beats what we remember: a real switch to the app and
        # away again takes the belief with it, and an earlier helper may have left it.
        reported = _reports_frontmost(self.pid)
        if reported is not None:
            self._active = bool(reported)
            self._focus = self._focus and self._active
        return self._active

    @property
    def believes_focus(self) -> bool:
        return self.really_active or (self.believes_active and self._focus)

    @contextlib.contextmanager
    def active(self, window: WindowRef | None):
        """For the length of one action the app believes it is active and focused."""
        with INPUT_LOCK:
            before, since = front_pid(), time.monotonic()
            activated = False
            try:
                activated = self._enforce(window)
                yield self
            finally:
                leaked = before not in (0, self.pid) and front_pid() == self.pid
                if leaked and not _person_went(self.pid, since):  # their own switch
                    _bring_back(before)
                elif before not in (0, self.pid) and activated and not leaked:
                    _watch_leak(self.pid, before, since)  # it may still come

    def _enforce(self, window: WindowRef | None) -> bool:
        """True when the app was told it is active (not only given key focus)."""
        if self.believes_focus:
            return False
        activated = not self.believes_active
        if not activated:
            notice = events.notice(PROCESS_NOTICE, KEY_FOCUS_RETURNED)
            events.post(self.pid, notice)
        else:
            with _screen_frozen():
                clicked = self._activate(window)
            # The app works through our events in its own time. What comes next
            # must not overtake them: a menu that opens before the activation
            # click has arrived is closed by that click.
            deadline = time.monotonic() + BELIEVE_WAIT
            while time.monotonic() < deadline and not (
                _reports_frontmost(self.pid)
                and (not clicked or _says(window.element, "AXMain"))
            ):
                time.sleep(POLL)
        self._active = self._focus = True
        return activated

    def _activate(self, window: WindowRef | None) -> bool:
        """The activation event, then one click at the window's activation point.
        The click is what makes the window key in the app's eyes. True when the
        window got that click."""
        window_id = window.window_id if window else 0
        flags = ACTIVATION_FLAGS if window_id else 0
        notice = events.notice(APPKIT_DEFINED, ACTIVATED, window_id, flags)
        events.post(self.pid, notice)
        point = window_id and _point(ax.get(window.element, "AXActivationPoint"))
        if point:
            for kind in ("down", "up"):
                events.post(self.pid, events.mouse(kind, point, window))
        return bool(point)

    def release(self) -> None:
        """Tell the app it is inactive again, unless the person made it active."""
        if not self.really_active and self.believes_active:
            for kind, subtype in (
                (PROCESS_NOTICE, KEY_FOCUS_REMOVED),
                (APPKIT_DEFINED, DEACTIVATED),
            ):
                events.post(self.pid, events.notice(kind, subtype))
        self._active = self._focus = False


def enforcer(target: AppTarget) -> Enforcer:
    """One per app process, shared by every agent that drives the app."""
    with INPUT_LOCK:
        return _ENFORCERS.setdefault(target.pid, Enforcer(target.pid))


def release(target: AppTarget) -> None:
    with INPUT_LOCK:
        if found := _ENFORCERS.pop(target.pid, None):
            found.release()


def release_all() -> None:
    with INPUT_LOCK:
        for found in list(_ENFORCERS.values()):
            found.release()
        _ENFORCERS.clear()


def focus_element(element) -> bool:
    """Give one element the keyboard focus inside its app. False when the app
    refuses: the caller may then click the field."""
    try:
        ax.set(element, "AXFocused", True)
    except Exception:  # noqa: BLE001
        return False
    return True
