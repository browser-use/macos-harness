"""How each tool reaches the app. Top rung first:

1. an accessibility action or attribute write: no event, no focus, no coordinates;
2. events posted to the process that owns the window, as a plain click when the app
   believes it has focus, after a made-up activation where a click may select or
   lands in web content, with the Command key everywhere else (a Command-click acts
   on a background window without raising it);
3. keys, after the app was made to believe it has focus.

There is no rung that moves the real pointer or brings the app to the front. Only
the secondary action `Raise` changes the window order, and the model asks for it
by name. No function here waits for the app afterwards or reads state.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time

from . import (
    apps,
    ax,
    errors,
    events,
    focus,
    keys,
    pasteboard,
    policy,
    presence,
    richtext,
    tree,
    windows,
)
from .model import SLOW_TYPING, TYPE_DELAY, AppSession, Line, Point, Rect, WindowRef

# A click on an element whose centre is hidden lands on the centre of the part that
# shows. Off = such a click fails, also when most of the element is in view.
CLICK_VISIBLE_PART = True
# Escape closes the open menu through the menu's own Cancel action. Off = the key
# is posted like any other and the menu view stays.
ESCAPE_CANCELS_MENU = True
# A click on a menu bar title does not press it: the app is told it is active, its menu
# is read as it is, closed, and an item is picked by its own action. Measured: pressed,
# the menu of an app in the background is drawn on the person's menu bar and takes
# their next clicks and keys.
PRESS_MENU_TITLES = False
ALWAYS_SIMULATE_CLICK = "MACOS_HARNESS_ALWAYS_SIMULATE_CLICK"  # "1": never rung 1

MOVE_LEAD = 0.067  # seconds between the mouse-moved event and the press
SCROLL_LEAD, SCROLL_REST = 0.017, 0.010  # seconds before and after the wheel event
CLICK_GAP = 0.1  # seconds between the clicks of a double click
# A drag moves in steps of at most this many points, this many seconds apart, at most
# DRAG_MAX steps. Measured: an AppKit drag session (Finder, a drop target) drops with
# steps of 26 points 20 ms apart, 0 of 6 with steps of 65 points or more.
DRAG_SPAN, DRAG_STEP, DRAG_MAX = 20.0, 0.02, 60
LONG_WAIT = 0.1  # a wait longer than this ends when the call is cancelled
PRESS_JOIN = 0.25  # seconds a press that opened no menu gets to come back
FILL_WAIT, FILL_POLL = 0.25, 0.01  # a menu opened to be filled: longest open, poll
MIN_EXTENT = 100.0  # a page is never shorter than this many points
MAX_PARENTS = 300
ESCAPE = keys.Chord(53, 0)
BROWSER_FIELD = "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"
MENU_FAILED = errors.menu_click_failed().message

WHEEL = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}
MENU_ROLES = {"AXMenuBarItem", "AXMenuItem"}
OPENS_MENU = {"AXMenuBarItem", "AXPopUpButton", "AXMenuButton"}
MAY_SELECT = {"AXRow", "AXCell"}  # a click there selects: never the Command tier
# Inside a web page only these are clicked by their action. Every other element (button,
# link, text field, slider, summary) gets real clicks, which also focus it. Measured.
WEB_ACTION_ROLES = {"AXCheckBox", "AXRadioButton"}
TEXT_FIELDS = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
NAME_FIELD = "saveAsNameTextField"  # the file name field of a save panel
SELECTION = {"text": (0, 1), "cursor_before": (0, 0), "cursor_after": (1, 1)}
DISCLOSE = {"Expand": True, "Collapse": False}
PAGE_BUTTON = {
    "Scroll Up": ("AXVerticalScrollBar", "AXDecrementPage"),
    "Scroll Down": ("AXVerticalScrollBar", "AXIncrementPage"),
    "Scroll Left": ("AXHorizontalScrollBar", "AXDecrementPage"),
    "Scroll Right": ("AXHorizontalScrollBar", "AXIncrementPage"),
}

quiet = contextlib.suppress(errors.HarnessError)


def _try(call, *args):
    """The answer, or None when the app refuses."""
    with quiet:
        return call(*args)
    return None


# --- geometry ---------------------------------------------------------------------


def _centre(rect: Rect) -> Point:
    return (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)


def _cut(a: Rect | None, b: Rect | None) -> Rect | None:
    """The part of `a` inside `b`. None when nothing is left."""
    if a is None or b is None:
        return a
    x, y = max(a[0], b[0]), max(a[1], b[1])
    w, h = min(a[0] + a[2], b[0] + b[2]) - x, min(a[1] + a[3], b[1] + b[3]) - y
    return (x, y, w, h) if w > 0 and h > 0 else None


def _inside(point: Point, rect: Rect) -> bool:
    return (
        rect[0] <= point[0] < rect[0] + rect[2]
        and rect[1] <= point[1] < rect[1] + rect[3]
    )


def _chain(element) -> list[tuple[object, str]]:
    """The element and its parents with their roles, up to its window."""
    out = []
    while element is not None and len(out) <= MAX_PARENTS:
        role = ax.get(element, "AXRole")
        out.append((element, role))
        element = None if role == "AXWindow" else ax.get(element, "AXParent")
    return out


def _visible_part(element, window: WindowRef) -> Rect | None:
    """The element's frame cut by its window and by every scroll area above it."""
    frame = ax.frame(element)
    part = (
        _cut(frame, window.frame) if frame and frame[2] > 0 and frame[3] > 0 else None
    )
    for parent, role in _chain(element)[1:]:
        if part and role == "AXScrollArea":
            part = _cut(part, ax.frame(parent))
    return part


