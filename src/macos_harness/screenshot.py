"""One window as a JPEG file, and how its pixels map back to the screen.

The picture is the target window alone, at full backing resolution, without its
shadow, taken by window id. So a covered window still gives its own pixels, and nothing
we draw on top (pointer, halo) is in it. The system's own capture tool takes it, in a
process of its own (see SOURCES for why).
"""

from __future__ import annotations

import logging
import secrets
import string
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import errors
from .model import Rect, Shot, WindowRef

log = logging.getLogger(__name__)

JPEG_QUALITY = 0.8
RANDOM_CHARACTERS = 6  # in the file name, so two captures in one second do not collide
MAX_AGE_S = 3600
PROOF_MAX = 200  # kept proof screenshots; the oldest go first
# The capture tool answers in about 80 ms.
CAPTURE_TIMEOUT_S = 2.0  # how long one way of taking the picture may stay silent
TOOL_TIMEOUT_S = 1.8  # the capture tool, inside that
RETRY_AFTER_S = 60.0  # a way that failed is not asked again before this
CAPTURE_TOOL = "/usr/sbin/screencapture"
# Not in the temp folder: an agent's sandbox gives each process its own.
FOLDER = Path.home() / "Library" / "Caches" / "macos-harness" / "screenshots"
PHASES = ("Capture Screenshot", "Write Screenshot File")  # what the log lines name


def _app_name(pid: int) -> str:
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return str(app.localizedName()) if app is not None else "Unknown"
    except Exception:  # noqa: BLE001 the name only labels the file
        return "Unknown"


def file_name(app_name: str, now: datetime | None = None) -> str:
    """`<App> Screenshot <date> at <time> <6 random characters>.jpeg`"""
    now = now or datetime.now().astimezone()  # the person's own clock
    clock = f"{now.hour % 12 or 12}.{now:%M.%S} {'AM' if now.hour < 12 else 'PM'}"
    alphabet = string.ascii_lowercase + string.digits
    tail = "".join(secrets.choice(alphabet) for _ in range(RANDOM_CHARACTERS))
    return f"{app_name} Screenshot {now:%Y-%m-%d} at {clock} {tail}.jpeg"


def proof_folder() -> Path:
    """Where kept screenshots live: in the state folder, so that only this user reads
    them. A path printed for the model must have no spaces, and the default state
    folder has one: then ~/.macos-harness is used."""
    from .protocol import state_dir

    state = state_dir()
    return (
        state if " " not in str(state) else Path.home() / ".macos-harness"
    ) / "proof"


def keep(path: str, app_name: str, now: datetime | None = None) -> str:
    """A copy of a screenshot that the hourly cleanup leaves, for proof of work:
    `<proof folder>/Demo-2026-10-12-21.05.03-x7k2qd.jpeg`, readable by this user
    only. The newest PROOF_MAX stay."""
    import os
    import re
    import shutil

    now = now or datetime.now().astimezone()
    app = re.sub(r"[^A-Za-z0-9]+", "-", app_name).strip("-") or "App"
    tail = Path(path).stem.rsplit(" ", 1)[-1]  # the random characters of the name
    folder = proof_folder()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    kept = folder / f"{app}-{now:%Y-%m-%d-%H.%M.%S}-{tail}.jpeg"
    shutil.copyfile(path, kept)
    os.chmod(kept, 0o600)
    old = sorted(folder.glob("*.jpeg"), key=lambda file: file.stat().st_mtime)
    for file in old[:-PROOF_MAX]:
        file.unlink(missing_ok=True)
    return str(kept)


def _window_image(window_id: int, extra_ids: tuple[int, ...]):
    """The window's own pixels from the window server. None when it gives nothing."""
    import Quartz

    # TODO: add the extra windows (sheets, menus) here.
    # Until then the key window is captured alone and extra_ids is not used.
    options = (
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution
    )
    return Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window_id, options
    )


def _within(seconds: float, call, *args) -> tuple[bool, object]:
    """(answered in time, the call's result).

    A capture call can stay silent for 40 s and then give nothing (measured). A state
    read must not hang on its picture, so the call runs on a thread we stop waiting for.
    """
    answer: list = []
    worker = threading.Thread(target=lambda: answer.append(call(*args)), daemon=True)
    worker.start()
    worker.join(seconds)
    return bool(answer), answer[0] if answer else None


