# ruff: noqa: F811 - a fixture arrives by import and is asked for by its name
from __future__ import annotations

import fcntl
import io
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import AppKit
import pytest
import Quartz
from fakes import BackgroundApp
from fakes_i import (  # noqa: F401 - the last two are fixtures
    Listener,
    alike,
    arrow_box,
    listener,
    pointer_socket,
)
from PIL import Image

import macos_harness.overlay as overlay_module
from macos_harness.overlay import (
    ACCENT,
    ACTIVE,
    DONE,
    FADE,
    FLASH,
    HALO,
    IDLE,
    LOADING_FILL,
    NOT_A_COMMAND,
    OVERLAY_LEVEL,
    PAUSED,
    ROCK,
    SIZE,
    SWING,
    TITLE,
    LivePointerOverlay,
    Mark,
    Scene,
    Track,
    bounds_of,
    draw_outline,
    draw_pointer,
    flash_box,
    flip_screens,
    grow,
    outline_panel,
    pointer_panel,
    read_command,
    socket_path,
    stacking,
    target_window,
    window_frame,
    window_origin,
)
from macos_harness.pointer import FILL, PIVOT, REST, outline_points, pose

FIXTURES = Path(__file__).parent / "fixtures" / "pointer"
SCREEN = [(0.0, 0.0, 1440.0, 900.0)]
BOTH_SCREENS = (-1920, -180, 1440, 900)


# --- the driver side: datagrams, never waited on ----------------------------


def test_commands_are_one_small_datagram_each(listener: Listener) -> None:
    pointer = LivePointerOverlay()

    pointer.move(100, 200, duration=0.25, pid=42, window=7)
    pointer.click(2)
    pointer.move(5, 6, duration=0)

    assert listener.commands() == [
        {"cmd": "move", **at(100, 200), "animate": True, "pid": 42, "window": 7},
        {"cmd": "press", "count": 2},
        {"cmd": "move", **at(5, 6), "animate": False, "pid": None, "window": 0},
    ]


def at(x: float, y: float) -> dict:
    return {"x": float(x), "y": float(y)}


def test_the_marks_and_the_activity_are_one_small_datagram_each(
    listener: Listener,
) -> None:
    pointer = LivePointerOverlay()
    frame, within = (10, 20, 30, 40), (0, 0, 800, 600)

    pointer.flash(frame, pid=42, window=7, within=within)
    pointer.flash(frame)
    pointer.halo(within, pid=42, window=7)
    pointer.activity("loading")
    pointer.done()

    where = {"pid": 42, "window": 7}
    nowhere = {"pid": None, "window": 0}
    assert listener.commands() == [
        {
            "cmd": "flash",
            "frame": [10, 20, 30, 40],
            "within": [0, 0, 800, 600],
            **where,
        },
        {"cmd": "flash", "frame": [10, 20, 30, 40], "within": None, **nowhere},
        {"cmd": "halo", "frame": [0, 0, 800, 600], **where},
        {"cmd": "activity", "state": "loading"},
        {"cmd": "done"},
    ]


def test_pointer_turned_off_sends_nothing_and_starts_nothing(
    pointer_socket: str, monkeypatch
) -> None:
    def no_helper(*args, **kwargs):
        pytest.fail("no helper may start")

    monkeypatch.setenv("MACOS_HARNESS_POINTER", "0")
    monkeypatch.setattr(overlay_module.subprocess, "Popen", no_helper)
    pointer = LivePointerOverlay()

    pointer.move(1, 2)
    pointer.click()
    pointer.flash((1, 2, 3, 4))
    pointer.halo((1, 2, 3, 4), pid=1)
    pointer.activity("idle")
    pointer.done()

    assert not os.path.exists(pointer_socket)
    assert not os.path.exists(pointer_socket + ".lock")


@pytest.mark.parametrize(
    ("switch", "on"),
    [("0", False), ("false", False), (" Off ", False), ("no", False)]
    + [("", True), ("1", True), ("yes", True)],
)
def test_the_switch_reads_like_the_other_harnesses(
    switch: str, on: bool, monkeypatch
) -> None:
    monkeypatch.setenv("MACOS_HARNESS_POINTER", switch)

    assert LivePointerOverlay().on is on


def test_a_state_folder_too_long_for_a_socket_address_falls_back_to_tmp() -> None:
    short, long = "/Users/x/Library/Application Support/macos-harness", "/" + "x" * 95
    assert socket_path(short) == short + "/pointer.sock"
    # 103 bytes is the longest socket address; past it the pointer would be off.
    assert socket_path(long) == f"/tmp/macos-harness-{os.getuid()}/pointer.sock"


def test_a_folder_that_cannot_be_written_never_fails_the_action(
    pointer_socket: str, monkeypatch
) -> None:
    # A sandboxed agent, or a read-only or full temp folder.
    monkeypatch.setattr(overlay_module, "address", lambda: "/var/empty/x/p.sock")
    monkeypatch.setattr(overlay_module.subprocess, "Popen", _Started())
    pointer = LivePointerOverlay()

    pointer.move(1, 2)
    pointer.click()


class _Started:
    """Stands in for subprocess.Popen and keeps the files the helper would inherit."""

    def __init__(self, keep: bool = True) -> None:
        self.keep = keep  # False: the helper dies at once
        self.calls: list[tuple[list[str], dict, list[int]]] = []

    def __call__(self, argv: list[str], **kwargs) -> SimpleNamespace:
        kept = [os.dup(fd) for fd in kwargs["pass_fds"]] if self.keep else []
        self.calls.append((argv, kwargs, kept))
        return SimpleNamespace(pid=4000 + len(self.calls))


