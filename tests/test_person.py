"""The person: their events, the gate before every call, stop, turns."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import fakes_g
import pytest
from fakes import Clock
from fakes_g import agent, app

from macos_harness import errors, events, person, protocol

OWN_PID = 4242
LEFT, RIGHT, OTHER, WHEEL, KEY = 1, 3, 25, 22, 10
NOTES = app("Notes", pid=501)
MAIL = app("Mail", pid=502)


class World:
    """The module with empty state, a fake clock, fake neighbours and no real timer."""

    def __init__(self, monkeypatch):
        self.clock = Clock()
        self.presence = fakes_g.FakePresence()
        self.timers: list[tuple[float, object]] = []
        monkeypatch.setattr(person, "presence", self.presence)
        monkeypatch.setattr(person, "runloop", fakes_g.FakeRunloop())
        monkeypatch.setattr(
            person, "_later", lambda delay, call: self.timers.append((delay, call))
        )
        monkeypatch.setattr(person, "_bundle_of", lambda pid: "")
        monkeypatch.setattr(person, "_touch_from_idle_time", lambda target, clock: None)
        monkeypatch.setattr(person, "secure_input_pid", lambda: None)
        for table in ("_drivers", "_targets", "_quiet_at"):
            monkeypatch.setattr(person, table, {})
        monkeypatch.setattr(person, "_stops", set())
        monkeypatch.setattr(person, "_stop_all", False)
        monkeypatch.setattr(person, "_awaiting_quiet", set())
        monkeypatch.setattr(person, "_own_pid", OWN_PID)
        monkeypatch.setattr(person, "_last_escape", -math.inf)

    def call(self, who, target=NOTES, state_read=False):
        """One gated call. Returns its refusal, or None when it may run."""
        try:
            person.gate(who, target, state_read, clock=self.clock)
        except errors.HarnessError as refusal:
            return refusal
        return None

    def read(self, who, target=NOTES):
        refusal = self.call(who, target, state_read=True)
        if refusal is None:
            person.after_state_read(target, clock=self.clock)
        return refusal

    def event(self, kind=LEFT, pid=NOTES.pid, source_pid=0, tag=0, **more):
        person.saw(kind, source_pid, tag, pid, clock=self.clock, **more)

    def escape(self, **more):
        self.event(KEY, pid=999, keycode=person.ESCAPE, **more)

    def person_keeps_going(self, kind=WHEEL):
        """From now on the person sends one event during every poll of a waiting call."""
        quiet_sleep = self.clock.sleep

        def sleep(seconds):
            quiet_sleep(seconds)
            self.event(kind)

        self.clock.sleep = sleep


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


# --- the gate table


def test_no_record_lets_every_call_through(world):
    who = agent()
    assert world.call(who) is None and world.read(who) is None
    assert world.clock.time() == 1000.0


def test_after_interruption_every_action_app_rejected_until_state(world):
    """Quiet with the reread mark: an action fails at once, a state read passes and
    clears the mark, and then actions run again."""
    who = agent()
    world.call(who)
    world.event()
    world.clock.sleep(6.0)
    for _ in range(2):
        refusal = world.call(who)
        assert refusal.name == "user_took_over" and refusal.code == 110
    assert world.clock.time() == 1006.0  # no wait: the person is quiet
    assert world.read(who) is None
    assert world.call(who) is None


def test_while_person_still_active_actions_held_off_debounce(world):
    """Every event moves the 5 s. Past 100 s of waiting the text says 5, a constant."""
    who = agent()
    world.call(who)
    world.event()
    world.person_keeps_going()
    refusal = world.call(who)
    assert world.clock.time() == pytest.approx(1100.0, abs=0.25)
    assert refusal.message == (
        "The user is working in '/Applications/Notes.app' right now. Wait 5 seconds, "
        "then try again."
    )
    assert refusal.name == "user_took_over" and refusal.cancelled
    assert (person.DEBOUNCE_S, person.WAIT_CAP_S, person.POLL_S) == (5.0, 100.0, 0.25)


def test_a_state_read_also_waits_out_the_debounce_and_also_gives_up(world):
    who = agent()
    world.call(who)
    world.event()
    assert world.read(who) is None
    assert world.clock.time() == pytest.approx(1005.0, abs=0.25)
    world.event()
    world.person_keeps_going(KEY)
    refusal = world.call(who, state_read=True)
    assert refusal.message.startswith("The user is working in")


def test_call_can_hang_long_time_before_interruption_error(world):
    """The wait falls out of the debounce: the error comes when the person is quiet."""
    who = agent()
    world.call(who)
    world.event()
    world.clock.sleep(1.9)
    refusal = world.call(who)
    assert refusal.message.startswith("The user did something in")
    assert world.clock.time() == pytest.approx(1005.0, abs=0.25)  # waited about 3.1 s


def test_a_state_read_while_the_person_is_busy_does_not_clear(world):
    who = agent()
    world.call(who)
    world.event()
    person.after_state_read(NOTES, clock=world.clock)  # a read that began before
    world.clock.sleep(6.0)
    assert world.call(who).name == "user_took_over"
    person.after_state_read(NOTES, clock=world.clock)
    assert world.call(who) is None


def test_interruption_error_tells_model_exactly_what_do_next(world):
    who = agent()
    world.call(who)
    world.event(KEY)
    world.clock.sleep(5.0)
    refusal = world.call(who)
    assert refusal.message == (
        "The user did something in '/Applications/Notes.app' since your last read. "
        "Call `get_app_state` again before you act on it."
    )
    assert refusal.cancelled  # the person did it: not a crash of the tool


def test_interruption_error_aborts_whole_cell_including_trailing_state(world):
    """A chain of calls stops at the refused action: the state read behind it never runs."""
    who = agent()
    world.call(who)
    world.event()
    world.clock.sleep(5.0)
    ran = []
    with pytest.raises(errors.HarnessError):
        for state_read in (False, True):
            person.gate(who, NOTES, state_read, clock=world.clock)
            ran.append(state_read)
    assert ran == [] and NOTES.path in person._quiet_at


# --- which events count


@pytest.mark.parametrize("kind", [LEFT, RIGHT, OTHER, WHEEL, KEY])
def test_monitor_watches_person_s_input_controlled_app(world, kind):
    who = agent()
    world.call(who)
    world.event(kind)
    assert person._quiet_at[NOTES.path] == 1005.0
    assert world.presence.activity() == ["paused"]


@pytest.mark.parametrize("kind", [2, 4, 5, 6, 11, 12, 26])  # ups, moves, drags, flags
def test_other_event_types_do_not_count(world, kind):
    world.call(agent())
    world.event(kind)
    assert person._quiet_at == {}


def test_person_s_input_told_apart_from_our_own(world):
    """Ours carry our pid as their source, and our tag. Either one is enough."""
    who = agent()
    world.call(who)
    world.event(source_pid=OWN_PID)
    world.event(tag=events.OWN_TAG)
    world.event(KEY, source_pid=OWN_PID, tag=events.OWN_TAG)
    assert person._quiet_at == {} and world.call(who) is None
    world.event(source_pid=777)  # another program's event is the person's
    assert NOTES.path in person._quiet_at


def test_input_a_menu_of_a_background_app_holds_closes_it_and_does_not_count(
    world, monkeypatch
):
    """An open pop-up menu of a driven app in the background takes the person's keys
    and clicks, which were meant for the app they are in."""
    who = agent()
    world.call(who)
    menu, closed = object(), []
    monkeypatch.setattr(person.windows, "shown_menu", lambda target: menu)
    monkeypatch.setattr(
        person.ax, "perform", lambda el, name: closed.append((el, name))
    )
    world.event(KEY, stray=True)
    assert person._quiet_at == {}  # not the person using the app
    assert world.call(who) is None  # the next action is not refused
    delay, close = world.timers[-1]
    close()
    assert delay == 0.0 and closed == [(menu, "AXCancel")]


def test_with_no_menu_to_cancel_the_stray_input_is_answered_with_escape(
    world, monkeypatch
):
    posted = []
    monkeypatch.setattr(person.windows, "shown_menu", lambda target: None)
    monkeypatch.setattr(person.events, "post", lambda pid, event: posted.append(pid))
    monkeypatch.setattr(person.events, "key", lambda code, *rest, down: (code, down))
    person._close_menu(NOTES)
    assert posted == [NOTES.pid, NOTES.pid]


def test_only_the_person_s_own_switch_to_an_app_counts_as_going_there(
    world, monkeypatch
):
    """focus.py asks it before it puts the front app back: a click of theirs in the
    app, Command-Tab, or a click on the Dock. Typing or clicking elsewhere is none."""
    monkeypatch.setattr(person, "_tap", object())
    monkeypatch.setattr(person, "_clicked", (0, -math.inf))
    monkeypatch.setattr(person, "_switched", -math.inf)
    monkeypatch.setattr(
        person, "_bundle_of", lambda pid: person.DOCK if pid == 77 else ""
    )
    since = world.clock.monotonic()
    went = lambda pid=NOTES.pid: person.went_to(pid, since)
    world.event(LEFT, pid=NOTES.pid, source_pid=OWN_PID)  # ours: not the person
    world.event(KEY, pid=NOTES.pid)  # a key follows the front app: it proves nothing
    world.event(LEFT, pid=999)  # a click in another app
    world.event(LEFT, pid=NOTES.pid, stray=True)  # held by a menu, meant for elsewhere
    world.event(KEY, pid=999, keycode=person.TAB)  # Tab alone
    assert not went()
    world.event(LEFT, pid=NOTES.pid)
    assert went() and not went(MAIL.pid)
    assert not person.went_to(
        NOTES.pid, world.clock.monotonic() + 1.0
    )  # before `since`
    world.event(LEFT, pid=999)
    assert not went()  # they clicked away again
    world.event(KEY, pid=999, keycode=person.TAB, command=True)
    assert went() and went(MAIL.pid)
    monkeypatch.setattr(person, "_switched", -math.inf)
    world.event(LEFT, pid=77)  # the Dock
    assert went()


def test_without_the_tap_a_click_says_the_person_went_there(world, monkeypatch):
    """No tap (no grant): where a click went is not known. A click counts, keys do
    not: someone typing in another app is not pulled into the driven one."""
    monkeypatch.setattr(person, "_tap", None)
    idle = {LEFT: 0.2, KEY: 0.1}
    monkeypatch.setattr(
        person, "_hid_idle", lambda kinds: min(idle.get(k, 60.0) for k in kinds)
    )
    now = person.time.monotonic()
    assert person.went_to(NOTES.pid, now - 1.0) and not person.went_to(
        NOTES.pid, now + 1.0
    )
    idle[LEFT] = 60.0  # only keys since then
    assert not person.went_to(NOTES.pid, now - 1.0)


def test_an_event_for_an_app_we_do_not_drive_is_ignored(world):
    who = agent()
    world.call(who)
    world.event(pid=MAIL.pid)
    world.event(pid=0)
    assert person._quiet_at == {} and world.presence.activity() == []


def test_an_app_is_also_known_by_bundle_id_when_its_pid_is_not(world, monkeypatch):
    """A state read launches the app: the gate ran before it had a pid."""
    launched = app("Notes", pid=None)
    world.call(agent(), launched)
    monkeypatch.setattr(
        person, "_bundle_of", lambda pid: {77: launched.bundle_id}.get(pid, "")
    )
    world.event(pid=78)
    assert person._quiet_at == {}
    world.event(pid=77)
    assert launched.path in person._quiet_at


def test_interventions_stored_app_target_reasons(world):
    """Per app, two kinds: the person touched it, the person stopped it."""
    who = agent()
    world.call(who)
    world.call(who, MAIL)
    world.event(pid=NOTES.pid)
    person.stop(MAIL, clock=world.clock)
    assert set(person._quiet_at) == {NOTES.path, MAIL.path}
    assert set(person._stops) == {MAIL.path}
    world.clock.sleep(6.0)
    assert world.call(who, NOTES).name == "user_took_over"
    assert world.call(who, MAIL).name == "stopped_by_user"


def test_the_pointer_shows_paused_then_waiting_when_the_debounce_ends(world):
    world.call(agent())
    world.event()
    world.clock.sleep(2.0)
    world.event()  # moves the 5 s, starts no second timer
    assert [delay for delay, _ in world.timers] == [5.0]
    world.clock.sleep(3.0)
    world.timers.pop()[1]()  # the timer fires 5 s after the first event: 2 s to go
    assert world.timers[0][0] == pytest.approx(2.0)
    world.clock.sleep(2.0)
    world.timers.pop()[1]()
    assert world.presence.activity() == ["paused", "paused", "loading"]
    assert world.timers == [] and person._awaiting_quiet == set()


def test_gate_tells_the_menu_which_apps_are_in_use(world):
    who = agent()
    world.call(who)
    world.call(who)
    world.call(who, MAIL)
    in_use = [call[1] for call in world.presence.calls if call[0] == "apps_in_use"]
    assert in_use == [[NOTES.path], [NOTES.path, MAIL.path]]
    assert person.driven() == [NOTES, MAIL]


# --- the tap


class FakeQuartz:
    """Just enough of the event tap calls. `events` holds field values per event."""

    kCGAnnotatedSessionEventTap, kCGTailAppendEventTap = 2, 1
    kCGEventTapOptionListenOnly = 1
    kCFRunLoopCommonModes = "common"
    kCGEventTargetUnixProcessID, kCGEventSourceUnixProcessID = 40, 41
    kCGEventSourceUserData, kCGMouseEventWindowUnderMousePointer = 42, 91
    kCGKeyboardEventKeycode, kCGKeyboardEventAutorepeat = 9, 8
    kCGWindowListOptionIncludingWindow, kCGWindowOwnerPID = 8, "owner"
    kCGEventSourceStateHIDSystemState = 1
    kCGEventFlagMaskCommand = 0x100000

    def CGEventGetFlags(self, event):
        return event.get("flags", 0)

    def __init__(self, may_listen=True, tap="tap"):
        self.may_listen, self.tap = may_listen, tap
        self.created, self.enabled, self.idle_calls = [], [], []
        self.windows = {}
        self.idle = math.inf

    def CGPreflightListenEventAccess(self):
        return self.may_listen

    def CGEventTapCreate(self, *args):
        self.created.append(args)
        return self.tap

    def CFMachPortCreateRunLoopSource(self, allocator, tap, order):
        return "source"

    def CFRunLoopGetMain(self):
        return "main loop"

    def CFRunLoopAddSource(self, loop, source, mode):
        self.added = (loop, source, mode)

    def CGEventTapEnable(self, tap, on):
        self.enabled.append((tap, on))

    def CGEventGetIntegerValueField(self, event, field):
        return event.get(field, 0)

    def CGWindowListCopyWindowInfo(self, option, window):
        return self.windows.get(window)

    def CGEventSourceSecondsSinceLastEventType(self, state, kind):
        self.idle_calls.append((state, kind))
        return self.idle

    def CGSessionCopyCurrentDictionary(self):
        return self.session


def test_input_observed_through_event_taps_re_enable_themselves(world, monkeypatch):
    quartz = FakeQuartz()
    monkeypatch.setattr(person, "Quartz", quartz)
    monkeypatch.setattr(person, "_tap", None)
    person.start_watch(OWN_PID)
    person.start_watch(OWN_PID)  # a second start makes no second tap
    ((location, place, options, mask, callback, _),) = quartz.created
    assert (location, place, options) == (
        2,
        1,
        1,
    )  # annotated session, tail, listen only
    assert mask == sum(1 << kind for kind in (1, 3, 25, 22, 10))
    assert quartz.added == ("main loop", "source", "common")
    assert quartz.enabled == [("tap", True)]
    for disabled in (0xFFFFFFFE, 0xFFFFFFFF):  # by timeout, by user input
        assert callback(None, disabled, {}, None) == {}
    assert quartz.enabled == [("tap", True)] * 3
    assert person._quiet_at == {}


def test_the_tap_reads_source_tag_target_key_and_repeat(world, monkeypatch):
    quartz = FakeQuartz()
    monkeypatch.setattr(person, "Quartz", quartz)
    monkeypatch.setattr(person, "_tap", "tap")
    saw = lambda *a, stray, command: seen.append((*a, stray, command))
    monkeypatch.setattr(person, "saw", saw)
    monkeypatch.setattr(person, "_frontmost_pid", lambda: 900)
    monkeypatch.setattr(person, "_targets", {NOTES.path: NOTES})
    quartz.windows = {55: [{"owner": 502}]}
    seen = []
    held = {"flags": 0x100108}  # Command is down
    person._on_event(None, KEY, {40: 501, 41: 7, 42: 3, 9: 53, 8: 1} | held, None)
    person._on_event(None, LEFT, {91: 55}, None)  # no target on it: the window's owner
    person._on_event(None, KEY, {}, None)  # nothing on it: the front app
    person._on_event(None, WHEEL, {91: 56}, None)  # a window that is gone
    assert seen == [
        (
            KEY,
            7,
            3,
            501,
            53,
            True,
            True,
            True,
        ),  # a key that went to an app not in front
        (LEFT, 0, 0, 502, 0, False, False, False),
        (KEY, 0, 0, 900, 0, False, False, False),
        (WHEEL, 0, 0, 900, 0, False, False, False),
    ]
    monkeypatch.setattr(person, "_targets", {})  # nothing driven: no lookup at all
    monkeypatch.setattr(person, "_target_pid", lambda event: 1 / 0)
    person._on_event(None, KEY, {40: 501, 9: 53}, None)
    assert seen[-1] == (KEY, 0, 0, 0, 53, False, False, False)


def test_a_failing_callback_never_raises_into_the_tap(world, monkeypatch):
    monkeypatch.setattr(person, "_tap", "tap")
    monkeypatch.setattr(person, "saw", lambda *args: 1 / 0)
    assert person._on_event(None, KEY, "an event", None) == "an event"


@pytest.mark.parametrize("quartz", [FakeQuartz(may_listen=False), FakeQuartz(tap=None)])
def test_without_the_grant_there_is_no_tap_and_no_prompt(world, monkeypatch, quartz):
    monkeypatch.setattr(person, "Quartz", quartz)
    monkeypatch.setattr(person, "_tap", None)
    person.start_watch(OWN_PID)
    assert person._tap is None and quartz.enabled == []
    assert quartz.created == [] or quartz.may_listen


def test_platform_fact_hid_session_event_counters_diverge_so(monkeypatch):
    """Without a tap the idle time of the hardware state stands in. It can only speak
    for the front app, and it never asks a private state."""
    quartz, clock = FakeQuartz(), Clock()
    monkeypatch.setattr(person, "Quartz", quartz)
    monkeypatch.setattr(person, "_quiet_at", {})
    monkeypatch.setattr(person, "_frontmost_pid", lambda: NOTES.pid)
    quartz.idle = 1.5
    person._touch_from_idle_time(MAIL, clock)  # not in front
    person._touch_from_idle_time(app("Notes", pid=None), clock)  # not running yet
    assert person._quiet_at == {}
    person._touch_from_idle_time(NOTES, clock)
    assert person._quiet_at[NOTES.path] == 1003.5
    assert {state for state, _ in quartz.idle_calls} == {1}
    assert {kind for _, kind in quartz.idle_calls} == set(person.EVENT_TYPES)
    quartz.idle = 4.0  # an older event never shortens what is already known
    person._touch_from_idle_time(NOTES, clock)
    assert person._quiet_at[NOTES.path] == 1003.5
    quartz.idle, person._quiet_at = 5.0, {}
    person._touch_from_idle_time(NOTES, clock)
    assert person._quiet_at == {}


@pytest.mark.parametrize(
    "tap, secure_input, asked",
    [(None, None, True), ("tap", None, False), ("tap", 321, True)],
)
def test_idle_time_is_asked_when_there_is_no_tap_or_keys_are_hidden_from_it(
    world, monkeypatch, tap, secure_input, asked
):
    calls = []
    monkeypatch.setattr(person, "_tap", tap)
    monkeypatch.setattr(person, "secure_input_pid", lambda: secure_input)
    monkeypatch.setattr(
        person, "_touch_from_idle_time", lambda *args: calls.append(args)
    )
    world.call(agent())
    assert bool(calls) is asked


def test_platform_fact_pointer_modifier_state_readable_without_events():
    """Read only: where the pointer is and which modifiers are down. Nothing is posted."""
    import Quartz
    from AppKit import NSEvent

    assert isinstance(Quartz.CGEventSourceFlagsState(1), int)
    assert isinstance(Quartz.CGEventSourceButtonState(1, 0), bool)
    assert isinstance(NSEvent.mouseLocation().x, float)  # the person may be moving it
    assert isinstance(NSEvent.pressedMouseButtons(), int)


def test_secure_input_owner_readable(monkeypatch):
    quartz = FakeQuartz()
    monkeypatch.setattr(person, "Quartz", quartz)
    for session, owner in (
        ({"kCGSSessionSecureInputPID": 321}, 321),
        ({}, None),
        (None, None),
    ):
        quartz.session = session
        assert person.secure_input_pid() == owner


# --- stop


def test_stop_user_driven_model_has_no_stop_method(world):
    """Every later call on the stopped app fails, also a state read, also after a turn
    end the model could send itself, until the person resumes."""
    who = agent(signals_turns=True)
    world.call(who)
    world.call(who, MAIL)
    person.stop(NOTES, clock=world.clock)
    assert person.stopped(NOTES) and not person.stopped(MAIL)
    world.clock.sleep(500.0)
    for state_read in (False, True, False):
        refusal = world.call(who, NOTES, state_read)
        assert refusal.message == (
            "The user stopped this app session. Stop here, and say in your final "
            "message that you stopped because they asked and can go on when they "
            "want. To go on, ask the user to click Resume in the macOS Harness menu "
            "bar item, or run `macos-harness resume` in a new terminal window."
        )
        assert refusal.cancelled and refusal.code == 111
    assert world.call(who, MAIL) is None
    person.turn_ended(who)
    person.turn_started(who)
    assert world.read(who).name == "stopped_by_user"  # a turn end lifts nothing
    person.resume(NOTES)
    assert world.read(who) is None and world.call(who) is None


def test_hosts_can_stop_app_session_pending_operations_finish(world):
    """A stop raises nothing in the call that runs. A call that still waits for the
    person is refused at its next poll."""
    who = agent()
    world.call(who)
    person.stop(NOTES, clock=world.clock)  # returns: the running call goes on
    person.resume(NOTES)
    world.event()
    real_sleep = world.clock.sleep

    def stop_after_two_seconds(seconds):
        real_sleep(seconds)
        if world.clock.time() >= 1002.0 and not person.stopped(NOTES):
            person.stop(NOTES, clock=world.clock)

    world.clock.sleep = stop_after_two_seconds
    assert world.call(who).name == "stopped_by_user"
    assert world.clock.time() == pytest.approx(1002.0, abs=0.25)


def test_the_person_using_or_stopping_the_app_interrupts_the_running_call(world):
    notes = app("Notes", pid=7)
    world.call(agent(), notes)
    since = world.clock.monotonic()
    assert not person.interrupted(notes, since)
    world.clock.sleep(1.0)
    person.saw(person.KEY_DOWN, 0, 0, 7, clock=world.clock)  # the person types there
    assert person.interrupted(notes, since)
    assert not person.interrupted(notes, world.clock.monotonic() + 0.1)  # a later call
    person.stop(notes, clock=world.clock)
    assert person.interrupted(notes, world.clock.monotonic() + 0.1)


def test_stop_with_no_app_stops_every_driven_app(world):
    who = agent()
    world.call(who)
    world.call(who, MAIL)
    person.stop(clock=world.clock)
    assert person.stopped(NOTES) and person.stopped(MAIL)
    assert world.presence.activity() == ["paused"]
    person.resume()
    assert not person.stopped(NOTES) and not person.stopped(MAIL)
    assert world.presence.activity() == ["paused", "loading"]


def test_a_stop_cancels_the_menu_the_agent_left_open(world, monkeypatch):
    """The last action opened a pop-up menu, then the person stopped the app: the menu
    stayed and took their next Return. Only a menu is cancelled, no key is sent."""
    done = []
    menu = object()
    shown = {NOTES.path: menu}
    fake_windows = SimpleNamespace(
        shown_menu=lambda target: shown.get(target.path),
        close_menu=lambda target: done.append(("close_menu", target.path)),
    )
    monkeypatch.setattr(person, "windows", fake_windows)
    monkeypatch.setattr(
        person, "ax", SimpleNamespace(perform=lambda el, name: done.append((el, name)))
    )
    monkeypatch.setattr(
        person, "events", SimpleNamespace(post=lambda *a: done.append("key"), OWN_TAG=1)
    )
    who = agent()
    world.call(who)
    world.call(who, MAIL)
    person.stop(clock=world.clock)
    for _delay, call in world.timers:
        call()
    assert done == [(menu, "AXCancel"), ("close_menu", NOTES.path)]


def test_stop_everything_also_refuses_an_app_not_driven_yet(world):
    """Escape twice, the menu's Stop All or `stop`: a call on Notes was refused, and
    the next call, on an app never driven before, ran."""
    who = agent(signals_turns=True)
    world.call(who)
    person.stop(clock=world.clock)
    assert world.call(who, MAIL, state_read=True).name == "stopped_by_user"
    assert person.stopped(MAIL)
    person.resume(
        NOTES
    )  # one app lifted: the stop of everything ends, Mail keeps its mark
    assert world.read(who) is None
    assert world.call(who, MAIL, state_read=True).name == "stopped_by_user"
    person.resume()
    assert world.read(who, MAIL) is None


def test_a_stop_holds_when_the_helper_is_started_again(world, monkeypatch):
    """`macos-harness helper restart` lifted every stop: they lived in memory only."""

    def restarted():
        monkeypatch.setattr(person, "_stops", set())
        monkeypatch.setattr(person, "_stop_all", False)
        person.start_watch(OWN_PID)

    person.stop(MAIL, clock=world.clock)
    restarted()
    assert person.stopped(MAIL) and not person.stopped(NOTES)
    assert world.presence.calls[-1] == ("stopped", True)  # the menu shows Resume again
    person.stop(clock=world.clock)
    restarted()
    assert person.stopped(NOTES)  # everything
    person.resume()
    assert ("stopped", False) in world.presence.calls
    restarted()
    assert not person.stopped(MAIL) and not person.stopped(NOTES)
    (protocol.state_dir() / "stops.json").write_text("{", encoding="utf-8")
    restarted()
    assert person.stopped(NOTES)  # a file that cannot be read stops everything
    person.resume()


@pytest.mark.parametrize(
    "record",
    [
        {"all": True, "apps": []},
        {"version": "0.1.9", "all": False, "apps": ["/Applications/Mail.app"]},
    ],
)
def test_a_stop_of_another_version_is_dropped_at_start(world, record, caplog):
    """A build before the Escape-twice fix wrote {"all": true} that nobody meant, and
    0.2.0 refused every app with it until a resume. Installing a version is the
    person's own act: the stops of another one go, and the log says so."""
    protocol.state_dir().mkdir(parents=True, exist_ok=True)
    (protocol.state_dir() / "stops.json").write_text(json.dumps(record), "utf-8")
    person.start_watch(OWN_PID)
    assert not person.stopped(NOTES) and not person.stopped(MAIL)
    assert ("stopped", True) not in world.presence.calls  # no menu bar item for it
    assert f"dropped the stops of version {record.get('version')}" in caplog.text


