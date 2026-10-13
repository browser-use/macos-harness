"""Event fields, read back from events that are built and never posted, and who
gets an event (with the fakes of fakes_e)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fakes import El
from fakes_e import APP_PID, load, rig

from macos_harness import errors, events
from macos_harness.model import AppKind, Line, WindowRef

Quartz = pytest.importorskip("Quartz")
WINDOW = WindowRef(None, 4242, 77, "Fixture", (100.0, 50.0, 600.0, 400.0))
POINT = (150.0, 80.0)
COMMAND = 0x100000
SRC = Path(__file__).parents[1] / "src/macos_harness"
OURS = ("events.py", "keys.py", "focus.py", "actions.py", "pasteboard.py")


def field(event, number: int) -> int:
    return Quartz.CGEventGetIntegerValueField(event, number)


@pytest.mark.parametrize(
    ("kind", "button", "kind_number", "click"),
    [
        ("down", 0, 1, 1),
        ("up", 0, 2, 1),
        ("dragged", 0, 6, 0),
        ("down", 1, 3, 1),
        ("up", 1, 4, 1),
        ("dragged", 1, 7, 0),
        ("down", 2, 25, 1),
        ("up", 2, 26, 1),
        ("dragged", 2, 27, 0),
        ("moved", 0, 5, 1),
        ("down", 0, 1, 2),  # the second press of a double click
    ],
)
def test_mouse_event_builders(kind, button, kind_number, click):
    asked = 2 if (kind, click) == ("down", 2) else 1
    event = events.mouse(kind, POINT, WINDOW, button, asked, COMMAND)
    assert Quartz.CGEventGetType(event) == kind_number
    assert Quartz.CGEventGetFlags(event) & events.MODIFIERS == COMMAND
    assert field(event, 1) == click  # click count: 0 on dragged, 1 on moved
    assert field(event, 3) == button
    assert field(event, 7) == 3
    assert [field(event, n) for n in (51, 91, 92)] == [4242] * 3  # the window id
    where = Quartz.CGEventGetLocation(event)
    assert (where.x, where.y) == POINT  # global, origin top left


def test_window_local_point_is_measured_from_the_window_top_left():
    read = events.private("CGEventGetWindowLocation", b"{CGPoint=dd}^{__CGEvent=}")
    if read is None:
        pytest.skip("this system cannot read the window-local point back")
    local = read(events.mouse("down", POINT, WINDOW))
    assert (local.x, local.y) == (50.0, 30.0)  # y grows downwards, like the screen
    local = read(events.wheel(POINT, 0, -10, WINDOW))
    assert (local.x, local.y) == (50.0, 30.0)


def test_synthesized_mouse_events_carry_running_event_number():
    numbers = [field(events.mouse(kind, POINT, WINDOW), 0) for kind in ("down", "up")]
    numbers.append(field(events.mouse("dragged", POINT, WINDOW), 0))
    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]


@pytest.mark.parametrize(("dx", "dy"), [(0, -150), (0, 150), (-120, 0), (120, 0)])
def test_wheel_event_is_one_pixel_event_with_two_axes(dx, dy):
    event = events.wheel(POINT, dx, dy, WINDOW)
    assert Quartz.CGEventGetType(event) == 22
    assert (field(event, 96), field(event, 97)) == (dy, dx)  # pixels, axis 1 and 2
    assert field(event, 88) == 1  # continuous: pixels, not lines
    assert field(event, 51) == 4242  # without it a background window does not move


def test_key_event_fields():
    down = events.key(9, COMMAND)
    up = events.key(9, COMMAND, down=False)
    assert [Quartz.CGEventGetType(e) for e in (down, up)] == [10, 11]
    assert field(down, 9) == 9 and Quartz.CGEventGetFlags(down) == COMMAND
    typed = events.key(0, 0, "é")
    assert Quartz.CGEventKeyboardGetUnicodeString(typed, 4, None, None) == (1, "é")
    pair = events.key(0, 0, "😀")  # two 16 bit units in one event
    assert Quartz.CGEventKeyboardGetUnicodeString(pair, 4, None, None)[0] == 2


def test_flags_changed_event_fields():
    event = events.flags_changed(COMMAND)
    assert Quartz.CGEventGetType(event) == 12
    assert Quartz.CGEventGetFlags(event) == COMMAND


def test_notice_events_carry_type_subtype_and_window():
    active = events.notice(13, 1, 4242, 0xC0000)
    assert Quartz.CGEventGetType(active) == 13
    assert (field(active, 83), field(active, 51)) == (1, 4242)
    assert Quartz.CGEventGetFlags(active) == 0xC0000
    focus = events.notice(21, 0x8000)
    assert (Quartz.CGEventGetType(focus), field(focus, 64)) == (21, 0x8000)


def test_default_hold_time_between_mouse_down_up(monkeypatch):
    """A click by point as the app gets it: moved, 67 ms, down, 50 ms, up; the
    second click of a double click 100 ms later."""
    assert events.CLICK_HOLD == 0.05
    r = rig(monkeypatch)
    r.actions.click(r.app, (300.0, 300.0), count=2)
    start = r.events.posted[0]["t"]
    seen = [(e["type"], e["click"], round(e["t"] - start, 3)) for e in r.events.posted]
    assert seen == [
        ("moved", 1, 0.0),
        ("down", 1, 0.067),
        ("up", 1, 0.117),
        ("down", 2, 0.217),
        ("up", 2, 0.267),
    ]


def test_post_stamps_the_event_and_sends_it_to_one_process(monkeypatch):
    sent, slept = [], []
    monkeypatch.setattr(Quartz, "CGEventPostToPid", lambda pid, e: sent.append(pid))
    monkeypatch.setattr(events.time, "sleep", slept.append)
    event = events.key(0, 0)
    events.post(77, event, 0.01)
    events.post(77, event)
    assert sent == [77, 77] and slept == [0.01]
    assert field(event, events.USER_DATA_FIELD) == events.OWN_TAG
    assert Quartz.CGEventGetTimestamp(event) > 0


def test_events_delivered_target_pid_stamped_window_id_window():
    """No file of the input ladder knows a way into the system event stream or a
    call that moves, warps or hides the real pointer."""
    banned = ("CGEventPost(", "EventTap", "CGWarpMouse", "MoveCursor", "CGAssociate")
    banned += ("HideCursor", "CGEventTapPostEvent", "IOHIDPostEvent")
    for name in OURS:
        text = (SRC / name).read_text()
        assert [word for word in banned if word in text] == [], name
    assert "CGEventPostToPid" in (SRC / "events.py").read_text()


def test_coregraphics_event_calls_private_window_server_calls(caplog):
    assert events.private("CGEventSetWindowLocation", b"v^{__CGEvent=}{CGPoint=dd}")
    with caplog.at_level(logging.WARNING):
        assert events.private("NoSuchCallAnywhere", b"v") is None
        assert events.private("NoSuchCallAnywhere", b"v") is None  # asked once
    assert len(caplog.records) == 1


# --- who gets the event --------------------------------------------------------------


def test_mouse_target_resolved_pid_window_id_window_bounds(monkeypatch):
    r = rig(monkeypatch)
    r.actions.click(r.app, (300.0, 300.0))
    assert {(e["pid"], e["window"]) for e in r.events.posted} == {(APP_PID, 7)}


def test_target_resolution_has_distinct_failure_cases_including_out(monkeypatch):
    r = rig(monkeypatch)
    with pytest.raises(errors.HarnessError) as outside:
        r.actions.click(r.app, (5.0, 5.0))
    assert outside.value.message.endswith("No window of the app is at (5.0, 5.0)")
    r.windows.window = WindowRef(r.window, 0, APP_PID, None, (100, 100, 600, 400))
    with pytest.raises(errors.HarnessError) as no_id:
        r.actions.click(r.app, (300.0, 300.0))
    assert no_id.value.message.endswith("No window could be found for this element")
    assert r.events.posted == []


def test_out_process_elements_web_content_view_services(monkeypatch):
    """An element another process draws: the event goes to that process and to the
    window that process owns."""
    r = rig(monkeypatch)
    r.add(El("AXGroup", rect=(200, 200, 100, 100), owner_pid=555))
    theirs = WindowRef(r.window, 9, 555, None, (100, 100, 600, 400))
    r.windows.ref = lambda element: theirs
    r.actions.click(r.app, (250.0, 250.0))
    assert {(e["pid"], e["window"]) for e in r.events.posted[:1]} == {(555, 9)}


def test_keyboard_events_have_their_own_target_resolution(monkeypatch):
    r = rig(monkeypatch)
    r.front[0] = APP_PID  # really active: no made-up activation in the way
    r.actions.press_key(r.app, "Tab")
    assert {e["pid"] for e in r.events.posted} == {APP_PID}
    r.root.attrs["AXFocusedUIElement"] = El("AXTextField", owner_pid=555)
    r.events.posted.clear()
    r.actions.press_key(r.app, "Tab")
    assert {e["pid"] for e in r.events.posted} == {555}


@pytest.mark.parametrize("inside", [True, False])
def test_synthesized_click_path_knows_whether_target_inside_web(monkeypatch, inside):
    r = rig(monkeypatch)
    link_el = El("AXLink", rect=(200, 200, 100, 20))
    r.add(El("AXWebArea" if inside else "AXGroup", children=[link_el]))
    line = Line(2, "link", link_el, "AXLink")
    r.actions.click(r.app, line)
    activated = [e for e in r.events.posted if e["type"] == "notice"]
    clicks = [e for e in r.events.posted if e.get("point") == (250.0, 210.0)]
    assert bool(activated) == inside
    assert {e["flags"] for e in clicks} == ({0} if inside else {COMMAND})


def test_chromium_and_electron_apps_never_get_the_command_tier(monkeypatch):
    for kind in (AppKind(chromium=True), AppKind(electron=True)):
        r = rig(monkeypatch, kind)
        r.actions.click(r.app, (300.0, 300.0))
        assert "notice" in r.events.types()
        assert {e["flags"] for e in r.events.posted if e["type"] == "down"} == {0}


def test_modules_load_without_their_neighbours():
    assert load("actions").click and load("focus").enforcer
