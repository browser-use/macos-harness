"""Fakes and helpers for the tests of elements.py and diff.py.

A chain file under tests/fixtures/diffs holds the reads of one app session and the
exact text each read must print:

    == read [full] [title=<window title>]
    <dots for the depth><key> <text of the line>
    == want
    <the text>
    == end

The key names the element: the same key in a later read is the same element. `*`
before a key marks the focused line. `@` marks a focused element that is not in the
tree. `full` is a read with diffing switched off.
"""

from __future__ import annotations

import re
from pathlib import Path

from fakes import El, FakeAx

from macos_harness import elements
from macos_harness.model import Capture, Line

CHAINS = Path(__file__).parent / "fixtures" / "diffs"
ROW = re.compile(r"^(\.*)([*@]?)(\w+) ?(.*)$", re.DOTALL)
HEADER = (
    'What changed in window "{}" since the last read: a line that starts with ~ '
    "changed, a line that starts with + is new. Elements that went away are listed by "
    "ID."
)


class FakeRender:
    preset: str | None = None  # a footer to print as it is

    def header(self, title: str, app_name: str) -> str:
        return f'Window: "{title}", App: {app_name}.'

    def footer(self, capture: Capture, numbered) -> str:
        if self.preset is not None:
            return self.preset
        line = capture.focused
        return f"The focused UI element is {line.id} {line.text}" if line else ""


def patch(monkeypatch) -> FakeAx:
    """Put the fakes in place of the two modules elements.py calls."""
    ax = FakeAx()
    monkeypatch.setattr(elements, "ax", ax)
    monkeypatch.setattr(elements, "render", FakeRender())
    return ax


def line(depth: int, text: str, element=None, **fields) -> Line:
    return Line(depth, text, element or El(), fields.pop("role", "AXGroup"), **fields)


def capture(lines: list[Line], title: str = "Main", **fields) -> Capture:
    return Capture(title, "App", 1, lines, **fields)


def rows(text: str, known: dict[str, El] | None = None, title="Main") -> Capture:
    """A capture from rows in the form of a chain file."""
    known = {} if known is None else known
    lines, focused = [], None
    for row in text.splitlines():
        dots, mark, key, body = ROW.match(row).groups()
        node = line(len(dots), body, known.setdefault(key, El()))
        focused = node if mark else focused
        if mark != "@":
            lines.append(node)
    return capture(lines, title, focused=focused)


def chain(name: str) -> list[tuple[Capture, bool, str]]:
    """The reads of one chain file: the capture, the full-tree flag, the wanted text."""
    known: dict[str, El] = {}
    blocks = (CHAINS / f"{name}.txt").read_text().split("== ")[1:]
    reads = []
    for read, want in zip(blocks[0:-1:2], blocks[1:-1:2]):
        options, _, body = read.partition("\n")
        flags, _, title = options.partition("title=")
        state = rows(body.removesuffix("\n"), known, title or "Main")
        want = want.removeprefix("want\n").removesuffix("\n")
        reads.append((state, " full" in flags, want))
    return reads


def replay(name: str) -> list[str]:
    """Run a chain through one Revisions and check every text. Returns the texts."""
    revisions = elements.Revisions()
    texts = []
    for state, full, want in chain(name):
        texts.append(revisions.present(state, full))
        assert texts[-1] == want.replace("<HEADER>", HEADER.format(state.window_title))
    return texts


def names() -> list[str]:
    return sorted(path.stem for path in CHAINS.glob("*.txt"))
