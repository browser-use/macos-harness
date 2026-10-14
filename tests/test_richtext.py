"""richtext.py: Markdown from styled runs, and the way back to the plain text."""

from __future__ import annotations

import pytest

from macos_harness import richtext, tree
from macos_harness.richtext import find, markdown


def md(*runs) -> str:
    return markdown(runs)[0]


def test_attributed_text_can_rendered_markdown_no_markdown_seen():
    whole = "Line one\nLINE 2 is here\nline three"
    assert (
        md((whole, {"bold": True})) == f"**{whole}**"
    )  # one pair around the whole run
    assert md(("plain text\nstays", {})) == "plain text\nstays"
    assert md(("a ", {}), ("b", {"italic": True}), (" c", {})) == "a *b* c"
    assert md(("x", {"bold": True, "italic": True})) == "***x***"
    assert md(("x", {"underline": 1})) == "<u>x</u>"
    assert md(("x", {"strikethrough": 1})) == "~~x~~"
    assert md(("Title\n", {"heading": 2}), ("body", {})) == "## Title\nbody"
    assert md(("said\nagain", {"quote": 1})) == "> said\n> again"
    assert md(("one\ntwo", {"list": True})) == "- one\n- two"
    assert md(("x = 1", {"code": True})) == "```\nx = 1\n```"


def test_rich_text_written_markdown_more_constructs_eight_state():
    assert md(("buy milk", {"list": "todo"})) == "- [ ] buy milk"
    assert md(("buy milk", {"list": "done"})) == "- [x] buy milk"
    assert md(("2", {"superscript": True})) == "<sup>2</sup>"
    assert md(("2", {"subscript": True})) == "<sub>2</sub>"
    assert md(("More", {"collapsed": True})) == (
        "<details><summary>More</summary>(collapsed, its content is not shown)</details>"
    )
    assert md(("Big", {"paragraph": "Title"})) == "# Big"
    assert md(("Mid", {"paragraph": "Heading"})) == "## Mid"
    assert md(("Small", {"paragraph": "Subheading"})) == "### Small"
    assert md(("plain", {"paragraph": "Body"})) == "plain"
    assert md(("x = 1", {"paragraph": "Fixed width"})) == "```\nx = 1\n```"
    assert md(("deep", {"quote": 2})) == "> > deep"


def test_links_inside_text_become_markdown_links_same_line():
    out = md(("see ", {}), ("the docs", {"link": "https://example.org/d"}), (".", {}))
    assert out == "see [the docs](https://example.org/d)."


def test_markers_hug_the_words_and_equal_runs_join():
    assert md((" bold ", {"bold": True}), ("next", {})) == " **bold** next"
    assert md(("a", {"bold": True}), ("b", {"bold": True})) == "**ab**"
    assert md(("  \n", {"bold": True})) == "  \n"  # white space alone gets no markers
    assert md() == ""


def test_text_attachments_source_code_context_recognised(monkeypatch):
    import fakes_b

    app = fakes_b.load("controls")
    area = fakes_b.at(app.window, [15, 0])
    read = []
    monkeypatch.setattr(
        tree, "_runs", lambda el: read.append(el) or [("x", {"bold": 1})]
    )
    assert app.read(monkeypatch).lines[18].value == "**x**"  # rich text: Markdown
    area.attrs["AXTextualContext"] = "AXTextualContextSourceCode"
    assert app.read(monkeypatch).lines[18].value == "Line 1\nLine 2"  # code: plain
    assert read == [area]  # the runs of a code area are not even read


def test_bold_and_italic_come_from_the_font_name():
    bold = {
        "AXFont": {"AXFontName": "Helvetica-Bold", "AXVisibleName": "Helvetica Bold"}
    }
    slanted = {"AXFont": {"AXFontName": "Helvetica-Oblique"}, "AXUnderline": 1}
    link = {"AXLink": "https://example.org", "AXForegroundColor": object()}
    assert tree._style(bold) == {"bold": True}
    assert tree._style(slanted) == {"underline": 1, "italic": True}
    assert tree._style(link) == {"link": "example.org"}  # short, as a link line
    assert tree._style({"AXFont": {"AXFontName": "Helvetica"}}) == {}


def test_runs_are_asked_with_an_accessibility_range_and_a_link_element_gives_its_url(
    monkeypatch,
):
    from Foundation import NSMutableAttributedString

    from macos_harness import ax

    target = object()  # what AppKit text hands over for a link: the link element
    text = NSMutableAttributedString.alloc().initWithString_("see the docs.")
    text.addAttributes_range_({"AXLink": target}, (4, 8))
    asked = []

    def param(el, name, arg):
        asked.append((name, type(arg).__name__))
        return text

    monkeypatch.setattr(ax, "param", param)
    monkeypatch.setattr(
        ax,
        "get",
        lambda el, name: {
            ("area", "AXNumberOfCharacters"): 13,
            (target, "AXURL"): "https://example.org/d",
        }.get((el, name)),
    )
    runs = tree._runs("area")
    assert asked == [("AXAttributedStringForRange", "AXValueRef")]  # not an NSValue
    assert runs == [("see ", {}), ("the docs", {"link": "example.org/d"}), (".", {})]


@pytest.mark.parametrize(
    ("host", "yes"),
    [
        ("docs.google.com", True),
        ("DOCS.GOOGLE.CO.UK", True),
        ("x.docs.google.com", True),
        ("notdocs.google.com", False),
        ("google.com", False),
    ],
)
def test_google_docs_host_test_exists(host, yes):
    assert richtext.is_docs_host(host) is yes


def test_google_docs_host_pattern_special_cased_near_markdown():
    assert richtext.DOCS_HOST.pattern == r"(?i)(?:^|\.)docs\.google\.[a-z.]+$"


def test_search_runs_markdown_text_mapped_back_source_offsets():
    runs = [("Plain ", {}), ("bold words", {"bold": True}), (" and the end", {})]
    text, offsets = markdown(runs)
    plain = "".join(run[0] for run in runs)
    assert text == "Plain **bold words** and the end" and len(offsets) == len(text) + 1
    for target in ("bold words", "Plain", "end", "words** and"[:5]):
        start, end = find(text, offsets, target)
        assert plain[start:end] == target
    assert find(text, offsets, "words", prefix="bold ") == (11, 16)
    assert find(text, offsets, "", prefix="Plain ") == (6, 6)  # a cursor, no selection
    assert find(text, offsets, "", prefix=text) == (len(plain), len(plain))


def test_search_needs_exactly_one_hit():
    text = "the cat and the hat and the cat"
    assert find(text, None, "the") is None  # three times: which one?
    assert find(text, None, "missing") is None
    assert find(text, None, "") is None
    assert find(text, None, "the", suffix=" hat") == (12, 15)
    assert find(text, None, "cat", prefix="the ", suffix=" and") == (4, 7)
    assert find("aaa", None, "aa") is None  # two hits that overlap are two hits
