"""Pair two states of one app and print what changed between them.

One pairing function serves twice. With the element as the key it says which lines
keep their number. With the number as the key it says what the model is told.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

from .model import Line

HEADER = (
    'What changed in window "{}" since the last read: a line that starts with ~ '
    "changed, a line that starts with + is new. Elements that went away are listed by "
    "ID."
)
NO_CHANGE = 'Nothing changed in window "{}" since the last read.'
REMOVED = "IDs of elements that went away: "
MARKS = {"update": "~", "insert": "+"}  # a removed line is never printed


@dataclass
class Change:
    kind: str  # "none", "update", "insert" or "remove"
    at: int  # position of the node in its own list: the old one for "remove"
    lines: list[Line]  # the node. For "insert" and "remove": its whole subtree
    old: Line | None = None  # the paired old node of "none" and "update"


def line(node: Line) -> str:
    """One printed line: tabs for the depth, the number, the text, the detail."""
    detail = f" {node.detail}" if node.detail else ""
    return "\t" * node.depth + f"{node.id} {node.text}{detail}"


def ranges(ids: Iterable[int]) -> str:
    """Numbers in ascending order, runs as a-b: `16-273, 275-279`."""
    runs: list[list[int]] = []
    for number in sorted(set(ids)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _children(lines: list[Line]) -> dict[int, list[int]]:
    """The children of every node, by position. -1 holds the roots."""
    children: dict[int, list[int]] = {-1: []}
    open_: list[int] = []  # the ancestors of the line being read
    for at, node in enumerate(lines):
        while open_ and lines[open_[-1]].depth >= node.depth:
            open_.pop()
        children[open_[-1] if open_ else -1].append(at)
        children[at] = []
        open_.append(at)
    return children


def _subtree(lines: list[Line], at: int) -> list[Line]:
    end = at + 1
    while end < len(lines) and lines[end].depth > lines[at].depth:
        end += 1
    return lines[at:end]


def _increasing(values: list[int]) -> set[int]:
    """Positions of one longest strictly increasing run of `values`."""
    ends: list[int] = []  # ends[k]: the smallest last value of a run of length k + 1
    last: list[int] = []  # the position of that value
    before: list[int | None] = []  # for each position: the one before it in its run
    for at, value in enumerate(values):
        k = bisect_left(ends, value)
        before.append(last[k - 1] if k else None)
        ends[k : k + 1], last[k : k + 1] = [value], [at]
    kept, at = set(), last[-1] if last else None
    while at is not None:
        kept.add(at)
        at = before[at]
    return kept


def pair(old: list[Line], new: list[Line], key: Callable[[Line], Hashable]):
    """Pair the nodes of two trees. Both lists are flat and depth first.

    Sibling lists are compared level by level, starting at the roots, and only under
    parents that were paired. So a node that moved to another parent is never paired.
    A sibling that moved out of order is an insert, and its old node is dropped
    without a remove.
    """
    old_children, new_children = _children(old), _children(new)
    changes: list[Change] = []
    queue = deque([(-1, -1)])
    while queue:
        old_parent, new_parent = queue.popleft()
        olds, news = old_children[old_parent], new_children[new_parent]
        same = 0  # length of the common start
        while same < min(len(olds), len(news)) and key(old[olds[same]]) == key(
            new[news[same]]
        ):
            same += 1
        rest: dict[Hashable, int] = {}
        for at in olds[same:]:
            rest.setdefault(key(old[at]), at)
        found = [(at, rest.pop(k)) for at in news[same:] if (k := key(new[at])) in rest]
        in_order = _increasing([old_at for _, old_at in found])
        paired = dict(zip(news[:same], olds[:same]))
        paired.update(found[i] for i in in_order)
        for at in news:
            if at not in paired:
                changes.append(Change("insert", at, _subtree(new, at)))
                continue
            was = paired[at]
            if old_children[was] or new_children[at]:
                queue.append((was, at))
            kind = "none" if old[was].text == new[at].text else "update"
            changes.append(Change(kind, at, [new[at]], old[was]))
        changes += [Change("remove", at, _subtree(old, at)) for at in rest.values()]
    return changes


def text(changes: list[Change], title: str, footer: str, budget: int) -> str | None:
    """The diff, or the no-change sentence. None when it costs more than `budget`.

    A changed line costs 1, an added subtree 1 per node, the removed line 1. The
    cost is counted before any line is written.
    """
    shown = sorted((c for c in changes if c.kind in MARKS), key=lambda c: c.at)
    removed = [node.id for c in changes if c.kind == "remove" for node in c.lines]
    cost = sum(len(c.lines) for c in shown) + bool(removed)
    if cost > budget:
        return None
    if not cost:
        parts = [NO_CHANGE.format(title)]
    else:
        parts = [HEADER.format(title)]
        parts += [REMOVED + ranges(removed)] if removed else []
        parts += [MARKS[c.kind] + line(node) for c in shown for node in c.lines]
    return "\n".join(parts + ([footer] if footer else []))
