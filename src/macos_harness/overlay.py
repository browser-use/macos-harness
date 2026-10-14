"""What the person sees on the screen: the drawn pointer, the flash around the
control acted on, and the halo around the driven window.

The driver side (LivePointerOverlay) sends one datagram per command and never
waits for anything. The drawing side is a process of its own, so its frames
stay smooth while the driver is busy: it draws click-through windows, glides
the pointer, and leaves when the driver has gone quiet. It never moves the
real pointer and never takes focus.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, NamedTuple

from .pointer import (
    FILL,
    FOG,
    FOG_RADIUS,
    PIVOT,
    PRESS,
    PRESS_ANCHOR,
    REST,
    RIM,
    RIM_WIDTH,
    STILL,
    TINT,
    Bounds,
    Point,
    clamp,
    outline_points,
    plan,
    pose,
    press_scale,
)
from .protocol import own_folder

try:
    import AppKit
    import Quartz
except ImportError:  # pragma: no cover - a non-macOS host still imports the driver
    AppKit = Quartz = None  # type: ignore[assignment]

Rect = tuple[float, float, float, float]  # x, y, width, height; y down from the top
Shape = tuple[float, ...]  # (a, sx, sy, b) of pointer.pose
Window = tuple[int, int, int, Bounds]  # id, owner pid, level, where it is

OFF = {"0", "false", "no", "off"}  # values of MACOS_HARNESS_POINTER that turn it off
STARTS = {"move", "flash", "halo"}  # commands that start the drawing process
RETRY = 5.0  # seconds from the start of a process that crashed to the next start
QUEUE = 1 << 18  # bytes of commands that may wait while the process starts
TITLE = "macOS Harness Pointer"
SIZE = 112.0  # the pointer's window, in points; the click point is its centre
OVERLAY_LEVEL = 102  # above menus
IDLE = 30.0  # seconds without a command until the pointer fades and the process exits
FADE = 0.44  # seconds to fade in or out
SHOW_BLUR = 7.0  # the pointer sharpens from this blur as it fades in
PRESS_GAP = 0.1  # seconds between the pulses of a double click
ACCENT = (255, 127, 39)  # the colour of the flash and the halo
FLASH = (2.0, 4.0)  # line width and corner radius around a control
HALO = (3.0, 16.0)  # line width and corner radius around a window
ACTIVE = 10.0  # seconds the halo stays after the last action
POLL = 0.1  # seconds between two reads of where the driven window is
DONE = 2.0  # seconds from the end of a turn to the pointer's fade
PAUSED = 0.5  # the pointer's opacity while the person has the app
ROCK, SWING = 10.0, 0.685  # while loading: degrees to each side, seconds per swing
ROCK_FRAME = 1 / 30  # seconds between two frames of the rocking
LOADING_FILL = 0.24  # the arrow's fill while loading
ACTIVITIES = {"idle", "loading", "paused"}


def socket_path(folder: str) -> str:
    """The drawing process's address, in the state folder. A socket address holds
    103 bytes; a longer folder falls back to this user's own folder in /tmp."""
    path = os.path.join(folder, "pointer.sock")
    short = f"/tmp/macos-harness-{os.getuid()}/pointer.sock"
    return path if len(os.fsencode(path)) <= 103 else short


def address() -> str:
    """Next to the helper's own socket: an agent's sandbox may hand out a temp
    folder that cannot be written to."""
    from .protocol import state_dir

    return socket_path(str(state_dir()))


# --- driver side ------------------------------------------------------------


