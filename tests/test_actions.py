"""The ladder per tool, against a fake app: which rung is taken, which events go
out in which order, and what is never done. Nothing is posted.

At the end, opt-in: every tool once against a real app of our own that stays in the
background, with the real pointer and the front app watched."""

from __future__ import annotations

import hashlib
import inspect
import os
import time

import pytest
from fakes import El
from fakes_e import APP_PID, LiveApp, Watch, load, rig

from macos_harness import errors
from macos_harness.model import AppKind, Line, WindowRef

COMMAND, SHIFT = 0x100000, 0x20000


def line(el: El, **more) -> Line:
    return Line(1, el.role, el, el.role, **more)


def button(r, **more) -> El:
    return r.add(El("AXButton", rect=(200, 200, 80, 20), acts={"AXPress": ""}, **more))


def message(call, *args, **kwargs) -> str:
    with pytest.raises(errors.HarnessError) as caught:
        call(*args, **kwargs)
    return caught.value.message.removeprefix("macOS Harness error 100: ")


def mouse(r) -> list[tuple]:
    kinds = ("moved", "down", "up", "dragged")
    return [(e["type"], e["flags"]) for e in r.events.posted if e["type"] in kinds]


# --- click: accessibility action or events -------------------------------------------


def test_element_click_tries_ax_action_first_falls_back(monkeypatch):
    r = rig(monkeypatch)
    el = button(r)
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXPress"] and r.events.posted == []
    refusing = rig(monkeypatch, refuse=["AXPress"])
    el = button(refusing)
    refusing.actions.click(refusing.app, line(el))
    assert mouse(refusing) == [("down", COMMAND), ("up", COMMAND)]


@pytest.mark.parametrize(
    ("role", "pressed"),
    [
        ("AXButton", False),
        ("AXLink", False),
        ("AXCheckBox", True),
        ("AXRadioButton", True),
    ],
)
def test_in_a_web_page_only_toggles_are_clicked_by_their_action(
    monkeypatch, role, pressed
):
    """Measured on a web view: a button, link, text field, slider or summary gets real
    clicks (after the app is told it is active), a checkbox or radio button its action."""
    r = rig(monkeypatch)
    el = El(role, rect=(200, 200, 80, 20), acts={"AXPress": ""})
    r.add(El("AXWebArea", rect=(100, 120, 600, 380), children=[el]))
    r.actions.click(r.app, line(el))
    assert (el.performed == ["AXPress"]) is pressed
    assert ("down" in r.events.types()) is not pressed


def test_press_the_app_did_not_answer_is_not_tried_again_as_events(monkeypatch):
    """No answer in time: the press may have happened (a dialog that blocks the app).
    A second click on top of it could hit something else."""
    r = rig(monkeypatch, refuse={"AXPress": "AXError.cannotComplete"})
    text = message(r.actions.click, r.app, line(button(r)))
    assert text == "The accessibility call failed: AXError.cannotComplete"
    assert r.events.posted == []


def test_press_needs_no_window(monkeypatch):
    """A window the window list does not show (minimised, another Space) still
    takes an accessibility action."""
    r = rig(monkeypatch)
    el = button(r)
    r.windows.ref = lambda element: (_ for _ in ()).throw(errors.no_windows())
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXPress"]


def test_pick_is_preferred_over_press(monkeypatch):
    r = rig(monkeypatch)
    el = r.add(
        El("AXButton", rect=(200, 200, 80, 20), acts={"AXPress": "", "AXPick": ""})
    )
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXPick"]


@pytest.mark.parametrize(
    "how",
    [{"count": 2}, {"button": 1}, {"button": 2}, {"key": "shift"}, {"hold": 0.2}],
)
def test_anything_but_one_plain_left_click_goes_as_events(monkeypatch, how):
    r = rig(monkeypatch)
    el = button(r)
    r.actions.click(r.app, line(el), **how)
    assert el.performed == [] and "down" in r.events.types()


def test_gate_forces_synthesized_clicks(monkeypatch):
    r = rig(monkeypatch)
    el = button(r)
    r.app.agent.env = {"MACOS_HARNESS_ALWAYS_SIMULATE_CLICK": "1"}
    r.actions.click(r.app, line(el))
    assert el.performed == [] and mouse(r) == [("down", COMMAND), ("up", COMMAND)]
    r.app.agent.env = {"MACOS_HARNESS_ALWAYS_SIMULATE_CLICK": "0"}
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXPress"]


def test_text_field_gets_a_command_click_at_its_centre(monkeypatch):
    """No action to press: down and up with the Command flag, 50 ms apart, no
    mouse-moved event, and the app is not told it is active."""
    r = rig(monkeypatch)
    el = r.add(El("AXTextField", rect=(200, 200, 100, 20)))
    r.actions.click(r.app, line(el))
    assert mouse(r) == [("down", COMMAND), ("up", COMMAND)]
    assert {e["point"] for e in r.events.posted} == {(250.0, 210.0)}
    assert round(r.events.posted[1]["t"] - r.events.posted[0]["t"], 3) == 0.05
    assert not r.focus.enforcer(r.app.target).believes_focus


@pytest.mark.parametrize("nested", [False, True])
def test_element_flag_says_click_may_select_rather_press(monkeypatch, nested):
    """A row, a cell, or anything inside one: the app is made active first, then a
    plain click. A Command-click would add to the selection."""
    r = rig(monkeypatch)
    text = El("AXStaticText", rect=(200, 200, 100, 20))
    row = r.add(El("AXRow", rect=(200, 200, 300, 20), children=[El("AXCell")]))
    row.children[0].children.append(text)
    r.add(row)
    r.actions.click(r.app, line(text if nested else row))
    assert r.events.types() == ["notice", "down", "up", "down", "up"]
    assert {e["flags"] for e in r.events.posted[1:]} == {0}
    assert r.events.posted[3]["point"] == ((250.0, 210.0) if nested else (350.0, 210.0))