def _position(element, window: WindowRef) -> bool:
    """Bring the element into view. True when its centre can be clicked."""
    for ask in (False, True):
        if ask:
            with quiet:
                if "AXScrollToVisible" in ax.actions(element):
                    ax.perform(element, "AXScrollToVisible")
                for parent, role in _chain(element)[1:]:
                    if role == "AXScrollArea":
                        ax.param(parent, "AXScrollToShowDescendant", element)
                        break
        part = _visible_part(element, window)
        if part and _inside(_centre(ax.frame(element)), part):
            return True
    return False


def _scroll_to(element, part: Rect) -> None:
    """Bring a part of an element to the middle of its scroll area by moving the
    scroll bars. A bar moves by the share of the content's way that the part is off
    the middle. On an axis where the part shows whole, nothing moves."""
    areas = (parent for parent, role in _chain(element)[1:] if role == "AXScrollArea")
    holder = next(areas, None)
    area, content = holder is not None and ax.frame(holder), ax.frame(element)
    if not (area and content):
        return
    for axis, name in enumerate(("AXHorizontalScrollBar", "AXVerticalScrollBar")):
        low, size, room = part[axis], part[axis + 2], content[axis + 2] - area[axis + 2]
        shows = area[axis] <= low and low + size <= area[axis] + area[axis + 2]
        bar = None if shows or room <= 0 else _try(ax.get, holder, name)
        if bar is not None:
            off = low + size / 2 - (area[axis] + area[axis + 2] / 2)
            share = (_try(ax.get, bar, "AXValue") or 0.0) + off / room
            _try(ax.set, bar, "AXValue", min(1.0, max(0.0, share)))


def _clickable_point(element, window: WindowRef) -> Point:
    if _position(element, window):
        return _centre(ax.frame(element))
    part = _visible_part(element, window) if CLICK_VISIBLE_PART else None
    if part is None:
        raise errors.cannot_click_offscreen()
    return _centre(part)


def _range(location: int, length: int):
    """A text range, wrapped the way the app wants it."""
    import ApplicationServices as api

    return api.AXValueCreate(api.kAXValueCFRangeType, (location, length))


