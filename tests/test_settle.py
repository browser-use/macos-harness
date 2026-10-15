"""settle.wait against a clock moved by hand and an observer fired by hand."""

from __future__ import annotations

import inspect
import types

import pytest
from fakes import El, FakeAx
from fakes_f import ScriptedClock, background_app

from macos_harness import errors, settle
from macos_harness.model import AppKind, AppTarget, Capture, WindowRef

TARGET = AppTarget("/Applications/Demo.app", "dev.demo", "Demo", 100)
NATIVE, WEB = AppKind(), AppKind(http=True)


class Rig:
    def __init__(self, monkeypatch, ax=None):
        self.clock = ScriptedClock()
        self.ax = ax or FakeAx({100: El("AXApplication")})
        self.window = WindowRef(
            El("AXWindow"), 7, 100, "Demo", (0.0, 0.0, 400.0, 300.0)
        )
        self.loading, self.reads, self.asked = False, [], []
        monkeypatch.setattr(settle, "ax", self.ax)
        fake_windows = types.SimpleNamespace(key_window=self.key_window)
        monkeypatch.setattr(settle, "windows", fake_windows)

    def key_window(self, target):
        self.asked.append(self.clock.elapsed)
        if self.window is None:
            raise errors.no_windows()
        return self.window

    def read(self, window):
        capture = Capture("Demo", "Demo", window.window_id, [], loading=self.loading)
        self.reads.append((self.clock.elapsed, capture))
        return capture

    def wait(self, kind=NATIVE, **options):
        return settle.wait(TARGET, kind, self.read, clock=self.clock, **options)

    def fire(self, offset, name, element=None):
        self.clock.at(offset, lambda: self.ax.observers[-1].fire(name, element))

    @property
    def polls(self):
        return len(self.clock.sleeps)

    @property
    def observer(self):
        return self.ax.observers[-1]


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


# --- the loop -------------------------------------------------------------------------


def test_native_floor_is_three_polls(rig):
    capture = rig.wait()
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]
    assert rig.clock.elapsed == pytest.approx(0.3)
    assert capture is rig.reads[0][1] and len(rig.reads) == 1


def test_a_read_whose_client_is_gone_stops_waiting(rig):
    rig.loading = True  # the app would keep it waiting the whole 5 s
    with pytest.raises(errors.HarnessError) as refusal:
        rig.wait(abandoned=lambda: rig.clock.elapsed >= 0.5)
    assert refusal.value.name == "request_timed_out"
    assert rig.clock.elapsed == pytest.approx(0.5)
    assert rig.observer.stopped  # its observer goes with it


def test_web_like_floor_is_one_poll(rig):
    assert rig.wait(WEB) is not None
    assert rig.clock.sleeps == [0.25]


def test_start_delay_caller_passes_delay_nil_predicate_app(rig):
    """With no poll given the app decides: 0.25 s web-like, 0.1 s the rest."""
    for kind in (AppKind(electron=True), AppKind(chromium=True), AppKind(http=True)):
        rig.clock.sleeps.clear()
        rig.wait(kind)
        assert set(rig.clock.sleeps) == {settle.POLL_WEB_LIKE} == {0.25}
    rig.clock.sleeps.clear()
    rig.wait(AppKind(catalyst=True))
    assert set(rig.clock.sleeps) == {settle.POLL_NATIVE} == {0.1}


def test_explicit_start_delay_used_6_17_callers_other(rig):
    """A poll the caller gives wins over the app's own, also for a web app."""
    rig.wait(WEB, poll=0.1)
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]


def test_poll_of_a_launch_is_honoured(rig):
    rig.fire(0.02, "AXCreated")
    rig.wait(poll=0.05)
    assert rig.clock.sleeps == [0.05] * 6  # 0.02 + 0.25, seen at the look at 0.3


def test_quiet_window_every_caller_passes_same_notificationdelay(rig):
    """Quiet means 0.25 s with no counted notification, unless told otherwise."""
    assert settle.QUIET == 0.25
    assert inspect.signature(settle.wait).parameters["quiet"].default == 0.25
    rig.fire(0.12, "AXLayoutChanged")
    rig.wait()
    assert rig.reads[0][0] == pytest.approx(0.4)  # 0.12 + 0.25, seen at the look at 0.4
    assert rig.asked == [pytest.approx(0.4)]  # no look at the window before it is quiet


def test_settle_primitive_takes_base_delay_notification_delay_scroll(rig):
    """The one function, its arguments and their defaults."""
    parameters = inspect.signature(settle.wait).parameters
    assert list(parameters) == [
        "target",
        "kind",
        "read",
        "poll",
        "quiet",
        "limit",
        "clock",
        "abandoned",
    ]  # no scroll switch
    defaults = {name: p.default for name, p in parameters.items()}
    assert (defaults["poll"], defaults["quiet"], defaults["limit"]) == (None, 0.25, 5.0)