class LivePointerOverlay:
    """Tell the drawing process where the agent acts. MACOS_HARNESS_POINTER=0
    (or false, no, off) turns all of it off."""

    def __init__(self) -> None:
        self._socket: socket.socket | None = None

    @property
    def on(self) -> bool:
        return os.environ.get("MACOS_HARNESS_POINTER", "").strip().lower() not in OFF

    def move(
        self,
        x: float,
        y: float,
        *,
        duration: float = 0.16,
        pid: int | None = None,
        window: int = 0,
    ) -> None:
        """Glide to a screen point; `duration` 0 jumps. `pid` and `window` are
        the app and the window acted on: the pointer is stacked just above it."""
        fields = {"x": float(x), "y": float(y), "animate": duration > 0}
        self._send("move", **fields, pid=pid, window=window)

    def click(self, count: int = 1) -> None:
        self._send("press", count=count)

    def flash(
        self,
        frame: Rect,
        *,
        pid: int | None = None,
        window: int = 0,
        within: Rect | None = None,
    ) -> None:
        """Outline the control at `frame`, inside the window at `within`. A pointer
        that is not on the control goes to its centre."""
        within = within and list(within)
        self._send("flash", frame=list(frame), within=within, pid=pid, window=window)

    def halo(self, frame: Rect, *, pid: int | None = None, window: int = 0) -> None:
        """Mark the driven window, for ACTIVE seconds from now."""
        self._send("halo", frame=list(frame), pid=pid, window=window)

    def activity(self, state: str) -> None:
        self._send("activity", state=state)

    def done(self) -> None:
        """The turn is over: the halo goes now, the pointer DONE seconds later."""
        self._send("done")

    def _send(self, cmd: str, **fields: Any) -> None:
        """One datagram, never waited on. With nobody listening, a command that
        draws starts the process. Whatever else fails (a full queue, a folder
        that cannot be written to), the command is dropped: the pointer may be
        late or absent, the action never is."""
        with contextlib.suppress(Exception):
            if not self.on:
                return
            if self._socket is None:
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._socket.setblocking(False)  # a full queue fails at once
            data, where = json.dumps({"cmd": cmd, **fields}).encode(), address()
            own_folder(os.path.dirname(where))  # another user's folder: dropped
            try:
                self._socket.sendto(data, where)
            except (FileNotFoundError, ConnectionRefusedError):
                if cmd in STARTS:
                    _start(where)
                    self._socket.sendto(data, where)


