"""The person comes first: notice their input and wait for them.

One listen-only event tap sees the person's clicks, scrolls and key presses. An event
on an app we drive leaves a record for that app. The next call on it waits until the
person has been quiet for 5 s, and an action then fails, so that the model reads the
app again before it acts. To stop the agent the person interrupts it in its own app;
the helper then cancels that client's running action (helper.py).
"""

from __future__ import annotations

import functools
import logging
import math
import threading
import time

import Quartz
from AppKit import NSRunningApplication, NSWorkspace

from . import ax, errors, events, presence, runloop, windows
from .model import AgentSession, AppTarget

# Left, right and other mouse down, scroll wheel, key down. No move, no key up.
EVENT_TYPES = (1, 3, 25, 22, 10)
KEY_DOWN, ESCAPE, TAB = 10, 53, 48
CLICKS, DOCK = (1, 3, 25), "com.apple.dock"
DEBOUNCE_S = 5.0  # the person counts as busy this long after their last event
WAIT_CAP_S = 100.0  # how long one call waits for the person to finish
POLL_S = 0.25

log = logging.getLogger(__name__)


_LOCK = threading.RLock()
_drivers: dict[str, set[str]] = {}  # app path -> keys of the agents that drive it
_targets: dict[str, AppTarget] = {}  # app path -> the app, with its newest pid
# App path -> when the person will have been quiet for the whole debounce. The entry
# stays until the model has read the app again.
_quiet_at: dict[str, float] = {}
_awaiting_quiet: set[str] = set()  # app paths with a "quiet again" timer running
_own_pid = 0
_tap = None
# How the person last went to an app themselves: (the process their last click landed
# in, when), and when they last pressed Command-Tab or clicked the Dock.
_clicked: tuple[int, float] = (0, -math.inf)
_switched = -math.inf


# --- the gate before every call on an app
# ------------------------------------------------------------------------------


def gate(
    agent: AgentSession,
    target: AppTarget,
    state_read: bool,
    clock=time,
    abandoned=lambda: False,
) -> None:
    """Hold a call back while the person is using the app, or refuse it.

    In order: the turn has ended; wait for the person to be quiet (5 s after their
    last event, at most 100 s); then an action fails until a state read has seen what
    they changed.
    """
    if agent.ended:
        raise errors.turn_ended()
    path, until = target.path, clock.monotonic() + WAIT_CAP_S
    _note_driven(agent, target)
    if _tap is None or secure_input_pid() is not None:  # no tap, or one blind to keys
        _touch_from_idle_time(target, clock)
    while True:
        now = clock.monotonic()
        quiet_at = _quiet_at.get(path)
        if quiet_at is None or now >= quiet_at:
            break
        if now >= until:
            raise errors.still_interacting(path)
        if abandoned():  # nobody waits for this call any more: let the app's queue go
            raise errors.request_timed_out()
        clock.sleep(POLL_S)
    if quiet_at is not None and not state_read:
        raise errors.user_changed(path)


def after_state_read(target: AppTarget, clock=time) -> None:
    """The model has now seen what the person did. Kept while they are still at it."""
    with _LOCK:
        if _quiet_at.get(target.path, math.inf) <= clock.monotonic():
            del _quiet_at[target.path]


def driven() -> list[AppTarget]:
    return list(_targets.values())


def _note_driven(agent: AgentSession, target: AppTarget) -> None:
    with _LOCK:
        _drivers.setdefault(target.path, set()).add(agent.key)
        _targets[target.path] = target


# --- turns
# ------------------------------------------------------------------------------


def interrupted(target: AppTarget, since: float) -> bool:
    """The person used the app after `since` (a monotonic time): the action that is
    running sends nothing more."""
    return _quiet_at.get(target.path, -math.inf) - DEBOUNCE_S >= since


def turn_started(agent: AgentSession) -> None:
    agent.signals_turns, agent.ended = True, False


