"""The questions that wait for the person (ask_before_first_use), one at a time, as a
small panel in the top right corner of the screen. Nothing here takes focus.

ASKS is plain data and runs anywhere. `draw` puts the first question on the screen
and runs on the helper's main thread only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

NAME = "macOS Harness"
PANEL_WIDTH, PANEL_MARGIN = 340.0, 16.0  # the question panel, and its gap to the edge


class Ask(NamedTuple):
    """One question to the person."""

    title: str
    subtitle: str | None
    choices: list[str]
    on_answer: Callable[[str], None]


ASKS: list[Ask] = []  # waiting, the first one on the screen. Main thread only.
_SHOWN: Any = None  # the panel controller, made at the first draw


def answer(ask: Ask, choice: str) -> None:
    """Hand the person's choice over. A question is answered once."""
    if ask in ASKS:
        ASKS.remove(ask)
        ask.on_answer(choice)


def draw() -> None:  # pragma: no cover - puts things on the screen
    """Show the first waiting question, or none. Main thread only."""
    global _SHOWN
    if _SHOWN is None:
        _SHOWN = _panel_class().alloc().init()
    _SHOWN.show()


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


def _panel_class() -> Any:  # pragma: no cover - AppKit objects on the main thread
    import AppKit
    import objc

    class MacosHarnessPanel(AppKit.NSObject):
        """The panel of the first waiting question on screen."""

        def init(self):
            self = objc.super(MacosHarnessPanel, self).init()  # noqa: PLW0642
            self.asked, self.panel = None, None
            return self

        def show(self):
            """Bring the panel in line with ASKS."""
            first = ASKS[0] if ASKS else None
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

        def pick_(self, sender):
            """A button of the panel was chosen."""
            if self.asked is not None:
                answer(self.asked, self.asked.choices[sender.tag()])
            self.show()

    return MacosHarnessPanel