def _start(where: str) -> None:
    """Start the one drawing process, detached, so it outlives this process.

    The lock file is the lock: it is taken here and handed over with the bound
    socket. The process holds both for life and a crash frees them. Commands
    sent while it is still starting wait in the socket. A second starter fails
    the lock and does nothing. The lock file holds the process's pid.

    A process that exits cleanly removes its socket file. One that crashed
    leaves it, dated when it started: if that was under RETRY ago, it could
    not run here (no screen, say), and no process is made until then."""
    serve = [sys.executable, "-m", "macos_harness.overlay", "--serve"]
    os.makedirs(os.path.dirname(where), mode=0o700, exist_ok=True)
    own_folder(os.path.dirname(where))
    lock = os.open(where + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with contextlib.suppress(FileNotFoundError):
            if time.time() - os.stat(where).st_mtime < RETRY:
                return
            os.unlink(where)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, QUEUE)
            listener.bind(where)
            child = subprocess.Popen(
                [*serve, str(listener.fileno()), str(IDLE)],
                pass_fds=(listener.fileno(), lock),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        os.ftruncate(lock, 0)
        os.write(lock, str(child.pid).encode())
    except OSError:
        pass  # another start holds the lock, or no process could be made
    finally:
        os.close(lock)


# --- drawing side: what is where and when, as pure functions ----------------


class Frame(NamedTuple):
    """The pointer at one moment."""

    x: int  # the click point, in whole screen points
    y: int
    shape: Shape
    press: float
    opacity: float
    blur: float
    wake: float  # seconds until the next frame is needed; 0 = the next refresh
    fill: float = FILL  # alpha of the arrow's fill


class Track:
    """The pointer over time: a pure function of the commands so far and of the
    clock the caller brings."""

    def __init__(self, now: float, idle: float = IDLE) -> None:
        self.idle = idle
        self.touched = now  # the last command; the idle clock runs from here
        self.shown: float | None = None  # when it came on screen; None before that
        self.move: dict[str, Any] = {}  # the running move, from pointer.plan
        self.began = 0.0  # when that move started
        self.pulses: list[float] = []  # when each press pulse starts
        self.activity = "idle"  # what the agent does: idle, loading or paused
        self.since = now  # when that began

    def go(
        self, end: Point, now: float, screens: list[Bounds], animate: bool = True
    ) -> None:
        """Send the pointer to `end`. Without `animate` it jumps."""
        here = None if self.shown is None else pose(self.move, now - self.began)[:2]
        bounds = bounds_of(screens, end, here or end)
        end = (clamp(end[0], bounds[0], bounds[2]), clamp(end[1], bounds[1], bounds[3]))
        if here is None:
            self.shown = now  # first show: at the target, fading in
        # The same target again leaves a running move alone.
        if here is None or math.dist(self.move["end"], end) >= STILL:
            self.move = plan(here if here and animate else end, end, bounds)
            self.began = now
            # A pulse not yet played was for the old target.
            self.pulses = [at for at in self.pulses if at <= now]
        self.touched = now

    def lands(self, now: float) -> float:
        """When a pointer that is shown is on its target and seen: after the move
        and the fade in."""
        return max(now, self.began + self.move["arrive"], self.shown + FADE)

    def press(self, count: int, now: float) -> None:
        if self.shown is not None:  # pulses wait for the move and the fade in
            self.pulses = [self.lands(now) + i * PRESS_GAP for i in range(count)]
        self.touched = now

    def act(self, state: str, now: float) -> None:
        """idle: an action runs. loading: the agent thinks. paused: the person
        has the app."""
        if state != self.activity:
            self.activity, self.since = state, now

    def frame(self, now: float) -> Frame | None:
        """What to draw now. None once the idle fade has finished: it is gone."""
        left = self.touched + self.idle + FADE - now  # until the idle fade has ended
        if left <= 0:
            return None
        if self.shown is None:
            return Frame(0, 0, REST, 1.0, 0.0, 0.0, left)
        x, y, *shape = pose(self.move, now - self.began)
        press = min((press_scale(now - at) for at in self.pulses), default=1.0)
        sharp = ease(clamp((now - self.shown) / FADE))  # the fade in
        opacity = min(sharp, ease(clamp(left / FADE)))  # and the idle fade out
        blur = SHOW_BLUR * (1 - sharp)
        ends = [self.began + self.move["duration"], self.shown + FADE]
        ends += [at + PRESS[-1][0] for at in self.pulses]
        rest = max(*ends, self.since)  # nothing has moved it since then
        wake = 0.0 if now < max(ends) or left < FADE else left - FADE
        fill = FILL
        if self.activity == "paused":
            opacity *= PAUSED
        if self.activity == "loading":
            fill = LOADING_FILL
        if self.activity == "loading" and now >= rest:  # the arrow rocks in place
            shape = [ROCK * math.sin(math.pi * (now - rest) / SWING), *REST[1:]]
            wake = min(wake, ROCK_FRAME)
        return Frame(round(x), round(y), tuple(shape), press, opacity, blur, wake, fill)


class Mark:
    """An outline over time: what it is around, when it came, when it goes."""

    def __init__(self) -> None:
        self.box: Rect | None = None  # None: nothing to mark
        self.since = self.until = 0.0  # fades in from `since`, out from `until`
        self.pid: int | None = None  # the app and the window it belongs to
        self.window = 0

    def show(
        self, box: Rect | None, now: float, until: float, appear: float = FADE
    ) -> None:
        """Mark `box` until `until`. One not seen yet takes `appear` seconds to
        come; one that shows already stays as it is."""
        if not appear or not self.alive(now):
            self.since = now - (FADE - appear)
        self.box, self.until = box, until

    def alive(self, now: float) -> bool:
        """It shows, or is fading in or out."""
        return self.box is not None and now < self.until + FADE

    def alpha(self, now: float) -> float:
        if not self.alive(now):
            return 0.0
        coming = ease(clamp((now - self.since) / FADE))
        return min(coming, ease(clamp((self.until + FADE - now) / FADE)))

    def follow(self, windows: list[Window], now: float) -> tuple[int, int] | None:
        """Move the mark onto its window as it is now. Returns where it sits:
        (window level, window to sit just above). None: it does not show, or its
        window has left this Space."""
        if not self.alive(now):
            return None
        x, y, width, height = self.box
        centre = (x + width / 2, y + height / 2)
        target = target_window(centre, self.pid, windows, self.window)
        if target is None:
            return None
        left, top, right, bottom = target[3]
        self.box = (left, top, right - left, bottom - top)
        return target[2], target[0]

    def wake(self, now: float) -> float:
        """Seconds until it changes next; 0 while it fades."""
        if not self.alive(now):
            return math.inf
        return 0.0 if now < self.since + FADE or now >= self.until else self.until - now


class Scene:
    """Everything drawn, over time: the pointer, the flash and the halo. A pure
    function of the commands so far and of the clock the caller brings."""

    def __init__(self, now: float, idle: float = IDLE) -> None:
        self.track = Track(now, idle)
        self.flash, self.halo = Mark(), Mark()
        self.pid: int | None = None  # the app and the window the pointer acts on
        self.window = 0

    def command(
        self,
        command: dict[str, Any],
        now: float,
        screens: list[Bounds],
        calm: bool = False,
    ) -> None:
        """Take one command from read_command. `calm`: the person asked for less
        motion, so the pointer jumps."""
        action, track = command["cmd"], self.track
        if action == "done":
            self.halo.until = min(self.halo.until, now)
            track.touched = min(track.touched, now + DONE - track.idle)
            return
        if action == "move":
            self.pid, self.window = command["pid"], command["window"]
            animate = bool(command.get("animate", True)) and not calm
            track.go((command["x"], command["y"]), now, screens, animate)
        elif action == "flash":
            x, y, width, height = frame = command["frame"]
            # A control acted on with no point of its own: the pointer goes to it.
            if track.shown is None or not holds(frame, track.move["end"]):
                self.pid, self.window = command["pid"], command["window"]
                track.go((x + width / 2, y + height / 2), now, screens, not calm)
            box = flash_box(frame, command.get("within"))
            self.flash.show(box, now, track.lands(now) + PRESS[-1][0], appear=0)
        elif action == "halo":
            self.halo.pid, self.halo.window = command["pid"], command["window"]
            self.halo.show(command["frame"], now, now + ACTIVE)
        elif action == "press":
            track.press(command["count"], now)
        elif action == "activity":
            track.act(command["state"], now)
        track.touched = now

    def wake(self, now: float, frame: Frame) -> float:
        """Seconds until the next frame is needed. While the halo shows, its
        window is looked up every POLL."""
        follow = POLL if self.halo.alive(now) else math.inf
        return min(frame.wake, self.flash.wake(now), self.halo.wake(now), follow)


def ease(u: float) -> float:
    """Ease in and out, 0 to 1."""
    return u * u * (3 - 2 * u)


def grow(box: Rect, by: float) -> Rect:
    x, y, width, height = box
    return (x - by, y - by, width + 2 * by, height + 2 * by)


def holds(box: Rect, point: Point) -> bool:
    x, y, width, height = box
    return x <= point[0] < x + width and y <= point[1] < y + height


def flash_box(frame: Rect, within: Rect | None) -> Rect | None:
    """The control to outline. None for one with no size, and for one over half
    its window: an outline that large marks nothing."""
    area = max(frame[2], 0) * max(frame[3], 0)
    if not area or (within and area > within[2] * within[3] / 2):
        return None
    return frame


def flip_screens(frames: list[Bounds]) -> list[Bounds]:
    """Screen frames as AppKit gives them (x, y, width, height; y up from the
    bottom of the primary screen) as (left, top, right, bottom) with y down from
    its top: the space actions give points in."""
    top = frames[0][1] + frames[0][3]
    return [(x, top - y - height, x + width, top - y) for x, y, width, height in frames]


def bounds_of(screens: list[Bounds], *points: Point) -> Bounds:
    """The union of the screens that hold any of the points; of all screens when
    none does."""

    def holds(screen: Bounds) -> bool:
        left, top, right, bottom = screen
        return any(left <= x < right and top <= y < bottom for x, y in points)

    held = [screen for screen in screens if holds(screen)] or screens
    lefts, tops, rights, bottoms = zip(*held, strict=True)
    return (min(lefts), min(tops), max(rights), max(bottoms))


def window_frame(box: Rect, primary_height: float) -> tuple[Point, Point]:
    """The AppKit frame (origin bottom left, y up; then the size) of a window
    that covers `box`."""
    x, y, width, height = box
    return ((x, primary_height - y - height), (width, height))


def window_origin(x: float, y: float, primary_height: float) -> Point:
    """The AppKit origin of the pointer's window: its centre is the click point
    (x, y)."""
    square = (x - SIZE / 2, y - SIZE / 2, SIZE, SIZE)
    return window_frame(square, primary_height)[0]


def target_window(
    point: Point, pid: int | None, windows: list[Window], window: int = 0
) -> Window | None:
    """The window acted on: the one with the id `window`, else the frontmost
    window of the app `pid` under the point. None: it is not on this Space.
    `windows` are those on screen, front to back."""
    for row in windows:
        (left, top, right, bottom), under = row[3], False
        if not window:
            under = left <= point[0] < right and top <= point[1] < bottom
        if row[0] == window or (row[1] == pid and under):
            return row
    return None


def stacking(
    point: Point,
    pid: int | None,
    frontmost: int | None,
    windows: list[Window],
    window: int = 0,
) -> tuple[int, int] | None:
    """Where the pointer sits: (window level, window to sit just above), where
    window 0 is the front of that level.

    No target app, or the target app in front: above everything, menus too.
    Else just above the window acted on, so whatever covers that window covers
    the pointer as well. None: no such window is on this Space, so the pointer
    stays off screen."""
    if pid is None or pid == frontmost:
        return OVERLAY_LEVEL, 0
    target = target_window(point, pid, windows, window)
    return target and (target[2], target[0])


NOT_A_COMMAND = (ValueError, TypeError, KeyError, OverflowError)


def read_command(data: bytes) -> dict[str, Any]:
    """A datagram as a command, its numbers typed, so that no bad value reaches
    the main thread (an error there ends the process). Raises one of
    NOT_A_COMMAND for anything else."""

    def number(value: Any) -> float:
        if not math.isfinite(value := float(value)):
            raise ValueError("not a place on any screen")
        return value

    command = dict(json.loads(data))
    action = command.get("cmd")
    if action == "move":
        command.update(x=number(command["x"]), y=number(command["y"]))
    if action in ("flash", "halo") or command.get("frame") is not None:
        x, y, width, height = (number(value) for value in command["frame"])
        command["frame"] = (x, y, width, height)
    if command.get("within") is not None:
        x, y, width, height = (number(value) for value in command["within"])
        command["within"] = (x, y, width, height)
    if action == "activity" and command["state"] not in ACTIVITIES:
        raise ValueError("not an activity")
    pid = command.get("pid")
    command["pid"] = None if pid is None else int(pid)
    command["window"] = int(command.get("window") or 0)
    command["count"] = int(command.get("count", 1))
    return command


# --- drawing side: drawing --------------------------------------------------


def draw_pointer(
    at: Point,
    shape: Shape = REST,
    press: float = 1.0,
    *,
    cover_only: bool = False,
    fill: float = FILL,
) -> None:
    """Draw the pointer into the current graphics context, which must be flipped
    (y down), with the click point at `at`.

    Seen = paint x cover. Paint: the fog tint, the arrow fill, the rim. Cover: 1
    inside the arrow, the fog row around it. `cover_only` draws the cover alone:
    the mask of the glass behind the pointer."""
    context = AppKit.NSGraphicsContext.currentContext()
    cg = context.CGContext()
    context.saveGraphicsState()
    a, sx, sy, b = shape
    pose_and_press = AppKit.NSAffineTransform.transform()
    pose_and_press.translateXBy_yBy_(at[0] + PIVOT[0], at[1] + PIVOT[1])
    pose_and_press.rotateByDegrees_(a)
    pose_and_press.scaleXBy_yBy_(sx, sy)
    pose_and_press.rotateByDegrees_(b)
    pose_and_press.translateXBy_yBy_(
        PRESS_ANCHOR[0] - PIVOT[0], PRESS_ANCHOR[1] - PIVOT[1]
    )
    pose_and_press.scaleBy_(press)
    pose_and_press.translateXBy_yBy_(-PRESS_ANCHOR[0], -PRESS_ANCHOR[1])
    pose_and_press.concat()

    white, outline = AppKit.NSColor.whiteColor(), outline_points()
    arrow = AppKit.NSBezierPath.bezierPath()
    arrow.appendBezierPathWithPoints_count_(outline, len(outline))
    arrow.closePath()
    paint_layers = 0 if cover_only else 2
    if paint_layers:
        disc = ((-FOG_RADIUS, -FOG_RADIUS), (2 * FOG_RADIUS, 2 * FOG_RADIUS))
        Quartz.CGContextBeginTransparencyLayer(cg, None)
        AppKit.NSColor.blackColor().colorWithAlphaComponent_(TINT).set()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(disc).fill()
        white.colorWithAlphaComponent_(fill).set()
        arrow.fill()
        white.colorWithAlphaComponent_(RIM).set()
        arrow.setLineWidth_(RIM_WIDTH)
        arrow.setMiterLimit_(10)
        arrow.stroke()
        # Keep the paint only where the cover, drawn next, is.
        Quartz.CGContextSetBlendMode(cg, Quartz.kCGBlendModeDestinationIn)
        Quartz.CGContextBeginTransparencyLayer(cg, None)
    fog = AppKit.NSGradient.alloc().initWithColors_atLocations_colorSpace_(
        [white.colorWithAlphaComponent_(alpha) for _, alpha in FOG],
        [radius / FOG_RADIUS for radius, _ in FOG],
        AppKit.NSColorSpace.sRGBColorSpace(),
    )
    fog.drawFromCenter_radius_toCenter_radius_options_((0, 0), 0, (0, 0), FOG_RADIUS, 0)
    white.set()
    arrow.fill()
    for _ in range(paint_layers):
        Quartz.CGContextEndTransparencyLayer(cg)
    context.restoreGraphicsState()


def draw_outline(box: Rect, width: float, radius: float) -> None:
    """Stroke the accent line into the current graphics context, in the `width`
    points just outside `box`: the mark never covers what it marks."""
    (x, y, w, h), bend = grow(box, width / 2), radius + width / 2
    line = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((x, y), (w, h)), bend, bend
    )
    line.setLineWidth_(width)
    red, green, blue = (part / 255 for part in ACCENT)
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(red, green, blue, 1).set()
    line.stroke()


