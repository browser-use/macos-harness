"""windows.py against a fake accessibility tree and a fake window server."""

from __future__ import annotations

import sys
import types

import pytest
from fakes import El, FakeAx
from fakes_f import FakeServer, ScriptedClock

from macos_harness import errors, windows
from macos_harness.model import AppTarget

TARGET = AppTarget("/Applications/Demo.app", "dev.demo", "Demo", 100)
OTHER = AppTarget("/Applications/Other.app", "dev.other", "Other", 300)


class Rig:
    def __init__(self, monkeypatch):
        self.app, self.other = El("AXApplication"), El("AXApplication", owner_pid=300)
        self.ax = FakeAx({100: self.app, 300: self.other})
        self.server, self.clock = FakeServer(), ScriptedClock()
        monkeypatch.setattr(windows, "ax", self.ax)
        monkeypatch.setattr(windows, "server_windows", self.server.server_windows)
        monkeypatch.setattr(windows, "server_window", self.server.server_window)
        monkeypatch.setattr(windows, "time", self.clock)
        monkeypatch.setattr(windows, "_MENUS", {})

    def window(self, title, frame, window_id, pid=100, listed=True, onscreen=True):
        attrs = {"AXTitle": title, "AXSubrole": "AXStandardWindow"}
        window = El("AXWindow", attrs, rect=frame, win_id=window_id, owner_pid=pid)
        if listed:
            self.app.attrs.setdefault("AXWindows", []).append(window)
            self.app.children.append(window)
        if window_id:
            self.server.add(window_id, pid, frame, title, onscreen=onscreen)
        return window

    def child(self, parent, role="AXButton", rect=None, pid=100, window_id=None):
        window_id = parent.win_id if window_id is None else window_id
        child = El(
            role, {"AXParent": parent}, rect=rect, owner_pid=pid, win_id=window_id
        )
        parent.children.append(child)
        return child

    def menu(self, opener_role="AXMenuBarItem", title="File"):
        opener = El(opener_role, {"AXTitle": title})
        menu = El("AXMenu", {"AXParent": opener})
        opener.children.append(menu)
        return opener, menu

    def hear(self, offset, name, element):
        self.clock.at(offset, lambda: self.ax.observers[-1].fire(name, element))


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


# --- the key window -------------------------------------------------------------------


def test_state_read_app_s_key_window(rig):
    """The focused window, and the app's own window order."""
    first = rig.window("First", (0, 0, 400, 300), 11)
    second = rig.window("Second", (50, 50, 400, 300), 12)
    rig.app.attrs["AXFocusedWindow"] = second
    key = windows.key_window(TARGET)
    assert (key.element, key.window_id, key.pid, key.title) == (
        second,
        12,
        100,
        "Second",
    )
    assert key.frame == (50.0, 50.0, 400.0, 300.0)
    assert [w.element for w in windows.ordered(TARGET)] == [first, second]


def test_a_window_is_named_as_the_window_server_names_it(rig):
    """The app may add to its title what the window server leaves out."""
    window = rig.window("All Fonts", (0, 0, 400, 300), 11)
    window.attrs["AXTitle"] = "All Fonts - 385 typefaces"
    assert windows.key_window(TARGET).title == "All Fonts"
    rig.server.rows[0]["title"] = None  # a sheet: the server has no name for it
    assert windows.key_window(TARGET).title is None
    rig.server.rows.clear()  # the server does not know the window: the app's title
    assert windows.key_window(TARGET).title == "All Fonts - 385 typefaces"


def test_key_window_tracking_kvo_primary_window_notion_separate(rig):
    """Focused, else main, else the last listed. Read again on every call."""
    first = rig.window("First", (0, 0, 400, 300), 11)
    last = rig.window("Last", (50, 50, 400, 300), 12)
    assert windows.key_window(TARGET).element is last
    rig.app.attrs["AXMainWindow"] = first
    assert windows.key_window(TARGET).element is first
    rig.app.attrs["AXFocusedWindow"] = last
    assert windows.key_window(TARGET).element is last
    assert rig.ax.observers == []  # no tracker object behind it


def test_no_capturable_window_explicit_error(rig):
    """An app with no window at all says so by name."""
    with pytest.raises(errors.HarnessError) as raised:
        windows.key_window(TARGET)
    text = "macOS Harness error 100: The app has no window"
    assert raised.value.message == text == errors.no_windows().message
    rig.app.attrs["AXWindows"] = []
    with pytest.raises(errors.HarnessError, match="The app has no window"):
        windows.key_window(TARGET)


