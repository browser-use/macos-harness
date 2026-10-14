from __future__ import annotations

import io
from pathlib import Path

import pytest
from fakes import Clock
from fakes_i import window
from PIL import Image

from macos_harness import liveview
from macos_harness.liveview import ACTIVE, JPEG, MAX_SIDE, RATE, Live, fit


def picture(colour: str, size: tuple[int, int] = (8, 8)) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", size, colour).save(data, "JPEG")
    return data.getvalue()


class Camera:
    """Stands in for the capture of a window: it hands out what a test queued,
    and the last of it again and again."""

    def __init__(self, *shots: bytes | None) -> None:
        self.shots, self.asked = list(shots), []

    def __call__(self, shown) -> bytes | None:
        self.asked.append(shown)
        return self.shots.pop(0) if len(self.shots) > 1 else self.shots[0]


def live(tmp_path: Path, camera: Camera, clock: Clock) -> Live:
    return Live(tmp_path / "live" / "1.jpg", camera, clock.monotonic)


def test_backgrounded_apps_being_worked_can_shown_picture_picture(tmp_path) -> None:
    # A picture file of the driven window that a host can show, written
    # five times a second while the agent acts and not after.
    clock, camera = Clock(), Camera(picture("red"), picture("blue"))
    view, here = live(tmp_path, camera, clock), window()
    assert not view.tick()  # no call yet: no picture, no file
    assert not view.path.exists()

    view.watch(here)
    assert view.tick()
    assert view.path.read_bytes() == picture("red")
    assert view.path.read_bytes().startswith(JPEG)
    assert view.tick()
    assert view.path.read_bytes() == picture("blue")
    assert camera.asked == [here, here]

    view.watch(here)
    view.run(sleep=clock.sleep)  # until the last call is ten seconds old
    assert len(camera.asked) == 2 + ACTIVE * RATE
    assert view.thread is None
    assert not view.tick()


def test_the_file_is_replaced_by_rename_and_only_when_the_picture_changed(
    tmp_path,
) -> None:
    clock, camera = Clock(), Camera(picture("red"), picture("red"), picture("blue"))
    view = live(tmp_path, camera, clock)
    view.watch(window())

    view.tick()
    first = view.path.stat().st_ino
    view.tick()  # the same picture: the file is left alone
    assert view.path.stat().st_ino == first
    view.tick()  # a new one: a new file takes the old one's name
    assert view.path.stat().st_ino != first
    assert [file.name for file in view.path.parent.iterdir()] == ["1.jpg"]


def test_pressing_minimize_active_window_can_trigger_picture_picture(tmp_path) -> None:
    # A window that gives no picture any more (minimised, closed) keeps its
    # last one in the file, and the pictures go on when it is back.
    clock = Clock()
    camera = Camera(picture("red"), None, b"not a picture", picture("blue"))
    view = live(tmp_path, camera, clock)
    view.watch(window())

    view.tick()
    assert view.tick() and view.tick()
    assert view.path.read_bytes() == picture("red")
    view.tick()
    assert view.path.read_bytes() == picture("blue")


def test_picture_picture_window_lookup_and_switch(tmp_path, monkeypatch) -> None:
    # The window is looked up by its id at every picture, the switch is an
    # environment variable, and with the switch unset nothing is made at all.
    started = []
    monkeypatch.setattr(liveview, "_LIVE", None)
    monkeypatch.setattr(liveview, "path", lambda: tmp_path / "live" / "9.jpg")
    monkeypatch.setattr(Live, "start", lambda self: started.append(self.window))
    first, second = window(window_id=7), window(window_id=8)

    for switch in ("", "0", "yes"):
        monkeypatch.setenv("MACOS_HARNESS_LIVE_VIEW", switch)
        liveview.watch(first)
    assert (liveview._LIVE, started) == (None, [])
    liveview.stop()  # nothing to stop

    monkeypatch.setenv("MACOS_HARNESS_LIVE_VIEW", "1")
    liveview.watch(first)
    liveview.watch(second)  # the agent moved on to another window
    assert started == [first, second]
    assert liveview._LIVE.window == second


def test_the_end_of_a_turn_removes_the_file(tmp_path) -> None:
    clock, camera = Clock(), Camera(picture("red"))
    view = live(tmp_path, camera, clock)
    view.watch(window())
    view.tick()

    view.stop()
    view.stop()  # twice: the file is gone already

    assert not view.path.exists()
    assert not view.tick()
    view.watch(window())  # the next turn starts with a fresh picture
    view.tick()
    assert view.path.read_bytes() == picture("red")


def test_a_camera_that_fails_ends_the_pictures_and_nothing_else(tmp_path) -> None:
    def broken(shown):
        raise OSError("no screen")

    view = Live(tmp_path / "1.jpg", broken, Clock().monotonic)
    view.watch(window())

    view.run(sleep=lambda seconds: None)

    assert view.thread is None


@pytest.mark.parametrize(
    ("size", "fitted"),
    [((3000, 1000), (1280, 427)), ((640, 400), (640, 400)), ((900, 2700), (427, 1280))],
)
def test_a_picture_is_fitted_inside_the_largest_side_as_a_jpeg(
    size, fitted, tmp_path
) -> None:
    shot = tmp_path / "shot.png"
    Image.new("RGB", size, "teal").save(shot)

    data = fit(str(shot))

    assert data.startswith(JPEG)
    with Image.open(io.BytesIO(data)) as small:
        assert (small.format, small.size) == ("JPEG", fitted)
    assert max(fitted) <= MAX_SIDE
    assert fit(str(tmp_path / "missing.png")) is None
