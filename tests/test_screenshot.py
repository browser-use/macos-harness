"""screenshot.py: the file, its name, its clean-up, and pixels back to screen points."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from macos_harness import errors, screenshot
from macos_harness.model import Shot, WindowRef

SHIPPED_SOURCES = screenshot.SOURCES  # as the module has them: the fixture swaps them

# What the system's JPEG writer puts in a file at quality 0.80, luma then chroma.
QUALITY_80_TABLES = {
    0: [
        2,
        2,
        2,
        3,
        4,
        5,
        7,
        8,
        2,
        2,
        2,
        3,
        4,
        5,
        7,
        8,
        2,
        2,
        3,
        4,
        5,
        7,
        8,
        10,
        3,
        3,
        4,
    ]
    + [5, 7, 8, 10, 11, 4, 4, 5, 7, 8, 10, 11, 13, 5, 5, 7, 8, 10, 11, 13, 13, 7, 7, 8]
    + [10, 11, 13, 13, 13, 8, 8, 10, 11, 13, 13, 13, 13],
    1: [2, 2, 3, 6, 13, 13, 13, 13, 2, 3, 3, 9, 13, 13, 13, 13, 3, 3, 7, 13, 13, 13, 13]
    + [13, 6, 9]
    + [13] * 38,
}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot, "FOLDER", tmp_path / "screenshots")
    monkeypatch.setattr(screenshot, "_silent_until", {})
    monkeypatch.setattr(screenshot, "SOURCES", (image_source,))
    return screenshot.FOLDER


def image_source(window_id, extra_ids):
    """The one source the tests use: whatever _window_image is patched to give."""
    return screenshot._window_image(window_id, extra_ids)


def fake_image(width: int = 64, height: int = 48):
    Quartz = pytest.importorskip("Quartz")
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    info = Quartz.kCGImageAlphaPremultipliedLast
    context = Quartz.CGBitmapContextCreate(None, width, height, 8, 0, space, info)
    Quartz.CGContextSetRGBFillColor(context, 0.2, 0.5, 0.9, 1.0)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, width, height))
    return Quartz.CGBitmapContextCreateImage(context)


def window(frame=(60.0, 58.0, 32.0, 24.0)) -> WindowRef:
    return WindowRef(element=None, window_id=7, pid=os.getpid(), title="t", frame=frame)


def test_coordinates_screenshot_pixels_divided_by_scale_factor():
    shot = Shot("p", scale=2.0, origin=(60.0, 58.0), size=(1120, 1184))
    assert shot.to_screen(560, 918) == (340.0, 517.0)  # window point (280, 459)
    assert shot.to_screen(0, 0) == (60.0, 58.0)


def test_coordinates_model_scaled_moved_window_frame_screen_step(folder, monkeypatch):
    monkeypatch.setattr(
        screenshot, "_window_image", lambda wid, extra: fake_image(64, 48)
    )
    monkeypatch.setattr(screenshot, "_app_name", lambda pid: "Demo")
    shot = screenshot.capture(window())
    assert (shot.scale, shot.origin, shot.size) == (2.0, (60.0, 58.0), (64, 48))
    assert shot.to_screen(32, 24) == (76.0, 70.0)  # the factor is 0.5 on a 2x display


def test_screenshot_jpeg_file_temp_folder_named(folder, monkeypatch):
    monkeypatch.setattr(screenshot, "_window_image", lambda wid, extra: fake_image())
    monkeypatch.setattr(screenshot, "_app_name", lambda pid: "Font Book")
    path = screenshot.capture(window()).path
    name = r"Font Book Screenshot \d{4}-\d\d-\d\d at \d{1,2}\.\d\d\.\d\d [AP]M [a-z0-9]{6}\.jpeg"
    assert os.path.dirname(path) == str(folder) and re.fullmatch(
        name, os.path.basename(path)
    )
    assert "Library/Caches/macos-harness/screenshots" in str(
        screenshot.__dict__["Path"].home() / "Library/Caches/macos-harness/screenshots"
    )


def test_screenshot_files_get_short_random_part_tracked_cleanup():
    at = datetime(2026, 10, 11, 23, 50, 57, tzinfo=UTC)
    one, two = screenshot.file_name("Safari", at), screenshot.file_name("Safari", at)
    assert one.startswith("Safari Screenshot 2026-10-11 at 11.50.57 PM ") and one != two
    assert screenshot.RANDOM_CHARACTERS == 6 and len(one.split(" ")[-1]) == 6 + len(
        ".jpeg"
    )
    assert " at 12.05.09 AM " in screenshot.file_name(
        "A", datetime(2026, 1, 2, 0, 5, 9, tzinfo=UTC)
    )
    assert " at 9.05.09 AM " in screenshot.file_name(
        "A", datetime(2026, 1, 2, 9, 5, 9, tzinfo=UTC)
    )


def test_jpeg_quality_value(folder):
    Image = pytest.importorskip("PIL.Image")
    folder.mkdir(parents=True)
    screenshot.write_jpeg(fake_image(), folder / "a.jpeg")
    with Image.open(folder / "a.jpeg") as picture:
        tables = {key: list(table) for key, table in picture.quantization.items()}
        facts = (
            picture.format,
            picture.mode,
            picture.size,
            picture.info.get("progressive"),
        )
    assert screenshot.JPEG_QUALITY == 0.8 and tables == QUALITY_80_TABLES
    assert facts == ("JPEG", "RGB", (64, 48), None)  # baseline, three components


def test_a_kept_screenshot_is_private_capped_and_at_a_path_without_spaces(
    tmp_path, monkeypatch
):
    """It was ~/.macos-harness/proof at 0755 with files 0644, never pruned. Now: in
    the state folder, the folder 0700, each file 0600, the newest PROOF_MAX kept."""
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(screenshot, "PROOF_MAX", 2)
    shot = tmp_path / "Text Edit Screenshot 2026-10-12 at 9.05.03 PM x7k2qd.jpeg"
    shot.write_bytes(b"jpeg")
    kept = [
        screenshot.keep(
            str(shot), "Text Edit", datetime(2026, 10, 12, 21, 5, second, tzinfo=UTC)
        )
        for second in (3, 4, 5)
    ]
    assert kept[0] == str(
        tmp_path / "state/proof/Text-Edit-2026-10-12-21.05.03-x7k2qd.jpeg"
    )
    assert " " not in kept[0] and Path(kept[2]).read_bytes() == b"jpeg"
    folder = Path(kept[2]).parent
    assert folder.stat().st_mode & 0o777 == 0o700
    assert Path(kept[2]).stat().st_mode & 0o777 == 0o600
    assert sorted(file.name for file in folder.iterdir()) == [
        Path(k).name for k in kept[1:]
    ]
    assert screenshot.FOLDER not in folder.parents  # the hourly prune never looks here


def test_a_state_folder_with_a_space_keeps_proof_in_the_home_folder(
    tmp_path, monkeypatch
):
    """A path printed for the model has no spaces: the default state folder is in
    "Application Support"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "MACOS_HARNESS_HOME", str(tmp_path / "Application Support" / "mh")
    )
    assert screenshot.proof_folder() == tmp_path / ".macos-harness" / "proof"