def test_state_read_must_come_before_any_action_turn(world):
    """After the person lifts a stop, the first action still needs a fresh state read."""
    who = agent()
    world.call(who)
    person.stop(NOTES, clock=world.clock)
    person.resume(NOTES)
    assert world.call(who).message.startswith("The user did something in")
    assert world.read(who) is None and world.call(who) is None


def test_a_stop_holds_however_long_the_agent_stays_quiet(world):
    """For a host that sends no turn signals a stop ended after 120 s with no refused
    call, and with it the stop of everything, for every agent."""
    who, hooked = agent(), agent("hooked", signals_turns=True)
    world.call(who)
    person.stop(clock=world.clock)
    world.clock.sleep(10_000.0)
    assert world.call(who).name == "stopped_by_user"
    assert world.call(hooked, MAIL, state_read=True).name == "stopped_by_user"
    person.resume()
    assert world.read(who) is None and not person.stopped(NOTES)


def test_the_menu_stops_and_resumes_through_the_callbacks(world, monkeypatch):
    monkeypatch.setattr(person, "Quartz", FakeQuartz(may_listen=False))
    monkeypatch.setattr(person, "_tap", None)
    person.start_watch(OWN_PID)
    who = agent()
    world.call(who)
    world.presence.stop_callback(NOTES)
    assert person.stopped(NOTES)
    world.presence.resume_callback(NOTES)
    world.presence.stop_callback(None)
    assert person.stopped(NOTES)
    world.presence.resume_callback(None)
    assert not person.stopped(NOTES)