def pointer_image(
    shape: Shape = REST,
    press: float = 1.0,
    *,
    cover_only: bool = False,
    fill: float = FILL,
) -> Any:
    """The pointer as a SIZE x SIZE image, click point at its centre, sharp at
    any screen scale."""

    def draw(_rect: Any) -> bool:
        at = (SIZE / 2, SIZE / 2)
        draw_pointer(at, shape, press, cover_only=cover_only, fill=fill)
        return True

    return AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        (SIZE, SIZE), True, draw
    )


def outline_image(size: Point, width: float, radius: float) -> Any:
    """The line around a box, as an image `width` larger than the box all round."""

    def draw(_rect: Any) -> bool:
        box = (width, width, size[0] - 2 * width, size[1] - 2 * width)
        draw_outline(box, width, radius)
        return True

    return AppKit.NSImage.imageWithSize_flipped_drawingHandler_(size, True, draw)


def panel() -> Any:
    """A window that only shows: it takes no clicks, no keys and no focus, and it
    is off screen until it is ordered in."""
    made = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (SIZE, SIZE)),
        AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered,
        True,
    )
    made.setTitle_(TITLE)
    made.setOpaque_(False)
    made.setBackgroundColor_(AppKit.NSColor.clearColor())
    made.setHasShadow_(False)
    made.setIgnoresMouseEvents_(True)
    made.setHidesOnDeactivate_(False)
    made.setReleasedWhenClosed_(False)
    # It comes and goes by its own fades only.
    made.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
    made.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )
    return made