def test_the_hourly_cleanup_also_takes_what_a_dead_capture_left(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot, "FOLDER", tmp_path)
    for name in ("a.jpeg", "capture-1.png", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    screenshot.cleanup(max_age_s=-1)
    assert [file.name for file in tmp_path.iterdir()] == ["notes.txt"]


def test_capture_uses_screencapturekit_streams_private_window_image_call(
    folder, monkeypatch
):
    asked = []
    monkeypatch.setattr(
        screenshot, "_window_image", lambda wid, extra: asked.append((wid, extra))
    )
    with pytest.raises(errors.HarnessError) as failure:
        screenshot.capture(window(), extras=((9, (0.0, 0.0, 10.0, 10.0)),))
    assert asked == [(7, ())]  # by window id, never the screen; no window, no extras
    assert failure.value.message.endswith("error 100: Taking the screenshot failed.")


def test_an_open_menu_or_sheet_is_drawn_over_the_window_where_it_is(
    folder, monkeypatch
):
    """The windows that belong to the captured one are added (an open menu,
    a sheet). The picture covers both; click pixels still map to the screen."""
    Quartz = pytest.importorskip("Quartz")
    images = {7: fake_image(64, 48), 9: fake_image(20, 40)}  # 2 pixels per point
    monkeypatch.setattr(screenshot, "_window_image", lambda wid, extra: images.get(wid))
    monkeypatch.setattr(screenshot, "_app_name", lambda pid: "Demo")
    menu = (80.0, 70.0, 10.0, 20.0)  # sticks out below the window (58 + 24 = 82)
    shot = screenshot.capture(window(), extras=((9, menu), (11, (0.0, 0.0, 5.0, 5.0))))
    assert (shot.scale, shot.origin, shot.size) == (2.0, (60.0, 58.0), (64, 64))
    assert shot.to_screen(50, 50) == (85.0, 83.0)  # inside the menu, below the window
    assert (
        Quartz.CGImageGetHeight(
            Quartz.CGImageSourceCreateImageAtIndex(
                Quartz.CGImageSourceCreateWithURL(
                    __import__("Foundation").NSURL.fileURLWithPath_(shot.path), None
                ),
                0,
                None,
            )
        )
        == 64
    )  # window 11 gave no picture: left out


def test_capture_error_strings(folder, monkeypatch):
    monkeypatch.setattr(screenshot, "_window_image", lambda wid, extra: fake_image())
    with pytest.raises(errors.HarnessError) as failure:
        screenshot.capture(window(frame=(0.0, 0.0, 0.0, 0.0)))
    assert "The screenshot has an unusable size: (64, 48)" in failure.value.message


def test_capture_that_never_answers_is_a_failure_not_a_hang(folder, monkeypatch):
    monkeypatch.setattr(screenshot, "CAPTURE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(screenshot, "_window_image", lambda wid, extra: time.sleep(2))
    started = time.monotonic()
    with pytest.raises(errors.HarnessError, match="Taking the screenshot failed"):
        screenshot.capture(window())
    assert time.monotonic() - started < 1.0


def test_sources_are_tried_in_order_and_only_one_that_hung_rests(folder, monkeypatch):
    asked, slow = [], [0.0]

    def first(window_id, extra_ids):
        asked.append("first")
        time.sleep(slow[0])

    def second(window_id, extra_ids):
        asked.append("second")
        return fake_image()

    monkeypatch.setattr(screenshot, "SOURCES", (first, second))
    monkeypatch.setattr(screenshot, "CAPTURE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(screenshot, "_app_name", lambda pid: "Demo")
    assert screenshot.capture(window()).size == (64, 48)
    screenshot.capture(window())
    # It answered with nothing (a minimised window): asked again the next time.
    assert asked == ["first", "second", "first", "second"]
    slow[0] = 0.5  # now it does not answer in time: its call still hangs
    screenshot.capture(window())
    screenshot.capture(window())
    assert asked[4:] == ["first", "second", "second"]  # left alone for a while


def test_one_failed_picture_does_not_cost_the_next_one(folder, monkeypatch):
    """The only source gave nothing once. The read after it gets its picture."""
    gives = [None, fake_image()]
    monkeypatch.setattr(screenshot, "SOURCES", (lambda wid, extra: gives.pop(0),))
    monkeypatch.setattr(screenshot, "_app_name", lambda pid: "Demo")
    with pytest.raises(errors.HarnessError, match="Taking the screenshot failed"):
        screenshot.capture(window())
    assert screenshot.capture(window()).size == (64, 48)


def test_platform_fact_screencapture_exits_0_even_wrote_nothing(folder, monkeypatch):
    ran = []
    monkeypatch.setattr(
        screenshot, "CAPTURE_TOOL", "/usr/bin/true"
    )  # exits 0, writes nothing
    monkeypatch.setattr(
        screenshot.subprocess, "run", lambda command, **how: ran.append(command)
    )
    assert screenshot._tool_image(7, ()) is None
    assert ran[0][:5] == ["/usr/bin/true", "-x", "-o", "-l", "7"]
    scratch = Path(ran[0][5])
    assert not scratch.name.startswith(".")  # the tool refuses to write a hidden file
    assert scratch.parent == folder and list(folder.iterdir()) == []  # nothing is left


def test_the_picture_is_taken_in_a_process_of_its_own():
    """The window server's call inside the helper is left out on purpose: measured, it
    can leave the helper reconnecting to the capture service for the rest of its life,
    and then no process on the Mac gets a picture that way."""
    assert SHIPPED_SOURCES == (screenshot._tool_image,)


def test_capture_can_target_another_window_add_extra_windows():
    assert screenshot.capture.__defaults__ == (
        (),
    )  # extra windows are accepted, unused


def test_capture_step_can_skip_screenshot_is_the_callers_choice(folder):
    assert not folder.exists()  # nothing is written until capture is called


def test_capture_phases_signposted_name():
    assert screenshot.PHASES == ("Capture Screenshot", "Write Screenshot File")


def test_cleanup_deletes_only_old_screenshots(folder):
    folder.mkdir(parents=True)
    old, new, other = folder / "old.jpeg", folder / "new.jpeg", folder / "keep.txt"
    for path in (old, new, other):
        path.write_bytes(b"x")
    long_ago = time.time() - 3601
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    screenshot.cleanup()
    assert sorted(p.name for p in folder.iterdir()) == ["keep.txt", "new.jpeg"]
    screenshot.cleanup(max_age_s=-1)
    assert sorted(p.name for p in folder.iterdir()) == ["keep.txt"]


def test_cleanup_without_a_folder_does_nothing(folder):
    screenshot.cleanup()
    assert not folder.exists()
