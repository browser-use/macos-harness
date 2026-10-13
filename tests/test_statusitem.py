from __future__ import annotations

import subprocess
import sys
import time

import AppKit
import pytest
import Quartz

from macos_harness.model import AppTarget
from macos_harness.statusitem import (
    PANEL_MARGIN,
    SESSION,
    Ask,
    Menu,
    _bar_class,
    ask_panel,
    panel_origin,
)

CHESS = AppTarget("/System/Applications/Chess.app", "com.apple.Chess", "Chess", 42)
STICKIES = AppTarget("/a/Stickies.app", "com.apple.Stickies", "Stickies", 43)
CHOICES = ["Deny", "Allow for this session", "Always allow"]


def titles(menu: Menu) -> list[str]:
    return [title for title, _pick in menu.rows()]


def test_menu_bar_item_drawn_host_app_state_request() -> None:
    # No host draws the item for us: the helper keeps what it shows as plain data,
    # the apps in use with a stop row for each.
    menu = Menu()
    assert menu.rows() == [("No Active Sessions", None)]

    menu.apps = [CHESS]
    assert titles(menu) == ["No Active Sessions", "", "Stop Using Chess", "Resume"]

    menu.apps, menu.label = [CHESS, STICKIES], "Typing in Stickies"
    assert titles(menu) == [
        "Typing in Stickies",
        "",
        "Stop Using Chess",
        "Stop Using Stickies",
        "Stop Using All Apps",
        "Resume",
    ]


def test_a_pick_tells_who_to_stop_and_a_line_of_text_does_nothing() -> None:
    menu, stopped, resumed = Menu(), [], []
    menu.on_stop, menu.on_resume = stopped.append, resumed.append
    menu.apps, menu.label = [CHESS, STICKIES], "Typing in Stickies"

    rows = dict(menu.rows())
    assert rows["Typing in Stickies"] is None
    rows["Stop Using Stickies"]()
    rows["Stop Using All Apps"]()
    rows["Resume"]()

    assert stopped == [STICKIES, None]
    assert resumed == [None]
    Menu().on_stop(CHESS)  # with no listener set, a pick does nothing
    Menu().on_resume(None)


def test_distributed_notification_tells_host_menu_state_changes() -> None:
    # No host listens for a notice either: the item itself comes and goes. It is
    # there while apps are in use or a question waits, and for SESSION seconds
    # after the last call.
    menu = Menu()
    assert not menu.visible(1000.0)  # a helper nobody has called shows nothing

    menu.touch(1000.0)
    assert menu.visible(1000.0 + SESSION - 0.1)
    assert not menu.visible(1000.0 + SESSION)

    menu.apps = [CHESS]
    assert menu.visible(1000.0 + 10 * SESSION)
    menu.end_turn()
    assert not menu.visible(1000.0)

    menu.asks.append(Ask("Allow Chess?", None, CHOICES, print))
    assert menu.visible(1000.0 + 10 * SESSION)


def test_while_a_stop_holds_the_item_stays_with_its_resume_row() -> None:
    """A stop outlives the agent's turn, and the item went away with the turn: the
    person had no visible way to resume until the next refused call."""
    menu, resumed = Menu(), []
    menu.on_resume = resumed.append
    menu.apps, menu.stopped = [CHESS], True
    menu.end_turn()
    assert menu.visible(1000.0 + 10 * SESSION)
    assert [title for title, _pick in menu.rows()] == ["Stopped by You", "Resume"]
    dict(menu.rows())["Resume"]()
    assert resumed == [None]
    menu.stopped = False
    assert not menu.visible(1000.0) and menu.rows() == [("No Active Sessions", None)]


def test_a_question_shows_its_choices_and_is_answered_once() -> None:
    menu, answers = Menu(), []
    first = Ask("Allow Chess?", "It can move pieces.", CHOICES, answers.append)
    second = Ask("Allow Stickies?", None, CHOICES[:2], answers.append)
    menu.asks += [first, second]

    assert titles(menu) == [
        "No Active Sessions",
        "",
        "Allow Chess?",
        *CHOICES,
        "",
        "Allow Stickies?",
        *CHOICES[:2],
    ]
    picks = [pick for title, pick in menu.rows() if title == "Deny"]
    picks[1]()  # the second question, in the menu
    picks[1]()
    menu.answer(first, "Always allow")  # the first, in its panel
    menu.answer(first, "Deny")

    assert answers == ["Deny", "Always allow"]
    assert menu.asks == []


def test_the_end_of_a_turn_keeps_a_question_and_the_listeners() -> None:
    menu, stopped = Menu(), []
    menu.on_stop = stopped.append
    menu.apps, menu.label = [CHESS], "Clicked in Chess"
    menu.touch(5.0)
    menu.asks.append(Ask("Allow Chess?", None, CHOICES, print))

    menu.end_turn()

    assert titles(menu) == ["No Active Sessions", "", "Allow Chess?", *CHOICES]
    assert menu.on_stop == stopped.append