def test_app_that_is_really_active_gets_a_plain_click(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.actions.click(r.app, (300.0, 300.0))
    assert mouse(r) == [("moved", 0), ("down", 0), ("up", 0)]


def test_catalyst_menu_button_is_activated_first(monkeypatch):
    r = rig(monkeypatch, AppKind(catalyst=True))
    menu_button = r.add(El("AXMenuButton", rect=(200, 200, 80, 20)))
    other = r.add(El("AXImage", rect=(200, 300, 80, 20)))
    r.actions.click(r.app, line(other))
    assert "notice" not in r.events.types()
    r.actions.click(r.app, line(menu_button), button=1)
    assert "notice" in r.events.types()


def test_click_by_point_right_button(monkeypatch):
    r = rig(monkeypatch)
    r.actions.click(r.app, (300.0, 300.0), button=1)
    assert mouse(r) == [("moved", 0), ("down", COMMAND), ("up", COMMAND)]
    assert [e["button"] for e in r.events.posted[1:]] == [1, 1]


def test_click_count_below_one_fails(monkeypatch):
    r = rig(monkeypatch)
    assert (
        message(r.actions.click, r.app, (300.0, 300.0), count=0)
        == "Invalid click count"
    )


def test_hit_test_app_element_use(monkeypatch):
    """The hit test asks the app, never the whole screen."""
    r = rig(monkeypatch)
    asked = []
    real = r.ax.at
    monkeypatch.setattr(
        r.ax, "at", lambda app, x, y: asked.append(app) or real(app, x, y)
    )
    r.actions.click(r.app, (300.0, 300.0))
    assert asked == [r.root]


def test_point_outside_window_fails_bare_name_global_points(monkeypatch):
    r = rig(monkeypatch)
    text = message(r.actions.click, r.app, (2655.5, -1228.0))
    assert text == "No window of the app is at (2655.5, -1228.0)"
    assert r.events.posted == []


def test_minimised_window_takes_no_event(monkeypatch):
    r = rig(monkeypatch)
    r.window.attrs["AXMinimized"] = True
    assert (
        message(r.actions.click, r.app, (300.0, 300.0))
        == "This element is off screen and cannot be clicked"
    )
    assert message(r.actions.scroll, r.app, (300.0, 300.0), "down", 1) == (
        "This element is off screen and cannot be clicked"
    )
    el = button(r)
    r.actions.click(r.app, line(el))  # an accessibility action still works
    assert el.performed == ["AXPress"] and r.events.posted == []
    field = r.add(El("AXTextField", rect=(200, 300, 80, 20)))
    field.attrs["AXWindow"] = r.window
    r.windows.ref = lambda element: 1 / 0  # the window is never looked up
    assert (
        message(r.actions.click, r.app, line(field))
        == "This element is off screen and cannot be clicked"
    )
    assert message(r.actions.scroll, r.app, line(field), "down", 1) == (
        "This element is off screen and cannot be clicked"
    )


# --- hold and held key ---------------------------------------------------------------


def test_hold_duration_held_key_clicks(monkeypatch):
    r = rig(monkeypatch)
    r.events.held = 0x20000000
    r.actions.click(r.app, (300.0, 300.0), key="cmd+shift", hold=0.3)
    kinds = r.events.types()
    assert kinds == ["moved", "notice", "down", "up", "flags", "down", "up", "flags"]
    press, release = r.events.posted[5], r.events.posted[6]
    assert press["flags"] == release["flags"] == COMMAND | SHIFT
    assert round(release["t"] - press["t"], 3) == 0.3
    assert [e["flags"] for e in r.events.posted if e["type"] == "flags"] == [
        COMMAND | SHIFT,
        0x20000000,  # what the person held before
    ]


def test_hold_ends_early_when_the_person_stops_the_app(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.app.cancel = lambda: r.clock.now >= 1000.5
    start = r.clock.now
    with pytest.raises(errors.HarnessError, match="The user did something in"):
        r.actions.click(r.app, (300.0, 300.0), hold=10.0)
    assert r.clock.now - start < 1.0
    assert r.events.types()[-2:] == ["down", "up"]  # the button still comes up


def test_typing_stops_at_the_key_where_the_person_takes_over(monkeypatch):
    """A long text went on to its end after a stop, and mixed with the person's own
    typing. Now every key asks first."""
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.app.cancel = lambda: len(r.events.posted) >= 6  # after three keys
    with pytest.raises(errors.HarnessError, match="The user did something in"):
        r.actions.type_text(r.app, "abcdefgh")
    assert r.events.types() == ["key_down", "key_up"] * 3


def test_clicking_stops_between_clicks_when_the_person_takes_over(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.app.cancel = lambda: r.events.types().count("up") >= 2
    with pytest.raises(errors.HarnessError, match="The user did something in"):
        r.actions.click(r.app, (300.0, 300.0), count=5)
    assert r.events.types().count("down") == r.events.types().count("up") == 2


def test_every_press_releases_in_finally(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    monkeypatch.setattr(r.actions, "_pause", lambda seconds, cancel: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        r.actions.click(r.app, (300.0, 300.0))
    assert r.events.types() == ["moved", "down", "up"]
    r.events.posted.clear()
    with pytest.raises(ZeroDivisionError):
        r.actions.press_key(r.app, "shift+a", hold=1.0)
    assert r.events.types() == ["flags", "key_down", "key_up", "flags"]


# --- where the click lands -----------------------------------------------------------


def scrolled(r, el, to):
    """The app moves the element when asked to scroll it into view."""
    real = r.ax.perform

    def perform(target, action):
        real(target, action)
        if action == "AXScrollToVisible":
            target.rect = to

    r.ax.perform = perform


def test_click_point_computed_element_option_scroll_view_first(monkeypatch):
    r = rig(monkeypatch)
    el = r.add(
        El("AXTextField", rect=(200, 900, 100, 20), acts={"AXScrollToVisible": ""})
    )
    scrolled(r, el, (200, 400, 100, 20))
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXScrollToVisible"]
    assert r.events.posted[0]["point"] == (250.0, 410.0)


def test_scroll_area_is_asked_when_the_element_cannot_scroll_itself(monkeypatch):
    r = rig(monkeypatch)
    el = El("AXTextField", rect=(200, 900, 100, 20))
    area = r.add(El("AXScrollArea", rect=(100, 100, 600, 400), children=[el]))
    area.attrs["AXScrollToShowDescendant"] = lambda shown: setattr(
        shown, "rect", (200, 300, 100, 20)
    )
    r.add(area)
    r.actions.click(r.app, line(el))
    assert r.events.posted[0]["point"] == (250.0, 310.0)


def test_click_lands_on_the_visible_part(monkeypatch):
    """A text area taller than its window: the centre is off screen, a part shows."""
    r = rig(monkeypatch)
    el = r.add(El("AXTextArea", rect=(200, 300, 100, 1000)))
    r.actions.click(r.app, line(el))
    assert r.events.posted[0]["point"] == (250.0, 400.0)  # the part is y 300 to 500
    monkeypatch.setattr(r.actions, "CLICK_VISIBLE_PART", False)
    assert (
        message(r.actions.click, r.app, line(el))
        == "This element is off screen and cannot be clicked"
    )


def test_visible_part_is_cut_by_every_scroll_area_above(monkeypatch):
    r = rig(monkeypatch)
    el = El("AXTextArea", rect=(200, 150, 100, 300))
    r.add(El("AXScrollArea", rect=(100, 100, 600, 100), children=[el]))
    r.actions.click(r.app, line(el))
    assert r.events.posted[0]["point"] == (250.0, 175.0)  # y 150 to 200 shows


@pytest.mark.parametrize("rect", [(200, 900, 100, 20), (200, 200, 0, 0), None])
def test_element_with_no_visible_part_cannot_be_clicked(monkeypatch, rect):
    r = rig(monkeypatch)
    el = r.add(El("AXTextField", rect=rect))
    assert (
        message(r.actions.click, r.app, line(el))
        == "This element is off screen and cannot be clicked"
    )
    assert r.events.posted == []


def test_element_positioned_before_click_call_reports_whether_could(monkeypatch):
    r = rig(monkeypatch)
    seen = r.add(El("AXButton", rect=(200, 200, 80, 20)))
    hidden = r.add(El("AXButton", rect=(200, 900, 80, 20)))
    assert r.actions._position(seen, r.windows.window) is True
    assert r.actions._position(hidden, r.windows.window) is False


def test_positioning_element_separate_step_returns_bool(monkeypatch):
    """An element pressed through accessibility is brought into view first, so the
    drawn pointer can go to it. It is pressed either way."""
    r = rig(monkeypatch)
    el = r.add(El("AXButton", rect=(200, 900, 80, 20)))
    el.acts = {"AXPress": "", "AXScrollToVisible": ""}
    scrolled(r, el, (200, 400, 80, 20))
    r.actions.click(r.app, line(el))
    assert el.performed == ["AXScrollToVisible", "AXPress"]
    # the outline sends the drawn pointer to the element, then the press shows
    assert r.presence.calls == [("flash", (200, 400, 80, 20)), ("press", 1)]
    assert r.presence.windows == [r.windows.window]


def test_clicks_can_animate_visible_cursor_first(monkeypatch):
    r = rig(monkeypatch)
    r.actions.click(r.app, (300.0, 300.0), count=2)
    assert r.presence.calls == [("move", (300.0, 300.0)), ("press", 2)]
    r.presence.calls.clear()
    field = r.add(El("AXTextField", rect=(200, 200, 100, 20)))
    r.actions.click(r.app, line(field))  # by number: the target is outlined too
    assert r.presence.calls == [
        ("flash", (200, 200, 100, 20)),
        ("move", (250.0, 210.0)),
        ("press", 1),
    ]


def test_pointer_move_without_click(monkeypatch):
    """A scroll moves the drawn pointer and presses nothing."""
    r = rig(monkeypatch)
    r.actions.scroll(r.app, (300.0, 300.0), "down", 1)
    assert r.presence.calls == [("move", (300.0, 300.0))]
    r.presence.calls.clear()
    area = r.add(El("AXScrollArea", rect=(200, 200, 300, 150)))
    r.actions.scroll(r.app, line(area), "down", 1)  # by number: outlined as well
    assert r.presence.calls == [
        ("flash", (200, 200, 300, 150)),
        ("move", (350.0, 275.0)),
    ]


# --- menus ---------------------------------------------------------------------------


def menu_bar(r):
    item = El("AXMenuItem", acts={"AXPress": "", "AXPick": ""})
    menu = El("AXMenu", children=[item], acts={"AXCancel": ""})
    title = El("AXMenuBarItem", rect=(10, 0, 40, 22), acts={"AXPress": ""})
    title.children.append(menu)
    r.root.children.append(El("AXMenuBar", children=[title]))
    r.windows.opens[id(title)] = menu
    return title, menu, item


def test_a_menu_bar_title_is_read_closed_and_never_pressed(monkeypatch):
    """Nothing opens on the person's menu bar: no press, no event for the menu bar.
    The app is told it is active (its menu items follow its key window), the title's
    menu is kept as the open one, and its items are picked as usual."""
    r = rig(monkeypatch)
    title, _, item = menu_bar(r)
    r.actions.click(r.app, line(title))
    assert title.performed == []
    assert r.events.types() == ["notice", "down", "up"]  # the activation, no more
    assert {e["point"] for e in r.events.posted[1:]} == {(130.0, 116.0)}
    assert r.windows.calls == [("keep", title)] and r.windows.menu is title
    r.actions.click(r.app, line(item))
    assert item.performed == ["AXPick"] and r.windows.menu is None


def test_a_menu_the_app_fills_only_when_it_opens_is_opened_filled_and_closed(
    monkeypatch,
):
    """Read closed, Chess's Game menu has no items: the app fills it as it opens. It
    is opened for real, cancelled as soon as the items are
    there, and kept as the open menu read closed."""
    r = rig(monkeypatch)
    title, menu, item = menu_bar(r)
    menu.children.clear()
    real = r.ax.perform

    def perform(el, action):
        real(el, action)
        if el is title and action == "AXPress":
            menu.children.append(item)  # the app's menuNeedsUpdate

    monkeypatch.setattr(r.ax, "perform", perform)
    r.actions.click(r.app, line(title))
    assert title.performed == ["AXPress"] and menu.performed == ["AXCancel"]
    assert r.windows.calls == [("keep", title)] and r.windows.menu is title


def test_a_menu_still_empty_after_the_wait_is_refused_plainly(monkeypatch):
    r = rig(monkeypatch)
    title, menu, _ = menu_bar(r)
    menu.children.clear()
    shown = Line(1, "Game", title, "AXMenuBarItem", name="Game")
    text = message(r.actions.click, r.app, shown)
    assert text.startswith("The Game menu is filled only when it opens on screen")
    assert title.performed == ["AXPress"] and menu.performed == ["AXCancel"]
    assert r.windows.menu is None  # no menu view is left behind


def test_clicking_menu_bar_title_opens_menu_title_line(monkeypatch):
    """With the switch on: the app is told it is active, the title is pressed, we
    wait for the menu. No mouse event goes to the menu bar."""
    r = rig(monkeypatch)
    monkeypatch.setattr(r.actions, "PRESS_MENU_TITLES", True)
    title, menu, _ = menu_bar(r)
    r.actions.click(r.app, line(title))
    assert title.performed == ["AXPress"]
    assert r.events.types() == ["notice", "down", "up"]  # the activation, no more
    assert {e["point"] for e in r.events.posted[1:]} == {(130.0, 116.0)}
    assert r.windows.calls == [("wait", title)] and r.windows.menu is menu


def test_clicking_a_menu_item_picks_it_and_leaves_the_menu_view(monkeypatch):
    r = rig(monkeypatch)
    _, menu, item = menu_bar(r)
    r.windows.menu = menu
    r.actions.click(r.app, line(item))
    assert item.performed == ["AXPick"] and r.events.posted == []
    assert r.windows.calls == ["close"]
    item.acts = {"AXPress": ""}
    r.actions.click(r.app, line(item))
    assert item.performed == ["AXPick", "AXPress"]


def test_menu_clicks_have_their_own_failure_texts(monkeypatch):
    r = rig(monkeypatch, refuse=["AXPick", "AXPress"])
    title, menu, item = menu_bar(r)
    for how in ({"button": 1}, {"count": 2}, {"key": "shift"}, {"hold": 0.5}):
        text = message(r.actions.click, r.app, line(item), **how)
        assert text == "A menu takes plain clicks only"
    r.windows.menu = menu
    assert (
        message(r.actions.click, r.app, line(item))
        == "The menu item could not be clicked"
    )
    # a menu is never left open after an error
    assert menu.performed == ["AXCancel"] and r.windows.menu is None
    r.windows.opens.clear()  # the refused press opens no menu
    monkeypatch.setattr(r.actions, "PRESS_MENU_TITLES", True)
    assert (
        message(r.actions.click, r.app, line(title))
        == "The menu item could not be clicked"
    )


def test_menu_item_the_app_did_not_answer_for(monkeypatch):
    """Picked, and the app is busy with what the item opened: not a failed click,
    and the menu view ends."""
    r = rig(monkeypatch, refuse={"AXPick": "AXError.cannotComplete"})
    _, menu, item = menu_bar(r)
    r.windows.menu = menu
    text = message(r.actions.click, r.app, line(item))
    assert text == "The accessibility call failed: AXError.cannotComplete"
    assert menu.performed == [] and r.windows.menu is None


def test_menu_that_nobody_reported_is_closed(monkeypatch):
    """The press does not come back and no menu was seen: a menu is open all the
    same, and it holds the person's keyboard. It is cancelled."""
    import threading

    r = rig(monkeypatch)
    menu = El("AXMenu", acts={"AXCancel": ""})
    popup = r.add(El("AXPopUpButton", rect=(200, 200, 80, 20), acts={"AXPress": ""}))
    popup.children.append(menu)
    closed = threading.Event()
    real = r.ax.perform

    def perform(el, action):
        real(el, action)
        if action == "AXPress":
            closed.wait(5)  # the press returns when the menu closes
        if action == "AXCancel":
            closed.set()

    r.ax.perform = perform
    monkeypatch.setattr(r.actions, "PRESS_JOIN", 0.05)
    assert (
        message(r.actions.click, r.app, line(popup))
        == "The menu item could not be clicked"
    )
    assert menu.performed == ["AXCancel"] and r.events.posted == []


def test_pop_up_menu_that_nobody_reported_is_closed(monkeypatch):
    """The press came back at once, the button now shows a menu of its own, and
    nobody reported it."""
    r = rig(monkeypatch)
    menu = El("AXMenu", acts={"AXCancel": ""})
    popup = r.add(El("AXPopUpButton", rect=(200, 200, 80, 20), acts={"AXPress": ""}))
    popup.children.append(menu)
    assert (
        message(r.actions.click, r.app, line(popup))
        == "The menu item could not be clicked"
    )
    assert menu.performed == ["AXCancel"] and r.events.posted == []
    plain = button(r)
    plain.children.append(El("AXMenu"))
    r.actions.click(r.app, line(plain))  # a plain button is not waited on
    assert plain.performed == ["AXPress"]


def test_cancel_action_closes_the_menu(monkeypatch):
    r = rig(monkeypatch)
    _, menu, _ = menu_bar(r)
    r.windows.menu = menu
    r.actions.secondary(r.app, line(menu, actions={"Cancel": "AXCancel"}), "Cancel")
    assert menu.performed == ["AXCancel"] and r.windows.calls == ["close"]


def test_pick_action_is_the_menu_item_click(monkeypatch):
    r = rig(monkeypatch)
    _, _, item = menu_bar(r)
    r.actions.secondary(r.app, line(item, actions={"Pick": "AXPick"}), "Pick")
    assert item.performed == ["AXPick"] and r.windows.calls == ["close"]


def test_escape_leaves_the_menu_view(monkeypatch):
    r = rig(monkeypatch)
    _, menu, _ = menu_bar(r)
    r.windows.menu = menu
    r.actions.press_key(r.app, "Escape")
    assert menu.performed == ["AXCancel"] and r.events.posted == []
    assert r.windows.menu is None
    r.actions.press_key(r.app, "Escape")  # no menu open: the key itself
    assert "key_down" in r.events.types()


@pytest.mark.parametrize("spec", ["Escape Escape", "shift+Escape", "Tab"])
def test_only_the_single_plain_escape_cancels_the_menu(monkeypatch, spec):
    r = rig(monkeypatch)
    _, menu, _ = menu_bar(r)
    r.windows.menu = menu
    r.actions.press_key(r.app, spec)
    assert menu.performed == [] and "key_down" in r.events.types()


def test_escape_is_a_plain_key_when_the_fix_is_off(monkeypatch):
    r = rig(monkeypatch)
    _, menu, _ = menu_bar(r)
    r.windows.menu = menu
    monkeypatch.setattr(r.actions, "ESCAPE_CANCELS_MENU", False)
    r.actions.press_key(r.app, "Escape")
    assert menu.performed == [] and r.windows.menu is menu


def test_pop_up_button_press_waits_for_its_menu(monkeypatch):
    r = rig(monkeypatch)
    popup = r.add(El("AXPopUpButton", rect=(200, 200, 80, 20), acts={"AXPress": ""}))
    r.windows.opens[id(popup)] = menu = El("AXMenu")
    r.actions.click(r.app, line(popup))
    assert popup.performed == ["AXPress"] and r.windows.menu is menu
    assert r.events.posted == []


# --- scroll --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "extent", "pixels"),
    [
        (0.42, 150, 63),
        (0.65, 150, 98),  # 97.5 rounds away from zero
        (1, 150, 150),
        (8, 150, 1200),
        (1, 40, 100),  # a page is never shorter than 100 points
        (0.42, 40, 42),
        (0.005, 100, 1),  # 0.5 rounds up
    ],
)
def test_how_pages_convert_wheel_deltas(monkeypatch, pages, extent, pixels):
    r = rig(monkeypatch)
    for size, directions in (
        ((300, extent), {"up": (0, pixels), "down": (0, -pixels)}),
        ((extent, 300), {"left": (-pixels, 0), "right": (pixels, 0)}),
    ):
        el = r.add(El("AXScrollArea", rect=(200, 200, *size)))
        for direction, want in directions.items():
            r.events.posted.clear()
            r.actions.scroll(r.app, line(el), direction, pages)
            wheel = r.events.posted[-1]
            assert (wheel["dx"], wheel["dy"]) == want, direction


def test_scroll_synthesized_wheel_event_integer_deltas_point_ax(monkeypatch):
    """One mouse-moved event with no flags, 17 ms, one wheel event, 10 ms. No
    activation: the window id on the event is enough."""
    r = rig(monkeypatch)
    el = r.add(El("AXScrollArea", rect=(200, 200, 300, 150)))
    start = r.clock.now
    r.actions.scroll(r.app, line(el), "down", 1)
    moved, wheel = r.events.posted
    assert (moved["type"], moved["flags"], moved["delay"]) == ("moved", 0, 0.017)
    assert (wheel["type"], wheel["dy"], wheel["delay"]) == ("wheel", -150, 0.010)
    assert moved["point"] == wheel["point"] == (350.0, 275.0)
    assert {moved["window"], wheel["window"]} == {7} and wheel["pid"] == APP_PID
    assert round(r.clock.now - start, 3) == 0.027


def test_scroll_by_point_uses_the_window_as_the_page(monkeypatch):
    r = rig(monkeypatch)
    r.actions.scroll(r.app, (300.0, 300.0), "down", 0.5)
    assert r.events.posted[-1]["dy"] == -200  # the window is 400 points high
    r.actions.scroll(r.app, (300.0, 300.0), "right", 0.5)
    assert r.events.posted[-1]["dx"] == 300  # and 600 wide
    assert message(r.actions.scroll, r.app, (5.0, 5.0), "down", 1).startswith(
        "No window of the app is at"
    )


# --- drag ----------------------------------------------------------------------------


def test_drag_synthesized_click_drag_point(monkeypatch):
    r = rig(monkeypatch)
    r.actions.drag(r.app, (200.0, 200.0), (400.0, 300.0))  # 224 points: 12 steps
    seen = [(e["type"], e["point"], e["click"], e["flags"]) for e in r.events.posted]
    path = [(200.0 + 200.0 * i / 12, 200.0 + 100.0 * i / 12) for i in range(13)]
    assert seen == [
        ("moved", (200.0, 200.0), 1, 0),
        ("down", (200.0, 200.0), 1, COMMAND),
        *[("dragged", point, 0, COMMAND) for point in path],
        ("up", (400.0, 300.0), 1, COMMAND),
    ]
    times = [round(e["t"] - r.events.posted[0]["t"], 3) for e in r.events.posted]
    # the wait is before the press, then 20 ms after each event: AppKit's drag
    # session drops only with short, spaced steps (Finder, measured)
    assert times == [0.0, *(round(0.067 + 0.02 * i, 3) for i in range(15))]
    assert r.presence.calls[0] == ("move", (200.0, 200.0))
    assert r.presence.calls[-1] == ("move", (400.0, 300.0))


def test_a_long_drag_takes_at_most_sixty_steps(monkeypatch):
    r = rig(monkeypatch)
    r.actions.drag(r.app, (200.0, 200.0), (5200.0, 200.0))
    assert r.events.types().count("dragged") == 61


def test_drag_releases_the_button_when_it_breaks(monkeypatch):
    r = rig(monkeypatch)
    real = r.events.mouse

    def mouse_(kind, point, *rest):
        if kind == "dragged" and point == (300.0, 250.0):
            raise RuntimeError("broke")
        return real(kind, point, *rest)

    monkeypatch.setattr(r.events, "mouse", mouse_)
    with pytest.raises(RuntimeError):
        r.actions.drag(r.app, (200.0, 200.0), (400.0, 300.0))
    last, before = r.events.posted[-1], r.events.posted[-2]
    assert (last["type"], last["click"]) == ("up", 1)
    assert last["point"] == before["point"] != (300.0, 250.0)  # where it broke off


def test_no_path_points_duration_modifier_handle_mac():
    from fakes_e import load

    assert list(inspect.signature(load("actions").drag).parameters) == [
        "app",
        "start",
        "end",
    ]


# --- keys ----------------------------------------------------------------------------


def test_press_key_events_per_chord(monkeypatch):
    """Activation once, then per chord: flags changed when a modifier differs from
    what the person holds, key down, key up, flags back. No waits."""
    r = rig(monkeypatch)
    start = r.clock.now
    r.actions.press_key(r.app, "super+a Tab")
    keys_only = [
        e for e in r.events.posted if e["type"] not in ("notice", "down", "up")
    ]
    assert [(e["type"], e.get("code"), e["flags"]) for e in keys_only] == [
        ("flags", None, COMMAND),
        ("key_down", 0, COMMAND),
        ("key_up", 0, COMMAND),
        ("flags", None, 0),
        ("key_down", 48, 0),
        ("key_up", 48, 0),
    ]
    assert r.events.types()[:3] == ["notice", "down", "up"]
    assert {e["text"] for e in keys_only if "text" in e} == {None}
    assert r.clock.now == start
    r.events.posted.clear()
    r.actions.press_key(r.app, "Tab")
    assert r.events.types() == ["key_down", "key_up"]  # it believes it has focus now


def test_modifier_the_person_already_holds_is_not_sent_again(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.events.held = COMMAND | 0x100
    r.actions.press_key(r.app, "super+a")
    assert r.events.types() == ["key_down", "key_up"]


def test_key_hold_keeps_the_last_key_down(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.actions.press_key(r.app, "Down Right", hold=0.5)
    downs = {e["code"]: e["t"] for e in r.events.posted if e["type"] == "key_down"}
    ups = {e["code"]: e["t"] for e in r.events.posted if e["type"] == "key_up"}
    assert ups[125] == downs[125] and round(ups[124] - downs[124], 3) == 0.5
    r.app.cancel = lambda: r.events.types()[-1] == "key_down" and r.clock.now > 1000
    start, sent = r.clock.now, len(r.events.posted)
    r.app.cancel = lambda: (
        len(r.events.posted) > sent
    )  # the person acts during the hold
    r.actions.press_key(r.app, "Down", hold=20.0)
    assert r.clock.now - start < 1.0 and r.events.types()[-1] == "key_up"


def test_typing_appends_existing_content_there_no_clear_first(monkeypatch):
    """Only the keys of the text itself: nothing selects or deletes first. Each
    event is followed by 0.25 ms."""
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    layout = {(0, 0): "a", (11, 0): "b", (36, 0): "\r"}
    monkeypatch.setattr(
        r.actions.keys, "current_layout", lambda: lambda c, m: layout.get((c, m))
    )
    r.actions.type_text(r.app, "ab\né")
    seen = [(e["type"], e["code"], e["text"]) for e in r.events.posted]
    assert seen == [
        (kind, code, text)
        for code, text in ((0, "a"), (11, "b"), (36, "\r"), (0, "é"))
        for kind in ("key_down", "key_up")
    ]
    assert {e["delay"] for e in r.events.posted} == {0.00025}


def test_typing_into_the_phone_mirroring_app_is_slower(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    target = r.app.target
    r.app.target = type(target)(target.path, "com.apple.ScreenContinuity", "x", APP_PID)
    monkeypatch.setattr(r.actions.keys, "current_layout", lambda: lambda c, m: None)
    r.actions.type_text(r.app, "a")
    assert {e["delay"] for e in r.events.posted} == {0.005}


def save_panel(r, value="Untitled"):
    """A save sheet: drawn by the panel service (pid 777), read through the app."""
    attrs = {"AXValue": value, "AXIdentifier": "saveAsNameTextField"}
    name = El("AXTextField", attrs=attrs, rect=(300, 200, 200, 22))
    name.settable_names = {"AXValue"}
    sheet = r.add(El("AXSheet", attrs={"AXIdentifier": "save-panel"}, children=[name]))
    r.root.attrs["AXFocusedWindow"] = sheet
    r.root.attrs["AXFocusedUIElement"] = name
    r.apps.service = 777
    return name


def test_keys_for_a_file_panel_go_to_the_service_that_draws_it(monkeypatch):
    """Measured on TextEdit's save sheet: keys posted to the app are lost. After the
    app was told it is active, one click on the focused field gives the panel its
    keys back."""
    r = rig(monkeypatch)
    save_panel(r)
    r.actions.press_key(r.app, "super+shift+g")
    keys_to = {e["pid"] for e in r.events.posted if e["type"] in ("key_down", "key_up")}
    clicks = [e["point"] for e in r.events.posted if e["type"] in ("down", "up")]
    assert keys_to == {777}
    assert clicks[-2:] == [(400.0, 211.0), (400.0, 211.0)]  # the name field
    r.events.posted.clear()
    r.actions.press_key(r.app, "Return")  # it believes it has focus now: no click
    assert {e["pid"] for e in r.events.posted} == {777}
    assert "down" not in r.events.types()


def test_no_event_goes_to_a_process_the_policy_forbids(monkeypatch):
    """An approved app showed a system password sheet, drawn by a process on the
    security list: a key press was posted to that process. Now the recipient is
    checked whenever it is not the app itself."""
    r = rig(monkeypatch)
    save_panel(r)
    r.apps.bundles[777] = "com.apple.LocalAuthenticationRemoteService"
    r.policy.refused = {"com.apple.LocalAuthenticationRemoteService"}
    with pytest.raises(errors.HarnessError, match="system security process"):
        r.actions.press_key(r.app, "Return")
    assert [e for e in r.events.posted if e["pid"] == 777] == []
    r.policy.refused = set()
    r.actions.press_key(r.app, "Return")  # the file panel's own service may get keys
    assert {e["pid"] for e in r.events.posted if e["type"] == "key_down"} == {777}
    r.policy.asked.clear()
    r.root.attrs["AXFocusedWindow"] = r.root.attrs["AXFocusedUIElement"] = None
    r.apps.service = None
    r.actions.press_key(r.app, "a")
    assert r.policy.asked == []  # the app itself was approved before the call


@pytest.mark.parametrize("how", ["press", "set", "select", "secondary"])
def test_no_accessibility_action_goes_to_a_process_the_policy_forbids(monkeypatch, how):
    """The tree printed the button and the secure field of a system password sheet
    inside an approved app: a click by number pressed the button, set_value wrote the
    field, with no check (only events were checked)."""
    r = rig(monkeypatch)
    acts = {"AXPress": "press", "AXConfirm": "confirm"}
    field = r.add(El("AXTextField", attrs={"AXValue": "x"}, acts=acts))
    field.owner_pid = 777
    field.settable_names = {"AXValue", "AXSelectedTextRange"}
    r.apps.bundles[777] = "com.apple.SecurityAgent"
    r.policy.refused = {"com.apple.SecurityAgent"}
    call = {
        "press": lambda: r.actions.click(r.app, line(field)),
        "set": lambda: r.actions.set_value(r.app, line(field), "secret"),
        "select": lambda: r.actions.select_text(r.app, line(field), "x"),
        "secondary": lambda: r.actions.secondary(
            r.app, line(field, actions={"Confirm": "AXConfirm"}), "Confirm"
        ),
    }[how]
    with pytest.raises(errors.HarnessError, match="system security process"):
        call()
    assert (
        field.performed == []
        and field.attrs["AXValue"] == "x"
        and r.events.posted == []
    )


@pytest.mark.parametrize("how", ["type", "set"])
def test_a_path_in_the_save_as_field_is_refused(monkeypatch, how):
    r = rig(monkeypatch)
    name = save_panel(r)
    path = "/Users/me/Documents/report.numbers"
    with pytest.raises(errors.HarnessError) as caught:
        if how == "type":
            r.actions.type_text(r.app, path)
        else:
            r.actions.set_value(r.app, line(name), path)
    assert "takes a file name, not a path" in caught.value.message
    assert r.events.posted == [] and name.attrs["AXValue"] == "Untitled"
    r.actions.set_value(r.app, line(name), "report")  # a name alone is fine
    assert name.attrs["AXValue"] == "report"


def test_a_document_is_clicked_before_its_text_is_set(monkeypatch):
    """Measured: TextEdit, set_value on a new document then super+s, froze 3 of 3.
    Clicked first, 2 of 2 opened the save sheet."""
    r = rig(monkeypatch)
    area = text_area(r, settable_names={"AXValue"})
    area.attrs["AXFocused"] = True  # focused already: still clicked
    r.actions.set_value(r.app, line(area), "Q3 revenue")
    assert [t for t in r.events.types() if t in ("down", "up")] == ["down", "up"]
    assert area.attrs["AXValue"] == "Q3 revenue"


def test_typing_nothing_is_an_error(monkeypatch):
    r = rig(monkeypatch)
    assert message(r.actions.type_text, r.app, "") == "No text was given to type"


def test_paste_sends_command_v_and_watches_the_focused_field(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    field = El("AXTextField", attrs={"AXValue": "old"})
    r.root.attrs["AXFocusedUIElement"] = field
    calls = []

    class Board:
        def paste(self, text, fmt, send_paste_key, timeout=2.0, landed=None):
            send_paste_key()
            before = landed()
            field.attrs["AXValue"] = "old" + text
            calls.append((text, fmt, timeout, before, landed()))

    monkeypatch.setattr(r.actions, "pasteboard", Board())
    r.actions.paste(r.app, "new", "md")
    assert calls == [("new", "md", 2.0, False, True)]
    assert [(e["type"], e.get("code"), e["flags"]) for e in r.events.posted] == [
        ("flags", None, COMMAND),
        ("key_down", 9, COMMAND),
        ("key_up", 9, COMMAND),
        ("flags", None, 0),
    ]


# --- set_value -----------------------------------------------------------------------


def test_setting_value_element_marked_settable_fails_after_round(monkeypatch):
    r = rig(monkeypatch)
    combo = r.add(El("AXComboBox", rect=(200, 200, 80, 20)))
    text = message(r.actions.set_value, r.app, line(combo), "x")
    assert text == "The value of this element cannot be set"
    assert combo.performed == [] and r.events.posted == []


@pytest.mark.parametrize(
    ("attrs", "submits"),
    [
        ({"AXSubrole": "AXSearchField"}, False),  # an app's search field: measured
        ({"AXIdentifier": "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"}, True),
        ({"AXKeyShortcutsValue": "⌘L"}, True),
        ({"AXSubrole": "AXSecureTextField", "AXIdentifier": "name"}, False),
        ({}, False),
    ],
)
def test_set_value_can_auto_submit_search_fields_browser(monkeypatch, attrs, submits):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    field = El("AXTextField", attrs=dict(attrs), settable_names={"AXValue"})
    r.add(field)
    r.actions.set_value(r.app, line(field), "query")
    assert field.attrs["AXValue"] == "query"
    assert field.performed == ["set AXFocused", "set AXValue"]
    returns = [e["code"] for e in r.events.posted if e["type"] == "key_down"]
    assert returns == ([36] if submits else [])


@pytest.mark.parametrize(
    ("current", "given", "written"),
    [
        (10.0, "42", 42.0),
        (0, "1", 1),
        (0, "0.5", 0.5),
        ("old", "42", "42"),
        (1.0, "x", "x"),
    ],
)
def test_value_follows_the_kind_that_is_there(monkeypatch, current, given, written):
    r = rig(monkeypatch)
    slider = El("AXSlider", attrs={"AXValue": current}, settable_names={"AXValue"})
    r.actions.set_value(r.app, line(slider), given)
    assert slider.attrs["AXValue"] == written
    assert type(slider.attrs["AXValue"]) is type(written)
    assert slider.performed == ["set AXValue"]  # a slider is not focused first


def test_field_that_refuses_focus_gets_one_click(monkeypatch):
    r = rig(monkeypatch, refuse=["AXFocused"])
    field = r.add(
        El("AXTextArea", rect=(200, 200, 100, 20), settable_names={"AXValue"})
    )
    r.actions.set_value(r.app, line(field), "text")
    assert mouse(r) == [("down", COMMAND), ("up", COMMAND)]
    assert field.attrs["AXValue"] == "text"


# --- select_text ---------------------------------------------------------------------

TEXT = "one two three two"


def text_area(r, **more) -> El:
    attrs = {"AXValue": TEXT, "AXNumberOfCharacters": len(TEXT)}
    return r.add(El("AXTextArea", attrs=attrs, rect=(200, 200, 100, 20), **more))


@pytest.mark.parametrize(
    ("target", "prefix", "span"),
    [
        ("bold part", "", (6, 9)),  # printed **bold part**
        ("a link", "", (20, 6)),  # printed [a link](x.org)
        ("part** and", "bold ", (11, 8)),  # across the marker, as it is printed
    ],
)
def test_rich_text_is_searched_as_printed_and_selected_in_the_source(
    monkeypatch, target, prefix, span
):
    """The model copies text from the printed Markdown; the selection lands on the
    same characters of the plain text (offsets of richtext.markdown)."""
    from macos_harness import richtext

    r = rig(monkeypatch)
    runs = [("Plain ", {}), ("bold part", {"bold": True}), (" and ", {})]
    runs += [("a link", {"link": "x.org"}), (".", {})]
    plain = "".join(text for text, _ in runs)
    area = r.add(El("AXTextArea", attrs={"AXValue": plain}, rect=(200, 200, 100, 20)))
    area.settable_names = {"AXSelectedTextRange"}
    monkeypatch.setattr(r.actions, "richtext", richtext)
    monkeypatch.setattr(
        r.actions.tree, "printed_text", lambda el: richtext.markdown(runs)
    )
    r.actions.select_text(r.app, line(area), target, prefix=prefix)
    assert area.attrs["AXSelectedTextRange"] == span
    start, length = span
    assert plain[start : start + length] == target.replace("*", "")


def test_select_text_error_texts(monkeypatch):
    r = rig(monkeypatch)
    fixed = text_area(r)
    assert message(r.actions.select_text, r.app, line(fixed), "one") == (
        "This element does not let its text selection be set"
    )
    area = text_area(r, settable_names={"AXSelectedTextRange"})
    not_found = "The element does not contain that text"
    assert message(r.actions.select_text, r.app, line(area), "four") == not_found
    assert message(r.actions.select_text, r.app, line(area), "two") == not_found
    with pytest.raises(errors.UsageError) as caught:
        r.actions.select_text(r.app, line(area), "one", mode="word")
    assert (
        caught.value.message
        == "--selection_type cannot be word. Use text, cursor_before or cursor_after."
    )


@pytest.mark.parametrize(
    ("mode", "span"),
    [("text", (8, 5)), ("cursor_before", (8, 0)), ("cursor_after", (13, 0))],
)
def test_selection_set_through_selected_text_range_attribute_after(
    monkeypatch, mode, span
):
    r = rig(monkeypatch)
    area = text_area(r, settable_names={"AXSelectedTextRange"})
    area.attrs["AXBoundsForRange"] = lambda span: (300.0, 200.0, 40.0, 10.0)
    r.actions.select_text(r.app, line(area), "three", mode=mode)
    assert area.attrs["AXSelectedTextRange"] == span
    assert r.presence.calls == [("move", (320.0, 205.0))]
    assert r.presence.windows == [r.windows.window]  # hidden with its window


def test_the_element_with_the_selection_gets_the_keyboard_focus(monkeypatch):
    """Text typed after a select_text lands at the selection, not in the field that
    had the focus before."""
    r = rig(monkeypatch)
    area = text_area(r, settable_names={"AXSelectedTextRange", "AXFocused"})
    r.actions.select_text(r.app, line(area), "three", mode="cursor_after")
    assert area.attrs["AXFocused"] is True
    assert area.attrs["AXSelectedTextRange"] == (13, 0)


def scrolled_text(r, line_top: float):
    """A text 840 high in a scroll area 150 high. The found text is 14 high and sits
    `line_top` below the top of the text."""
    area = text_area(r, settable_names={"AXSelectedTextRange"})
    area.rect = (77.0, 281.0, 254.0, 840.0)
    area.attrs["AXBoundsForRange"] = lambda span: (77.0, 281.0 + line_top, 120.0, 14.0)
    bar = El("AXScrollBar", {"AXValue": 0.0}, settable_names={"AXValue"})
    holder = El("AXScrollArea", {"AXVerticalScrollBar": bar}, rect=(76, 280, 256, 150))
    r.window.children.remove(area)
    holder.children.append(area)
    r.add(holder)
    return area, bar


def test_text_out_of_view_is_scrolled_to_the_middle_of_its_scroll_area(monkeypatch):
    """The scroll bar gets the share of the content's way that the text is off the
    middle: 339 points of the 690 the content can move."""
    r = rig(monkeypatch)
    area, bar = scrolled_text(r, 406)
    r.actions.select_text(r.app, line(area), "three")
    assert bar.attrs["AXValue"] == 339 / 690
    assert area.attrs["AXSelectedTextRange"] == (8, 5)


@pytest.mark.parametrize(("line_top", "share"), [(84, 0.0), (826, 1.0)])
def test_text_in_view_does_not_scroll_and_the_bar_stays_in_range(
    monkeypatch, line_top, share
):
    r = rig(monkeypatch)
    area, bar = scrolled_text(r, line_top)
    r.actions.select_text(r.app, line(area), "three")
    assert bar.attrs["AXValue"] == share  # shown whole: untouched. Last line: the end


def test_prefix_and_suffix_pick_one_of_several(monkeypatch):
    r = rig(monkeypatch)
    area = text_area(r, settable_names={"AXSelectedTextRange"})
    r.actions.select_text(r.app, line(area), "two", prefix="three ")
    assert area.attrs["AXSelectedTextRange"] == (14, 3)
    r.actions.select_text(r.app, line(area), "two", suffix=" three")
    assert area.attrs["AXSelectedTextRange"] == (4, 3)


def test_web_text_is_selected_through_text_markers(monkeypatch):
    r = rig(monkeypatch)
    area = text_area(r, settable_names={"AXSelectedTextMarkerRange"})
    area.attrs["AXTextMarkerForIndex"] = lambda index: ("mark", index)
    area.attrs["AXTextMarkerRangeForUnorderedTextMarkers"] = lambda marks: tuple(marks)
    r.actions.select_text(r.app, line(area), "one")
    assert area.attrs["AXSelectedTextMarkerRange"] == (("mark", 0), ("mark", 3))


# --- secondary actions ---------------------------------------------------------------


def test_unlisted_action_name_rejected(monkeypatch):
    r = rig(monkeypatch)
    el = button(r)
    shown = Line(1, "button", el, "AXButton", actions={"Raise": "AXRaise"}, id=2)
    for name in ("Explode", "raise", "AXRaise"):
        text = message(r.actions.secondary, r.app, shown, name)
        assert text == f"{name} is not among the secondary actions of element 2"
    assert el.performed == []


def test_raising_window_opt_offered_model_secondary_action(monkeypatch):
    """The one way a window is raised: the model asks for it by name. No click, no
    key and no scroll ever performs it."""
    r = rig(monkeypatch)
    shown = line(r.window, actions={"Raise": "AXRaise"})
    r.actions.click(r.app, (300.0, 300.0))
    r.actions.press_key(r.app, "Tab")
    r.actions.scroll(r.app, (300.0, 300.0), "down", 1)
    assert r.window.performed == []
    r.actions.secondary(r.app, shown, "Raise")
    assert r.window.performed == ["AXRaise"]


def scroll_area(r, with_bar: bool) -> tuple[El, El]:
    page = El("AXButton", attrs={"AXSubrole": "AXIncrementPage"})
    back = El("AXButton", attrs={"AXSubrole": "AXDecrementPage"})
    bar = El("AXScrollBar", children=[back, page])
    area = r.add(El("AXScrollArea", rect=(200, 200, 300, 150)))
    if with_bar:
        area.attrs["AXVerticalScrollBar"] = bar
    return area, bar


def test_scroll_bar_path_exists_besides_wheel_events_action(monkeypatch):
    """Scroll Down presses the scroll bar's page button. Without one, the page-scroll
    action. Never a value write, never a wheel event."""
    r = rig(monkeypatch)
    names = {"Scroll Down": "AXScrollDownByPage", "Scroll Up": "AXScrollUpByPage"}
    area, bar = scroll_area(r, with_bar=True)
    r.actions.secondary(r.app, line(area, actions=names), "Scroll Down")
    r.actions.secondary(r.app, line(area, actions=names), "Scroll Up")
    assert [child.performed for child in bar.children] == [["AXPress"], ["AXPress"]]
    assert area.performed == [] and bar.performed == [] and r.events.posted == []
    bare, _ = scroll_area(r, with_bar=False)
    r.actions.secondary(r.app, line(bare, actions=names), "Scroll Down")
    assert bare.performed == ["AXScrollDownByPage"]


@pytest.mark.parametrize("attribute", ["AXDisclosing", "AXExpanded"])
def test_expand_collapse_map_toggle(monkeypatch, attribute):
    r = rig(monkeypatch)
    row = r.add(El("AXRow", rect=(200, 200, 80, 20), settable_names={attribute}))
    names = {"Expand": "AXExpand", "Collapse": "AXCollapse"}
    r.actions.secondary(r.app, line(row, actions=names), "Expand")
    assert row.attrs[attribute] is True
    r.actions.secondary(r.app, line(row, actions=names), "Collapse")
    assert row.attrs[attribute] is False
    assert row.performed == [f"set {attribute}"] * 2  # an attribute, not an action


def test_presentation_actions_have_three_kinds(monkeypatch):
    """Press, show menu, scroll to visible: each printed action runs as the
    accessibility action it stands for. A shown menu is waited for."""
    r = rig(monkeypatch)
    el = button(r)
    names = {
        "Show Menu": "AXShowMenu",
        "Increment": "AXIncrement",
        "Zoom": "AXZoomWindow",
    }
    r.windows.opens[id(el)] = menu = El("AXMenu")
    for name in names:
        r.actions.secondary(r.app, line(el, actions=names), name)
    assert el.performed == ["AXShowMenu", "AXIncrement", "AXZoomWindow"]
    assert r.windows.calls == [("wait", el)] and r.windows.menu is menu
    assert r.presence.calls[0] == ("flash", (200, 200, 80, 20))
    assert set(r.presence.windows) == {r.windows.window}


def test_editable_text_has_insert_index_replace_helpers_beside(monkeypatch):
    """Not built: no tool path uses an insert-at-index or a replace helper."""
    r = rig(monkeypatch)
    public = {name for name in vars(r.actions) if not name.startswith("_")}
    assert not {"insert_text", "replace_text"} & public
    tools = {"click", "drag", "scroll", "press_key", "type_text", "set_value"}
    assert tools | {"select_text", "secondary", "paste"} <= public


def test_no_action_waits_after_the_input_or_marks_the_session(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID
    r.actions.press_key(r.app, "Tab")
    assert r.app.needs_settle is False and r.clock.now == 1000.0


def test_window_of_another_process_gets_its_own_events(monkeypatch):
    r = rig(monkeypatch)
    r.windows.window = WindowRef(r.window, 9, 555, None, (100, 100, 600, 400))
    r.actions.scroll(r.app, (300.0, 300.0), "down", 1)
    assert {(e["pid"], e["window"]) for e in r.events.posted} == {(555, 9)}


# --- live: every tool once against our own test app, which stays in the background ---
#
# MACOS_HARNESS_LIVE=1 MACOS_HARNESS_TESTAPP=<the test app's main file>. Every action
# runs under a watch: the real pointer is sampled each millisecond and the front app
# is read before and after. A test fails when either changed.

MODS = 0x1E0000
INPUT = {1, 2, 3, 4, 5, 6, 10, 11, 12, 22}  # mouse, key and wheel event types


@pytest.fixture(scope="module")
def live_app():
    script = os.environ.get("MACOS_HARNESS_TESTAPP")
    if not script:
        pytest.skip("live test: set MACOS_HARNESS_TESTAPP to the test app's main file")
    app = LiveApp.start(script)
    yield app
    load("focus").release_all()
    app.quit()


@pytest.fixture
def live(live_app):
    """The test app: reset, inactive in its own eyes, and not the front app."""
    ax = load("ax")
    load("focus").release_all()
    live_app.command("reset")
    first = live_app.find(Role="AXWindow", Title="vmouse-test")
    ax.perform(first, "AXRaise")  # the app's main window again, whatever ran before
    assert load("focus").front_pid() != live_app.pid
    load("windows").watch(live_app.session.target)  # as the first state read does
    yield live_app
    load("windows").unwatch(live_app.session.target)
    for menu in live_app.open_menus():
        load("ax").perform(menu, "AXCancel")


def undisturbed(live, action) -> list[dict]:
    """Run one action while the app is in the background. Returns what it logged."""
    for _ in range(3):
        live.mark()
        with Watch() as watch:
            action()
        if watch.clean:
            return live.since()
        if not watch.person:
            pytest.fail(
                f"the real pointer moved {watch.moved} points, "
                f"the front app went from {watch.front} to {watch.front_after}"
            )
    pytest.skip("the person used the Mac during every try")


def sent(rows) -> list[dict]:
    """The events of ours the app received: their source is this process."""
    return [r for r in rows if r["kind"] == "event" and r["srcpid"] == os.getpid()]


def shape(event) -> tuple:
    """What two events are compared by: kind, modifiers, and what the kind carries."""
    kind = event["type"]
    if kind == 13:
        return (13, event["subtype"], event["flags"])
    if kind == 22:
        return (22, event["pd1"], event["pd2"])
    if kind in (10, 11):
        return (kind, event["flags"] & MODS, event["keycode"])
    if kind == 12:
        return (12, event["flags"] & MODS)
    return (kind, event["flags"] & MODS, event["click"], event["button"])


def did(rows, kind: str, name: str = "what") -> list:
    return [r[name] for r in rows if r["kind"] == kind]


@pytest.mark.live
def test_live_click_by_number_is_an_accessibility_action(live):
    a = load("actions")
    for name in ("btn1", "chk"):
        target = live.line(live.find(Identifier=name))
        rows = undisturbed(live, lambda target=target: a.click(live.session, target))
        assert did(rows, "action", "name") == [name]
        assert sent(rows) == []  # not one event
    assert live.state()["active"] is False  # the app never believed it was active


@pytest.mark.live
def test_live_click_by_point_double_click_right_click(live):
    a, events = load("actions"), load("events")
    rows = undisturbed(live, lambda: a.click(live.session, live.point("btn2")))
    assert did(rows, "action", "name") == ["btn2"]
    ours = sent(rows)
    assert [shape(e) for e in ours] == [
        (5, 0, 1, 0),
        (1, COMMAND, 1, 0),
        (2, COMMAND, 1, 0),
    ]
    # every event names the window, and is marked as ours for the person detector
    assert {e["win"] for e in ours} == {e["w91"] for e in ours} == {live.layout["w1"]}
    assert {e["user"] for e in ours} == {events.OWN_TAG}
    rows = undisturbed(live, lambda: a.click(live.session, live.point("pad"), count=2))
    clicks = [(r["what"], r["click"]) for r in rows if r["kind"] == "view"]
    assert clicks == [
        ("mouseDown", 1),
        ("mouseUp", 1),
        ("mouseDown", 2),
        ("mouseUp", 2),
    ]
    rows = undisturbed(live, lambda: a.click(live.session, live.point("pad"), button=1))
    assert "rightMouseDown" in did(rows, "view")
    assert live.state()["active"] is False


@pytest.mark.live
def test_live_text_field_click_is_a_command_click(live):
    a = load("actions")
    field = live.line(live.find(Identifier="field"))
    rows = undisturbed(live, lambda: a.click(live.session, field))
    assert [shape(e) for e in sent(rows)] == [(1, COMMAND, 1, 0), (2, COMMAND, 1, 0)]
    state = live.state()
    assert state["responder"].startswith("NSTextView") and state["active"] is False


@pytest.mark.live
def test_live_table_row_click_makes_the_app_believe_it_is_active(live):
    a, ax, focus = load("actions"), load("ax"), load("focus")
    rows_of_table = [el for el in live.walk() if ax.get(el, "AXRole") == "AXRow"]
    rows = undisturbed(live, lambda: a.click(live.session, live.line(rows_of_table[3])))
    assert [shape(e) for e in sent(rows)][:4] == [
        (13, 1, 0xC0000),  # told it is active
        (1, 0, 1, 0),  # one click at the window's activation point
        (2, 0, 1, 0),
        (1, 0, 1, 0),  # the click on the row, no modifier
    ]
    state = live.state()
    assert (state["table_row"], state["active"]) == (3, True)
    assert state["key"] == live.layout["w1"]
    undisturbed(live, lambda: focus.release(live.session.target))
    assert live.state()["active"] is False


@pytest.mark.live
def test_live_typing_and_key_chords(live):
    a, focus = load("actions"), load("focus")
    assert focus.focus_element(live.find(Identifier="field"))
    rows = undisturbed(live, lambda: a.type_text(live.session, "Hello é ✓"))
    assert live.state()["field"] == "Hello é ✓"
    assert [shape(e) for e in sent(rows)][:3] == [
        (13, 1, 0xC0000),
        (1, 0, 1, 0),
        (2, 0, 1, 0),
    ]
    rows = undisturbed(live, lambda: a.press_key(live.session, "super+a BackSpace"))
    assert live.state()["field"] == ""
    assert [shape(e) for e in sent(rows)] == [
        (12, COMMAND),
        (10, COMMAND, 0),
        (11, COMMAND, 0),
        (12, 0),
        (10, 0, 51),
        (11, 0, 51),
    ]


@pytest.mark.live
def test_live_scroll_moves_a_background_table(live):
    a = load("actions")
    area = live.line(live.find(Identifier="tablescroll"))  # 290 by 150 points
    top = live.state()["table_y"]
    rows = undisturbed(live, lambda: a.scroll(live.session, area, "down", 1))
    assert [shape(e) for e in sent(rows)] == [(5, 0, 1, 0), (22, -150, 0)]
    assert live.state()["table_y"] == top + 150
    undisturbed(live, lambda: a.scroll(live.session, area, "down", 0.42))
    assert live.state()["table_y"] == top + 150 + 63
    undisturbed(live, lambda: a.scroll(live.session, area, "up", 8))
    assert live.state()["table_y"] == top
    for direction, pixels in (("right", 290), ("left", -290)):
        rows = undisturbed(live, lambda d=direction: a.scroll(live.session, area, d, 1))
        assert shape(sent(rows)[-1]) == (22, 0, pixels)  # the second axis arrives


@pytest.mark.live
def test_live_drag_inside_a_view(live):
    a = load("actions")
    x, y = live.point("pad")
    rows = undisturbed(
        live, lambda: a.drag(live.session, (x - 40, y - 20), (x + 40, y + 20))
    )
    moves = did(rows, "view")
    assert moves[0] == "mouseDown" and moves[-1] == "mouseUp"
    assert moves.count("mouseDragged") == 6  # 89 points: 5 steps of at most 20
    ends = [r["local"] for r in rows if r["kind"] == "view"]
    assert (ends[0], ends[-1]) == ([55.0, 75.0], [135.0, 35.0])


@pytest.mark.live
def test_live_drag_and_drop_lands_on_the_drop_target():
    """An AppKit drag session drops only on a display: the usual test app sits above
    every display, so this one opens on the main display, in the background."""
    script = os.environ.get("MACOS_HARNESS_TESTAPP")
    if not script:
        pytest.skip("live test: set MACOS_HARNESS_TESTAPP to the test app's main file")
    app = LiveApp.start(script, origin=(40, 200))
    try:
        a = load("actions")
        app.command("reset")
        rows = undisturbed(
            app,
            lambda: a.drag(app.session, app.point("dragsrc"), app.point("droptarget")),
        )
        assert "dropped" in did(rows, "drag")
    finally:
        load("focus").release_all()
        app.quit()


@pytest.mark.live
def test_live_set_value(live):
    a = load("actions")
    field = live.line(live.find(Identifier="field"))
    slider = live.line(live.find(Identifier="slider"))
    rows = undisturbed(live, lambda: a.set_value(live.session, field, "set é"))
    rows += undisturbed(live, lambda: a.set_value(live.session, slider, "42"))
    state = live.state()
    assert (state["field"], state["slider"]) == ("set é", 42.0)
    assert sent(rows) == []
    button_line = live.line(live.find(Identifier="btn1"))
    text = message(a.set_value, live.session, button_line, "x")
    assert text == "The value of this element cannot be set"


@pytest.mark.live
def test_live_select_text(live):
    a, ax = load("actions"), load("ax")
    area = next(el for el in live.walk() if ax.get(el, "AXRole") == "AXTextArea")
    undisturbed(
        live, lambda: a.set_value(live.session, live.line(area), "one two three")
    )
    rows = undisturbed(
        live, lambda: a.select_text(live.session, live.line(area), "two")
    )
    assert ax.get(area, "AXSelectedText") == "two" and sent(rows) == []
    undisturbed(
        live,
        lambda: a.select_text(
            live.session, live.line(area), "two", mode="cursor_after"
        ),
    )
    assert ax.get(area, "AXSelectedText") == ""


@pytest.mark.live
def test_live_secondary_actions(live):
    a, ax = load("actions"), load("ax")
    slider = live.line(
        live.find(Identifier="slider"), actions={"Increment": "AXIncrement"}
    )
    area = live.line(
        live.find(Identifier="tablescroll"),
        actions={"Scroll Down": "AXScrollDownByPage"},
    )
    top = live.state()["table_y"]
    rows = undisturbed(live, lambda: a.secondary(live.session, slider, "Increment"))
    rows += undisturbed(live, lambda: a.secondary(live.session, area, "Scroll Down"))
    state = live.state()
    assert state["slider"] > 0 and state["table_y"] > top and sent(rows) == []
    second = next(
        w
        for w in ax.get(ax.app(live.pid), "AXWindows")
        if ax.get(w, "AXTitle") == "vmouse-test-2"
    )
    shown = live.line(second, actions={"Raise": "AXRaise"})
    undisturbed(live, lambda: a.secondary(live.session, shown, "Raise"))
    assert (
        ax.get(ax.get(ax.app(live.pid), "AXMainWindow"), "AXTitle") == "vmouse-test-2"
    )


@pytest.mark.live
def test_live_paste_and_the_clipboard_comes_back(live):
    a, focus, pasteboard = load("actions"), load("focus"), load("pasteboard")

    def digest():
        held = pasteboard._snapshot(pasteboard._board())
        rows = sorted(
            (k, hashlib.sha256(v).hexdigest()) for i in held for k, v in i.items()
        )
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    before = digest()
    assert focus.focus_element(live.find(Identifier="field"))
    undisturbed(live, lambda: a.paste(live.session, "pasted é text", "text"))
    assert live.state()["field"] == "pasted é text"
    assert digest() == before  # the person's clipboard, byte for byte


@pytest.mark.live
def test_live_menu_pick(live):
    a, ax, windows = load("actions"), load("ax"), load("windows")
    bar = ax.get(ax.app(live.pid), "AXMenuBar")
    title = next(
        t for t in ax.get(bar, "AXChildren") if ax.get(t, "AXTitle") == "Probe"
    )

    def item(name):
        menu = ax.get(title, "AXChildren")[0]
        found = next(
            i for i in ax.get(menu, "AXChildren") if ax.get(i, "AXTitle") == name
        )
        return live.line(found)

    rows = undisturbed(live, lambda: a.click(live.session, item("Pong")))
    assert did(rows, "action", "name") == ["menu:Pong"]  # the menu never showed
    rows = undisturbed(live, lambda: a.click(live.session, live.line(title)))
    assert windows.open_menu(live.session.target) is not None
    assert not [e for e in sent(rows) if e["type"] in INPUT and e["win"] == 0]
    undisturbed(live, lambda: a.click(live.session, item("Ping")))
    deadline = time.time() + 3  # the app acts when its menu has faded out
    while "menu:Ping" not in did(live.since(), "action", "name"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert windows.open_menu(live.session.target) is None


@pytest.mark.live
def test_live_pop_up_menu_is_never_left_open(live):
    a, ax, windows = load("actions"), load("ax"), load("windows")
    popup = live.line(live.find(Identifier="popup"))
    reported = []

    def press():
        try:
            a.click(live.session, popup)
            reported.append(True)
        except errors.HarnessError as error:
            assert error.message.endswith("The menu item could not be clicked")

    undisturbed(live, press)
    if reported:  # the menu was seen: pick its second item
        assert windows.open_menu(live.session.target) is None  # the window is read
        menu = windows.shown_menu(live.session.target)
        items = [el for el in live.walk(menu) if ax.get(el, "AXRole") == "AXMenuItem"]
        undisturbed(live, lambda: a.click(live.session, live.line(items[1])))
        assert live.state()["popup"] == "Two"
    assert live.open_menus() == []


@pytest.mark.live
def test_live_minimised_window_takes_no_event(live):
    a = load("actions")
    live.command("minimize2")
    time.sleep(1.5)
    try:
        field = live.line(live.find(Identifier="w2field"))
        assert (
            message(a.click, live.session, field)
            == "This element is off screen and cannot be clicked"
        )
        pressed = live.line(live.find(Identifier="w2btn"))
        rows = undisturbed(live, lambda: a.click(live.session, pressed))
        assert did(rows, "action", "name") == ["w2btn"]
        assert live.state()["w2_mini"] is True  # and we did not put it back
    finally:
        live.command("deminimize2")
        time.sleep(1.5)