def _rect(value) -> Rect | None:
    """A rect as the app gives it: wrapped, or a plain tuple."""
    if value is None or isinstance(value, tuple):
        return value
    import ApplicationServices as api

    found, rect = api.AXValueGetValue(value, api.kAXValueCGRectType, None)
    if not found:
        return None
    return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)


def _pages(pages: float, extent: float) -> int:
    """Pages to pixels, a half rounds away from zero."""
    return math.floor(pages * max(extent, MIN_EXTENT) + 0.5)


# --- who gets the event -----------------------------------------------------------


def _at(app: AppSession, point: Point) -> tuple[WindowRef, object]:
    """The window and the element under a point. The hit test asks the app, never
    the whole screen, so another app's window on top does not take the click."""
    window = windows.at(app.target, point)
    element = ax.at(ax.app(app.target.pid), point[0], point[1])
    if element is not None and ax.pid(element) != app.target.pid:
        window = windows.ref(element)  # drawn by another process: it gets the event
    return window, element


def _recipient(app: AppSession, pid: int) -> int:
    """The process an event is about to be posted to. One that is not the app itself
    (it draws part of the app's window) must pass the policy as well: a system
    password sheet inside an approved app gets no click and no key."""
    if pid != app.target.pid:
        policy.check_recipient(apps.bundle_id(pid))
    return pid


def _owner(app: AppSession, element) -> None:
    """An element's process passes the policy too: an accessibility action reaches the
    button or the secure field of a sheet another process draws just as an event does."""
    pid = _try(ax.pid, element)
    if pid is not None:
        _recipient(app, pid)


def _front_window(app: AppSession) -> WindowRef | None:
    """The window keys go to: the app's key window. None when it has no window."""
    return _try(windows.key_window, app.target)


def _key_pid(app: AppSession, activated: bool) -> int:
    """Keys go to the app, or to the process that owns its focused element. An open
    or save panel is drawn by a system service and takes keys only from there:
    posted to the app they are lost (measured on TextEdit's save sheet). When the
    app was just told it is active, the panel gets its keys back only through a
    click on its focused field."""
    app_el = ax.app(app.target.pid)
    focused = _try(ax.get, app_el, "AXFocusedUIElement")
    window = _try(ax.get, app_el, "AXFocusedWindow")
    panel = (
        window is not None and _try(ax.get, window, "AXIdentifier") in apps.FILE_PANELS
    )
    service = apps.panel_service(app.target.pid) if panel else None
    if service is not None:
        if activated and focused is not None:
            _click_once(app, focused)
        return _recipient(app, service)
    return app.target.pid if focused is None else _recipient(app, ax.pid(focused))


def _in_web_page(element) -> bool:
    return "AXWebArea" in (role for _, role in _chain(element))


def _needs_activation(app: AppSession, element, forced: bool) -> bool:
    kind = apps.kind(app.target)
    roles = [role for _, role in _chain(element)]
    return bool(
        forced
        or kind.chromium
        or kind.electron
        or "AXWebArea" in roles
        or MAY_SELECT.intersection(roles)
        or (kind.catalyst and roles[:1] == ["AXMenuButton"])
    )


def _pause(seconds: float, cancel) -> None:
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if seconds > LONG_WAIT and cancel():
            return
        time.sleep(min(left, LONG_WAIT / 2))


def _go_on(app: AppSession) -> None:
    """Before every event of an action: when the person used the app since the call
    began, or the client went away (the host interrupted it), nothing more is sent."""
    if app.cancel():
        raise errors.user_changed(app.target.path)


def _show(element, window: WindowRef | None = None) -> WindowRef | None:
    """Outline a target that was given by its number. With its window the drawn
    pointer sits just above that window and is hidden with it."""
    window = window or _try(windows.ref, element)
    if frame := ax.frame(element):
        presence.flash(frame, window)
    return window


def _usable(window: WindowRef) -> None:
    """Events need the window's id, and a minimised window takes none at all."""
    if not window.window_id:
        raise errors.no_window_id()
    if _try(ax.get, window.element, "AXMinimized"):
        raise errors.cannot_click_offscreen()


