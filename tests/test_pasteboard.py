"""Paste and restore, on a clipboard of our own: the person's clipboard is never
touched by these tests."""

from __future__ import annotations

import inspect
import plistlib

import pytest
from fakes_e import load

from macos_harness import errors

AppKit = pytest.importorskip("AppKit")
pasteboard = load("pasteboard")
PLAIN, HTML, RTF = pasteboard.PLAIN, pasteboard.HTML, pasteboard.RTF


@pytest.fixture
def board(monkeypatch):
    """A private clipboard that holds what "the person" copied: two kinds of data."""
    own = AppKit.NSPasteboard.pasteboardWithUniqueName()
    item = AppKit.NSPasteboardItem.alloc().init()
    item.setString_forType_("the person's text", PLAIN)
    item.setString_forType_("<b>the person's text</b>", HTML)
    own.clearContents()
    own.writeObjects_([item])
    monkeypatch.setattr(pasteboard, "_board", lambda: own)
    yield own
    own.releaseGlobally()


def held(board) -> dict[str, bytes]:
    return pasteboard._snapshot(board)[0]


def failure(call, *args, **kwargs) -> str:
    with pytest.raises(errors.HarnessError) as caught:
        call(*args, **kwargs)
    return caught.value.message.removeprefix("macOS Harness error 100: ")


def test_paste_puts_lazy_item_pasteboard_triggers_paste_waits(board):
    """The text is on the clipboard as one item that gives its data when asked.
    The app's read is what ends the wait. One paste at a time."""
    seen = []

    def app_pastes():
        seen.append(len(board.pasteboardItems()))
        seen.append(pasteboard._LOCK.locked())
        seen.append(str(board.stringForType_(PLAIN)))

    pasteboard.paste("typed by the tool", "text", app_pastes)
    assert seen == [1, True, "typed by the tool"]
    assert not pasteboard._LOCK.locked()


def test_paste_restores_person_s_previous_clipboard_afterwards(board):
    before = held(board)
    assert set(before) == {PLAIN, HTML}
    pasteboard.paste("x", "text", lambda: board.stringForType_(PLAIN))
    assert held(board) == before  # every kind of data, byte for byte


def test_empty_clipboard_stays_empty(board):
    board.clearContents()
    pasteboard.paste("x", "text", lambda: board.stringForType_(PLAIN))
    assert pasteboard._snapshot(board) == []


def test_paste_detects_clash_person_s_own_clipboard_use(board):
    """The person copied something while we pasted: their copy stays, nothing is
    restored over it, and the model is told."""

    def app_pastes():
        board.stringForType_(PLAIN)
        board.clearContents()
        board.setString_forType_("copied by the person meanwhile", PLAIN)

    text = failure(pasteboard.paste, "x", "text", app_pastes)
    assert text.startswith("The clipboard changed during the paste")
    assert held(board) == {PLAIN: b"copied by the person meanwhile"}


def test_paste_times_out_if_app_never_reads_clipboard(board):
    before = held(board)
    text = failure(pasteboard.paste, "x", "text", lambda: None, timeout=0.05)
    assert text == "The app did not take the text from the clipboard in time"
    assert held(board) == before  # and the person's clipboard is back
    default = inspect.signature(pasteboard.paste).parameters["timeout"].default
    assert default == pasteboard.READ_TIMEOUT == 2.0


def test_write_failure_has_its_own_text(board, monkeypatch):
    monkeypatch.setattr(pasteboard, "_write", lambda board, provider: None)
    text = failure(pasteboard.paste, "x", "text", lambda: None)
    assert text == "The text could not be put on the clipboard"


def test_write_is_for_this_mac_only():
    class Board:
        def __init__(self):
            self.calls = []

        def prepareForNewContentsWithOptions_(self, options):
            self.calls.append(options)

        def writeObjects_(self, items):
            return True

        def changeCount(self):
            return 41

    provider = pasteboard._provider_class().alloc().init()
    provider.payload = {PLAIN: b"x"}
    fake = Board()
    assert pasteboard._write(fake, provider) == 41
    assert fake.calls == [1]  # not sent to the person's other devices


