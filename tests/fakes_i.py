"""Stand-ins and fixtures for the tests of what the person sees. Nothing here
draws on the screen: a test that turns the pointer on gets a socket of its own
and a listener in place of the drawing process."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest
from PIL import Image, ImageChops

import macos_harness.overlay as overlay_module
from macos_harness import liveview, presence, statusitem
from macos_harness.model import WindowRef
from macos_harness.pointer import outline_points


class Listener:
    """Stands where the drawing process would: it owns the socket and reads what
    a driver sent."""

    def __init__(self, path: str) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(path)
        self.socket.setblocking(False)

    def commands(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(json.loads(self.socket.recv(2048)))
            except BlockingIOError:
                return out


def alike(
    ours: Image.Image, want: Image.Image, back: tuple[int, ...], box: Any = None
) -> float:
    """The share of pixels within 12 of 255 of each other, with both pictures laid
    over one flat colour. `box` scores only that part of them."""
    flat = [
        Image.alpha_composite(
            Image.new("RGBA", picture.size, back), picture.convert("RGBA")
        )
        .convert("RGB")
        .crop(box)
        for picture in (ours, want)
    ]
    red, green, blue = ImageChops.difference(*flat).split()
    worst = ImageChops.lighter(ImageChops.lighter(red, green), blue).histogram()
    return sum(worst[:13]) / sum(worst)


def arrow_box(scale: int) -> tuple[int, ...]:
    """The pixels of the arrow and its rim in an 80 x 80 pt picture with the click
    point in its middle. The arrow is 3% of the picture: scored alone, a missing
    rim shows."""
    xs, ys = zip(*outline_points(), strict=True)
    edges = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
    return tuple(round((40 + edge) * scale) for edge in edges)


def window(pid: int = 42, window_id: int = 7, frame=(100.0, 50.0, 800.0, 600.0)):
    """A window of the app `pid`, as windows.py hands one out."""
    return WindowRef(element=None, window_id=window_id, pid=pid, title="W", frame=frame)


@pytest.fixture
def pointer_socket(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Turn the pointer on, pointed at a socket path no real process uses."""
    # A short path: the address of a socket holds 104 bytes.
    folder = tempfile.mkdtemp(prefix="mhp", dir="/tmp")
    path = os.path.join(folder, "p.sock")
    monkeypatch.setenv("MACOS_HARNESS_POINTER", "1")
    monkeypatch.setattr(overlay_module, "address", lambda: path)
    yield path
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def listener(pointer_socket: str) -> Iterator[Listener]:
    """A stand-in that is already listening, so no process is started."""
    stand_in = Listener(pointer_socket)
    yield stand_in
    stand_in.socket.close()


@pytest.fixture
def seen(listener: Listener, monkeypatch: pytest.MonkeyPatch) -> Iterator[list]:
    """presence with the pointer on and nothing real behind it: the main thread
    runs a call at once, and the menu bar item is drawn into the list given."""
    drawn: list[list] = []
    monkeypatch.setattr(presence, "_on_main", lambda fn: fn())
    monkeypatch.setattr(presence, "_paused_until", 0.0)
    monkeypatch.setattr(presence, "OVERLAY", overlay_module.LivePointerOverlay())
    monkeypatch.setattr(statusitem, "MENU", statusitem.Menu())
    monkeypatch.setattr(statusitem, "draw", lambda menu: drawn.append(menu.rows()))
    monkeypatch.setattr(liveview, "_LIVE", None)
    yield drawn
