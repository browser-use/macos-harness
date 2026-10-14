"""A picture of the driven window, kept fresh in a file while the agent acts,
so a host can show what happens in a window the person does not see.

Opt-in: MACOS_HARNESS_LIVE_VIEW=1. The file is <state folder>/live/<helper pid>.jpg,
replaced by rename, RATE times a second while an action is under ACTIVE
seconds old. It is taken on a thread of its own: no action ever waits for a
picture, and the model never gets one.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .model import WindowRef

RATE = 5.0  # pictures per second while the agent acts
ACTIVE = 10.0  # seconds after the last call during which pictures are taken
QUALITY = 0.8  # of the JPEG
MAX_SIDE = 1280  # pixels: the picture is fitted inside a square of this side
JPEG = b"\xff\xd8"  # how every picture starts


class Live:
    """The file of one helper. `capture(window)` gives JPEG bytes or None."""

    def __init__(
        self,
        path: Path,
        capture: Callable[[WindowRef], bytes | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path, self.capture, self.clock = path, capture, clock
        self.window: WindowRef | None = None
        self.touched = -ACTIVE  # the last call
        self.last: bytes | None = None  # what the file holds
        self.thread: threading.Thread | None = None

    def watch(self, window: WindowRef | None) -> None:
        """The agent acts on `window` now."""
        self.window, self.touched = window, self.clock()

    def tick(self) -> bool:
        """Take one picture. False: the last call is too old, take no more."""
        if self.window is None or self.clock() - self.touched >= ACTIVE:
            return False
        data = self.capture(self.window)
        if data and data.startswith(JPEG) and data != self.last:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A reader sees the old picture or the new one, never half of one.
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as new:
                new.write(data)
            os.replace(new.name, self.path)
            self.last = data
        return True

    def stop(self) -> None:
        """The turn is over: no more pictures, and the file goes."""
        self.window, self.last = None, None
        with contextlib.suppress(OSError):
            self.path.unlink()

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """Take pictures until the agent has gone quiet."""
        with contextlib.suppress(Exception):
            while self.tick():
                sleep(1 / RATE)
        self.thread = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()


def capture(window: WindowRef) -> bytes | None:  # pragma: no cover - a real window
    """The window alone, by its id, as a JPEG fitted inside MAX_SIDE. None with
    no grant to record the screen: nothing is asked for here."""
    import Quartz

    if not window.window_id or not Quartz.CGPreflightScreenCaptureAccess():
        return None
    with tempfile.TemporaryDirectory() as folder:
        shot = os.path.join(folder, "shot.png")
        which = ["-x", "-o", "-l", str(window.window_id)]
        command = ["/usr/sbin/screencapture", *which, shot]
        subprocess.run(command, timeout=5, check=False)
        return fit(shot)


def fit(path: str) -> bytes | None:
    """The picture file at `path` as JPEG bytes, no side over MAX_SIDE."""
    import Foundation
    import Quartz

    url = Foundation.NSURL.fileURLWithPath_(path)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        return None
    small: dict[Any, Any] = {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceThumbnailMaxPixelSize: MAX_SIDE,
    }
    image = Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, small)
    if image is None:
        return None
    data = Foundation.NSMutableData.data()
    out = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    how = {Quartz.kCGImageDestinationLossyCompressionQuality: QUALITY}
    Quartz.CGImageDestinationAddImage(out, image, how)
    return bytes(data) if Quartz.CGImageDestinationFinalize(out) else None


_LIVE: Live | None = None


def path() -> Path:
    from .protocol import state_dir

    return state_dir() / "live" / f"{os.getpid()}.jpg"


def watch(window: WindowRef | None) -> None:
    """Called with each state read. Does nothing unless MACOS_HARNESS_LIVE_VIEW=1."""
    global _LIVE
    if os.environ.get("MACOS_HARNESS_LIVE_VIEW", "").strip() != "1":
        return
    if _LIVE is None:
        _LIVE = Live(path(), capture)
    _LIVE.watch(window)
    _LIVE.start()


def stop() -> None:
    if _LIVE is not None:
        _LIVE.stop()
