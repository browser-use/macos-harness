"""Which window of an app a call works on, and the menu an app has open.

Nothing is cached about a window: its frame, id and state are read again on every call.
All coordinates are global points, origin top left, signed. Nothing is clamped to zero.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import ax, errors, focus
from .model import AppTarget, Point, Rect, WindowRef

MAX_PARENTS = 300  # how far ref() climbs from an element another process draws
MENU_WAIT = 0.25  # seconds a click may take to open its menu
MENU_POLL = 0.01
MENU_OPENED, MENU_CLOSED = "AXMenuOpened", "AXMenuClosed"
NEW_WINDOWS = (
    "AXWindowCreated",
    "AXSheetCreated",
)  # the app is told it wants no attention
KEY_WINDOW = ("AXFocusedWindow", "AXMainWindow", "AXWindows")  # in this order
WINDOW_LIST_ALL = 0  # the window server's option for every window, on screen or not
# A control that shows its open menu as a child. Closed, it has no such child.
MENU_BUTTONS = ("AXPopUpButton", "AXMenuButton")


# --- the window server ----------------------------------------------------------------


def _server_rows(option: int, window_id: int) -> list[dict]:
    import Quartz

    rows = Quartz.CGWindowListCopyWindowInfo(option, window_id) or []
    out = []
    for row in rows:
        box = row.get("kCGWindowBounds") or {}
        frame = tuple(float(box.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
        out.append(
            {
                "id": int(row.get("kCGWindowNumber", 0)),
                "pid": int(row.get("kCGWindowOwnerPID", 0)),
                "title": row.get("kCGWindowName"),
                "layer": int(row.get("kCGWindowLayer", 0)),
                "frame": frame,
            }
        )
    return out


def server_windows(pid: int | None = None) -> list[dict]:
    """Windows on screen, front first, without the desktop. One app's when pid is set."""
    import Quartz

    option = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    rows = _server_rows(option, Quartz.kCGNullWindowID)
    return [row for row in rows if pid is None or row["pid"] == pid]


def server_window(window_id: int) -> dict | None:
    """One window by id, also when it is minimised or on another Space."""
    import Quartz

    # Asking for one window leaves out a minimised one (measured). All windows has it.
    for option in (Quartz.kCGWindowListOptionIncludingWindow, WINDOW_LIST_ALL):
        rows = _server_rows(option, window_id if option else Quartz.kCGNullWindowID)
        if row := next((row for row in rows if row["id"] == window_id), None):
            return row
    return None


def _associated(window_id: int) -> set[int]:
    """The windows the window server ties to this one. Empty when the call is missing."""
    from . import events

    connection = events.private("CGSMainConnectionID", b"i")
    copy = events.private("CGSCopyAssociatedWindows", b"@ii")
    if not (connection and copy):
        return set()
    return {int(number) for number in copy(connection(), window_id) or ()}


def extra_windows(target: AppTarget, window_id: int) -> list[dict]:
    """The app's windows in front of this one that belong to it (associated with it):
    an open menu, a popover. They are added to the window's picture. Back to
    front, the order they are drawn in. A sheet that is the key window has its parent
    behind it: the sheet is pictured alone."""
    rows = server_windows(target.pid)  # front first
    ids = [row["id"] for row in rows]
    front = rows[: ids.index(window_id)] if window_id in ids else []
    return [r for r in reversed(front) if window_id in _associated(r["id"])]


# --- windows --------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _matching_id(pid: int, frame: Rect | None, title: str | None) -> int:
    """The on-screen window of the app with the same bounds. The title breaks a tie."""
    same = [row for row in server_windows(pid) if row["frame"] == frame]
    same.sort(key=lambda row: row["title"] != title)
    return same[0]["id"] if same else 0


def _frame(element: Any) -> Rect | None:
    frame = ax.frame(element)
    return tuple(float(v) for v in frame) if frame else None