def _aim(element) -> tuple[WindowRef, Point]:
    """The window an element's events go to, and the point to click. A minimised
    window has no part on screen and is never put back by us."""
    own = _try(ax.get, element, "AXWindow")
    if own is not None and _try(ax.get, own, "AXMinimized"):
        raise errors.cannot_click_offscreen()
    window = windows.ref(element)
    return window, _clickable_point(element, window)


def _unanswered(error: errors.HarnessError) -> bool:
    """The app did not answer in time. What we asked for may still have happened,
    so it is never asked a second time in another way."""
    return "cannotComplete" in error.message


# --- mouse ------------------------------------------------------------------------


def _deliver(app, window, element, series, button=0, key=None, hold=None, lead=None):
    """Post a series of (kind, point, click count, seconds to wait) to the process
    that owns the window. A button that went down always comes up again."""
    _usable(window)
    enforcer, pid = focus.enforcer(app.target), _recipient(app, window.pid)
    held = keys.modifiers(key) if key else 0
    flags, before, pressed = held, events.held_flags(), None
    with focus.INPUT_LOCK, contextlib.ExitStack() as stack:
        if lead is not None:
            events.post(pid, events.mouse("moved", series[0][1], window), lead)
        if not enforcer.believes_focus:
            if _needs_activation(app, element, bool(key or hold)):
                stack.enter_context(enforcer.active(window))
            else:
                flags |= keys.COMMAND
        if held:
            events.post(pid, events.flags_changed(held))
        try:
            for kind, point, count, wait in series:
                _go_on(app)
                events.post(
                    pid, events.mouse(kind, point, window, button, count, flags)
                )
                pressed = None if kind == "up" else (point, count or pressed[1])
                _pause(hold if hold and kind == "down" else wait, app.cancel)
        finally:
            if pressed:
                up = events.mouse("up", pressed[0], window, button, pressed[1], flags)
                events.post(pid, up)
            if held:
                events.post(pid, events.flags_changed(before))


def _click_once(app: AppSession, element) -> None:
    window, point = _aim(element)
    series = [("down", point, 1, events.CLICK_HOLD), ("up", point, 1, 0)]
    _deliver(app, window, element, series)


def _press(app: AppSession, element, action: str, role: str = "") -> None:
    """Rung 1. A press that opens a menu returns only when the menu closes, so it
    runs on the side while we wait for the menu to show."""
    opens = role in OPENS_MENU and action in ("AXPress", "AXPick")
    if not opens and action != "AXShowMenu":
        return ax.perform(element, action)
    failed: list[errors.HarnessError] = []

    def run():
        try:
            ax.perform(element, action)
        except errors.HarnessError as error:
            failed.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if windows.wait_for_menu(app.target, element) is not None:
        return
    thread.join(PRESS_JOIN)
    if failed:
        raise failed[0]
    children = _try(ax.get, element, "AXChildren") or []
    menus = [c for c in children if _try(ax.get, c, "AXRole") == "AXMenu"]
    if thread.is_alive() or menus:
        # No menu was reported, yet one may be open (the press has not come back,
        # or the element shows a menu of its own). An open menu of a background
        # app holds the keyboard: close it and say the click failed.
        for menu in menus:
            _try(ax.perform, menu, "AXCancel")
        raise errors.menu_click_failed()


def cancel_menu(app: AppSession) -> None:
    """Close the open menu the way the app offers. Never leave one open: an open
    menu of a background app holds the system's keyboard. Also at a turn end."""
    try:
        if (menu := windows.shown_menu(app.target)) is not None:
            with quiet:
                ax.perform(menu, "AXCancel")
    finally:
        windows.close_menu(app.target)


