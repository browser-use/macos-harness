# ruff: noqa: F811 - a fixture arrives by import and is asked for by its name
from __future__ import annotations

import time

import pytest
from fakes_i import (  # noqa: F401 - the last three are fixtures
    Listener,
    listener,
    pointer_socket,
    seen,
    window,
)

import macos_harness.overlay as overlay_module
from macos_harness import presence, statusitem
from macos_harness.model import AppTarget

CHESS = AppTarget("/System/Applications/Chess.app", "com.apple.Chess", "Chess", 42)
STICKIES = AppTarget("/System/Applications/Stickies.app", "com.apple.Stickies", "S", 43)
CHOICES = ["Deny", "Allow for this session", "Always allow"]


def every_call() -> list:
    """One call of each function the other modules have."""
    here = window()
    return [
        lambda: presence.pointer_move((300, 200), here),
        lambda: presence.pointer_press(2),
        lambda: presence.pointer_activity("loading"),
        lambda: presence.flash((280, 190, 40, 20), here),
        lambda: presence.halo(here),
        lambda: presence.label("Clicking in Chess"),
        lambda: presence.live_view(here),
        lambda: presence.apps_in_use([CHESS]),
        lambda: presence.ask("Allow Chess?", None, CHOICES, print),
        lambda: presence.turn_done(),
    ]


def fail(*args, **kwargs):
    raise AssertionError("nothing may be started or shown")


def test_what_the_agent_does_is_one_datagram_each_for_the_overlay(
    seen: list, listener: Listener
) -> None:
    here = window(pid=42, window_id=7, frame=(100.0, 50.0, 800.0, 600.0))
    where = {"pid": 42, "window": 7}
    nowhere = {"pid": None, "window": 0}

    presence.pointer_move((300, 200), here)
    presence.pointer_press(2)
    presence.flash((280, 190, 40, 20), here)
    presence.halo(here)
    presence.pointer_activity("loading")
    presence.pointer_move((5, 6), None)  # a point in no window we know
    presence.flash((1, 2, 3, 4), None)
    presence.halo(None)  # no window, no halo
    presence.turn_done()

    within = [100.0, 50.0, 800.0, 600.0]
    assert listener.commands() == [
        {"cmd": "move", "x": 300.0, "y": 200.0, "animate": True, **where},
        {"cmd": "press", "count": 2},
        {"cmd": "flash", "frame": [280, 190, 40, 20], "within": within, **where},
        {"cmd": "halo", "frame": within, **where},
        {"cmd": "activity", "state": "loading"},
        {"cmd": "move", "x": 5.0, "y": 6.0, "animate": True, **nowhere},
        {"cmd": "flash", "frame": [1, 2, 3, 4], "within": None, **nowhere},
        {"cmd": "done"},
    ]


@pytest.mark.parametrize("switch", ["0", "false", "OFF", " no "])
def test_with_the_switch_off_nothing_is_sent_started_or_shown(
    switch: str, seen: list, listener: Listener, monkeypatch
) -> None:
    monkeypatch.setenv("MACOS_HARNESS_POINTER", switch)
    monkeypatch.setattr(overlay_module.subprocess, "Popen", fail)
    monkeypatch.setattr(presence, "_on_main", fail)
    monkeypatch.setattr(presence.liveview, "watch", fail)

    shown = [call() for call in every_call()]

    assert shown == [False] * 10  # ask among them: nobody will see the question
    assert listener.commands() == []
    assert seen == []
    assert statusitem.MENU.asks == []


def test_every_call_returns_in_under_a_millisecond_with_a_dead_overlay(
    seen: list, listener: Listener, monkeypatch
) -> None:
    def no_process(*args, **kwargs):
        raise OSError("no more processes")

    listener.socket.close()  # nobody listens, and nobody can be started
    monkeypatch.setattr(overlay_module.subprocess, "Popen", no_process)

    for call in every_call():
        took = []
        for _ in range(9):
            began = time.perf_counter()
            call()
            took.append(time.perf_counter() - began)
        assert sorted(took)[4] < 0.001


