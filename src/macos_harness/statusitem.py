"""The menu bar item: the apps the agent uses, a way to stop it, and the
questions that wait for the person. A question also shows as a small panel.

Menu is plain data and runs anywhere. `draw` puts it on the screen and runs on
the helper's main thread only. Nothing here takes focus.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NamedTuple

from .model import AppTarget

SESSION = 120.0  # seconds the item stays after the last call
NO_APPS, STOPPED = "No Active Sessions", "Stopped by You"
STOP, STOP_ALL, RESUME = "Stop Using {}", "Stop Using All Apps", "Resume"
SYMBOL = "cursorarrow"  # the icon in the menu bar
NAME = "macOS Harness"
PANEL_WIDTH, PANEL_MARGIN = 340.0, 16.0  # the question panel, and its gap to the edge

Row = tuple[str, Callable[[], None] | None]  # title, what a pick does
DIVIDER: Row = ("", None)


class Ask(NamedTuple):
    """One question to the person."""

    title: str
    subtitle: str | None
    choices: list[str]
    on_answer: Callable[[str], None]


class Menu:
    """What the item shows. Every method runs on the main thread."""

    def __init__(self) -> None:
        self.label = ""  # what the agent does now: "Clicking in Notes"
        self.apps: list[AppTarget] = []
        self.stopped = False  # a stop of the person holds: only they lift it
        self.asks: list[Ask] = []
        self.touched: float | None = None  # the last call; None: no call yet
        self.on_stop: Callable[[AppTarget | None], None] = lambda target: None
        self.on_resume: Callable[[AppTarget | None], None] = lambda target: None

    def touch(self, now: float) -> None:
        self.touched = now

    def visible(self, now: float) -> bool:
        """While a question waits, while apps are in use, while a stop holds (Resume
        is its way out), and for SESSION seconds after the last call."""
        recent = self.touched is not None and now - self.touched < SESSION
        return bool(self.asks or self.apps or self.stopped or recent)

    def answer(self, ask: Ask, choice: str) -> None:
        """Hand the person's choice over. A question is answered once."""
        if ask in self.asks:
            self.asks.remove(ask)
            ask.on_answer(choice)

    def end_turn(self) -> None:
        """The agent is done: no apps, no label. A question stays until the person
        answers it."""
        self.label, self.apps, self.touched = "", [], None

    def rows(self) -> list[Row]:
        """The menu, top to bottom. A row with nothing to do is a line of text."""
        rows: list[Row] = [(self.label or (STOPPED if self.stopped else NO_APPS), None)]
        if self.apps:
            rows.append(DIVIDER)
        for app in self.apps:
            rows.append((STOP.format(app.name), lambda app=app: self.on_stop(app)))
        if len(self.apps) > 1:
            rows.append((STOP_ALL, lambda: self.on_stop(None)))
        if self.apps or self.stopped:
            rows.append((RESUME, lambda: self.on_resume(None)))
        for ask in self.asks:
            rows += [DIVIDER, (ask.title, None)]
            rows += [
                (choice, lambda ask=ask, choice=choice: self.answer(ask, choice))
                for choice in ask.choices
            ]
        return rows


MENU = Menu()
_BAR: Any = None  # the item on screen, made at the first draw


def draw(menu: Menu = MENU) -> None:  # pragma: no cover - puts things on the screen
    """Show `menu` as it is now. Main thread only."""
    global _BAR
    if _BAR is None:
        _BAR = _bar_class().alloc().init()
    _BAR.show_(menu)