def pointer_panel() -> tuple[Any, Any, Any]:
    """The pointer's window with its glass and its picture."""
    made, frame = panel(), AppKit.NSMakeRect(0, 0, SIZE, SIZE)
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    glass = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame)
    glass.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)  # it tints least
    glass.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    glass.setState_(AppKit.NSVisualEffectStateActive)
    glass.setHidden_(workspace.accessibilityDisplayShouldReduceTransparency())
    picture = AppKit.NSImageView.alloc().initWithFrame_(frame)
    picture.setWantsLayer_(True)
    picture.setLayerUsesCoreImageFilters_(True)  # for the blur of the fade in
    for view in (glass, picture):
        made.contentView().addSubview_(view)
    return made, glass, picture


def outline_panel() -> tuple[Any, Any]:
    """A window for one outline, with the picture that fills it."""
    made = panel()
    picture = AppKit.NSImageView.alloc().initWithFrame_(made.contentView().bounds())
    picture.setImageScaling_(AppKit.NSImageScaleNone)
    picture.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    made.contentView().addSubview_(picture)
    return made, picture


def screens() -> list[Bounds]:
    frames = [screen.frame() for screen in AppKit.NSScreen.screens()]
    return flip_screens([(*frame.origin, *frame.size) for frame in frames])