def _lock_is_free(path: str) -> bool:
    lock = os.open(path + ".lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(lock)
    return True


def test_with_no_helper_one_is_started_detached_and_only_one(
    pointer_socket: str, monkeypatch
) -> None:
    started = _Started()
    monkeypatch.setattr(overlay_module.subprocess, "Popen", started)

    LivePointerOverlay().move(10, 20)

    ((argv, kwargs, (inherited, lock)),) = started.calls
    assert argv[1:4] == ["-m", "macos_harness.overlay", "--serve"]
    assert kwargs["start_new_session"] is True  # the driver's exit does not end it
    # The first command waits in the socket the helper inherits; the lock is its.
    waiting = socket.socket(fileno=inherited)
    assert json.loads(waiting.recv(2048))["cmd"] == "move"
    assert not _lock_is_free(pointer_socket)
    with open(pointer_socket + ".lock") as pid_file:
        assert pid_file.read() == "4001"  # the helper's pid, for whoever looks

    LivePointerOverlay().move(30, 40)  # a second driver reuses the helper
    os.unlink(pointer_socket)  # a start that races one in progress does nothing
    LivePointerOverlay().move(50, 60)
    assert len(started.calls) == 1

    # The helper crashes after a while: its lock is free and its socket file stale.
    waiting.close()
    os.close(lock)
    assert _lock_is_free(pointer_socket)
    Listener(pointer_socket).socket.close()
    os.utime(pointer_socket, (0, 0))
    LivePointerOverlay().move(70, 80)
    assert len(started.calls) == 2
    replacement = socket.socket(fileno=started.calls[1][2][0])
    assert json.loads(replacement.recv(2048))["x"] == 70.0


def test_a_burst_sent_while_the_process_starts_waits_in_its_socket(
    pointer_socket: str, monkeypatch
) -> None:
    started = _Started()
    monkeypatch.setattr(overlay_module.subprocess, "Popen", started)
    pointer = LivePointerOverlay()

    for i in range(500):  # far more than one action sends
        pointer.move(i, i)

    waiting = Listener.__new__(Listener)
    waiting.socket = socket.socket(fileno=started.calls[0][2][0])
    waiting.socket.setblocking(False)
    assert [command["x"] for command in waiting.commands()] == list(range(500))


def test_a_helper_that_cannot_run_is_not_started_again_by_every_move(
    pointer_socket: str, monkeypatch
) -> None:
    started = _Started(keep=False)
    monkeypatch.setattr(overlay_module.subprocess, "Popen", started)
    pointer = LivePointerOverlay()

    for i in range(20):
        pointer.move(i, i)
    assert len(started.calls) == 1

    os.utime(pointer_socket, (0, 0))  # RETRY seconds have passed
    pointer.move(1, 1)
    assert len(started.calls) == 2


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"\xff",
        b"[1, 2]",
        b'{"cmd": "move"}',
        b'{"cmd": "move", "x": "a", "y": 1}',
        b'{"cmd": "move", "x": null, "y": 1}',
        b'{"cmd": "move", "y": 1, "x": 1%s}' % (b"0" * 400),  # too large for a float
        b'{"cmd": "move", "x": NaN, "y": 1}',  # no such place
        b'{"cmd": "press", "count": "x"}',
        b'{"cmd": "press", "count": 1e999}',
        b'{"cmd": "flash"}',
        b'{"cmd": "flash", "frame": [1, 2, 3]}',
        b'{"cmd": "flash", "frame": [1, 2, 3, "a"]}',
        b'{"cmd": "flash", "frame": [1, 2, 3, 4], "within": 5}',
        b'{"cmd": "halo", "frame": null, "pid": 1}',
        b'{"cmd": "halo", "frame": [1, 2, 3, 4], "pid": "x"}',
        b'{"cmd": "halo", "frame": [1, 2, 3, 4], "window": [1]}',
        b'{"cmd": "activity"}',
        b'{"cmd": "activity", "state": "dancing"}',
    ],
)
def test_a_datagram_that_is_not_a_command_is_dropped_before_the_main_thread(
    data: bytes,
) -> None:
    with pytest.raises(NOT_A_COMMAND):
        read_command(data)


def test_a_command_has_its_numbers_typed() -> None:
    move = b'{"cmd": "move", "x": 1, "y": -2, "animate": false, "pid": 9}'
    press = b'{"cmd": "press"}'

    flash = b'{"cmd": "flash", "frame": [1, 2, 3, 4], "within": [0, 0, 9, 9]}'
    typed = {"pid": None, "window": 0, "count": 1}

    assert read_command(move) == {
        "cmd": "move",
        "x": 1.0,
        "y": -2.0,
        "animate": False,
        "pid": 9,
        "window": 0,
        "count": 1,
    }
    assert read_command(press) == {"cmd": "press", **typed}
    assert read_command(flash) == {
        "cmd": "flash",
        "frame": (1.0, 2.0, 3.0, 4.0),
        "within": (0.0, 0.0, 9.0, 9.0),
        **typed,
    }


def test_full_queue_or_dead_helper_never_holds_or_fails_the_action(
    listener: Listener, monkeypatch
) -> None:
    pointer = LivePointerOverlay()

    began = time.perf_counter()
    for i in range(2000):  # nobody reads: the queue fills
        pointer.move(i, i)
    assert time.perf_counter() - began < 0.5
    assert 0 < len(listener.commands()) < 2000  # the rest were dropped, not awaited

    def no_process(*args, **kwargs):
        raise OSError("no more processes")

    listener.socket.close()  # the helper is dead, and a new one cannot be made
    monkeypatch.setattr(overlay_module.subprocess, "Popen", no_process)
    pointer.move(1, 2)
    pointer.click()