def test_no_call_ever_raises(seen: list, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("no state folder, no main thread")

    monkeypatch.setattr(overlay_module, "address", broken)
    monkeypatch.setattr(presence, "_on_main", broken)
    monkeypatch.setattr(statusitem, "draw", broken)

    for call in every_call():
        call()
    assert not presence.ask("Allow Chess?", None, CHOICES, print)  # nobody sees it
    assert not presence.pointer_move(None, None)  # what no caller should send
    presence.flash("here", None)
    presence.pointer_activity(7)


def test_a_broken_menu_bar_never_reaches_the_main_thread_as_an_error(
    seen: list, monkeypatch
) -> None:
    def broken(menu):
        raise RuntimeError("no menu bar")

    monkeypatch.setattr(statusitem, "draw", broken)

    presence.label("Clicking in Chess")  # the fake main thread runs it at once

    assert statusitem.MENU.label == "Clicking in Chess"


def test_while_the_person_has_the_app_idle_and_loading_are_ignored(
    seen: list, listener: Listener, monkeypatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(presence, "_now", lambda: clock[0])

    presence.pointer_activity("paused")  # the person touched the app
    presence.pointer_activity("idle")  # a call in flight: ignored
    presence.pointer_activity("loading")
    clock[0] += presence.PAUSE - 0.01
    presence.pointer_activity("paused")  # touched again: the pause starts over
    clock[0] += presence.PAUSE - 0.01
    presence.pointer_activity("loading")
    clock[0] += 0.02
    presence.pointer_activity("loading")  # the pause is over

    states = [command["state"] for command in listener.commands()]
    assert states == ["paused", "paused", "loading"]


def test_the_end_of_a_turn_ends_the_pause(
    seen: list, listener: Listener, monkeypatch
) -> None:
    presence.pointer_activity("paused")
    presence.turn_done()
    presence.pointer_activity("idle")

    cmds = [(command["cmd"], command.get("state")) for command in listener.commands()]
    assert cmds == [("activity", "paused"), ("done", None), ("activity", "idle")]


def test_a_question_is_answered_once_on_the_main_thread(
    seen: list, monkeypatch
) -> None:
    queued, answers = [], []
    monkeypatch.setattr(presence, "_on_main", queued.append)

    assert presence.ask("Allow Chess?", "It can move pieces.", CHOICES, answers.append)

    assert statusitem.MENU.asks == []  # nothing yet: the caller's thread shows nothing
    queued.pop()()  # the main thread
    (ask,) = statusitem.MENU.asks
    assert (ask.title, ask.subtitle, ask.choices) == (
        "Allow Chess?",
        "It can move pieces.",
        CHOICES,
    )
    rows = dict(seen[-1])
    rows["Always allow"]()  # the person picks in the menu
    rows["Deny"]()  # and a second pick comes too late
    statusitem.MENU.answer(ask, "Deny")

    assert answers == ["Always allow"]
    assert statusitem.MENU.asks == []


def test_a_question_settled_elsewhere_is_withdrawn_unanswered(seen: list) -> None:
    answers = []
    presence.ask("Allow Chess?", None, CHOICES, answers.append)
    presence.ask("Allow Stickies?", None, CHOICES, answers.append)

    assert presence.withdraw("Allow Chess?")

    assert [ask.title for ask in statusitem.MENU.asks] == ["Allow Stickies?"]
    assert "Allow Chess?" not in dict(seen[-1]) and answers == []
    statusitem.MENU.asks.clear()


def test_the_menu_stops_one_app_or_all_of_them_and_resumes(seen: list) -> None:
    stopped, resumed = [], []
    presence.on_stop(stopped.append)
    presence.on_resume(resumed.append)

    presence.apps_in_use([CHESS, STICKIES])
    rows = dict(seen[-1])
    rows["Stop Using Chess"]()
    rows["Stop Using All Apps"]()
    rows["Resume"]()

    assert stopped == [CHESS, None]
    assert resumed == [None]


def test_the_label_is_the_first_row_and_the_end_of_a_turn_empties_the_menu(
    seen: list, listener: Listener
) -> None:
    presence.apps_in_use([CHESS])
    presence.label("Clicking in Chess")
    assert seen[-1][0] == ("Clicking in Chess", None)
    presence.label("Clicked in Chess")
    assert seen[-1][0] == ("Clicked in Chess", None)
    presence.ask("Allow Stickies?", None, CHOICES, print)

    presence.turn_done()

    titles = [title for title, _pick in seen[-1]]
    # A question stays until the person answers it.
    assert titles == ["No Active Sessions", "", "Allow Stickies?", *CHOICES]
    assert listener.commands() == [{"cmd": "done"}]