def turn_ended(agent: AgentSession) -> None:
    """Forget what this agent drove: person records, refused apps."""
    # Only a host that also says when a turn starts can be held until the next one.
    agent.ended = agent.signals_turns
    agent.denied.clear()
    with _LOCK:
        for path, keys in list(_drivers.items()):
            keys.discard(agent.key)
            if not keys:  # another agent that still drives the app keeps its marks
                for table in (_drivers, _targets, _quiet_at):
                    table.pop(path, None)


# --- noticing the person
# ------------------------------------------------------------------------------


def start_watch(own_pid: int) -> None:
    """Called once when the helper starts."""
    global _own_pid
    _own_pid = own_pid
    runloop.on_main(_start_tap)


def saw(
    kind: int,
    source_pid: int,
    tag: int,
    pid: int,
    keycode: int = 0,
    clock=time,
    stray: bool = False,
    command: bool = False,
) -> None:
    """One event from the tap. Our own are dropped, the person's are counted.

    A stray event went to a driven app in the background only because a menu open
    there holds the person's input (a pop-up of a web page): it was meant for the app
    they are in. It does not count, and the menu is closed so the next one gets there.
    """
    global _clicked, _switched
    if kind not in EVENT_TYPES or source_pid == _own_pid or tag == events.OWN_TAG:
        return
    now = clock.monotonic()
    if kind in CLICKS and not stray:
        if _bundle_of(pid) == DOCK:
            _switched = now
        else:
            _clicked = (pid, now)
    elif kind == KEY_DOWN and keycode == TAB and command:
        _switched = now
    path = _driven_path(pid)
    if path is None:
        return
    if stray:
        target = _targets[path]
        _later(0.0, lambda: _close_menu(target))
        return
    _quiet_at[path] = now + DEBOUNCE_S
    presence.pointer_activity("paused")
    _tell_when_quiet(path, clock)


def _tell_when_quiet(path: str, clock) -> None:
    """Show the pointer as waiting again once the debounce is over."""

    def check() -> None:
        left = _quiet_at.get(path, 0.0) - clock.monotonic()
        if left > 0:
            _later(left, check)
        else:
            _awaiting_quiet.discard(path)
            presence.pointer_activity("loading")

    if path not in _awaiting_quiet:
        _awaiting_quiet.add(path)
        _later(DEBOUNCE_S, check)


def _close_menu(target: AppTarget) -> None:
    """Close the menu that holds the person's input: its Cancel action, else Escape."""
    try:
        menu = windows.shown_menu(target)
        if menu is not None:
            return ax.perform(menu, "AXCancel")
    except errors.HarnessError:
        pass
    for down in (True, False):
        events.post(target.pid, events.key(ESCAPE, 0, None, down=down))


def _later(delay: float, call) -> None:
    timer = threading.Timer(delay, call)
    timer.daemon = True
    timer.start()


def _driven_path(pid: int) -> str | None:
    """The driven app a process belongs to: by pid, else by its bundle id."""
    targets = driven()
    for target in targets:
        if target.pid == pid:
            return target.path
    bundle = _bundle_of(pid) if targets else ""
    return next((t.path for t in targets if bundle and t.bundle_id == bundle), None)


# --- the system side
# ------------------------------------------------------------------------------