def test_a_driver_that_exits_sends_no_quit(
    listener: Listener, pointer_socket: str
) -> None:
    one_shot_call = (
        "import macos_harness.overlay as overlay;"
        f"overlay.address = lambda: {pointer_socket!r};"
        "overlay.LivePointerOverlay().move(1, 2)"
    )

    subprocess.run(
        [sys.executable, "-c", one_shot_call],
        check=True,
        env={**os.environ, "MACOS_HARNESS_POINTER": "1"},
    )

    # No quit: the pointer outlives the call that showed it.
    assert [command["cmd"] for command in listener.commands()] == ["move"]


# --- the pointer over time: commands and a clock in, frames out -------------


def _shown(at: tuple[float, float]) -> Track:
    """A pointer that came on screen at t = 0."""
    track = Track(0.0)
    track.go(at, 0.0, SCREEN)
    return track


def test_first_show_appears_at_the_target_and_fades_in() -> None:
    track = _shown((700, 400))

    frames = [track.frame(t) for t in (0.0, FADE / 2, FADE)]

    assert [(f.x, f.y, f.shape) for f in frames] == [(700, 400, REST)] * 3  # no glide
    assert [(f.opacity, f.blur) for f in frames] == [(0, 7), (0.5, 3.5), (1, 0)]
    # It animates while it fades in; after that nothing is drawn until idle.
    assert frames[0].wake == 0
    assert frames[2].wake == pytest.approx(IDLE - FADE)


def test_same_target_keeps_the_running_move_and_a_new_one_starts_where_it_is() -> None:
    track = _shown((100, 100))
    track.go((900, 600), 10.0, SCREEN)
    running = track.move

    track.go((900.3, 600), 10.1, SCREEN)
    assert track.move is running
    assert track.began == 10.0

    track.go((300, 200), 10.12, SCREEN)
    assert track.move["start"] == pytest.approx(pose(running, 0.12)[:2])
    assert track.began == 10.12
    _, _, a, sx, _, b = pose(track.move, 0.0)
    assert (a + b, sx) == pytest.approx((0, 1))  # it turns from the rest heading again


def test_a_chain_of_ten_actions_ends_exactly_on_the_last_target() -> None:
    track = Track(0.0)
    targets = [(100 + 97 * i, 100 + 53 * (i % 3)) for i in range(10)]

    for i, target in enumerate(targets):
        track.go(target, i * 0.05, SCREEN)
        track.press(1, i * 0.05)

    last = track.frame(3.0)
    assert (last.x, last.y) == targets[-1]
    assert (last.shape, last.press, last.opacity) == (REST, 1.0, 1.0)
    assert last.wake > 0  # nothing is still animating


def test_without_animation_the_pointer_jumps() -> None:
    track = _shown((100, 100))

    track.go((900, 600), 5.0, SCREEN, animate=False)

    frame = track.frame(5.0)
    assert (frame.x, frame.y, frame.shape) == (900, 600, REST)
    assert frame.wake > 0


def test_a_target_outside_every_screen_waits_at_the_edge() -> None:
    track = _shown((700, 400))
    track.go((1600, -40), 1.0, SCREEN)
    assert track.move["end"] == (1440, 0)

    first = Track(0.0)
    first.go((-50, 2000), 0.0, SCREEN)
    assert (first.frame(0.0).x, first.frame(0.0).y) == (0, 900)


def test_press_pulses_when_the_glide_arrives_once_per_press() -> None:
    track = _shown((100, 100))
    track.go((900, 600), 1.0, SCREEN)
    track.press(2, 1.0)  # sent with the move, before the mouse events
    arrive = 1.0 + track.move["arrive"]

    assert track.frame(arrive - 0.01).press == 1.0
    first, between, second = (
        track.frame(arrive + t).press for t in (0.075, 0.125, 0.175)
    )
    assert first < 0.9
    assert second < 0.9
    assert between > max(first, second)
    assert track.frame(arrive + 0.5).press == 1.0


def test_the_first_press_plays_once_the_pointer_has_faded_in() -> None:
    track = _shown((700, 400))
    track.press(1, 0.0)

    deepest = min((track.frame(ms / 1000) for ms in range(1000)), key=lambda f: f.press)

    assert deepest.press < 0.9
    assert deepest.opacity == 1


def test_a_press_not_yet_played_is_dropped_by_a_move_to_a_new_target() -> None:
    def lowest_press(track: Track, start: float) -> float:
        return min(track.frame(start + ms / 1000).press for ms in range(1500))

    track = _shown((100, 100))
    track.go((900, 600), 1.0, SCREEN)
    track.press(1, 1.0)  # booked for when the glide arrives
    track.go((300, 300), 1.05, SCREEN)  # a scroll elsewhere, say: no press
    assert lowest_press(track, 1.05) == 1.0


def test_an_idle_pointer_fades_out_and_then_is_gone() -> None:
    track = _shown((700, 400))

    assert track.frame(IDLE - 0.01).opacity == 1
    assert track.frame(IDLE + FADE / 2).opacity == pytest.approx(0.5)
    assert track.frame(IDLE + FADE) is None
    # A helper nobody ever talked to leaves as well.
    assert Track(0.0).frame(IDLE + FADE) is None


def test_a_command_during_the_idle_fade_brings_the_pointer_straight_back() -> None:
    track = _shown((700, 400))
    assert 0 < track.frame(IDLE + 0.3).opacity < 1

    track.go((900, 600), IDLE + 0.3, SCREEN)

    assert track.frame(IDLE + 0.3).opacity == 1
    assert track.move["start"] == (700, 400)  # an ordinary glide from where it is
    assert track.frame(IDLE + FADE + 0.1) is not None  # not removed at the old time


