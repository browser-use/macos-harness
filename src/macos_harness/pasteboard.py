"""Paste through the clipboard and give the person their clipboard back.

The text goes on the clipboard as one item that hands out its data only when an app
asks for it. That request is how we know the app read it. Afterwards everything the
person had on the clipboard is put back, unless they copied something meanwhile.
"""

from __future__ import annotations

import contextlib
import functools
import html
import plistlib
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import errors, protocol, runloop

READ_TIMEOUT = 2.0  # seconds the app gets to ask for the data
LAND_TIMEOUT, LAND_POLL = 2.0, 0.025  # seconds the pasted text gets to show up
CURRENT_HOST_ONLY = 1  # the text does not travel to the person's other devices
RESTORE_FILE = "paste-restore.bin"
PLAIN, HTML, RTF = "public.utf8-plain-text", "public.html", "public.rtf"

_LOCK = threading.Lock()  # one paste at a time, in the whole helper
_INLINE = [
    (r"`([^`]+)`", r"<code>\1</code>"),
    (r"\*\*(.+?)\*\*", r"<b>\1</b>"),
    (r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=\S)\*", r"<i>\1</i>"),
    (r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>'),
]
_LISTS = [(r"[-*] +", "ul"), (r"\d+\. +", "ol")]


def _board():
    from AppKit import NSPasteboard

    return NSPasteboard.generalPasteboard()


def _restore_path() -> Path:
    return protocol.state_dir() / RESTORE_FILE


def markdown_html(text: str) -> str:
    """Headings, lists, paragraphs, bold, italic, code and links. No more."""
    out = []
    for block in re.split(r"\n\s*\n", html.escape(text.strip(), quote=False)):
        lines = block.split("\n")
        heading = re.fullmatch(r"(#{1,6}) +(.*)", block)
        tag = next(
            (t for p, t in _LISTS if all(re.match(p, line) for line in lines)), None
        )
        if heading:
            out.append(f"<h{len(heading[1])}>{heading[2]}</h{len(heading[1])}>")
        elif tag:
            items = (re.sub(r"^\S+ +", "", line) for line in lines)
            out.append(
                f"<{tag}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{tag}>"
            )
        else:
            out.append("<p>" + "<br>".join(lines) + "</p>")
    body = "".join(out)
    for pattern, replacement in _INLINE:
        body = re.sub(pattern, replacement, body)
    return body


def representations(text: str, fmt: str) -> dict[str, bytes]:
    """What goes on the clipboard. text: plain text only. md and html: the HTML, the
    same as rich text, and a plain text for apps that take nothing else."""
    if fmt not in ("md", "html"):
        return {PLAIN: text.encode()}
    from Foundation import NSAttributedString, NSData

    page = '<meta charset="utf-8">' + (markdown_html(text) if fmt == "md" else text)
    data = page.encode()
    made = NSAttributedString.alloc().initWithHTML_documentAttributes_(
        NSData.dataWithBytes_length_(data, len(data)), None
    )
    rich = made[0] if isinstance(made, tuple) else made
    if rich is None:
        raise errors.paste_write_failed()
    rtf = rich.RTFFromRange_documentAttributes_((0, rich.length()), {})
    plain = text if fmt == "md" else str(rich.string())
    return {HTML: data, RTF: bytes(rtf), PLAIN: plain.encode()}


@functools.cache
def _provider_class():
    import AppKit  # noqa: F401 the protocol below lives there
    import objc
    from Foundation import NSData, NSObject

    serves = objc.protocolNamed("NSPasteboardItemDataProvider")

    class LazyPasteData(NSObject, protocols=[serves]):
        """Hands out one kind of data when an app asks, and notes that it asked."""

        def pasteboard_item_provideDataForType_(self, board, item, kind):
            data = self.payload.get(str(kind), b"")
            item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), kind)
            self.was_read.set()

    return LazyPasteData


def _snapshot(board) -> list[dict[str, bytes]]:
    """Every item and every kind of data on the clipboard, as bytes."""
    saved = []
    for item in board.pasteboardItems() or []:
        kinds = {str(k): item.dataForType_(k) for k in item.types() or []}
        saved.append({k: bytes(v) for k, v in kinds.items() if v is not None})
    return saved


def _write(board, provider) -> int | None:
    """Put the lazy item on the clipboard. Returns the change count that is ours."""
    from AppKit import NSPasteboardItem

    item = NSPasteboardItem.alloc().init()
    if not item.setDataProvider_forTypes_(provider, list(provider.payload)):
        return None
    board.prepareForNewContentsWithOptions_(CURRENT_HOST_ONLY)
    return board.changeCount() if board.writeObjects_([item]) else None


def _restore(board, saved: list[dict[str, bytes]], ours: int) -> bool:
    """Put the saved items back. False, and nothing touched, when the clipboard
    changed since we wrote it: that change is the person's."""
    from AppKit import NSPasteboardItem
    from Foundation import NSData

    if board.changeCount() != ours:
        return False
    board.clearContents()
    items = []
    for kinds in saved:
        item = NSPasteboardItem.alloc().init()
        for kind, data in kinds.items():
            item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), kind)
        items.append(item)
    if items:
        board.writeObjects_(items)
    return True


def _wait(done: Callable[[], bool], timeout: float, poll: float = 0.01) -> bool:
    """True as soon as `done()` is. On the main thread the run loop keeps turning,
    because that is where the clipboard asks our item for its data."""
    deadline = time.monotonic() + timeout
    on_main = threading.current_thread() is threading.main_thread()
    while not done():
        if time.monotonic() >= deadline:
            return False
        if on_main:
            from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

            CFRunLoopRunInMode(kCFRunLoopDefaultMode, poll, False)
        else:
            time.sleep(poll)
    return True


def paste(
    text: str,
    fmt: str,
    send_paste_key: Callable[[], None],
    timeout: float = READ_TIMEOUT,
    landed: Callable[[], bool] | None = None,
) -> None:
    """Paste `text` into the app that `send_paste_key` sends Command-V to.
    `landed` says whether the pasted text has shown up in the app."""
    with _LOCK:
        board = _board()
        provider = _provider_class().alloc().init()
        provider.was_read = threading.Event()
        provider.payload = runloop.call_on_main(lambda: representations(text, fmt))
        saved = runloop.call_on_main(lambda: _snapshot(board))
        ours = runloop.call_on_main(lambda: _write(board, provider))
        if ours is None:
            raise errors.paste_write_failed()
        path = _restore_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_bytes(plistlib.dumps({"count": ours, "items": saved}))
        failure = None
        try:
            send_paste_key()
            if not _wait(provider.was_read.is_set, timeout):
                failure = errors.paste_not_read()
            elif landed:
                _wait(landed, LAND_TIMEOUT, LAND_POLL)
        finally:
            restored = runloop.call_on_main(lambda: _restore(board, saved, ours))
            path.unlink(missing_ok=True)
        if not restored:
            raise errors.paste_conflict()
        if failure:
            raise failure


def restore_saved() -> None:
    """A helper that died in the middle of a paste left the person's clipboard in
    a file. Put it back if the clipboard still holds our text."""
    path = _restore_path()
    if not path.exists():
        return
    with contextlib.suppress(Exception):  # a half-written file restores nothing
        left = plistlib.loads(path.read_bytes())
        runloop.call_on_main(lambda: _restore(_board(), left["items"], left["count"]))
    path.unlink(missing_ok=True)