def test_platform_fact_axwindows_lists_only_windows_current_space(rig):
    """A window on another Space is not listed but is still the main window."""
    away = rig.window("Away", (0, 0, 400, 300), 11, listed=False)
    rig.app.attrs.update(AXWindows=[], AXMainWindow=away)
    assert windows.key_window(TARGET).element is away
    assert windows.ordered(TARGET) == []


def test_sheets_panels_same_app_appear_inside_state_their(rig):
    """Panels and dialogs are windows of their own; a sheet can be the key one."""
    rig.window("Document", (0, 0, 800, 600), 11)
    panel = rig.window("Open", (100, 100, 500, 400), 12)
    panel.attrs["AXSubrole"] = "AXDialog"
    listed = windows.ordered(TARGET)
    assert [(w.title, w.window_id) for w in listed] == [("Document", 11), ("Open", 12)]
    sheet = El("AXSheet", {"AXTitle": None}, rect=(200, 60, 400, 200), win_id=13)
    rig.app.attrs["AXFocusedWindow"] = sheet
    key = windows.key_window(TARGET)
    assert (key.element, key.title, key.window_id) == (sheet, None, 13)


def test_a_target_that_is_not_running_has_no_window(rig):
    gone = AppTarget(TARGET.path, TARGET.bundle_id, TARGET.name, None)
    with pytest.raises(errors.HarnessError, match="No running app matches"):
        windows.key_window(gone)


def test_window_bounds_tracked_live_window_server(rig):
    """No live observer. The frame is read again at every call."""
    window = rig.window("Moves", (0, 0, 400, 300), 11)
    assert windows.key_window(TARGET).frame == (0.0, 0.0, 400.0, 300.0)
    window.rect = (640, -200, 400, 300)
    assert windows.key_window(TARGET).frame == (640.0, -200.0, 400.0, 300.0)
    assert rig.ax.observers == []


def test_window_state_checks(rig):
    """A minimised window is still read; actions refuse it (test_actions)."""
    window = rig.window("Doc", (0, 0, 400, 300), 11)
    window.attrs.update(AXMinimized=True)
    assert windows.key_window(TARGET).element is window


# --- window ids and the window server -------------------------------------------------


def test_private_calls_map_ax_elements_window_ids_pids(rig):
    """The id and the owning pid come from the element, not from the app."""
    rig.window("Remote", (0, 0, 400, 300), 4242, pid=555)
    key = windows.key_window(TARGET)
    assert (key.window_id, key.pid) == (4242, 555)


def test_map_ax_windows_cg_windows(rig):
    """The id call first, else the bounds, among windows on screen only."""
    rig.window("Doc", (10, 20, 400, 300), 0)
    rig.server.add(70, 100, (10, 20, 400, 300), "Doc", onscreen=False)
    assert windows.key_window(TARGET).window_id == 0
    rig.server.add(71, 300, (10, 20, 400, 300), "Doc")  # another app's window
    assert windows.key_window(TARGET).window_id == 0
    rig.server.add(72, 100, (10, 20, 400, 300), "Doc")
    assert windows.key_window(TARGET).window_id == 72


def test_window_capture_picked_best_match_between_ax_cg(rig):
    """Several windows with the same bounds: the title decides."""
    rig.window("Second", (10, 20, 400, 300), 0)
    rig.server.add(80, 100, (10, 20, 400, 300), "First")
    rig.server.add(81, 100, (10, 20, 400, 300), "Second")
    rig.server.add(82, 100, (11, 20, 400, 300), "Second")  # other bounds
    assert windows.key_window(TARGET).window_id == 81
    rig.server.rows[1]["title"] = None  # titles need a permission we may not have
    assert windows.key_window(TARGET).window_id == 80