def test_a_stop_from_anywhere_is_shown_in_the_menu(world):
    """The command and Escape twice have no menu pick: the first row says it."""
    world.call(agent())
    person.stop(NOTES, by="command")
    person.resume(NOTES)
    person.stop(None, by="escape")
    shown = [call[1] for call in world.presence.calls if call[0] == "label"]
    assert shown == [f"Stopped {NOTES.name}", "Resumed", f"Stopped {NOTES.name}"]
    person.resume(None)
    person.turn_ended(agent())
    person.stop(None)  # nothing is driven any more
    assert world.presence.calls[-1] == ("label", "Stopped every app")


def test_stop_end_reasons_enumerated(world, caplog):
    assert person.STOP_REASONS == ("command", "menu", "escape")
    world.call(agent())
    with caplog.at_level("INFO", logger="macos_harness.person"):
        person.stop(NOTES, clock=world.clock)
        person.stop(NOTES, by="menu", clock=world.clock)
        world.escape()
        world.escape()
        person.stop(NOTES, by="made up", clock=world.clock)
    said = [record.getMessage().split(":")[0] for record in caplog.records]
    assert said == [
        f"stopped by {by}" for by in ("command", "menu", "escape", "command")
    ]


# --- Escape twice


def presses(world, *sequence) -> bool:
    """Play (seconds since start, who, held) key downs. True when they stopped the app."""
    world.call(agent())
    start = world.clock.now
    for at, who, *held in sequence:
        world.clock.now = start + at
        own = {"source_pid": OWN_PID} if who == "agent" else {}
        world.escape(repeat=bool(held), **own)
    return person.stopped(NOTES)