def _menu_click(app: AppSession, line: Line) -> None:
    """A menu bar title: the app is told it is active, so that its items are the
    ones of its key window, then its menu is taken as the open one. An item: pick,
    else press. No mouse event goes to a menu either way."""
    element = line.element
    try:
        if line.role == "AXMenuBarItem":
            with focus.enforcer(app.target).active(_front_window(app)):
                if not PRESS_MENU_TITLES:
                    if not _has_items(element):  # the app fills it only when it opens
                        _fill(element)
                    if not _has_items(element):
                        raise errors.menu_fills_on_open(line.name)
                    return windows.keep_menu(app.target, element)
                return _press(app, element, "AXPress", line.role)
        ax.perform(element, _press_action(element) or "AXPress")
    except errors.HarnessError as error:
        empty = error.message == errors.menu_fills_on_open(line.name).message
        if _unanswered(error) or empty:
            windows.close_menu(app.target)
            raise
        cancel_menu(app)
        raise errors.menu_click_failed() from None
    windows.close_menu(app.target)


def _has_items(title) -> bool:
    menus = _try(ax.get, title, "AXChildren") or []
    return any(_try(ax.get, menu, "AXChildren") for menu in menus)


def _fill(title) -> None:
    """Open the title's menu for real, only until the app has
    filled it (at most FILL_WAIT), then cancel it. While open it takes the person's
    input, so it stays open for as short as the app allows."""
    press = threading.Thread(target=_try, args=(ax.perform, title, "AXPress"))
    press.daemon = True
    press.start()
    deadline = time.monotonic() + FILL_WAIT
    try:
        while not _has_items(title) and time.monotonic() < deadline:
            time.sleep(FILL_POLL)
    finally:
        for menu in _try(ax.get, title, "AXChildren") or []:
            _try(ax.perform, menu, "AXCancel")
        press.join(PRESS_JOIN)


def _press_action(element) -> str | None:
    offered = ax.actions(element)
    return next((name for name in ("AXPick", "AXPress") if name in offered), None)


def click(
    app: AppSession,
    target: Line | Point,
    button: int = 0,
    count: int = 1,
    key: str | None = None,
    hold: float | None = None,
) -> None:
    if count < 1:
        raise errors.invalid_click_count()
    plain = button == 0 and count == 1 and key is None and hold is None
    lead = None
    if isinstance(target, Line):
        element = target.element
        _owner(app, element)
        if target.role in MENU_ROLES:
            if not plain:
                raise errors.menu_action_not_supported()
            return _menu_click(app, target)
        action = _press_action(element)
        forced = app.agent.env.get(ALWAYS_SIMULATE_CLICK) == "1"
        if target.role not in WEB_ACTION_ROLES and _in_web_page(element):
            action = None
        if plain and action and not forced:
            window = _try(windows.ref, element)
            if window is not None:
                _try(_position, element, window)
            _show(element, window)
            presence.pointer_press(1)
            try:
                return _press(app, element, action, target.role)
            except errors.HarnessError as error:
                if _unanswered(error) or error.message == MENU_FAILED:
                    raise
                # the app refused the action: the click goes as events
        window, point = _aim(element)
        _show(element, window)
    else:
        point, lead = target, MOVE_LEAD
        window, element = _at(app, point)
    presence.pointer_move(point, window)
    presence.pointer_press(count)
    series = []
    for i in range(1, count + 1):
        series += [("down", point, i, events.CLICK_HOLD), ("up", point, i, CLICK_GAP)]
    series[-1] = ("up", point, count, 0)
    _deliver(app, window, element, series, button, key, hold, lead)


def drag(app: AppSession, start: Point, end: Point) -> None:
    """Press at the start, move there in short steps, release at the end. One
    move through the midpoint only drops in an AppKit drag session
    (Finder) 0 of 4 times, the short steps 7 of 7. No held key."""
    window, element = _at(app, start)
    span = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = min(DRAG_MAX, max(2, math.ceil(span / DRAG_SPAN)))
    points = [
        (
            start[0] + (end[0] - start[0]) * i / steps,
            start[1] + (end[1] - start[1]) * i / steps,
        )
        for i in range(steps + 1)
    ]
    presence.pointer_move(start, window)
    presence.pointer_press(1)
    path = [("dragged", point, 0, DRAG_STEP) for point in points]
    series = [("down", start, 1, DRAG_STEP), *path, ("up", end, 1, 0)]
    _deliver(app, window, element, series, lead=MOVE_LEAD)
    presence.pointer_move(end, window)