def test_hard_stop_loop_exits_now_start_5_0(rig):
    """No window at all runs the full 5.0 s and gives None. It never raises."""
    rig.window = None
    assert rig.wait() is None
    assert settle.LIMIT == 5.0
    assert 5.0 <= rig.clock.elapsed <= 5.0 + 0.1 + 1e-6
    assert rig.reads == [] and rig.observer.stopped


def test_long_wait_near_5_s_applies_app_signals(rig):
    """A window that keeps a busy sign up runs to the stop."""
    rig.loading = True
    assert rig.wait() is None
    assert 5.0 <= rig.clock.elapsed <= 5.1 + 1e-6
    assert len(rig.reads) >= 45  # looked again at every quiet poll


def test_loading_indicator_identifier_hard_coded(rig):
    """The capture says whether a loading sign was found; the wait goes on."""
    rig.loading = True
    rig.clock.at(0.95, lambda: setattr(rig, "loading", False))
    capture = rig.wait()
    assert capture.loading is False
    assert rig.clock.elapsed == pytest.approx(1.0)
    assert capture is rig.reads[-1][1]


def test_extreme_limit_is_checked_before_the_sleep_only(rig):
    rig.window = None
    rig.wait(poll=0.25, limit=0.3)
    assert rig.clock.sleeps == [0.25, 0.25]  # 0.25 < 0.3, so one more poll


def test_a_window_that_appears_after_two_seconds_is_read(rig):
    window, rig.window = rig.window, None
    rig.clock.at(1.98, lambda: setattr(rig, "window", window))
    capture = rig.wait(poll=0.05)
    assert capture is rig.reads[0][1]
    assert rig.clock.elapsed == pytest.approx(2.0)


def test_another_window_error_is_not_swallowed(rig, monkeypatch):
    def broken(target):
        raise errors.accessibility("AXError.cannotComplete")

    monkeypatch.setattr(settle.windows, "key_window", broken)
    with pytest.raises(errors.HarnessError, match="cannotComplete"):
        rig.wait()
    assert rig.observer.stopped


# --- what counts as a notification ----------------------------------------------------


def test_ax_notifications_named_settle_invalidation_logic(rig):
    """These 10 names, on the application element, in this order."""
    seen = []
    observe = rig.ax.observe

    def recording(pid, element, names, callback):
        seen.append((pid, element, tuple(names)))
        return observe(pid, element, names, callback)

    rig.ax.observe = recording
    rig.wait()
    assert seen == [(100, rig.ax.apps[100], settle.NOTIFICATIONS)]
    assert settle.NOTIFICATIONS == (
        "AXTitleChanged",
        "AXLayoutChanged",
        "AXFocusedWindowChanged",
        "AXFocusedUIElementChanged",
        "AXElementBusyChanged",
        "AXMenuOpened",
        "AXSheetCreated",
        "AXWindowCreated",
        "AXCreated",
        "AXUIElementDestroyed",
    )


@pytest.mark.parametrize("name", settle.NOTIFICATIONS[1:])
def test_settle_tracks_time_last_ax_notification(rig, name):
    """Each of the nine plain names moves the time of the last notification."""
    rig.fire(0.2, name)
    rig.wait()
    assert rig.reads[0][0] == pytest.approx(0.5)  # 0.2 + 0.25, seen at the look at 0.5


def test_notification_vocabulary_only_10_names_count(rig):
    """Only the 10 count. Selection, value, move and resize names do not."""
    others = (
        "AXValueChanged",
        "AXSelectedTextChanged",
        "AXSelectedChildrenChanged",
        "AXSelectedRowsChanged",
        "AXRowCountChanged",
        "AXMoved",
        "AXResized",
        "AXWindowMoved",
        "AXWindowResized",
        "AXMenuClosed",
        "AXMenuItemSelected",
        "AXMainWindowChanged",
        "AXApplicationActivated",
    )
    assert not set(others) & set(settle.NOTIFICATIONS)
    for step, name in enumerate(others):
        rig.fire(0.05 + 0.02 * step, name)
    rig.wait()
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]


def test_the_same_title_again_does_not_reset_quiet_and_a_new_title_does(rig):
    titled = El("AXWindow", {"AXTitle": "Inbox"})
    rig.fire(0.02, "AXTitleChanged", titled)  # the first title of an element counts
    for step in range(1, 40):
        rig.fire(0.02 + 0.1 * step, "AXTitleChanged", titled)  # the same title forever
    rig.wait()
    assert rig.reads[0][0] == pytest.approx(0.3)  # 0.02 + 0.25, seen at the look at 0.3

    rig.reads.clear()
    start = rig.clock.elapsed
    other = El("AXWindow", {"AXTitle": "Inbox"})
    rig.fire(start + 0.05, "AXTitleChanged", other)
    rig.clock.at(start + 0.2, lambda: other.attrs.update(AXTitle="Inbox (1)"))
    rig.fire(start + 0.21, "AXTitleChanged", other)
    rig.wait()
    assert rig.reads[0][0] - start == pytest.approx(0.5)  # 0.21 + 0.25, seen at 0.5


def test_a_title_that_is_not_text_does_not_count(rig):
    rig.fire(0.2, "AXTitleChanged", El("AXWindow", {"AXTitle": 5}))
    rig.fire(0.22, "AXTitleChanged", El("AXWindow"))
    rig.wait()
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]