def windows_on_screen() -> list[Window]:
    """The windows on this Space, front to back."""
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return [
        (
            int(row[Quartz.kCGWindowNumber]),
            int(row[Quartz.kCGWindowOwnerPID]),
            int(row[Quartz.kCGWindowLayer]),
            (box["X"], box["Y"], box["X"] + box["Width"], box["Y"] + box["Height"]),
        )
        for row in rows or []
        for box in [row[Quartz.kCGWindowBounds]]
    ]


class _Overlay:  # pragma: no cover - runs in the drawing process
    """Three windows, one Scene, one timer. Everything but `listen` runs on the
    main thread."""

    def __init__(self, idle: float, address: str) -> None:
        self.scene = Scene(time.monotonic(), idle)
        self.address = address  # the socket file
        self.panel, self.glass, self.picture = pointer_panel()
        self.flash, self.halo = outline_panel(), outline_panel()
        self.sizes: dict[Any, Point] = {}  # the size each outline is drawn at
        self.screens = screens()
        self.look: Any = None  # the (shape, press, blur, fill) on screen
        self.timer: Any = None
        self.due = False  # a drawing is booked for the commands that came
        self.polled = 0.0  # when the windows were last looked up
        rates = [
            screen.maximumFramesPerSecond() for screen in AppKit.NSScreen.screens()
        ]
        self.refresh = 1 / max(rates)
        self.workspace = AppKit.NSWorkspace.sharedWorkspace()
        for change in (
            AppKit.NSWorkspaceActiveSpaceDidChangeNotification,
            AppKit.NSWorkspaceDidActivateApplicationNotification,
        ):
            self.workspace.notificationCenter().addObserverForName_object_queue_usingBlock_(
                change, None, AppKit.NSOperationQueue.mainQueue(), self.restack
            )

    def listen(self, listener: socket.socket) -> None:
        """Hand each command to the main thread. Runs on a thread of its own."""
        main = AppKit.NSOperationQueue.mainQueue()
        while True:
            data = listener.recv(2048)
            with contextlib.suppress(*NOT_A_COMMAND):  # dropped here
                command = read_command(data)
                main.addOperationWithBlock_(
                    lambda command=command: self.command(command)
                )

    def command(self, command: dict[str, Any]) -> None:
        self.screens = screens()
        calm = self.workspace.accessibilityDisplayShouldReduceMotion()
        self.scene.command(command, time.monotonic(), self.screens, calm)
        if not self.due:  # one drawing for all the commands that wait in line
            self.due = True
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(self.draw)

    def draw(self) -> None:
        self.due = False
        self.tick()  # place it and draw it first, then let it be seen
        self.restack()

    def leave(self) -> None:
        """Exit, and remove the socket file so the next command starts a process
        at once. The windows go with the process."""
        with contextlib.suppress(OSError):
            os.unlink(self.address)
        os._exit(0)

    def restack(self, _note: Any = None) -> None:
        """Order each window for its target. Runs on a command, on a Space
        change, on an app switch, and every POLL while the halo shows."""
        scene, now = self.scene, time.monotonic()
        self.polled, windows, place = now, windows_on_screen(), None
        if scene.track.shown is not None:
            front = self.workspace.frontmostApplication()
            frontmost = front and int(front.processIdentifier())
            aim = scene.track.move["end"]
            place = stacking(aim, scene.pid, frontmost, windows, scene.window)
        self.order(self.panel, place, 0)
        # The flash sits right under the pointer.
        under = self.panel.windowNumber() if scene.flash.alive(now) else 0
        self.order(self.flash[0], place and under and (place[0], under), 1)
        self.order(self.halo[0], scene.halo.follow(windows, now), 0)

    def order(self, window: Any, place: Any, below: int) -> None:
        """Put a window at (level, just above that window), or just below it
        with `below`; off screen with no place."""
        if not place:
            window.orderOut_(None)
            return
        how = AppKit.NSWindowBelow if below else AppKit.NSWindowAbove
        window.setLevel_(place[0])
        window.orderWindow_relativeTo_(how, place[1])

    def outline(self, mark: Mark, drawn: Any, look: Point, now: float) -> None:
        """Size, place and fade one outline's window."""
        window, picture = drawn
        window.setAlphaValue_(mark.alpha(now))
        if mark.box is None:
            return
        origin, size = window_frame(grow(mark.box, look[0]), self.screens[0][3])
        window.setFrame_display_((origin, size), True)
        if self.sizes.get(window) != size:
            self.sizes[window] = size
            picture.setImage_(outline_image(size, *look))

    def tick(self, _timer: Any = None) -> None:
        """Draw the frame of this moment and book the next one."""
        now = time.monotonic()
        frame = self.scene.track.frame(now)
        if frame is None:
            self.leave()  # idle: nothing outlives a driver that has gone quiet
        if now - self.polled >= POLL and self.scene.halo.alive(now):
            self.restack()
        origin = window_origin(frame.x, frame.y, self.screens[0][3])
        self.panel.setFrameOrigin_(origin)
        self.panel.setAlphaValue_(frame.opacity)
        if (frame.shape, frame.press, frame.blur, frame.fill) != self.look:
            self.look = (frame.shape, frame.press, frame.blur, frame.fill)
            mask = pointer_image(frame.shape, frame.press, cover_only=True)
            image = pointer_image(frame.shape, frame.press, fill=frame.fill)
            self.picture.setImage_(image)
            self.glass.setMaskImage_(mask)
            blur = Quartz.CIFilter.filterWithName_withInputParameters_(
                "CIGaussianBlur", {"inputRadius": frame.blur}
            )
            self.picture.setContentFilters_([blur] if frame.blur else [])
        self.outline(self.scene.flash, self.flash, FLASH, now)
        self.outline(self.scene.halo, self.halo, HALO, now)
        if self.timer is not None:
            self.timer.invalidate()
        wait = max(self.scene.wake(now, frame), self.refresh)
        self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            wait, False, self.tick
        )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "--serve":
        print("macos_harness.overlay is an internal process", file=sys.stderr)
        return 2
    app = AppKit.NSApplication.sharedApplication()
    # Never active, never frontmost, never among the apps.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    listener = socket.socket(fileno=int(args[1]))
    overlay = _Overlay(float(args[2]), listener.getsockname())
    threading.Thread(target=overlay.listen, args=(listener,), daemon=True).start()
    overlay.tick()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