def scroll(app: AppSession, target: Line | Point, direction: str, pages: float) -> None:
    """One wheel event, in pixels: a page is the height (or width) of the element,
    or of the window when a point is given."""
    if isinstance(target, Line):
        window, point = _aim(target.element)
        frame = ax.frame(target.element) or window.frame
        _show(target.element, window)
    else:
        point = target
        window, _ = _at(app, point)
        frame = window.frame
    _usable(window)
    _recipient(app, window.pid)
    x, y = WHEEL[direction]
    n = _pages(pages, frame[2] if x else frame[3])
    presence.pointer_move(point, window)
    with focus.INPUT_LOCK:
        events.post(window.pid, events.mouse("moved", point, window), SCROLL_LEAD)
        events.post(window.pid, events.wheel(point, x * n, y * n, window), SCROLL_REST)


# --- keys -------------------------------------------------------------------------


def _send_keys(app, presses, delay=0.0, hold=None) -> None:
    """Per key: tell the app which modifiers are down when they differ from what
    the person holds, key down, key up, and the person's modifiers again."""
    enforcer = focus.enforcer(app.target)
    window = None if enforcer.believes_focus else _front_window(app)
    with enforcer.active(window):
        pid = _key_pid(app, activated=window is not None)
        for press in presses:
            _go_on(app)
            before = events.held_flags()
            text = press.text or None
            changed = before & events.MODIFIERS != press.flags
            if changed:
                events.post(pid, events.flags_changed(press.flags), delay)
            events.post(pid, events.key(press.code, press.flags, text), delay)
            try:
                if hold and press is presses[-1]:
                    _pause(hold, app.cancel)
            finally:
                up = events.key(press.code, press.flags, text, down=False)
                events.post(pid, up, delay)
                if changed:
                    events.post(pid, events.flags_changed(before), delay)


def press_key(app: AppSession, spec: str, hold: float | None = None) -> None:
    chords = keys.chords(spec)
    menu_open = chords == [ESCAPE] and windows.shown_menu(app.target) is not None
    if ESCAPE_CANCELS_MENU and menu_open:
        return cancel_menu(app)
    presses = [keys.KeyPress(chord.code, chord.flags, "") for chord in chords]
    _send_keys(app, presses, hold=hold)


def type_text(app: AppSession, text: str) -> None:
    """Always key events, added to what is there. Never the clipboard."""
    if not text:
        raise errors.no_text_to_type()
    focused = _try(ax.get, ax.app(app.target.pid), "AXFocusedUIElement")
    _no_path_as_name(focused, text)
    delay = SLOW_TYPING.get(app.target.bundle_id, TYPE_DELAY)
    _send_keys(app, keys.presses(text), delay)


def _no_path_as_name(element, text: str) -> None:
    """A file panel's name field takes a name. A path typed there saves a file whose
    name holds the path (seen: colons in names in the home folder)."""
    named = element is not None and _try(ax.get, element, "AXIdentifier") == NAME_FIELD
    if named and "/" in text:
        raise errors.path_in_file_name()


def paste(app: AppSession, text: str, fmt: str) -> None:
    field = _try(ax.get, ax.app(app.target.pid), "AXFocusedUIElement")
    watched = ["AXValue", "AXSelectedTextRange", "AXNumberOfCharacters"]
    before = ax.get_many(field, watched) if field is not None else []
    landed = (lambda: ax.get_many(field, watched) != before) if any(before) else None
    pasteboard.paste(text, fmt, lambda: press_key(app, "super+v"), landed=landed)


# --- accessibility only -----------------------------------------------------------


def _like(current, value: str):
    """A slider takes a number, a text field a string: follow what is there."""
    if isinstance(current, (int, float)):
        with contextlib.suppress(ValueError):
            number = float(value)
            whole = isinstance(current, int) and number.is_integer()
            return int(number) if whole else number
    return value