def test_a_paused_pointer_is_half_seen_and_a_loading_one_rocks_in_place() -> None:
    track = _shown((700, 400))

    track.act("paused", 1.0)  # the person has the app
    assert track.frame(1.0).opacity == PAUSED

    track.act("loading", 2.0)  # the agent thinks
    track.act("loading", 2.3)  # said again: the rocking does not start over
    half = SWING / 2
    frames = [track.frame(2.0 + t) for t in (0, half, 2 * half, 3 * half)]
    assert [f.shape[0] for f in frames] == pytest.approx([0, ROCK, 0, -ROCK])
    assert {(f.x, f.y, f.opacity, f.fill) for f in frames} == {
        (700, 400, 1.0, LOADING_FILL)
    }
    assert frames[1].wake == pytest.approx(1 / 30)  # it is drawn 30 times a second

    track.act("idle", 5.0)  # an action runs
    still = track.frame(5.0)
    assert (still.shape, still.fill) == (REST, FILL)
    assert still.wake > 1


def test_a_loading_pointer_glides_first_and_rocks_once_it_has_arrived() -> None:
    track = _shown((100, 100))
    track.act("loading", 1.0)

    track.go((900, 600), 3.0, SCREEN)

    arrived = 3.0 + track.move["duration"]
    assert track.frame(3.1).shape == pytest.approx(pose(track.move, 0.1)[2:])
    assert track.frame(arrived).shape == REST
    assert track.frame(arrived + SWING / 2).shape[0] == pytest.approx(ROCK)


# --- the flash and the halo: commands and a clock in, alphas out ------------


def _command(cmd: str, **fields) -> dict:
    """A command as read_command hands it over, for the app 42 and its window 7."""
    return {"cmd": cmd, "pid": 42, "window": 7, "count": 1, **fields}


def _flash(frame: tuple, within: tuple | None = None) -> dict:
    return _command("flash", frame=frame, within=within)


BUTTON = (100.0, 60.0, 40.0, 20.0)
ON_BUTTON = _command("move", x=110.0, y=65.0)
HALO_ON = _command("halo", frame=(100.0, 50.0, 800.0, 600.0))


def test_a_flash_stays_until_the_press_pulse_ends_and_then_fades() -> None:
    scene = Scene(0.0)
    scene.command(ON_BUTTON, 0.0, SCREEN)

    scene.command(_flash(BUTTON), 10.0, SCREEN)  # the pointer rests on the control
    times = (0.0, 0.3, 0.3 + FADE / 2, 0.3 + FADE)
    alphas = [scene.flash.alpha(10.0 + t) for t in times]
    assert alphas == pytest.approx([1, 1, 0.5, 0])  # 0.74 s in all

    scene.command(_command("move", x=260.0, y=65.0), 20.0, SCREEN)  # a hop away
    scene.command(_flash((250.0, 60.0, 40.0, 20.0)), 20.0, SCREEN)
    assert scene.flash.until == pytest.approx(20.0 + 0.173 + 0.3, abs=0.002)
    assert scene.flash.box == (250.0, 60.0, 40.0, 20.0)  # one flash at a time
    assert scene.flash.alpha(20.9) > 0
    assert scene.flash.alpha(20.92) == 0  # 0.91 s after a hop


def test_the_first_flash_waits_for_the_pointer_to_fade_in() -> None:
    scene = Scene(0.0)

    scene.command(ON_BUTTON, 0.0, SCREEN)
    scene.command(_flash(BUTTON), 0.0, SCREEN)

    assert scene.flash.alpha(0.0) == 1  # it does not fade in
    assert scene.flash.until == pytest.approx(FADE + 0.3)  # as the press pulse


def test_a_flash_with_no_pointer_on_it_sends_the_pointer_to_its_centre() -> None:
    scene = Scene(0.0)

    scene.command(_flash(BUTTON), 0.0, SCREEN)  # an action with no point of its own
    assert scene.track.move["end"] == (120, 70)
    assert (scene.pid, scene.window) == (42, 7)  # stacked above that window

    scene.command(ON_BUTTON, 1.0, SCREEN)  # a click on the control, then its flash
    scene.command(_flash(BUTTON), 1.0, SCREEN)
    assert scene.track.move["end"] == (110, 65)  # it stays at the click point

    scene.command(_flash((500.0, 500.0, 10.0, 10.0)), 2.0, SCREEN)
    assert scene.track.move["end"] == (505, 505)
    assert scene.track.move["start"] == pytest.approx((110, 65))  # it glides there

    scene.command(_flash((0.0, 0.0, 700.0, 500.0)), 3.0, SCREEN, calm=True)
    assert scene.track.frame(3.0)[:2] == (350, 250)  # less motion: a jump


@pytest.mark.parametrize(
    ("frame", "within"),
    [
        ((1, 2, 0, 5), None),
        ((1, 2, 5, 0), None),
        ((1, 2, -5, -5), None),
        ((0, 0, 700, 500), (0, 0, 800, 600)),
    ],
    ids=["no width", "no height", "turned inside out", "over half its window"],
)
def test_no_outline_around_nothing_or_around_most_of_the_window(frame, within) -> None:
    assert flash_box(frame, within) is None

    scene = Scene(0.0)
    scene.command(_flash(BUTTON), 0.0, SCREEN)
    scene.command(_flash(frame, within), 0.1, SCREEN)
    assert scene.flash.alpha(0.1) == 0  # and the flash before it is gone


def test_a_control_of_half_its_window_or_in_no_window_is_outlined() -> None:
    half = (0, 0, 400, 600)
    assert flash_box(half, (0, 0, 800, 600)) == half
    assert flash_box((0, 0, 5000, 5000), None) == (0, 0, 5000, 5000)