def test_window_filters_used_listing_app_s_windows(monkeypatch):
    """The server list is on screen only, without the desktop, front first."""
    asked = []

    def copy(option, window_id):
        asked.append((option, window_id))
        box = {"X": -5, "Y": 30, "Width": 300, "Height": 200}
        rows = [(31, 100, 0, "Top"), (32, 300, 0, "Else"), (33, 100, 25, None)]
        return [
            {
                "kCGWindowNumber": number,
                "kCGWindowOwnerPID": pid,
                "kCGWindowLayer": layer,
                "kCGWindowName": name,
                "kCGWindowBounds": box,
            }
            for number, pid, layer, name in rows
        ]

    quartz = types.SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=16,
        kCGWindowListOptionIncludingWindow=8,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=copy,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    mine = windows.server_windows(100)
    assert asked == [(1 | 16, 0)]
    assert [(row["id"], row["layer"], row["title"]) for row in mine] == [
        (31, 0, "Top"),
        (33, 25, None),
    ]
    assert mine[0]["frame"] == (-5.0, 30.0, 300.0, 200.0)
    assert len(windows.server_windows()) == 3
    assert windows.server_window(32)["pid"] == 300 and asked[-1] == (8, 32)
    assert windows.server_window(99) is None
    assert asked[-2:] == [(8, 99), (0, 0)]  # asked by id, then among all windows
    assert windows.WINDOW_LIST_ALL == 0