@pytest.mark.parametrize(
    "sequence, stops",
    [
        ([(0.0, "person")], False),
        ([(0.0, "person"), (0.1, "person")], True),
        ([(0.0, "person"), (0.45, "person")], True),
        ([(0.0, "person"), (0.7, "person")], False),
        ([(0.0, "agent"), (0.1, "agent")], False),
        ([(0.0, "agent"), (0.1, "person")], False),
        ([(0.0, "person"), (0.1, "agent"), (0.2, "person")], True),
        ([(0.0, "person"), (0.1, "person", "held"), (0.2, "person", "held")], False),
        (
            [(0.0, "person"), (0.7, "person"), (1.0, "person")],
            True,
        ),  # the second starts a new pair
        ([(0.0, "person"), (0.1, "person"), (0.2, "person")], True),
    ],
)
def test_escape_twice_within_500_ms_stops_every_driven_app(world, sequence, stops):
    assert presses(world, *sequence) is stops


def test_escape_twice_with_no_app_driven_stops_nothing(world):
    """Escape twice in an editor or a dialog, with no agent at work, stopped every app
    and wrote it down: the harness refused every call until a resume, also after a
    restart."""
    world.escape()
    world.escape()
    assert not person.stopped(NOTES)
    assert not (protocol.state_dir() / "stops.json").exists()
    assert not [
        call for call in world.presence.calls if call[0] in ("label", "stopped")
    ]