def test_the_halo_fades_in_stays_ten_seconds_after_the_last_action_and_fades_out() -> (
    None
):
    scene = Scene(0.0)
    scene.command(HALO_ON, 0.0, SCREEN)
    halo = scene.halo

    times = (0, FADE / 2, FADE, ACTIVE, ACTIVE + FADE / 2, ACTIVE + FADE)
    assert [halo.alpha(t) for t in times] == pytest.approx([0, 0.5, 1, 1, 0.5, 0])
    assert (halo.pid, halo.window, halo.box) == (42, 7, (100.0, 50.0, 800.0, 600.0))

    scene.command(HALO_ON, 8.0, SCREEN)  # the next action: ten seconds from here
    assert [halo.alpha(t) for t in (8.0, 17.99, 18 + FADE)] == [1, 1, 0]

    scene.command(HALO_ON, 18.3, SCREEN)  # during the fade out: back at once
    assert halo.alpha(18.3) == 1

    scene.command(HALO_ON, 60.0, SCREEN)  # after it was gone: it fades in again
    assert halo.alpha(60.0) == 0
    assert halo.alpha(60.0 + FADE) == 1


def test_the_next_frame_is_due_at_once_in_a_fade_and_ten_times_a_second_under_a_halo() -> (
    None
):
    scene = Scene(0.0)
    scene.command(HALO_ON, 0.0, SCREEN)

    def wake(now: float) -> float:
        return scene.wake(now, scene.track.frame(now))

    assert wake(0.1) == 0  # the halo fades in
    assert wake(5.0) == 0.1  # its window is looked up again
    assert wake(ACTIVE + 0.1) == 0  # it fades out
    assert wake(ACTIVE + FADE) == pytest.approx(IDLE - ACTIVE)  # until it is gone

    scene.command(ON_BUTTON, 20.0, SCREEN)
    scene.command(_flash(BUTTON), 21.0, SCREEN)
    assert wake(21.1) == pytest.approx(0.2)  # the flash starts to go at 21.3
    assert wake(21.4) == 0


def test_the_end_of_a_turn_takes_the_halo_now_and_the_pointer_two_seconds_later() -> (
    None
):
    scene = Scene(0.0)
    scene.command(ON_BUTTON, 0.0, SCREEN)
    scene.command(HALO_ON, 0.0, SCREEN)

    scene.command(_command("done"), 5.0, SCREEN)
    scene.command(_command("done"), 5.5, SCREEN)  # said twice: nothing starts over

    assert [scene.halo.alpha(5.0 + t) for t in (0, FADE)] == [1, 0]
    opacity = [scene.track.frame(5.0 + DONE + t).opacity for t in (0, FADE / 2)]
    assert opacity == pytest.approx([1, 0.5])
    assert scene.track.frame(5.0 + DONE + FADE) is None  # and the process leaves

    scene.command(ON_BUTTON, 6.0, SCREEN)  # the next turn began before that
    assert scene.track.frame(6.0 + IDLE).opacity == 1


def test_the_halo_follows_its_window_and_hides_when_the_window_is_gone() -> None:
    scene = Scene(0.0)
    scene.command(HALO_ON, 0.0, SCREEN)  # window 7 of app 42, at (100, 50)
    halo = scene.halo
    other = (5, 99, 0, (0, 0, 2000, 2000))

    moved = [other, (7, 42, 0, (300, 200, 900, 700))]  # moved, and made smaller
    assert halo.follow(moved, 0.1) == (0, 7)  # just above it
    assert halo.box == (300, 200, 600, 500)

    assert halo.follow([other], 0.2) is None  # on another Space, or minimised
    assert halo.box == (300, 200, 600, 500)  # where it was, for when it is back

    raised = [(7, 42, 3, (300, 200, 900, 700)), other]  # back, in front, a level up
    assert halo.follow(raised, 0.3) == (3, 7)

    assert halo.follow(raised, ACTIVE + FADE) is None  # the halo is over
    assert Mark().follow(raised, 0.0) is None

    # The id of the window is not known: the app's window under the halo's middle.
    scene.command({**HALO_ON, "window": 0}, 20.0, SCREEN)
    assert halo.follow([other, (8, 42, 0, (90, 40, 910, 660))], 20.1) == (0, 8)
    assert halo.box == (90, 40, 820, 620)


def test_any_command_keeps_the_pointer_from_idling_out() -> None:
    scene = Scene(0.0)
    scene.command(ON_BUTTON, 0.0, SCREEN)

    scene.command(_command("activity", state="loading"), 25.0, SCREEN)
    scene.command(HALO_ON, 50.0, SCREEN)

    assert scene.track.frame(50.0 + IDLE).opacity == 1
    assert scene.track.frame(50.0 + IDLE + FADE) is None


def test_a_mark_that_shows_nothing_is_never_due() -> None:
    mark = Mark()
    assert (mark.alpha(1.0), mark.alive(1.0), mark.wake(1.0)) == (0, False, math.inf)


# --- screens and stacking ---------------------------------------------------


def test_points_map_onto_a_second_screen_at_negative_coordinates() -> None:
    # AppKit frames (y up): a 1440 x 900 primary, and a 1920 x 1080 screen to
    # its left on the same baseline.
    screens = flip_screens([(0, 0, 1440, 900), (-1920, 0, 1920, 1080)])

    assert screens == [(0, 0, 1440, 900), (-1920, -180, 0, 900)]
    assert bounds_of(screens, (-100, -100)) == (-1920, -180, 0, 900)
    assert bounds_of(screens, (-100, -100), (700, 400)) == BOTH_SCREENS  # a move across
    assert bounds_of(screens, (5000, 5000)) == BOTH_SCREENS  # on no screen: all
    assert window_origin(-100, -100, 900) == (-100 - SIZE / 2, 1000 - SIZE / 2)


