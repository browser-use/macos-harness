"""Input events, built for one window and posted to one process.

Nothing here reaches the system event stream, so the real pointer never moves and
the person's keyboard focus stays where it is. An event without the window id does
nothing in a background app: every mouse and wheel event carries it.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import logging
import time

from .model import Point, WindowRef

log = logging.getLogger(__name__)

# Written into the user-data field of every event we post. The person detector reads
# it to tell our input from the person's.
OWN_TAG = 0x6D68_6576
USER_DATA_FIELD = 42
MODIFIERS = 0x1E0000  # shift, control, option, command
CLICK_HOLD = 0.05  # seconds between a mouse down and its up

# AppKit event type by button: left, right, other.
_TYPES = {
    "down": (1, 3, 25),
    "up": (2, 4, 26),
    "dragged": (6, 7, 27),
    "moved": (5, 5, 5),
}
_FIXED_CLICKS = {"dragged": 0, "moved": 1}  # the rest carry their place in the series
_WINDOW_ID_FIELDS = (51, 91, 92)
_BUTTON_FIELD, _SUBTYPE_FIELD, _SUBTYPE = 3, 7, 3
_FLAGS_CHANGED = 12
_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework"
_numbers = itertools.count(1)  # one running event number for the whole process


@functools.cache
def private(name: str, signature: bytes):
    """A call the system does not document, found by name once. None when this
    system lacks it: the caller skips that step."""
    import objc

    found: dict = {}
    with contextlib.suppress(Exception):
        bundle = objc.loadBundle("CoreGraphics", {}, bundle_path=_GRAPHICS)
        objc.loadBundleFunctions(bundle, found, [(name, signature)])
    if name not in found:
        log.warning("%s is not on this system: its step is skipped", name)
    return found.get(name)


@functools.cache
def _source():
    import Quartz

    return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _in_window(event, point: Point, window: WindowRef):
    """Stamp where the event is: the global point, the window, the point inside it."""
    import Quartz

    Quartz.CGEventSetLocation(event, point)
    for field in _WINDOW_ID_FIELDS:
        Quartz.CGEventSetIntegerValueField(event, field, window.window_id)
    local = (point[0] - window.frame[0], point[1] - window.frame[1])
    if place := private("CGEventSetWindowLocation", b"v^{__CGEvent=}{CGPoint=dd}"):
        place(event, local)
    return event


def mouse(
    kind: str,
    point: Point,
    window: WindowRef,
    button: int = 0,
    click: int = 1,
    flags: int = 0,
):
    """One mouse event. kind: down, up, dragged or moved. button: 0 left, 1 right,
    2 other."""
    import Quartz
    from AppKit import NSEvent

    make = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_
    count = _FIXED_CLICKS.get(kind, click)
    kind_number = _TYPES[kind][min(button, 2)]
    event = make(
        kind_number, point, 0, 0, window.window_id, None, next(_numbers), count, 1.0
    ).CGEvent()
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventSetIntegerValueField(event, _BUTTON_FIELD, button)
    Quartz.CGEventSetIntegerValueField(event, _SUBTYPE_FIELD, _SUBTYPE)
    return _in_window(event, point, window)


def wheel(point: Point, dx: int, dy: int, window: WindowRef):
    """One wheel event in pixels with both axes: dy up and down, dx left and right."""
    import Quartz

    units = Quartz.kCGScrollEventUnitPixel
    event = Quartz.CGEventCreateScrollWheelEvent(_source(), units, 2, dy, dx)
    return _in_window(event, point, window)


def key(code: int, flags: int, text: str | None = None, down: bool = True):
    """One key event. `text` makes the event carry these characters whatever the
    key code says."""
    import Quartz

    event = Quartz.CGEventCreateKeyboardEvent(_source(), code, down)
    Quartz.CGEventSetFlags(event, flags)
    if text:
        units = len(text.encode("utf-16-le")) // 2
        Quartz.CGEventKeyboardSetUnicodeString(event, units, text)
    return event


def flags_changed(flags: int):
    """The event an app gets when a modifier key goes down or up."""
    import Quartz

    event = Quartz.CGEventCreate(_source())
    Quartz.CGEventSetType(event, _FLAGS_CHANGED)
    Quartz.CGEventSetFlags(event, flags)
    return event


def held_flags() -> int:
    """The flags of the keyboard as the person holds it right now. The modifier
    keys are the bits of MODIFIERS."""
    import Quartz

    state = Quartz.kCGEventSourceStateCombinedSessionState
    return Quartz.CGEventSourceFlagsState(state)


def notice(kind: int, subtype: int, window_id: int = 0, flags: int = 0):
    """An event that carries no input, only news for the app (it became active)."""
    from AppKit import NSEvent

    make = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_
    subtype -= 0x10000 if subtype > 0x7FFF else 0  # the field is a signed 16 bit number
    return make(kind, (0, 0), flags, 0, window_id, None, subtype, 0, 0).CGEvent()


def post(pid: int, event, delay: float = 0.0) -> None:
    """Deliver one event to one process, then sleep `delay` seconds."""
    import Quartz

    Quartz.CGEventSetIntegerValueField(event, USER_DATA_FIELD, OWN_TAG)
    Quartz.CGEventSetTimestamp(event, time.monotonic_ns())
    Quartz.CGEventPostToPid(pid, event)
    if delay:
        time.sleep(delay)