def ask_panel(ask: Ask, target: Any) -> Any:
    """The panel of one question: its title, its text and one button per choice.
    It floats, takes no focus, and is off screen until it is ordered in. A click
    sends `pick:` to `target` with the choice's place as the button's tag."""
    import AppKit

    made = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (PANEL_WIDTH, 10)),
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskUtilityWindow
        | AppKit.NSWindowStyleMaskHUDWindow
        | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered,
        True,
    )
    made.setTitle_(NAME)
    made.setLevel_(AppKit.NSFloatingWindowLevel)
    made.setFloatingPanel_(True)
    made.setBecomesKeyOnlyIfNeeded_(True)  # a click on a button takes no focus
    made.setHidesOnDeactivate_(False)  # the helper is never the active app
    made.setReleasedWhenClosed_(False)
    made.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    texts = [(ask.title, AppKit.NSFont.boldSystemFontOfSize_(13))]
    if ask.subtitle:
        texts.append((ask.subtitle, AppKit.NSFont.systemFontOfSize_(11)))
    labels = []
    for text, font in texts:
        label = AppKit.NSTextField.wrappingLabelWithString_(text)
        label.setFont_(font)
        label.setSelectable_(False)
        label.setPreferredMaxLayoutWidth_(PANEL_WIDTH - 2 * PANEL_MARGIN)
        labels.append(label)
    buttons = []
    for place, choice in enumerate(ask.choices):
        button = AppKit.NSButton.buttonWithTitle_target_action_(choice, target, "pick:")
        button.setTag_(place)
        button.setRefusesFirstResponder_(True)
        buttons.append(button)
    row = AppKit.NSStackView.stackViewWithViews_(buttons)
    column = AppKit.NSStackView.stackViewWithViews_([*labels, row])
    column.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical)
    column.setAlignment_(AppKit.NSLayoutAttributeLeading)
    column.setEdgeInsets_((PANEL_MARGIN,) * 4)
    column.setFrameSize_(column.fittingSize())
    made.setContentSize_(column.fittingSize())
    made.setContentView_(column)
    return made


def panel_origin(size: tuple[float, float], visible: tuple[float, ...]) -> tuple:
    """Where the question panel goes: the top right corner of the screen area
    `visible` (x, y, width, height; y up), PANEL_MARGIN in from both edges."""
    x, y, width, height = visible
    return (
        x + width - size[0] - PANEL_MARGIN,
        y + height - size[1] - PANEL_MARGIN,
    )


def _bar_class() -> Any:  # pragma: no cover - AppKit objects on the main thread
    import AppKit
    import objc

    class MacosHarnessBar(AppKit.NSObject):
        """The item, its menu and the question panel on screen."""

        def init(self):
            self = objc.super(MacosHarnessBar, self).init()  # noqa: PLW0642
            bar = AppKit.NSStatusBar.systemStatusBar()
            self.item = bar.statusItemWithLength_(AppKit.NSSquareStatusItemLength)
            icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                SYMBOL, NAME
            )
            self.item.button().setImage_(icon)
            self.item.setMenu_(AppKit.NSMenu.alloc().init())
            self.item.menu().setDelegate_(self)
            self.item.setVisible_(False)
            self.menu, self.picks, self.asked, self.panel = None, [], None, None
            self.timer = None
            return self

        def show_(self, menu):
            """Bring the item and the panel in line with `menu`."""
            self.menu, now = menu, time.monotonic()
            self.item.setVisible_(menu.visible(now))
            if self.timer is not None:
                self.timer.invalidate()
            if menu.touched is not None:  # look again when the session is over
                left = max(menu.touched + SESSION - now, 0) + 0.1
                self.timer = (
                    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                        left, False, lambda _timer: self.show_(menu)
                    )
                )
            first = menu.asks[0] if menu.asks else None
            if first is self.asked:
                return
            if self.panel is not None:
                self.panel.orderOut_(None)
            self.asked, self.panel = first, None
            if first is not None:
                self.panel = ask_panel(first, self)
                screen = AppKit.NSScreen.screens()[0].visibleFrame()
                size = self.panel.frame().size
                area = (*screen.origin, *screen.size)
                self.panel.setFrameOrigin_(panel_origin((*size,), area))
                self.panel.orderFrontRegardless()

        def menuNeedsUpdate_(self, shown):
            """The menu opens: fill it with the rows of this moment."""
            shown.removeAllItems()
            self.picks = [pick for _title, pick in self.menu.rows()]
            for place, (title, pick) in enumerate(self.menu.rows()):
                if not title:
                    shown.addItem_(AppKit.NSMenuItem.separatorItem())
                    continue
                action = "pick:" if pick else None
                row = shown.addItemWithTitle_action_keyEquivalent_(title, action, "")
                row.setTarget_(self)
                row.setTag_(place)

        def pick_(self, sender):
            """A row of the menu, or a button of the panel, was chosen."""
            if isinstance(sender, AppKit.NSMenuItem):
                self.picks[sender.tag()]()
            elif self.asked is not None:
                self.menu.answer(self.asked, self.asked.choices[sender.tag()])
            self.show_(self.menu)

    return MacosHarnessBar