def test_pointer_sits_above_everything_or_just_above_its_target_or_nowhere() -> None:
    windows = [  # on this Space, front to back: (id, owner pid, level, where)
        (7, 99, 0, (0, 0, 500, 500)),
        (8, 42, 0, (100, 100, 400, 400)),
        (9, 42, 3, (0, 0, 1000, 800)),
    ]
    above_everything = (OVERLAY_LEVEL, 0)

    assert stacking((200, 200), None, 1, windows) == above_everything  # no target app
    assert stacking((200, 200), 42, 42, windows) == above_everything  # it is in front
    # The target app is in the background: just above its window under the point.
    assert stacking((200, 200), 42, 1, windows) == (0, 8)
    assert stacking((600, 600), 42, 1, windows) == (3, 9)
    assert stacking((2000, 600), 42, 1, windows) is None  # no window of it there
    assert stacking((200, 200), 42, 1, windows[:1]) is None  # it is on another Space


def test_the_window_acted_on_is_found_by_its_id_or_else_under_the_point() -> None:
    windows = [  # on this Space, front to back: (id, owner pid, level, where)
        (7, 99, 0, (0, 0, 500, 500)),
        (8, 42, 0, (100, 100, 400, 400)),
        (9, 42, 3, (0, 0, 1000, 800)),
    ]

    assert target_window((200, 200), 42, windows) == windows[1]
    assert target_window((200, 200), 42, windows, 9) == windows[2]  # its id is known
    assert target_window((200, 200), 42, windows, 77) is None  # on another Space
    assert target_window((200, 200), None, windows) is None
    assert stacking((200, 200), 42, 1, windows, 9) == (3, 9)
    assert stacking((200, 200), 42, 1, windows, 77) is None
    assert stacking((200, 200), 42, 42, windows, 77) == (OVERLAY_LEVEL, 0)


def test_outlines_map_onto_a_second_screen_at_negative_coordinates() -> None:
    # A 1440 x 900 primary at scale 2 and a 1920 x 1080 screen at scale 1 to its
    # left, 180 higher. Points are the same on both: the scale never enters.
    window = (100, 50, 800, 600)
    assert grow(window, HALO[0]) == (97, 47, 806, 606)  # the halo, 3 outside it
    assert window_frame(grow(window, HALO[0]), 900) == ((97, 247), (806, 606))

    button = (-1800, -100, 40, 20)  # on the left screen
    assert grow(button, FLASH[0]) == (-1802, -102, 44, 24)  # the flash, 2 outside
    assert window_frame(grow(button, FLASH[0]), 900) == ((-1802, 978), (44, 24))


# --- drawing, offscreen -----------------------------------------------------


def _render(scale: int, **look) -> Image.Image:
    """The pointer drawn into an 80 x 80 pt bitmap, click point in the middle.
    No window."""
    return _canvas(scale, lambda: draw_pointer((40, 40), **look))


def _canvas(scale: int, draw) -> Image.Image:
    """What `draw` paints into an 80 x 80 pt bitmap with y down. No window."""
    side = 80 * scale
    make = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_
    bitmap = make(
        None, side, side, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    flip = AppKit.NSAffineTransform.transform()
    flip.translateXBy_yBy_(0, side)
    flip.scaleXBy_yBy_(scale, -scale)
    flip.concat()
    draw()
    AppKit.NSGraphicsContext.restoreGraphicsState()
    png = bitmap.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, {}
    )
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


@pytest.mark.parametrize("scale", [1, 2])
def test_drawn_pointer_matches_the_stored_picture(scale: int) -> None:
    ours, want = _render(scale), Image.open(FIXTURES / f"pointer@{scale}x.png")

    for back in ((255, 255, 255, 255), (32, 48, 64, 255)):
        assert alike(ours, want, back) >= 0.98
        # The arrow alone: 0.83 to 0.99 as drawn, 0.73 at most with half the rim.
        assert alike(ours, want, back, arrow_box(scale)) >= 0.78


def test_cover_is_whole_inside_the_arrow_and_fades_out_around_it() -> None:
    cover = _render(1, cover_only=True).getchannel("A")

    assert cover.getpixel((40, 40)) == 255  # the arrow body is fully frosted
    assert cover.getpixel((45, 42)) == 255
    assert cover.getpixel((40, 60)) == pytest.approx(0.228 * 255, abs=6)  # 20 pt out
    assert cover.getpixel((2, 2)) == 0


def test_press_shrinks_the_arrow_toward_its_anchor() -> None:
    def arrow_box(press: float) -> tuple[int, ...]:
        cover = _render(4, press=press, cover_only=True).getchannel("A")
        return cover.point(lambda alpha: 255 * (alpha == 255)).getbbox()

    rest, pressed = arrow_box(1.0), arrow_box(0.7)

    shrink = (pressed[2] - pressed[0]) / (rest[2] - rest[0])
    assert shrink == pytest.approx(0.7, abs=0.03)
    # Toward a point inside the arrow, not toward one of its sides.
    assert pressed[0] > rest[0]
    assert pressed[2] < rest[2]


@pytest.mark.parametrize(
    "shape",
    [(90.0, 1.0, 1.0, 0.0), (0.0, 1.38, 0.82, 0.0), (30.0, 1.3, 0.85, -70.0)],
    ids=["quarter turn clockwise", "stretch along x", "a hop pose"],
)
def test_a_pose_is_drawn_turned_and_stretched_as_the_motion_says(shape) -> None:
    # A pose is rotate(a) scale(sx, sy) rotate(b) about PIVOT; y down, clockwise.
    a, sx, sy, b = shape
    xs, ys = [], []
    for x, y in outline_points():
        x, y = x - PIVOT[0], y - PIVOT[1]
        for angle, kx, ky in ((b, sx, sy), (a, 1, 1)):
            c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            x, y = (x * c - y * s) * kx, (x * s + y * c) * ky
        xs.append(x + PIVOT[0])
        ys.append(y + PIVOT[1])

    cover = _render(8, shape=shape, cover_only=True).getchannel("A")
    box = cover.point(lambda alpha: 255 * (alpha == 255)).getbbox()

    want = [min(xs), min(ys), max(xs), max(ys)]
    assert [edge / 8 - 40 for edge in box] == pytest.approx(want, abs=0.25)


