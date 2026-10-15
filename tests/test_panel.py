"""panel.py: the questions to the person, one panel at a time. Nothing takes focus."""

from __future__ import annotations

import subprocess
import sys
import time

import AppKit
import pytest
import Quartz

from macos_harness import panel
from macos_harness.panel import PANEL_MARGIN, Ask, _panel_class, ask_panel, panel_origin

CHOICES = ["Deny", "Allow for this session", "Always allow"]


@pytest.fixture(autouse=True)
def asks(monkeypatch):
    monkeypatch.setattr(panel, "ASKS", [])


def test_a_question_is_answered_once() -> None:
    answers = []
    first = Ask("Allow Chess?", "It can move pieces.", CHOICES, answers.append)
    second = Ask("Allow Stickies?", None, CHOICES[:2], answers.append)
    panel.ASKS.extend([first, second])

    panel.answer(second, "Deny")
    panel.answer(second, "Deny")
    panel.answer(first, "Always allow")

    assert answers == ["Deny", "Always allow"]
    assert panel.ASKS == []


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


def test_the_first_question_gets_the_panel_and_a_click_answers_it() -> None:
    answers = []
    shown = _panel_class().alloc().init()
    shown.show()
    assert shown.panel is None  # no question, no panel
    ask = Ask("Allow Chess?", None, CHOICES, answers.append)
    panel.ASKS.append(ask)
    shown.asked = ask  # as if its panel were on screen; none is ordered in here
    button = AppKit.NSButton.alloc().init()
    button.setTag_(1)
    shown.pick_(button)  # the person clicks the second button
    assert answers == ["Allow for this session"]
    assert (shown.asked, shown.panel, panel.ASKS) == (None, None, [])


# One question for 2 s, in a process of its own that takes no focus and goes by
# itself. A click on the second button is made by the button itself.
ON_SCREEN = """
import AppKit
from macos_harness import panel

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
ask = panel.Ask(
    'Allow macOS Harness to use "Chess"?',
    "A test of the prompt. It goes by itself in 2 seconds.",
    ["Deny", "Allow for this session", "Always allow"],
    lambda choice: print("answer", choice, flush=True),
)
panel.ASKS.append(ask)
panel.draw()
shown = panel._SHOWN
print("panel", shown.panel is not None, flush=True)


def click(timer):
    row = shown.panel.contentView().views()[-1]
    row.views()[1].performClick_(None)
    print("panel", shown.panel, flush=True)


timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_
timer(1.3, False, click)
timer(2.0, False, lambda timer: app.terminate_(None))
app.run()
"""


@pytest.mark.live  # a question panel, for 2 s
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
        on_screen = before[AppKit.NSFloatingWindowLevel]
        shot = tmp_path / "panel.png"
        own = ["-l", str(on_screen[Quartz.kCGWindowNumber])]
        subprocess.run(["/usr/sbin/screencapture", "-x", "-o", *own, shot], check=True)
        front_with_panel = frontmost()
    finally:
        out, _ = shown.communicate(timeout=5)

    assert on_screen[Quartz.kCGWindowName] == "macOS Harness"
    assert 250 < on_screen[Quartz.kCGWindowBounds]["Width"] < 480
    assert front_with_panel == front != shown.pid  # nothing took focus
    assert out.splitlines() == [
        "panel True",  # on screen while the question waits
        "answer Allow for this session",  # once
        "panel None",  # an answered question goes
    ]