def test_pasted_text_is_waited_for_before_the_restore(board, monkeypatch):
    """After the app read the clipboard: ask every 25 ms whether the text landed,
    for at most 2.0 s, and only then restore."""
    asked = []

    def landed():
        asked.append(str(board.stringForType_(PLAIN)))
        return len(asked) == 3

    pasteboard.paste("x", "text", lambda: board.stringForType_(PLAIN), landed=landed)
    assert asked == ["x", "x", "x"]
    assert (pasteboard.LAND_TIMEOUT, pasteboard.LAND_POLL) == (2.0, 0.025)
    monkeypatch.setattr(pasteboard, "LAND_TIMEOUT", 0.05)
    pasteboard.paste("x", "text", lambda: board.stringForType_(PLAIN), landed=bool)


def test_restore_file_lives_only_during_the_paste(board, private_state):
    path = private_state / pasteboard.RESTORE_FILE
    seen = {}

    def app_pastes():
        seen.update(plistlib.loads(path.read_bytes()))
        seen["mode"] = path.stat().st_mode & 0o777
        board.stringForType_(PLAIN)

    pasteboard.paste("x", "text", app_pastes)
    assert seen["items"] == [held(board)] and seen["mode"] == 0o600
    assert seen["count"] > 0 and not path.exists()


def test_helper_that_died_mid_paste_gives_the_clipboard_back(board, private_state):
    path = private_state / pasteboard.RESTORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = [held(board)]
    board.clearContents()
    board.setString_forType_("the tool's text, left behind", PLAIN)
    path.write_bytes(plistlib.dumps({"count": board.changeCount(), "items": saved}))
    pasteboard.restore_saved()
    assert pasteboard._snapshot(board) == saved and not path.exists()


def test_left_over_file_restores_nothing_once_the_person_copied(board, private_state):
    path = private_state / pasteboard.RESTORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    before = held(board)
    stale = {"count": board.changeCount() - 1, "items": [{PLAIN: b"old"}]}
    for content in (plistlib.dumps(stale), b"", b"not a plist"):
        path.write_bytes(content)
        pasteboard.restore_saved()
        assert held(board) == before and not path.exists()
    pasteboard.restore_saved()  # no file: nothing to do


def test_what_md_becomes_pasteboard(board):
    """md and html: the HTML, the same as rich text, and plain text."""
    assert pasteboard.representations("**b**", "text") == {PLAIN: b"**b**"}
    made = pasteboard.representations("# Title\n\nSome **bold** text", "md")
    assert list(made) == [HTML, RTF, PLAIN]
    assert b"<h1>Title</h1><p>Some <b>bold</b> text</p>" in made[HTML]
    assert made[RTF].startswith(b"{\\rtf") and b"bold" in made[RTF]
    assert made[PLAIN] == b"# Title\n\nSome **bold** text"  # the source, as written
    page = pasteboard.representations("<p>café <i>au lait</i></p>", "html")
    assert page[PLAIN].decode().strip() == "café au lait"
    assert "café".encode() in page[HTML]


def test_pasteboard_type_format_names_paste_path(board):
    """Every kind is served lazily: the app that asks for rich text gets rich text."""
    seen = {}

    def app_pastes():
        for kind in (HTML, RTF, PLAIN):
            seen[kind] = bytes(board.dataForType_(kind))

    pasteboard.paste("*hi*", "md", app_pastes)
    assert seen[PLAIN] == b"*hi*" and b"<i>hi</i>" in seen[HTML]
    assert seen[RTF].startswith(b"{\\rtf")


@pytest.mark.parametrize(
    ("source", "page"),
    [
        ("plain", "<p>plain</p>"),
        ("a\nb", "<p>a<br>b</p>"),
        ("## Two", "<h2>Two</h2>"),
        ("- a\n- b", "<ul><li>a</li><li>b</li></ul>"),
        ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
        ("`x < y`", "<p><code>x &lt; y</code></p>"),
        ("[t](https://example.com)", '<p><a href="https://example.com">t</a></p>'),
        ("2 * 3 * 4", "<p>2 * 3 * 4</p>"),  # arithmetic is not emphasis
    ],
)
def test_markdown_to_html(source, page):
    assert pasteboard.markdown_html(source) == page