def test_escape_twice_while_an_app_is_driven_stops_everything_for_good(world):
    world.call(agent())
    world.escape()
    world.escape()
    assert person.stopped(NOTES) and person.stopped(MAIL)
    record = json.loads((protocol.state_dir() / "stops.json").read_text("utf-8"))
    assert record["all"] is True
    person.resume()


def test_escape_twice_counts_when_the_app_became_driven_between_the_presses(world):
    world.escape()
    world.call(agent())  # the agent's first call lands between the two presses
    world.clock.sleep(0.1)
    world.escape()
    assert person.stopped(NOTES)
    person.resume()


def test_a_third_escape_does_not_pair_with_the_second(world):
    assert presses(world, (0.0, "person"), (0.1, "person"))
    person.resume()
    world.clock.sleep(0.1)
    world.escape()
    assert not person.stopped(NOTES)


def test_another_key_twice_stops_nothing(world):
    world.call(agent())
    world.event(KEY, keycode=36)
    world.event(KEY, keycode=36)
    assert not person.stopped(NOTES)


# --- live: the real tap, fed only by an event that carries our tag


@pytest.mark.live
def test_live_tap_drops_our_own_event_and_counts_the_same_event_from_a_stranger(
    world, monkeypatch
):
    """Posts one tagged key down to this test process and to nothing else. The real
    tap must see it with our pid as its source, and drop it. Read as a stranger's
    event (tag and pid unknown to the module) the same event is counted."""
    import os
    import time

    import Quartz

    if not Quartz.CGPreflightListenEventAccess():
        pytest.skip("this app may not listen to input events")
    me = app("Tests", pid=os.getpid())
    seen = []
    counted = person.saw
    monkeypatch.setattr(
        person, "saw", lambda *a, **kw: (seen.append(a), counted(*a, **kw))[1]
    )
    monkeypatch.setattr(person, "_frontmost_pid", os.getpid)  # where the person is
    monkeypatch.setattr(person, "_tap", None)
    monkeypatch.setattr(person, "_own_pid", os.getpid())
    world.call(agent(), me)
    person._start_tap()
    tap = person._tap
    assert tap is not None

    def post_one_tagged_key():
        key = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventSetIntegerValueField(key, events.USER_DATA_FIELD, events.OWN_TAG)
        Quartz.CGEventPostToPid(os.getpid(), key)
        end = time.time() + 2.0
        while time.time() < end and not seen:
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.05, False)
        return seen.pop()

    try:
        kind, source_pid, tag, pid, *_ = post_one_tagged_key()
        assert (kind, source_pid, tag, pid) == (
            KEY,
            os.getpid(),
            events.OWN_TAG,
            os.getpid(),
        )
        assert person._quiet_at == {}

        monkeypatch.setattr(person, "_own_pid", 0)
        monkeypatch.setattr(person, "events", fakes_g.FakeEvents(own_tag=1))
        post_one_tagged_key()
        assert me.path in person._quiet_at
    finally:
        Quartz.CGEventTapEnable(tap, False)
        Quartz.CFMachPortInvalidate(tap)