def _window_ref(window: Any) -> WindowRef:
    # TODO: follow a window's bounds live from the window server.
    # Meanwhile they are read here, at each state read and each coordinate action.
    pid, title = ax.pid(window), _text(ax.get(window, "AXTitle"))
    frame = _frame(window)
    window_id = ax.window_id(window) or _matching_id(pid, frame, title)
    # A window is named as the window server names it: "All Fonts", where the app's
    # own title says "All Fonts - 385 typefaces". No server row: the app's title.
    row = server_window(window_id) if window_id else None
    title = _text(row["title"]) if row else title
    return WindowRef(window, window_id, pid, title, frame or (0.0, 0.0, 0.0, 0.0))


def _app(target: AppTarget) -> Any:
    if target.pid is None:
        raise errors.app_not_running(target.path)
    return ax.app(target.pid)


def key_window(target: AppTarget) -> WindowRef:
    """The focused window, else the main window, else the last of the window list."""
    focused, main, listed = ax.get_many(_app(target), KEY_WINDOW)
    for window in (focused, main, *(listed or ())[-1:]):
        if window is not None:
            return _window_ref(window)
    raise errors.no_windows()


def ordered(target: AppTarget) -> list[WindowRef]:
    """Every window the app lists, in the app's own order. Panels and dialogs count.

    The app lists only windows of the current Space; the others are still reached
    through the focused and main window.
    """
    return [_window_ref(window) for window in ax.get(_app(target), "AXWindows") or ()]


def at(target: AppTarget, point: Point) -> WindowRef:
    """The window and process an event at this point goes to."""
    listed = ordered(target)
    if not listed:
        raise errors.no_windows()
    x, y = point
    for window in listed:
        left, top, width, height = window.frame
        if left <= x < left + width and top <= y < top + height:
            hit = ax.at(_app(target), x, y)
            foreign = hit is not None and ax.pid(hit) != window.pid
            return ref(hit) if foreign else window
    raise errors.window_not_found_at(point)


def _owned_window(node: Any) -> WindowRef | None:
    """The window a node is in, when the node's own process owns that window."""
    window_id = ax.window_id(node)
    row = server_window(window_id) if window_id else None
    if not row or row["pid"] != ax.pid(node):
        return None
    is_window = ax.get(node, "AXRole") == "AXWindow"
    window = node if is_window else ax.get(node, "AXWindow")
    if window is None:  # no window element to ask: the server's bounds have to do
        return WindowRef(node, window_id, row["pid"], None, row["frame"])
    # The app's own frame, as key_window gives it: events are counted from there.
    frame, title = _frame(window) or row["frame"], _text(ax.get(window, "AXTitle"))
    return WindowRef(window, window_id, row["pid"], title, frame)


def ref(element: Any) -> WindowRef:
    """The window and process that own an element.

    An element another process draws (a web page, a file panel) names a window of the
    host app. So climb, the element first, to the nearest one whose own process owns
    its window, and send events there.
    """
    node = element
    for climbed in range(MAX_PARENTS + 1):
        if node is None:
            break
        if found := _owned_window(node):
            return found
        # Most elements name their window. In the same process that saves the climb.
        window = ax.get(node, "AXWindow") if climbed == 0 else None
        same_process = window is not None and ax.pid(window) == ax.pid(node)
        if same_process and (found := _owned_window(window)):
            return found
        node = ax.get(node, "AXParent")
    raise errors.element_outside_app()


# --- the open menu, one per app, shared by every agent --------------------------------


@dataclass
class _Menus:
    watchers: int = 0
    observer: Any = None
    heard: list[tuple[str, Any]] = field(default_factory=list)  # not yet looked at
    menu: Any = None  # the open menu itself
    root: Any = None  # what a state read prints alone: the menu bar item of the menu
    kept: Any = None  # a title taken by keep_menu: nothing is open on screen


_MENUS: dict[int, _Menus] = {}
_LOCK = threading.Lock()


def _menus(target: AppTarget) -> _Menus:
    with _LOCK:
        return _MENUS.setdefault(target.pid, _Menus())