@pytest.mark.parametrize("look", [FLASH, HALO], ids=["flash", "halo"])
def test_an_outline_is_an_accent_line_just_outside_what_it_marks(look) -> None:
    width, _radius = look
    left, top, right, bottom = 20, 24, 60, 54
    box = (left, top, right - left, bottom - top)

    picture = _canvas(4, lambda: draw_outline(box, *look))

    def pixel(x: float, y: float) -> tuple[int, ...]:
        return picture.getpixel((round(x * 4), round(y * 4)))

    middle = (top + bottom) / 2
    for x, y in (  # the middle of the line on each side
        (left - width / 2, middle),
        (right + width / 2, middle),
        (40, top - width / 2),
        (40, bottom + width / 2),
    ):
        assert pixel(x, y) == pytest.approx((*ACCENT, 255), abs=4)
    assert pixel(left + 0.5, middle)[3] == 0  # nothing on what it marks
    assert pixel(40, middle)[3] == 0
    assert pixel(left - width - 0.5, middle)[3] == 0  # nothing beyond the line
    assert pixel(left - width + 0.25, top - width + 0.25)[3] == 0  # a round corner


def test_an_outline_window_takes_no_clicks_and_no_focus_either() -> None:
    made, picture = outline_panel()

    assert made.ignoresMouseEvents()
    assert not made.canBecomeKeyWindow()
    assert not made.isVisible()
    assert picture.superview() == made.contentView()


def test_window_takes_no_clicks_no_keys_no_focus_and_starts_off_screen() -> None:
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyProhibited
    )

    panel, _glass, _picture = pointer_panel()

    assert panel.ignoresMouseEvents()
    assert not panel.canBecomeKeyWindow()
    assert not panel.canBecomeMainWindow()
    assert not panel.isVisible()
    assert not panel.hasShadow()
    assert (panel.frame().size.width, panel.frame().size.height) == (SIZE, SIZE)


# --- the real helper process ------------------------------------------------


def _windows_on_screen(helper: subprocess.Popen) -> list[dict]:
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return [row for row in rows or [] if row[Quartz.kCGWindowOwnerPID] == helper.pid]


def _frontmost() -> int:
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier())


@pytest.fixture
def helpers(pointer_socket: str, monkeypatch) -> list[subprocess.Popen]:
    """The real helper processes the test started."""
    started: list[subprocess.Popen] = []
    real = subprocess.Popen

    def recorded(argv, **kwargs):
        process = real(argv, **kwargs)
        if "--serve" in argv:
            started.append(process)
        return process

    monkeypatch.setattr(overlay_module.subprocess, "Popen", recorded)
    return started


def test_helper_outlives_its_driver_serves_the_next_and_leaves_when_idle(
    helpers: list[subprocess.Popen], pointer_socket: str, monkeypatch
) -> None:
    monkeypatch.setattr(overlay_module, "IDLE", 1.0)

    # pid 1 owns no window, so the pointer has no target here and stays off screen.
    LivePointerOverlay().move(300, 300, pid=1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        for bad in (
            b'{"cmd": "move", "x": "a", "y": 1}',
            b'{"cmd": "press", "count": "x"}',
            b'{"cmd": "flash", "frame": [1, 2]}',
            b'{"cmd": "halo", "frame": [1, 2, 3, 4], "pid": 1}',  # no such window
            b'{"cmd": "flash", "frame": [300, 300, 0, 0], "pid": 1}',  # no size
            b'{"cmd": "activity", "state": "loading"}',
        ):
            sender.sendto(bad, pointer_socket)  # dropped; they do not end the helper
    time.sleep(0.5)
    LivePointerOverlay().move(320, 300, pid=1)  # a second driver
    time.sleep(0.2)

    (helper,) = helpers
    assert helper.poll() is None
    assert not _windows_on_screen(helper)
    assert _frontmost() != helper.pid
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
        helper.pid
    )
    assert app.activationPolicy() == AppKit.NSApplicationActivationPolicyProhibited
    # The model never meets it among the apps: those are the regular ones.
    with open(pointer_socket + ".lock") as pid_file:
        assert pid_file.read() == str(helper.pid)
    assert app.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular
    # It leaves by its own timer, 1.44 s after the last command, and takes its
    # socket file with it.
    assert helper.wait(timeout=5) == 0
    assert not os.path.exists(pointer_socket)

    LivePointerOverlay().move(300, 300, pid=1)  # so the next move starts one at once
    assert len(helpers) == 2
    assert helpers[1].wait(timeout=5) == 0  # idle again: it leaves by itself


def test_real_helper_survives_the_exit_of_the_call_that_started_it(
    pointer_socket: str,
) -> None:
    one_shot_call = (
        "import macos_harness.overlay as overlay;"
        f"overlay.address = lambda: {pointer_socket!r};"
        "overlay.IDLE = 1.0;"
        "overlay.LivePointerOverlay().move(300, 300, pid=1)"
    )

    subprocess.run(
        [sys.executable, "-c", one_shot_call],
        check=True,
        env={**os.environ, "MACOS_HARNESS_POINTER": "1"},
    )
    time.sleep(0.1)

    assert not _lock_is_free(pointer_socket)  # the call is over, its helper lives on
    deadline = time.monotonic() + 5
    while not _lock_is_free(pointer_socket) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _lock_is_free(pointer_socket)