def set_value(app: AppSession, line: Line, value: str) -> None:
    element = line.element
    _owner(app, element)
    if not ax.settable(element, "AXValue"):
        raise errors.not_settable()
    _no_path_as_name(element, value)
    _show(element)
    # A text area (a document) is clicked first even when it has focus. Measured:
    # written in the background with no click, a document app (TextEdit) that saves
    # right after waits on itself for good.
    document = line.role == "AXTextArea"
    unfocused = line.role in TEXT_FIELDS and not ax.get(element, "AXFocused")
    if document or (unfocused and not focus.focus_element(element)):
        _click_once(app, element)
    ax.set(element, "AXValue", _like(ax.get(element, "AXValue"), value))
    # Only a browser's address field is submitted. A search field of an app is not:
    # measured, a value set there starts no search until Return is pressed by a call.
    identifier, shortcut = ax.get_many(element, ["AXIdentifier", "AXKeyShortcutsValue"])
    if identifier == BROWSER_FIELD or shortcut == "⌘L":
        press_key(app, "Return")


def select_text(
    app: AppSession,
    line: Line,
    text: str,
    prefix: str = "",
    suffix: str = "",
    mode: str = "text",
) -> None:
    if mode not in SELECTION:
        raise errors.usage("selection_type", value=mode)
    element = line.element
    _owner(app, element)
    plain = ax.settable(element, "AXSelectedTextRange")
    if not plain and not ax.settable(element, "AXSelectedTextMarkerRange"):
        raise errors.cannot_select_text()
    # Searched as printed: a rich text area prints Markdown, so the model's text may
    # carry markers. The offsets lead back to the plain text the app selects in.
    printed, offsets = tree.printed_text(element)
    found = richtext.find(printed, offsets, text, prefix, suffix)
    if found is None:
        raise errors.text_not_found()
    start, end = (found[i] for i in SELECTION[mode])
    span = _range(start, end - start)
    bounds = lambda: _rect(_try(ax.param, element, "AXBoundsForRange", span))
    if not _try(ax.get, element, "AXFocused"):
        focus.focus_element(element)  # what is typed next goes where the selection is
    if hidden := bounds():
        _scroll_to(element, hidden)
    if shown := bounds():
        presence.pointer_move(_centre(shown), _try(windows.ref, element))
    if plain:
        return ax.set(element, "AXSelectedTextRange", span)
    marks = [ax.param(element, "AXTextMarkerForIndex", i) for i in (start, end)]
    span = ax.param(element, "AXTextMarkerRangeForUnorderedTextMarkers", marks)
    ax.set(element, "AXSelectedTextMarkerRange", span)


def _page_button(element, bar: str, part: str) -> bool:
    """Press the page button of the element's scroll bar. Never a value write."""
    found = _try(ax.get, element, bar)
    for child in (found is not None and _try(ax.get, found, "AXChildren")) or []:
        if ax.get(child, "AXSubrole") == part:
            ax.perform(child, "AXPress")
            return True
    return False


def secondary(app: AppSession, line: Line, action: str) -> None:
    """One of the actions printed on the element's line, by its exact name."""
    if action not in line.actions:
        raise errors.invalid_secondary_action(action, line.id)
    element, name = line.element, line.actions[action]
    _owner(app, element)
    _show(element)
    disclosing = next(
        (a for a in ("AXDisclosing", "AXExpanded") if ax.settable(element, a)), None
    )
    if action in DISCLOSE and disclosing:
        ax.set(element, disclosing, DISCLOSE[action])
    elif action in PAGE_BUTTON and _page_button(element, *PAGE_BUTTON[action]):
        pass
    elif name == "AXPick":
        _menu_click(app, line)
    elif name == "AXCancel":
        try:
            ax.perform(element, name)
        finally:
            windows.close_menu(app.target)
    else:
        _press(app, element, name, line.role)
