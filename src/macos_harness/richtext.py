"""Rich text as Markdown, with a map back to the plain text it came from.

Input: the runs of an attributed string as (text, style) pairs. A style is a dict with
any of: bold, italic, underline, strikethrough, superscript, subscript, link (a URL),
heading (a level), paragraph (a style name), quote (a level), list (True, "done" or
"todo"), code, collapsed.

The markers are our choice of Markdown. The one form a measured tree pins is **bold**
around a whole run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

Run = tuple[str, dict]

# (style, opening, closing): markers around the text of a run, innermost first.
INLINE = (
    ("strikethrough", "~~", "~~"),
    ("underline", "<u>", "</u>"),
    ("superscript", "<sup>", "</sup>"),
    ("subscript", "<sub>", "</sub>"),
    ("italic", "*", "*"),
    ("bold", "**", "**"),
)
PARAGRAPHS = {"Title": 1, "Heading": 2, "Subheading": 3}  # style name -> heading level
LISTS = {"todo": "- [ ] ", "done": "- [x] "}  # the two checklist states
COLLAPSED = "</summary>(collapsed, its content is not shown)</details>"
FENCE = "```"
# The host of an online document editor whose text is laid out its own way.
DOCS_HOST = re.compile(r"(?i)(?:^|\.)docs\.google\.[a-z.]+$")


def is_docs_host(host: str) -> bool:
    return bool(DOCS_HOST.search(host))


def _line_prefix(style: dict) -> str:
    """What every line of a run starts with: quote marks, a heading, a list bullet."""
    level = style.get("heading") or PARAGRAPHS.get(style.get("paragraph", ""), 0)
    bullet = style.get("list")
    return (
        "> " * int(style.get("quote") or 0)
        + ("#" * int(level) + " " if level else "")
        + (LISTS.get(bullet, "- ") if bullet else "")
    )


def _merged(runs: Iterable[Run]) -> list[Run]:
    out: list[Run] = []
    for text, style in runs:
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + text, style)
        elif text:
            out.append((text, style))
    return out


def markdown(attributed: Iterable[Run]) -> tuple[str, list[int]]:
    """The Markdown text, and for each of its characters the offset in the plain text.

    The list has one entry more than the text: the plain text's length.
    """
    out: list[str] = []
    offsets: list[int] = []
    source = 0

    def plain(text: str) -> None:  # text of the source: every character has its place
        nonlocal source
        if text:
            out.append(text)
            offsets.extend(range(source, source + len(text)))
            source += len(text)

    def mark(text: str) -> None:  # a marker we add: it points at what follows it
        if text:
            out.append(text)
            offsets.extend([source] * len(text))

    for text, style in _merged(attributed):
        code = style.get("code") or style.get("paragraph") == "Fixed width"
        opening = [o for name, o, _ in INLINE if style.get(name)]
        closing = [c for name, _, c in reversed(INLINE) if style.get(name)]
        if style.get("link"):
            opening, closing = ["[", *opening], [*closing, f"]({style['link']})"]
        if style.get("collapsed"):
            opening, closing = ["<details><summary>", *opening], [*closing, COLLAPSED]
        if code:
            opening, closing = [FENCE + "\n"], ["\n" + FENCE]
        # Markers hug the words: white space at either end of the run stays outside.
        body = text if code or not opening else text.strip()
        if not body:
            opening, closing, body = [], [], text
        start = text.find(body)
        prefix = _line_prefix(style)
        plain(text[:start])
        for number, line in enumerate(body.split("\n")):
            if number:
                plain("\n")
            if line and (not out or out[-1].endswith("\n")):
                mark(prefix)
            mark("".join(opening) if not number else "")
            plain(line)
        mark("".join(closing))
        plain(text[start + len(body) :])
    return "".join(out), [*offsets, source]


def find(
    text: str,
    offsets: list[int] | None,
    target: str,
    prefix: str = "",
    suffix: str = "",
) -> tuple[int, int] | None:
    """Where `target` sits in the plain text, found by searching the printed text.

    The printed text must hold prefix + target + suffix exactly once. Else None.
    `offsets` is the list markdown() gave, or None when the printed text is plain.
    """
    needle = prefix + target + suffix
    first = text.find(needle)
    if not needle or first < 0 or text.find(needle, first + 1) >= 0:
        return None
    start = first + len(prefix)
    end = start + len(target)
    if offsets is None:
        return start, end
    last = offsets[end - 1] + 1 if target else offsets[start]
    return offsets[start], last