def test_scroll_events_can_part_settle_signal_but_no(rig):
    """There is no scroll switch, so a value change
    (some apps send bursts of it during a tree walk) is not listened for."""
    rig.fire(0.2, "AXValueChanged")
    rig.wait()
    assert "AXValueChanged" not in rig.observer.names
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]


def test_settle_called_inside_action_methods_includingscrollevents_false_every(rig):
    """No action waits by itself, so the module offers the wait and no more."""
    public = [name for name in vars(settle) if not name.startswith("_")]
    functions = [n for n in public if inspect.isfunction(getattr(settle, n))]
    assert functions == ["wait"]


def test_wait_descendant_given_type_appear():
    """Waiting for a descendant of a type is on no tool path. Not built."""
    assert not [name for name in vars(settle) if "descendant" in name.lower()]


# --- the observer ---------------------------------------------------------------------


def test_an_app_that_never_notifies_still_goes_quiet(rig):
    """An app that takes every name and never sends one still goes quiet."""
    rig.wait()
    assert rig.polls == 3 and rig.observer.names == list(settle.NOTIFICATIONS)


def test_a_name_that_cannot_be_registered_is_ignored(monkeypatch):
    """Error -25204 on one name: the observer skips it, the wait runs on the rest."""

    class Picky(FakeAx):
        def observe(self, pid, el, names, callback):
            return super().observe(
                pid, el, [n for n in names if n != "AXCreated"], callback
            )

    rig = Rig(monkeypatch, Picky({100: El("AXApplication")}))
    rig.fire(0.1, "AXCreated")
    rig.fire(0.2, "AXSheetCreated")
    assert rig.wait() is not None
    assert rig.reads[0][0] == pytest.approx(0.5)


def test_any_other_error_from_registering_is_raised(monkeypatch):
    class Refusing(FakeAx):
        def observe(self, pid, el, names, callback):
            raise errors.accessibility(self.error_name(-25202))

    rig = Rig(monkeypatch, Refusing({100: El("AXApplication")}))
    with pytest.raises(errors.HarnessError, match="invalidUIElement"):
        rig.wait()
    assert rig.polls == 0


def test_the_observer_is_stopped_also_when_the_read_fails(rig):
    def broken(window):
        raise errors.accessibility("AXError.cannotComplete")

    with pytest.raises(errors.HarnessError):
        settle.wait(TARGET, NATIVE, broken, clock=rig.clock)
    assert rig.observer.stopped
    rig.wait()
    assert rig.observer.stopped and len(rig.ax.observers) == 2


# --- what the caller builds on --------------------------------------------------------


def test_settle_paid_once_capture_once_action_action_marks(rig):
    """One wait is one observer and one read, and it knows no session flag."""
    capture = rig.wait()
    assert len(rig.ax.observers) == 1 and len(rig.reads) == 1
    assert capture is rig.reads[0][1]  # the caller prints this very capture
    assert "app" not in inspect.signature(settle.wait).parameters


def test_capture_time_settle_reads_and_clears_the_flag_first(rig):
    """A wait that fails leaves nothing behind for the next read to undo."""

    def broken(window):
        raise errors.accessibility("AXError.cannotComplete")

    with pytest.raises(errors.HarnessError):
        settle.wait(TARGET, NATIVE, broken, clock=rig.clock)
    rig.clock.sleeps.clear()
    assert rig.wait() is not None
    assert rig.clock.sleeps == [0.1, 0.1, 0.1]  # counted from its own start


def test_keyboard_actions_carry_their_own_settle_switch(rig):
    """There is no per-action switch. The same wait serves every action."""
    assert not {"action", "tool", "key"} & set(
        inspect.signature(settle.wait).parameters
    )


def test_no_settle_runs_between_actions_inside_cell_typing(rig):
    """Run before keys, the wait ends only once the new sheet stopped moving."""
    rig.fire(0.05, "AXSheetCreated")
    for step in range(8):
        rig.fire(0.08 + 0.05 * step, "AXCreated")  # the sheet builds until 0.43 s
    rig.fire(0.43, "AXFocusedUIElementChanged")
    rig.wait()
    assert rig.reads[0][0] == pytest.approx(0.7)  # 0.43 + 0.25, seen at the look at 0.7


# --- live -----------------------------------------------------------------------------


@pytest.mark.live
def test_live_an_idle_app_settles_at_the_floor():
    """A real app that does nothing is quiet after three polls, and its window is read."""
    import time

    from macos_harness import apps

    with background_app() as target:
        reads = []

        def read(window):
            reads.append(window)
            return Capture(window.title or "", target.name, window.window_id, [])

        settle.wait(target, apps.kind(target), read, poll=0.05)  # the launch is over
        reads.clear()
        start = time.monotonic()
        capture = settle.wait(target, apps.kind(target), read)
        assert 0.3 <= time.monotonic() - start < 1.5
        assert len(reads) == 1 and capture.window_id == reads[0].window_id != 0
