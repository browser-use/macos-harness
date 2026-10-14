"""Numbers for the lines of a state, and the way back from a number to an element.

A number belongs to an element for as long as the element stays in the tree from
one read to the next. New elements get the numbers after the largest one that
stayed, so a number that left the tree can come back on another element.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from itertools import count

from . import ax, diff, errors, render
from .model import Capture, Line

DESTROYED = "AXUIElementDestroyed"


class _Same:
    """An element as a dictionary key. Two keys are equal when ax.same says so.

    The hash only picks the bucket. Different elements often share one hash.
    """

    __slots__ = ("element",)

    def __init__(self, element):
        self.element = element

    def __hash__(self) -> int:
        return hash(self.element)

    def __eq__(self, other) -> bool:
        return ax.same(self.element, other.element)


def _element(line: Line) -> _Same:
    return _Same(line.element)


def _number(line: Line) -> int | None:
    return line.id


def _extra(capture: Capture) -> list[Line]:
    """The focus and selection lines. They may or may not also be in the tree."""
    return [*([capture.focused] if capture.focused else []), *capture.selected]


def _full(capture: Capture, footer: str) -> str:
    head = render.header(capture.window_title, capture.app_name)
    body = "\n".join(map(diff.line, capture.lines))
    return f"{head}\n{body}\n" + (f"\n{footer}" if footer else "")


class Revisions:
    """The last state of one app as one agent saw it."""

    def __init__(self):
        self.last: Capture | None = None
        self._observer = None
        self._pid: int | None = None
        self._lock = threading.Lock()
        self._destroyed: list = []  # elements the app reported gone since the last read

    def present(self, capture: Capture, disable_diff: bool) -> str:
        """Number the lines and return the full tree, the diff or the no-change text."""
        old = None if disable_diff else self.last
        lines = capture.lines
        if old is None:  # a new lineage counts from 0
            for number, node in enumerate(lines):
                node.id = number
        else:  # a paired element keeps its number
            for change in diff.pair(old.lines, lines, _element):
                if change.old:
                    change.lines[0].id = change.old.id
        fresh = count(max((n.id for n in lines if n.id is not None), default=0) + 1)
        given: dict[_Same, int] = {}  # numbers of this pass, by element
        for node in [*lines, *_extra(capture)]:  # the tree first, then the focus
            key = _element(node)
            if node.id is None:
                node.id = given[key] if key in given else next(fresh)
            given[key] = node.id
        footer = render.footer(capture, True)
        text = None
        if old is not None:
            changes = diff.pair(old.lines, lines, _number)
            text = diff.text(changes, capture.window_title, footer, len(lines))
        self.last = capture
        with self._lock:
            self._destroyed.clear()
        return _full(capture, footer) if text is None else text

    def resolve(
        self, index: int, reread: Callable[[], Capture], ignore_value: bool = False
    ) -> Line:
        """The line with this number in the last state, with an element that is alive."""
        known = [*self.last.lines, *_extra(self.last)] if self.last else []
        line = next((node for node in known if node.id == index), None)
        if line is None:
            raise errors.invalid_element_id(index)
        if self._observer is None:
            raise errors.no_invalidation_monitor()
        if not self._dead(line.element):
            return line

        def alike(node: Line) -> bool:
            return node.match == line.match and (
                ignore_value or node.value == line.value
            )

        if any(alike(n) and not ax.same(n.element, line.element) for n in known):
            raise errors.ambiguous_refetch(started=False)
        found = [node for node in reread().lines if alike(node)]
        if not found:
            raise errors.stale_element()
        if len(found) > 1:
            raise errors.ambiguous_refetch(started=True)
        line.element = found[0].element
        return line

    def watch(self, target) -> None:
        """Start marking destroyed elements of the app. Calling it again is free."""
        if self._observer is not None and self._pid == target.pid:
            return
        self._stop()
        app = ax.app(target.pid)
        self._observer = ax.observe(target.pid, app, (DESTROYED,), self._mark)
        self._pid = target.pid

    def reset(self) -> None:
        """Forget the last state and stop watching. The next read counts from 0."""
        self._stop()
        self.last = None

    def _mark(self, name: str, element) -> None:
        with self._lock:
            self._destroyed.append(element)

    def _dead(self, element) -> bool:
        with self._lock:
            destroyed = list(self._destroyed)
        return any(ax.same(element, gone) for gone in destroyed) or not ax.alive(
            element
        )

    def _stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
        self._observer = self._pid = None
        with self._lock:
            self._destroyed.clear()