def test_a_minimised_window_is_found_among_all_windows(monkeypatch):
    """Measured: asking the server for one window by id leaves out a minimised one."""
    row = {"kCGWindowNumber": 41, "kCGWindowOwnerPID": 100, "kCGWindowBounds": {}}
    quartz = types.SimpleNamespace(
        kCGWindowListOptionIncludingWindow=8,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda option, window_id: [] if option else [row],
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    assert windows.server_window(41)["pid"] == 100


def test_an_element_of_a_minimised_window_still_names_it(rig):
    window = rig.window("Doc", (40, 60, 400, 300), 11, onscreen=False)
    window.attrs["AXMinimized"] = True
    button = rig.child(window, rect=(50, 70, 80, 20))
    button.attrs["AXWindow"] = window
    found = windows.ref(button)
    assert (found.window_id, found.pid, found.element) == (11, 100, window)


def test_a_window_named_by_an_element_has_the_app_s_own_frame(rig):
    """Measured: the server's bounds can differ from the app's frame by a few points."""
    window = rig.window("Doc", (60, 58, 560, 592), 11)
    rig.server.rows[0]["frame"] = (63.0, 61.0, 554.0, 586.0)
    assert windows.ref(window).frame == windows.key_window(TARGET).frame
    assert windows.ref(window).frame == (60.0, 58.0, 560.0, 592.0)


# --- a point, an element --------------------------------------------------------------


def test_all_coordinates_signed_floats_cg(rig):
    """A window on a display left of and above the main one. Nothing clamped."""
    rig.window("Main", (0, 0, 400, 300), 11)
    left = rig.window("Left", (-1800.5, -900.25, 800, 600), 12)
    hit = windows.at(TARGET, (-1500.0, -700.0))
    assert hit.element is left and hit.frame == (-1800.5, -900.25, 800.0, 600.0)
    assert (
        windows.at(TARGET, (-1800.5, -900.25)).element is left
    )  # the corner is inside
    with pytest.raises(errors.HarnessError):
        windows.at(TARGET, (-1000.5, -700.0))  # the far edge is outside


def test_the_first_listed_window_that_holds_the_point_wins(rig):
    front = rig.window("Front", (0, 0, 400, 300), 11)
    rig.window("Behind", (100, 100, 400, 300), 12)
    assert windows.at(TARGET, (150, 150)).element is front
    assert windows.at(TARGET, (450, 350)).title == "Behind"


def test_a_point_in_no_window(rig):
    rig.window("Main", (0, 0, 400, 300), 11)
    with pytest.raises(errors.HarnessError) as raised:
        windows.at(TARGET, (900, 50))
    assert raised.value.message == errors.window_not_found_at((900, 50)).message
    assert "No window of the app is at (900.0, 50.0)" in raised.value.message
    rig.app.attrs["AXWindows"] = []
    with pytest.raises(errors.HarnessError, match="The app has no window"):
        windows.at(TARGET, (10, 10))


def test_an_element_names_its_own_window(rig):
    window = rig.window("Doc", (40, 60, 400, 300), 11)
    button = rig.child(window, rect=(50, 70, 80, 20))
    button.attrs["AXWindow"] = window
    found = windows.ref(button)
    assert (found.element, found.window_id, found.pid) == (window, 11, 100)
    assert (found.title, found.frame) == ("Doc", (40.0, 60.0, 400.0, 300.0))
    assert windows.ref(window).element is window


def test_an_element_that_names_its_window_needs_no_climb(rig):
    """Many toolkits give no window id for a child. Its window attribute does."""
    window = rig.window("Doc", (40, 60, 400, 300), 11)
    group = rig.child(window, "AXGroup", window_id=0)
    text = rig.child(group, "AXStaticText", window_id=0)
    text.attrs["AXWindow"] = window
    del text.attrs["AXParent"]  # so a climb would find nothing
    assert windows.ref(text).window_id == 11
    orphan = rig.child(group, "AXStaticText", window_id=0)
    assert windows.ref(orphan).element is window  # no attribute: the parents lead there


def test_an_element_of_another_process_goes_to_the_window_that_owns_it(rig):
    """A web page drawn by a helper process: events go to the host window."""
    window = rig.window("Browser", (0, 0, 800, 600), 11)
    area = rig.child(window, "AXScrollArea", rect=(0, 50, 800, 550))
    page = rig.child(area, "AXWebArea", rect=(0, 50, 800, 550), pid=200)
    link = rig.child(page, "AXLink", rect=(20, 80, 100, 20), pid=200)
    found = windows.ref(link)
    assert (found.pid, found.window_id) == (100, 11)
    assert windows.at(TARGET, (30, 90)).pid == 100  # the point is over the link


def test_an_element_of_a_process_with_its_own_window_goes_there(rig):
    """A file panel drawn by a service: the service owns the window, so events go to it."""
    window = rig.window("Doc", (0, 0, 800, 600), 11)
    rig.server.add(77, 200, (100, 100, 500, 400), "Open")
    remote = rig.child(
        window, "AXGroup", rect=(100, 100, 500, 400), pid=200, window_id=77
    )
    button = rig.child(remote, rect=(120, 120, 80, 20), pid=200)
    found = windows.at(TARGET, (130, 130))
    assert windows.ref(button).window_id == found.window_id == 77
    assert (found.pid, found.frame) == (200, (100.0, 100.0, 500.0, 400.0))


@pytest.mark.parametrize(("parents", "found"), [(300, True), (301, False)])
def test_an_element_of_another_process_climbs_at_most_300_parents(rig, parents, found):
    node = rig.window("Host", (0, 0, 800, 600), 11)
    for _ in range(parents):
        node = rig.child(node, "AXGroup", pid=200)
    assert windows.MAX_PARENTS == 300
    if found:
        assert windows.ref(node).pid == 100
    else:
        with pytest.raises(errors.HarnessError, match="has no parent in the app"):
            windows.ref(node)


def test_nothing_here_moves_or_changes_a_window(rig):
    """The two tests below rest on this: every call only reads."""
    window = rig.window("Doc", (0, 0, 400, 300), 11)
    button = rig.child(window, rect=(10, 10, 80, 20))
    windows.key_window(TARGET), windows.ordered(TARGET), windows.at(TARGET, (20, 20))
    windows.ref(button)
    assert [el.performed for el in rig.app.walk()] == [[], [], []]


def test_space_stage_manager_minimized_window_handling_present_name(rig):
    """No Space step. A window is reached through accessibility or not at all."""
    assert not [name for name in vars(windows) if "space" in name.lower()]


def test_stage_manager_support_bring_off_stage_window_stage(rig):
    """No window is brought onto the stage. Not on any tool path, not built."""
    assert not [name for name in vars(windows) if "stage" in name.lower()]


# --- the open menu --------------------------------------------------------------------


def test_open_menus_tracked_observer_shown_menu_read_attribute(rig):
    """Found by its notification within 0.25 s, kept, and printed from the bar item."""
    item, menu = rig.menu()
    windows.watch(TARGET)
    assert rig.ax.observers[0].names[:2] == ["AXMenuOpened", "AXMenuClosed"]
    assert windows.open_menu(TARGET) is None
    rig.hear(0.034, "AXMenuOpened", menu)
    assert windows.wait_for_menu(TARGET, item) is item
    assert rig.clock.elapsed == pytest.approx(0.04) and set(rig.clock.sleeps) == {0.01}
    assert windows.open_menu(TARGET) is item
    assert windows.open_menu(TARGET) is item  # kept until it is closed


def test_a_new_window_or_sheet_tells_the_system_the_app_wants_no_attention(
    rig, monkeypatch
):
    """On each window or sheet the target creates: a background
    app's new window must not bring it forward (seen: Pages came to the front)."""
    told = []
    monkeypatch.setattr(windows.focus, "no_attention", told.append)
    _, menu = rig.menu()
    windows.watch(TARGET)
    assert rig.ax.observers[0].names[2:] == ["AXWindowCreated", "AXSheetCreated"]
    rig.ax.observers[0].fire("AXWindowCreated", menu)
    rig.ax.observers[0].fire("AXSheetCreated", menu)
    assert told == [TARGET.pid, TARGET.pid]
    assert windows.open_menu(TARGET) is None  # not taken for a menu event


def test_the_windows_that_belong_to_the_key_window_are_its_extras(monkeypatch):
    """A window of the app in front of the key window whose associated windows
    include it (an open menu, a popover) goes into the picture, back to front.
    Another window of the app does not, nor one behind (a sheet's parent)."""
    rows = [  # front first, as the window server lists them
        {"id": 9, "frame": (1, 1, 2, 2)},  # the open menu
        {"id": 12, "frame": (3, 3, 4, 4)},  # another window of the app
        {"id": 8, "frame": (5, 5, 6, 6)},  # a popover of the key window
        {"id": 7, "frame": (0, 0, 9, 9)},  # the key window
        {"id": 5, "frame": (0, 0, 9, 9)},  # its parent, behind
    ]
    tied = {9: {7}, 12: {30}, 8: {7}, 5: {7}}
    monkeypatch.setattr(windows, "server_windows", lambda pid: rows)
    monkeypatch.setattr(windows, "_associated", lambda wid: tied.get(wid, set()))
    assert windows.extra_windows(TARGET, 7) == [rows[2], rows[0]]
    assert windows.extra_windows(TARGET, 99) == []  # not on screen: none


def test_a_pop_up_menu_is_printed_inside_its_window(rig):
    """Only a menu of the menu bar is read alone. A pop-up's menu is open, Escape
    closes it, and a state read still prints the window, with the menu under its
    button."""
    button, menu = rig.menu("AXPopUpButton")
    button.attrs["AXShownMenuUIElement"] = menu
    assert windows.wait_for_menu(TARGET, button) is menu
    assert rig.clock.sleeps == []  # found at the first look, with no notification
    assert windows.open_menu(TARGET) is None
    assert windows.shown_menu(TARGET) is menu
    windows.watch(TARGET)
    rig.ax.observers[0].fire("AXMenuClosed", menu)
    assert windows.shown_menu(TARGET) is None


@pytest.mark.parametrize("role", ["AXPopUpButton", "AXMenuButton"])
def test_a_pop_up_button_shows_its_open_menu_as_a_child(rig, role):
    """Measured on a real pop-up button: no shown-menu attribute, the menu is a child."""
    button = El(role)
    assert windows.wait_for_menu(TARGET, button) is None  # closed: no child
    menu = El("AXMenu", {"AXParent": button})
    start = rig.clock.elapsed
    rig.clock.at(start + 0.015, lambda: button.children.append(menu))
    assert windows.wait_for_menu(TARGET, button) is menu
    assert rig.clock.elapsed - start == pytest.approx(0.02)
    assert windows.shown_menu(TARGET) is menu and windows.open_menu(TARGET) is None
    menu.dead = True
    assert windows.shown_menu(TARGET) is None


def test_the_menu_a_menu_bar_item_always_holds_is_not_an_open_menu(rig):
    item, _menu = rig.menu()  # the menu is a child of the item, open or not
    assert windows.wait_for_menu(TARGET, item) is None
    other = El("AXGroup", children=[El("AXMenu")])  # not a control that opens a menu
    assert windows.wait_for_menu(TARGET, other) is None


def test_a_menu_bar_item_that_says_it_is_selected_is_the_open_menu(rig):
    item, _ = rig.menu()
    rig.clock.at(0.095, lambda: item.attrs.update(AXSelected=True))
    assert windows.wait_for_menu(TARGET, item) is item
    assert rig.clock.elapsed == pytest.approx(0.1)


def test_no_menu_within_a_quarter_second(rig):
    button = El("AXButton")
    windows.watch(TARGET)
    assert windows.wait_for_menu(TARGET, button) is None
    assert windows.MENU_WAIT == 0.25 and windows.MENU_POLL == 0.01
    assert 0.25 <= rig.clock.elapsed <= 0.26 + 1e-9
    assert windows.open_menu(TARGET) is None


def test_the_menu_is_kept_until_close_menu(rig):
    item, menu = rig.menu()
    windows.watch(TARGET)
    rig.hear(0.02, "AXMenuOpened", menu)
    windows.wait_for_menu(TARGET, item)
    windows.close_menu(TARGET)
    assert windows.open_menu(TARGET) is None


def test_the_menu_is_kept_until_the_menu_closed_notification(rig):
    item, menu = rig.menu()
    windows.watch(TARGET)
    rig.hear(0.02, "AXMenuOpened", menu)
    windows.wait_for_menu(TARGET, item)
    rig.ax.observers[0].fire("AXMenuClosed", El("AXMenu"))  # some other menu
    assert windows.open_menu(TARGET) is item
    rig.ax.observers[0].fire("AXMenuClosed", menu)
    assert windows.open_menu(TARGET) is None


def test_a_menu_taken_without_opening_survives_what_its_filling_sent(rig):
    """Chess's Game menu, 12 Oct: it was opened for a moment to be filled and cancelled.
    Those two notices arrived after the menu was taken, and the next read showed the
    window again."""
    item, menu = rig.menu()
    windows.watch(TARGET)
    windows.keep_menu(TARGET, item)
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    rig.ax.observers[0].fire("AXMenuClosed", menu)
    assert windows.open_menu(TARGET) is item
    other_item, other = rig.menu(title="Edit")
    rig.ax.observers[0].fire("AXMenuOpened", other)  # the person opens another menu
    assert windows.open_menu(TARGET) is other_item
    windows.keep_menu(TARGET, item)
    windows.close_menu(TARGET)
    assert windows.open_menu(TARGET) is None


def test_a_menu_the_person_opened_shows_at_the_next_read(rig):
    item, menu = rig.menu()
    windows.watch(TARGET)
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    assert windows.open_menu(TARGET) is item
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    rig.ax.observers[0].fire("AXMenuClosed", menu)
    assert windows.open_menu(TARGET) is None


def test_one_menu_per_app_and_a_submenu_belongs_to_it(rig):
    file_item, file_menu = rig.menu(title="File")
    edit_item, edit_menu = rig.menu(title="Edit")
    submenu = El("AXMenu", {"AXParent": El("AXMenuItem", {"AXParent": file_menu})})
    windows.watch(TARGET)
    rig.hear(0.01, "AXMenuOpened", file_menu)
    assert windows.wait_for_menu(TARGET, file_item) is file_item
    rig.ax.observers[0].fire("AXMenuOpened", submenu)
    assert windows.open_menu(TARGET) is file_item
    rig.ax.observers[0].fire("AXMenuClosed", submenu)
    assert windows.open_menu(TARGET) is file_item
    rig.hear(0.05, "AXMenuOpened", edit_menu)  # a menu that never closed is replaced
    assert windows.wait_for_menu(TARGET, edit_item) is edit_item
    assert windows.open_menu(TARGET) is edit_item


def test_each_app_has_its_own_menu(rig):
    item, menu = rig.menu()
    windows.watch(TARGET), windows.watch(OTHER)
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    assert windows.open_menu(TARGET) is item and windows.open_menu(OTHER) is None


def test_a_menu_whose_element_died_is_forgotten(rig):
    item, menu = rig.menu()
    windows.watch(TARGET)
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    assert windows.open_menu(TARGET) is item
    item.dead = True
    assert windows.open_menu(TARGET) is None


def test_close_menu_forgets_a_notification_that_came_late(rig):
    item, menu = rig.menu()
    item.attrs["AXSelected"] = True
    windows.watch(TARGET)
    assert windows.wait_for_menu(TARGET, item) is item  # seen before its notification
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    windows.close_menu(TARGET)
    assert windows.open_menu(TARGET) is None


def test_watch_twice_and_unwatch_once_keeps_the_observer(rig):
    item, menu = rig.menu()
    windows.watch(TARGET), windows.watch(TARGET)
    assert len(rig.ax.observers) == 1
    windows.unwatch(TARGET)
    assert not rig.ax.observers[0].stopped
    rig.ax.observers[0].fire("AXMenuOpened", menu)
    assert windows.open_menu(TARGET) is item
    windows.unwatch(TARGET)
    assert rig.ax.observers[0].stopped and windows.open_menu(TARGET) is None
    windows.unwatch(TARGET)  # one too many is harmless
    windows.watch(TARGET)
    assert len(rig.ax.observers) == 2


def test_a_watch_that_fails_does_not_count(rig, monkeypatch):
    def refuse(pid, element, names, callback):
        raise errors.accessibility("AXError.cannotComplete")

    working = rig.ax.observe
    monkeypatch.setattr(rig.ax, "observe", refuse)
    with pytest.raises(errors.HarnessError):
        windows.watch(TARGET)
    monkeypatch.setattr(rig.ax, "observe", working)
    windows.watch(TARGET)
    assert len(rig.ax.observers) == 1