def _keep(state: _Menus, menu: Any) -> bool:
    """Keep a menu as the open one. A submenu is part of the menu that is kept.

    Only a menu of the menu bar is read alone. Any other (a pop-up button's) is part
    of its window and is printed there, under the control that shows it.
    """
    parent = ax.get(menu, "AXParent")
    role = ax.get(parent, "AXRole") if parent is not None else None
    if role == "AXMenuItem":
        return False
    state.menu = menu
    state.root = parent if role == "AXMenuBarItem" else None
    return True


def _catch_up(state: _Menus) -> bool:
    """Apply what the observer heard. True when a menu opened and is still open."""
    with _LOCK:
        heard, state.heard = state.heard, []
    opened = False
    for name, menu in heard:
        if state.kept is not None:  # what filling that menu sent says nothing about it
            if name == MENU_CLOSED or ax.same(ax.get(menu, "AXParent"), state.kept):
                continue
            state.kept = None  # another menu really opened: it takes over
        if name == MENU_OPENED:
            opened = _keep(state, menu) or opened
        elif state.menu is None or ax.same(menu, state.menu):
            state.menu = state.root = None
            opened = False
    return opened


def _shown_by(state: _Menus, opener: Any) -> bool:
    """Ask the clicked element itself, for apps that send no menu notification."""
    shown, role, selected, children = ax.get_many(
        opener, ("AXShownMenuUIElement", "AXRole", "AXSelected", "AXChildren")
    )
    if shown is None and role in MENU_BUTTONS:
        menus = (c for c in children or () if ax.get(c, "AXRole") == "AXMenu")
        shown = next(menus, None)
    if shown is not None:
        return _keep(state, shown)
    if role == "AXMenuBarItem" and selected is True:
        state.menu, state.root = None, opener
        return True
    return False


def open_menu(target: AppTarget) -> Any | None:
    """The menu bar menu this app has open, as the element a state read prints alone.
    None when no menu is open, and for a menu that is printed inside its window."""
    return _open(target).root


def shown_menu(target: AppTarget) -> Any | None:
    """Any menu the app has open, wherever it is printed: what Escape closes."""
    state = _open(target)
    return state.menu if state.root is None else state.root


def _open(target: AppTarget) -> _Menus:
    state = _menus(target)
    _catch_up(state)
    if any(el is not None and not ax.alive(el) for el in (state.root, state.menu)):
        state.menu = state.root = None
    return state


def wait_for_menu(target: AppTarget, opener: Any) -> Any | None:
    """After a click that may open a menu: look for it, and keep what is found."""
    state = _menus(target)
    end = time.monotonic() + MENU_WAIT
    while not (_catch_up(state) or _shown_by(state, opener)):
        if time.monotonic() >= end:
            return None
        time.sleep(MENU_POLL)
    return state.menu if state.root is None else state.root


def keep_menu(target: AppTarget, title: Any) -> None:
    """Take a menu bar item's menu as the open one, though nothing opened on screen."""
    state = _menus(target)
    state.menu, state.root, state.kept = None, title, title


def close_menu(target: AppTarget) -> None:
    """Forget the open menu: an item was picked or the menu was cancelled."""
    state = _menus(target)
    with _LOCK:
        state.heard = []
    state.menu = state.root = state.kept = None


def watch(target: AppTarget) -> None:
    """Hear this app's menus open and close. Counted: two agents may drive one app."""
    state = _menus(target)
    with _LOCK:
        state.watchers += 1
        if state.watchers > 1:
            return

    def hear(name: str, element: Any) -> None:  # on the main run loop: only store
        if name in NEW_WINDOWS:
            return focus.no_attention(target.pid)
        with _LOCK:
            state.heard.append((name, element))

    try:
        names = (MENU_OPENED, MENU_CLOSED, *NEW_WINDOWS)
        state.observer = ax.observe(target.pid, _app(target), names, hear)
    except Exception:
        with _LOCK:
            state.watchers -= 1
        raise


def unwatch(target: AppTarget) -> None:
    """The observer and the kept menu go with the last watcher."""
    with _LOCK:
        state = _MENUS.get(target.pid)
        if state is None:
            return
        state.watchers -= 1
        if state.watchers > 0:
            return
        del _MENUS[target.pid]
    if state.observer is not None:
        state.observer.stop()