# --- turns


def test_turn_ended_text(world):
    who = agent()
    person.turn_started(who)
    person.turn_ended(who)
    assert world.call(who, state_read=True).message == (
        "The turn is over, so macOS Harness takes no more calls. It takes calls again "
        "after the user's next message."
    )
    assert person.driven() == []  # a refused call does not make the app driven


def test_host_tells_helper_turn_ends_calls_fail_until(world):
    who = agent()
    person.turn_started(who)
    assert who.signals_turns and world.call(who) is None
    person.turn_ended(who)
    assert who.ended and world.call(who).name == "turn_ended"
    person.turn_started(who)
    assert not who.ended and world.call(who) is None


def test_turn_ended_without_turn_started_only_resets(world):
    who = agent(denied={NOTES.path})
    world.call(who)
    world.event()
    person.turn_ended(who)
    assert not who.ended and who.denied == set()
    assert person._quiet_at == person._drivers == {}
    assert person.driven() == []
    assert world.presence.calls[-1] == ("apps_in_use", [])
    assert world.call(who) is None


def test_one_agent_s_turn_end_keeps_the_marks_of_an_app_another_agent_drives(world):
    first, second = agent("agent-1"), agent("agent-2")
    world.call(first)
    world.call(second)
    world.call(first, MAIL)
    person.stop(NOTES, clock=world.clock)
    person.stop(MAIL, clock=world.clock)
    person.turn_ended(first)
    assert person.stopped(NOTES) and person.stopped(MAIL)  # stops outlive a turn end
    assert person.driven() == [NOTES]
    assert world.call(second).name == "stopped_by_user"
    person.turn_ended(second)
    assert person.stopped(NOTES)
    person.resume()
    assert not person.stopped(NOTES) and not person.stopped(MAIL)