def _start_tap() -> None:
    """One listen-only tap on the five event types. No grant: no tap and no prompt."""
    global _tap
    if _tap is not None or not Quartz.CGPreflightListenEventAccess():
        return
    _tap = Quartz.CGEventTapCreate(
        Quartz.kCGAnnotatedSessionEventTap,  # here an event knows its target process
        Quartz.kCGTailAppendEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        sum(1 << kind for kind in EVENT_TYPES),
        _on_event,
        None,
    )
    if _tap is None:
        return
    source = Quartz.CFMachPortCreateRunLoopSource(None, _tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetMain(), source, Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(_tap, True)


def _on_event(proxy, kind, event, refcon):
    """The tap's callback, on the main thread. It only reads, and it never raises."""
    try:
        if kind not in EVENT_TYPES:  # the system switched the tap off: switch it on
            Quartz.CGEventTapEnable(_tap, True)
            return event
        field = functools.partial(Quartz.CGEventGetIntegerValueField, event)
        pid = _target_pid(event) if _targets else 0  # nothing driven: nothing to map
        saw(
            kind,
            field(Quartz.kCGEventSourceUnixProcessID),
            field(Quartz.kCGEventSourceUserData),
            pid,
            field(Quartz.kCGKeyboardEventKeycode),
            stray=bool(pid) and _stray(event, kind, pid),
            command=bool(
                Quartz.CGEventGetFlags(event) & Quartz.kCGEventFlagMaskCommand
            ),
        )
    except Exception:
        log.exception("could not read an event of the person")
    return event


def _target_pid(event) -> int:
    """Where an event goes: named on it, else the window under it, else the front app."""
    field = functools.partial(Quartz.CGEventGetIntegerValueField, event)
    if pid := field(Quartz.kCGEventTargetUnixProcessID):
        return pid
    if window := field(Quartz.kCGMouseEventWindowUnderMousePointer):
        option = Quartz.kCGWindowListOptionIncludingWindow
        for row in Quartz.CGWindowListCopyWindowInfo(option, window) or ():
            return row.get(Quartz.kCGWindowOwnerPID, 0)
    return _frontmost_pid()


def _stray(event, kind: int, pid: int) -> bool:
    """The event goes to a process that is not where the person is: a key to an app
    that is not in front, a click on another process's window."""
    if kind == KEY_DOWN:
        return pid != _frontmost_pid()
    window = Quartz.CGEventGetIntegerValueField(
        event, Quartz.kCGMouseEventWindowUnderMousePointer
    )
    option = Quartz.kCGWindowListOptionIncludingWindow
    rows = Quartz.CGWindowListCopyWindowInfo(option, window) or () if window else ()
    owner = next((row.get(Quartz.kCGWindowOwnerPID, 0) for row in rows), 0)
    return owner not in (0, pid)


def _frontmost_pid() -> int:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else 0


@functools.lru_cache(maxsize=256)
def _bundle_of(pid: int) -> str:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str(app.bundleIdentifier() or "") if app else ""


def _touch_from_idle_time(target: AppTarget, clock) -> None:
    """Where the tap cannot see: ask how long ago the person's last event was.

    This tells nothing about which app got it, so it counts only for the front app.
    The hardware state is asked, never a private one: that call does not return.
    Events we post to one process do not reset it (measured).
    """
    idle = _hid_idle()
    if idle >= DEBOUNCE_S or not target.pid or _frontmost_pid() != target.pid:
        return
    quiet_at = clock.monotonic() + DEBOUNCE_S - idle
    _quiet_at[target.path] = max(quiet_at, _quiet_at.get(target.path, quiet_at))


def _hid_idle(kinds: tuple[int, ...] = EVENT_TYPES) -> float:
    """Seconds since the person's last click, scroll or key, from the hardware state."""
    state = Quartz.kCGEventSourceStateHIDSystemState
    since = Quartz.CGEventSourceSecondsSinceLastEventType
    return min(since(state, kind) for kind in kinds)


def went_to(pid: int, since: float) -> bool:
    """Did the person go to this app themselves after `since` (a monotonic time): a
    click of theirs landed in it, or they pressed Command-Tab or clicked the Dock?
    Their typing in another app says nothing: keys follow the front app, also one that
    came to the front by itself. Without the tap only the hardware's idle time is
    known, not where a click went: any click counts."""
    if _tap is None:
        return time.monotonic() - _hid_idle(CLICKS) >= since
    return max(_switched, _clicked[1] if _clicked[0] == pid else -math.inf) >= since


def secure_input_pid() -> int | None:
    """The process that holds secure keyboard input, if any (a password field).

    While one does, no tap sees a key press: the person's typing there is invisible.
    """
    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    return session.get("kCGSSessionSecureInputPID")