@pytest.mark.live  # draws the pointer on the screen for about 1 s
def test_live_pointer_is_drawn_takes_no_focus_and_leaves(
    helpers: list[subprocess.Popen], monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(overlay_module, "IDLE", 1.5)  # it leaves even if this dies
    pointer = LivePointerOverlay()
    shot = tmp_path / "pointer.png"

    pointer.move(200, 200)
    deadline = time.monotonic() + 3
    while not _windows_on_screen(helpers[0]) and time.monotonic() < deadline:
        time.sleep(0.02)
    (window,) = _windows_on_screen(helpers[0])
    time.sleep(FADE + 0.1)
    front = _frontmost()
    own_window = ["-l", str(window[Quartz.kCGWindowNumber])]
    subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-o", *own_window, shot], check=True
    )

    bounds = window[Quartz.kCGWindowBounds]
    assert window[Quartz.kCGWindowName] == TITLE
    assert (bounds["X"], bounds["Y"]) == (200 - SIZE / 2, 200 - SIZE / 2)
    assert window[Quartz.kCGWindowLayer] == OVERLAY_LEVEL
    with Image.open(shot) as image:
        alpha = image.convert("RGBA").getchannel("A")
        middle = (alpha.width // 2, alpha.height // 2)
        assert alpha.getpixel(middle) > 200  # the arrow, at the click point
        assert alpha.getpixel((2, 2)) == 0  # nothing in the corner of its window
    assert front != helpers[0].pid
    assert helpers[0].wait(timeout=5) == 0


# A flat window of our own for 2.8 s: it takes no focus and no clicks, and it
# goes by itself even if the test dies.
OWN_WINDOW = """
import AppKit
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
top = AppKit.NSScreen.screens()[0].frame().size.height
window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    ((60, top - 80 - 200), (300, 200)), 0, AppKit.NSBackingStoreBuffered, False
)
window.setBackgroundColor_(
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.2, 0.3, 0.4, 1)
)
window.setIgnoresMouseEvents_(True)
window.orderFrontRegardless()
AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
    2.8, False, lambda timer: app.terminate_(None)
)
app.run()
"""


def _capture(window: dict, path: Path) -> Image.Image:
    """One window alone, by its id."""
    own = ["-l", str(window[Quartz.kCGWindowNumber])]
    subprocess.run(["/usr/sbin/screencapture", "-x", "-o", *own, path], check=True)
    with Image.open(path) as image:
        return image.convert("RGBA")


@pytest.mark.live  # a 300 x 200 window, the pointer, a flash and a halo for 2 s
def test_live_pointer_flash_and_halo_sit_on_a_background_window_and_touch_nothing(
    helpers: list[subprocess.Popen], monkeypatch, tmp_path: Path
) -> None:
    def mouse() -> tuple[float, float]:
        return tuple(Quartz.CGEventGetLocation(Quartz.CGEventCreate(None)))

    monkeypatch.setattr(overlay_module, "IDLE", 1.2)  # it leaves even if this dies
    frame, button, target = (60, 80, 300, 200), (120, 140, 80, 30), (150.0, 150.0)
    owner = BackgroundApp([sys.executable, "-c", OWN_WINDOW], tmp_path, "OwnWindow")
    pointer, front, real = LivePointerOverlay(), _frontmost(), mouse()
    try:
        deadline = time.monotonic() + 2
        while not _windows_on_screen(owner) and time.monotonic() < deadline:
            time.sleep(0.02)
        (own,) = _windows_on_screen(owner)
        where = {"pid": owner.pid, "window": int(own[Quartz.kCGWindowNumber])}
        for step in range(50):  # the agent acts on that window, in the background
            pointer.move(70 + step, 90 + step, **where)
            time.sleep(0.004)
        pointer.move(*target, **where)
        pointer.click()
        pointer.flash(button, within=frame, **where)
        pointer.halo(frame, **where)
        time.sleep(FADE + 0.1)
        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        order = [row[Quartz.kCGWindowOwnerPID] for row in rows]
        drawn = _windows_on_screen(helpers[0])
        # The flash first: it is the first to go.
        flash, halo, arrow = (
            _capture(drawn[i], tmp_path / f"{i}.png") for i in (1, 2, 0)
        )
        below = _capture(own, tmp_path / "own.png")
        front_after, real_after = _frontmost(), mouse()
    finally:
        owner.kill()

    # Front to back: the pointer, the flash, the halo, then the window acted on.
    first = order.index(helpers[0].pid)
    assert order[first : first + 4] == [helpers[0].pid] * 3 + [owner.pid]
    boxes = [
        tuple(row[Quartz.kCGWindowBounds][key] for key in ("X", "Y", "Width", "Height"))
        for row in drawn
    ]
    assert boxes == [
        (target[0] - SIZE / 2, target[1] - SIZE / 2, SIZE, SIZE),
        grow(button, FLASH[0]),
        grow(frame, HALO[0]),
    ]
    assert {row[Quartz.kCGWindowLayer] for row in drawn} == {own[Quartz.kCGWindowLayer]}
    assert arrow.getpixel((arrow.width // 2, arrow.height // 2))[3] > 200
    for line, box in ((flash, boxes[1]), (halo, boxes[2])):
        scale = line.width / box[2]  # pixels per point
        red, green, blue, alpha = line.getpixel((line.width // 2, round(scale)))
        assert alpha > 0 and red > green > blue  # the accent line, 1 pt in
        assert line.getpixel((line.width // 2, line.height // 2))[3] == 0
    edge = halo.getpixel((halo.width // 2, round(halo.width / boxes[2][2])))
    # The halo is in by now. The screen has its own colours: within 30 of ours.
    assert edge == pytest.approx((*ACCENT, 255), abs=30)
    # A picture of the window by its id shows the window alone: no pointer, no
    # flash, no halo. That is the picture the model gets.
    assert len(below.getcolors()) == 1
    # Nothing took focus, and the real pointer did not go where the drawn one went.
    assert front_after == front
    assert helpers[0].pid != front_after
    assert real_after == real or math.dist(real_after, target) > 5
    assert helpers[0].wait(timeout=5) == 0
