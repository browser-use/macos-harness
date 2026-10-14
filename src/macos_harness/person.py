"""The person comes first: notice their input, wait for them, stop when they say so.

One listen-only event tap sees the person's clicks, scrolls and key presses. An event
on an app we drive leaves a record for that app. The next call on it waits until the
person has been quiet for 5 s, and an action then fails, so that the model reads the
app again before it acts. A stop is per app and comes from the person, never from
the model.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import threading
import time

import Quartz
from AppKit import NSRunningApplication, NSWorkspace

from . import ax, errors, events, presence, protocol, runloop, windows
from .model import AgentSession, AppTarget

# Left, right and other mouse down, scroll wheel, key down. No move, no key up.
EVENT_TYPES = (1, 3, 25, 22, 10)
KEY_DOWN, ESCAPE, TAB = 10, 53, 48
CLICKS, DOCK = (1, 3, 25), "com.apple.dock"
DEBOUNCE_S = 5.0  # the person counts as busy this long after their last event
WAIT_CAP_S = 100.0  # how long one call waits for the person to finish
POLL_S = 0.25
DOUBLE_PRESS_S = 0.5  # Escape twice within this stops every driven app
STOP_REASONS = ("command", "menu", "escape")

log = logging.getLogger(__name__)


_LOCK = threading.RLock()
_drivers: dict[str, set[str]] = {}  # app path -> keys of the agents that drive it
_targets: dict[str, AppTarget] = {}  # app path -> the app, with its newest pid
# App path -> when the person will have been quiet for the whole debounce. The entry
# stays until the model has read the app again.
_quiet_at: dict[str, float] = {}
_stops: set[str] = set()  # paths of the apps the person stopped
# The person stopped everything: every app is refused, also one not driven yet.
_stop_all = False
_awaiting_quiet: set[str] = set()  # app paths with a "quiet again" timer running
_own_pid = 0
_tap = None
_last_escape = -math.inf
# How the person last went to an app themselves: (the process their last click landed
# in, when), and when they last pressed Command-Tab or clicked the Dock.
_clicked: tuple[int, float] = (0, -math.inf)
_switched = -math.inf


# --- the gate before every call on an app
# ------------------------------------------------------------------------------


def gate(agent: AgentSession, target: AppTarget, state_read: bool, clock=time) -> None:
    """Hold a call back while the person is using the app, or refuse it.

    In order: the turn has ended; the person stopped this app; wait for them to be
    quiet (5 s after their last event, at most 100 s); then an action fails until a
    state read has seen what they changed.
    """
    if agent.ended:
        raise errors.turn_ended()
    path, until = target.path, clock.monotonic() + WAIT_CAP_S
    _note_driven(agent, target)
    if _tap is None or secure_input_pid() is not None:  # no tap, or one blind to keys
        _touch_from_idle_time(target, clock)
    while True:
        now = clock.monotonic()
        if _stop_holds(path):
            raise errors.stopped_by_user()
        quiet_at = _quiet_at.get(path)
        if quiet_at is None or now >= quiet_at:
            break
        if now >= until:
            raise errors.still_interacting(path)
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
        keys = _drivers.setdefault(target.path, set())
        new = agent.key not in keys
        keys.add(agent.key)
        _targets[target.path] = target
    if new:
        presence.apps_in_use(driven())


# --- stop and turns
# ------------------------------------------------------------------------------


def _keep_stops() -> None:
    """Write the stops down: a helper that is started again still holds them. While
    one holds, the menu bar item stays as the way out."""
    record = {"all": _stop_all, "apps": sorted(_stops)}
    presence.stopped(_stop_all or bool(_stops))
    try:
        folder = protocol.state_dir()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        draft = folder / "stops.tmp"
        draft.write_text(json.dumps(record), encoding="utf-8")
        os.replace(draft, folder / "stops.json")
    except OSError as error:
        log.warning("the stops were not written down: %s", error)


def load_stops() -> None:
    """At helper start: the stops the last helper held. A file that cannot be read
    stops everything, until the person resumes."""
    global _stop_all
    try:
        record = json.loads((protocol.state_dir() / "stops.json").read_text("utf-8"))
        everything, paths = bool(record["all"]), [str(path) for path in record["apps"]]
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError):
        everything, paths = True, []
    with _LOCK:
        _stop_all = _stop_all or everything
        _stops.update(paths)
    if _stop_all or _stops:  # nothing is drawn at start when nothing holds
        presence.stopped(True)


def stop(target: AppTarget | None = None, by: str = "command", clock=time) -> None:
    """The person stops one app, or everything: every driven app and any app a call
    names from now on. A running action sends nothing more."""
    global _stop_all
    now = clock.monotonic()
    with _LOCK:
        _stop_all = _stop_all or target is None
        paths = [target.path] if target else list(_drivers)
        for path in paths:
            _stops.add(path)
            # After a resume the first action must follow a fresh state read.
            _quiet_at[path] = now
        names = ", ".join(_targets[path].name for path in paths if path in _targets)
        open_menus = [_targets[path] for path in paths if path in _targets]
        _keep_stops()
    for (
        stopped_app
    ) in open_menus:  # a menu the agent left open would take the next keys
        _later(0.0, lambda target=stopped_app: _cancel_menu(target))
    log.info("stopped by %s: %s", by if by in STOP_REASONS else "command", paths)
    presence.pointer_activity("paused")
    presence.label(f"Stopped {names or 'every app'}")  # the menu's first row


def resume(target: AppTarget | None = None) -> None:
    """The person lifts a stop: of one app (the other marks stay), or of everything."""
    global _stop_all
    with _LOCK:
        _stop_all = False
        _stops.difference_update([target.path] if target else set(_stops))
        _keep_stops()
    presence.pointer_activity("loading")
    presence.label("Resumed")


def stopped(target: AppTarget) -> bool:
    return target.path in _stops or _stop_all


def interrupted(target: AppTarget, since: float) -> bool:
    """The person stopped this app, or used it after `since` (a monotonic time): the
    action that is running sends nothing more."""
    used = _quiet_at.get(target.path, -math.inf) - DEBOUNCE_S
    return stopped(target) or used >= since


def _stop_holds(path: str) -> bool:
    """True while a stop is in force for this call. Only the person lifts it. Under a
    stop of everything the refused app gets a mark of its own."""
    with _LOCK:
        if path not in _stops and not _stop_all:
            return False
        _stops.add(path)
        return True


def turn_started(agent: AgentSession) -> None:
    agent.signals_turns, agent.ended = True, False


def turn_ended(agent: AgentSession) -> None:
    """Forget what this agent drove: person records, refused apps. A stop stays: only
    the person lifts it (a turn end the model can send itself must not)."""
    # Only a host that also says when a turn starts can be held until the next one.
    agent.ended = agent.signals_turns
    agent.denied.clear()
    with _LOCK:
        for path, keys in list(_drivers.items()):
            keys.discard(agent.key)
            if not keys:  # another agent that still drives the app keeps its marks
                for table in (_drivers, _targets, _quiet_at):
                    table.pop(path, None)
    presence.apps_in_use(driven())


# --- noticing the person
# ------------------------------------------------------------------------------


def start_watch(own_pid: int) -> None:
    """Called once when the helper starts."""
    global _own_pid
    _own_pid = own_pid
    load_stops()
    presence.on_stop(lambda target=None: stop(target, by="menu"))
    presence.on_resume(resume)
    runloop.on_main(_start_tap)


def saw(
    kind: int,
    source_pid: int,
    tag: int,
    pid: int,
    keycode: int = 0,
    repeat: bool = False,
    clock=time,
    stray: bool = False,
    command: bool = False,
) -> None:
    """One event from the tap. Our own are dropped, the person's are counted.

    A stray event went to a driven app in the background only because a menu open
    there holds the person's input (a pop-up of a web page): it was meant for the app
    they are in. It does not count, and the menu is closed so the next one gets there.
    """
    global _last_escape, _clicked, _switched
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
    if kind == KEY_DOWN and keycode == ESCAPE and not repeat:
        pair = now - _last_escape <= DOUBLE_PRESS_S
        _last_escape = -math.inf if pair else now
        if (
            pair and _drivers
        ):  # the brake of a running agent: with none, an ordinary key
            stop(by="escape", clock=clock)
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


def _cancel_menu(target: AppTarget) -> None:
    """Cancel the menu a stopped app has open, if it has one. Nothing else is sent."""
    try:
        menu = windows.shown_menu(target)
        if menu is not None:
            ax.perform(menu, "AXCancel")
            windows.close_menu(target)
    except errors.HarnessError:
        pass


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
            bool(field(Quartz.kCGKeyboardEventAutorepeat)),
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