def test_the_question_panel_floats_takes_no_focus_and_starts_off_screen() -> None:
    target = AppKit.NSObject.alloc().init()
    ask = Ask("Allow Chess?", "It can move pieces.", CHOICES, print)

    made = ask_panel(ask, target)

    assert not made.isVisible()
    assert made.level() == AppKit.NSFloatingWindowLevel
    assert made.becomesKeyOnlyIfNeeded()
    assert not made.hidesOnDeactivate()  # the helper is never the active app
    assert made.styleMask() & AppKit.NSWindowStyleMaskNonactivatingPanel
    *labels, row = made.contentView().views()
    assert [label.stringValue() for label in labels] == [ask.title, ask.subtitle]
    buttons = row.views()
    assert [button.title() for button in buttons] == CHOICES
    assert [button.tag() for button in buttons] == [0, 1, 2]
    assert {(button.target(), button.action()) for button in buttons} == {
        (target, "pick:")
    }
    assert not any(button.acceptsFirstResponder() for button in buttons)
    width, height = made.contentView().fittingSize()
    assert 200 < width < 480
    assert 60 < height < 200


def test_a_question_with_no_text_has_only_its_title() -> None:
    made = ask_panel(Ask("Allow Chess?", None, CHOICES[:1], print), None)

    *labels, row = made.contentView().views()
    assert [label.stringValue() for label in labels] == ["Allow Chess?"]
    assert [button.title() for button in row.views()] == ["Deny"]


def test_the_question_panel_sits_in_the_top_right_corner_of_the_screen() -> None:
    # The area below the menu bar of a 1440 x 900 screen, y up.
    assert panel_origin((340, 120), (0, 0, 1440, 875)) == (
        1440 - 340 - PANEL_MARGIN,
        875 - 120 - PANEL_MARGIN,
    )
    # A second screen to the left, at negative coordinates.
    assert panel_origin((340, 120), (-1920, 180, 1920, 1055))[0] == -340 - PANEL_MARGIN


def test_the_item_fills_its_menu_when_opened_and_hands_a_pick_over() -> None:
    # The item is made hidden and stays hidden: no app is in use and no call came.
    menu, stopped, answers = Menu(), [], []
    menu.on_stop = stopped.append
    bar = _bar_class().alloc().init()
    try:
        bar.show_(menu)
        assert not bar.item.isVisible()
        assert bar.panel is None

        menu.apps = [CHESS]
        shown = bar.item.menu()
        bar.menuNeedsUpdate_(shown)  # the person opens the menu
        rows = [shown.itemAtIndex_(i) for i in range(shown.numberOfItems())]
        assert [row.title() for row in rows] == [
            "No Active Sessions",
            "",
            "Stop Using Chess",
            "Resume",
        ]
        assert rows[1].isSeparatorItem()
        assert rows[0].action() is None  # a line of text
        menu.apps = []  # as show_ after a pick would bring the item on screen
        bar.pick_(rows[2])
        assert stopped == [CHESS]

        bar.asked = Ask("Allow Chess?", None, CHOICES, answers.append)
        menu.asks.append(bar.asked)  # as if its panel were on screen
        button = AppKit.NSButton.alloc().init()
        button.setTag_(1)
        bar.pick_(button)  # the person clicks the second button
        assert answers == ["Allow for this session"]
        assert (bar.asked, bar.panel, menu.asks) == (None, None, [])
    finally:
        if bar.timer is not None:
            bar.timer.invalidate()
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(bar.item)


# The item and one question for 2 s, in a process of its own that takes no focus
# and goes by itself. A click on the second button is made by the button itself.
ON_SCREEN = """
import AppKit
from macos_harness import statusitem

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
menu = statusitem.Menu()
ask = statusitem.Ask(
    'Allow macOS Harness to use "Chess"?',
    "A test of the prompt. It goes by itself in 2 seconds.",
    ["Deny", "Allow for this session", "Always allow"],
    lambda choice: print("answer", choice, flush=True),
)
menu.asks.append(ask)
statusitem.draw(menu)
bar = statusitem._BAR
print("item", bar.item.isVisible(), flush=True)


def click(timer):
    row = bar.panel.contentView().views()[-1]
    row.views()[1].performClick_(None)
    print("item", bar.item.isVisible(), "panel", bar.panel, flush=True)


timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_
timer(1.3, False, click)
timer(2.0, False, lambda timer: app.terminate_(None))
app.run()
"""


@pytest.mark.live  # the menu bar item and a question panel, for 2 s
def test_live_question_shows_takes_no_focus_and_hands_the_click_over(tmp_path) -> None:
    def windows() -> dict[int, dict]:
        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        mine = [row for row in rows if row[Quartz.kCGWindowOwnerPID] == shown.pid]
        return {int(row[Quartz.kCGWindowLayer]): row for row in mine}

    def frontmost() -> int:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(front.processIdentifier())

    front = frontmost()
    shown = subprocess.Popen(
        [sys.executable, "-c", ON_SCREEN], stdout=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 1.0
        while AppKit.NSFloatingWindowLevel not in windows():
            assert time.monotonic() < deadline, "no panel came"
            time.sleep(0.05)
        before = windows()
        panel = before[AppKit.NSFloatingWindowLevel]
        shot = tmp_path / "panel.png"
        own = ["-l", str(panel[Quartz.kCGWindowNumber])]
        subprocess.run(["/usr/sbin/screencapture", "-x", "-o", *own, shot], check=True)
        front_with_panel = frontmost()
    finally:
        out, _ = shown.communicate(timeout=5)

    assert panel[Quartz.kCGWindowName] == "macOS Harness"
    assert 250 < panel[Quartz.kCGWindowBounds]["Width"] < 480
    assert front_with_panel == front != shown.pid  # nothing took focus
    assert out.splitlines() == [
        "item True",  # in the menu bar while the question waits
        "answer Allow for this session",  # once
        "item False panel None",  # an answered question goes, and the item with it
    ]