def _tool_image(window_id: int, extra_ids: tuple[int, ...]):
    """The window's own pixels through the system's capture tool, as one PNG file."""
    import Quartz
    from Foundation import NSURL

    FOLDER.mkdir(parents=True, exist_ok=True)
    # Not a hidden name: the tool refuses to write one, and still exits 0.
    path = FOLDER / f"capture-{secrets.token_hex(4)}.png"
    command = [CAPTURE_TOOL, "-x", "-o", "-l", str(window_id), str(path)]
    try:
        subprocess.run(
            command, capture_output=True, timeout=TOOL_TIMEOUT_S, check=False
        )
        # Only the file counts: exit 0 does not say that one was written.
        if not path.is_file() or not path.stat().st_size:
            return None
        source = Quartz.CGImageSourceCreateWithURL(
            NSURL.fileURLWithPath_(str(path)), None
        )
        now = {Quartz.kCGImageSourceShouldCacheImmediately: True}  # the file goes
        return source and Quartz.CGImageSourceCreateImageAtIndex(source, 0, now)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        path.unlink(missing_ok=True)


# The ways to take the picture, tried in this order. Only the tool: it takes one frame
# through the system's capture service in a process of its own, which can be ended.
# _window_image, the window server's call inside this process, takes 15 ms where the
# tool takes 80, and is left out on purpose. Measured three times on macOS 27.0: the
# capture service drops the line of a process that calls it, the system library then
# reconnects without pause, thousands of times a second, for as long as the process
# lives, and while one such process lives the call hangs in every other process.
SOURCES = (_tool_image,)
_silent_until: dict = {}  # source -> when it may be asked again after it did not answer


def _first_image(window_id: int, extra_ids: tuple[int, ...]):
    """The first source that gives a picture.

    One that did not answer in time is left alone for a while: its call still hangs.
    One that answered with nothing (a minimised window) is asked again next time.
    """
    for source in SOURCES:
        if time.monotonic() < _silent_until.get(source, 0.0):
            continue
        answered, image = _within(CAPTURE_TIMEOUT_S, source, window_id, extra_ids)
        if image is not None:
            return image
        if not answered:
            _silent_until[source] = time.monotonic() + RETRY_AFTER_S
    return None


def write_jpeg(image, path: Path) -> None:
    import Quartz
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    destination = Quartz.CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
    quality = {Quartz.kCGImageDestinationLossyCompressionQuality: JPEG_QUALITY}
    if destination is None:
        raise errors.screenshot_failed()
    Quartz.CGImageDestinationAddImage(destination, image, quality)
    if not Quartz.CGImageDestinationFinalize(destination):
        raise errors.screenshot_failed()


def _composed(base, frame: Rect, extras: list[tuple[object, Rect]], scale: float):
    """One picture of the window and the windows over it (menus, sheets), each drawn
    where it is on screen. Returns (image, origin of the whole in global points)."""
    import Quartz

    frames = [frame, *(f for _, f in extras)]
    left, top = min(f[0] for f in frames), min(f[1] for f in frames)
    right = max(f[0] + f[2] for f in frames)
    bottom = max(f[1] + f[3] for f in frames)
    width, height = round((right - left) * scale), round((bottom - top) * scale)
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, space, Quartz.kCGImageAlphaPremultipliedFirst
    )
    Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
    Quartz.CGContextFillRect(context, ((0, 0), (width, height)))
    for image, (x, y, w, h) in [(base, frame), *extras]:  # back to front
        place = ((x - left) * scale, (bottom - y - h) * scale)  # the context counts up
        Quartz.CGContextDrawImage(context, (place, (w * scale, h * scale)), image)
    return Quartz.CGBitmapContextCreateImage(context), (left, top)


def capture(window: WindowRef, extras: tuple[tuple[int, Rect], ...] = ()) -> Shot:
    """Take the picture and write the file. A failure is reported as it is. Extra
    windows (id, frame) that give a picture are drawn over the window."""
    import Quartz

    cleanup()
    started = time.monotonic()
    image = _first_image(window.window_id, ())
    if image is None:
        raise errors.screenshot_failed()
    size = (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image))
    x, y, width, _height = window.frame
    if not (size[0] and size[1] and width > 0):
        raise errors.screenshot_size(size)
    scale, origin = size[0] / width, (x, y)
    drawn = [(i, frame) for wid, frame in extras if (i := _first_image(wid, ()))]
    if drawn:
        image, origin = _composed(image, window.frame, drawn, scale)
        size = (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image))
    log.debug("%s: %.0f ms", PHASES[0], (time.monotonic() - started) * 1000)
    started = time.monotonic()
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = FOLDER / file_name(_app_name(window.pid))
    write_jpeg(image, path)
    log.debug("%s: %.0f ms", PHASES[1], (time.monotonic() - started) * 1000)
    return Shot(path=str(path), scale=scale, origin=origin, size=size)


def cleanup(max_age_s: float = MAX_AGE_S) -> None:
    """Delete our screenshot files that are older than an hour."""
    oldest = time.time() - max_age_s
    files = [*FOLDER.glob("*.jpeg"), *FOLDER.glob("capture-*.png")]  # and what a
    for path in files if FOLDER.is_dir() else ():  # capture that died left behind
        try:
            if path.stat().st_mtime < oldest:
                path.unlink()
        except OSError:  # another call removed it first
            continue
