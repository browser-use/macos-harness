"""What the person sees of the agent, as one seam for the other modules.

The pointer, the flash and the halo are drawn by a process of their own
(overlay.py): each call here is one datagram that nobody waits for. The
questions to the person live on the helper's main thread (panel.py). Every
function returns at once and never raises. With
MACOS_HARNESS_POINTER=0 (or false, no, off) nothing is shown and nothing is
started. Each returns True when it handed something over to be shown, else
False: a caller that waits for the person (ask) can tell that nobody will see
the question.
"""

from __future__ import annotations

import contextlib
import functools
import time
from collections.abc import Callable
from typing import Any

from . import liveview, overlay, panel
from .model import Point, Rect, WindowRef

OVERLAY = overlay.LivePointerOverlay()
# The person touched the app: the pointer stays paused this long, whatever the
# calls in flight say. Just under the 5 s after which the caller ends the pause.
PAUSE = 4.75
_paused_until = 0.0
_now = time.monotonic


def _seen(fn: Callable[..., None]) -> Callable[..., bool]:
    """Something the person sees: off with the switch, and never in the way of
    the caller."""

    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> bool:
        with contextlib.suppress(Exception):
            if OVERLAY.on:
                fn(*args, **kwargs)
                return True
        return False

    return call


def _on_main(fn: Callable[[], None]) -> None:
    from . import runloop

    runloop.on_main(fn)


def _asks(change: Callable[[list[panel.Ask]], None]) -> None:
    """Change the waiting questions on the main thread and show the first one."""

    def run() -> None:
        with contextlib.suppress(Exception):
            change(panel.ASKS)
            panel.draw()

    _on_main(run)


def _where(window: WindowRef | None) -> dict[str, int | None]:
    """The app and the window of an action, as the overlay takes them."""
    return {"pid": window and window.pid, "window": window.window_id if window else 0}


@_seen
def pointer_move(point: Point, window: WindowRef | None = None) -> None:
    """The agent acts at `point` (global screen points, origin top left)."""
    OVERLAY.move(point[0], point[1], **_where(window))


@_seen
def pointer_press(count: int = 1) -> None:
    """The agent presses: one pulse per press, once the pointer has arrived."""
    OVERLAY.click(count)


@_seen
def pointer_activity(state: str) -> None:
    """idle: an action runs. loading: the agent thinks. paused: the person has
    the app; then idle and loading are ignored for PAUSE seconds."""
    global _paused_until
    now = _now()
    if state == "paused":
        _paused_until = now + PAUSE
    elif now < _paused_until:
        return
    OVERLAY.activity(state)


@_seen
def flash(rect: Rect, window: WindowRef | None = None) -> None:
    """Outline the control the agent acts on. A pointer that is not on it goes
    to its centre: an action with no point of its own still shows where."""
    OVERLAY.flash(rect, within=window and window.frame, **_where(window))


@_seen
def halo(window: WindowRef | None) -> None:
    """Mark the window the agent drives."""
    if window is not None:
        OVERLAY.halo(window.frame, **_where(window))


@_seen
def live_view(window: WindowRef | None) -> None:
    """Keep a picture of the driven window for a host to show."""
    liveview.watch(window)


@_seen
def turn_done() -> None:
    """The agent's turn is over: the halo goes, the pointer fades. A question to the
    person stays."""
    global _paused_until
    _paused_until = 0.0
    OVERLAY.done()
    liveview.stop()


@_seen
def ask(
    title: str,
    subtitle: str | None,
    choices: list[str],
    on_answer: Callable[[str], None],
) -> None:
    """Ask the person, in a panel. `on_answer` gets the choice once, on the main
    thread. Nothing waits here. False: with the switch off no question is shown,
    so no answer will come."""
    question = panel.Ask(title, subtitle, list(choices), on_answer)
    _asks(lambda asks: asks.append(question))


@_seen
def withdraw(title: str) -> None:
    """A question that was settled another way (the approve command) goes away."""

    def drop(asks: list[panel.Ask]) -> None:
        asks[:] = [ask for ask in asks if ask.title != title]

    _asks(drop)
