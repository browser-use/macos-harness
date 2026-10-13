"""The app is made to believe it is active. The system's front app never changes,
and when it does by accident, the app that was in front is put back."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fakes import El
from fakes_e import APP_PID, OTHER_PID, rig

from macos_harness import errors
from macos_harness.model import Line

SOURCE = (Path(__file__).parents[1] / "src/macos_harness/focus.py").read_text()
ACTIVATION = [
    # type 13 subtype 1 with the window id, then one click at the activation point
    ("notice", 13, 1, 7, 0xC0000),
    ("down", None, None, 7, 0),
    ("up", None, None, 7, 0),
]


def rows(r):
    keep = ("type", "kind", "subtype", "window", "flags")
    return [tuple(e.get(k) for k in keep) for e in r.events.posted]


def enforced(r):
    enforcer = r.focus.enforcer(r.app.target)
    with enforcer.active(r.windows.window):
        pass
    return enforcer


def test_focus_enforcer_makes_target_app_behave_active_without(monkeypatch):
    r = rig(monkeypatch)
    enforcer = r.focus.enforcer(r.app.target)
    assert not enforcer.believes_focus
    with enforcer.active(r.windows.window):
        assert enforcer.believes_focus
    assert enforcer.believes_focus  # it lasts until release, not for one action
    assert r.front == [OTHER_PID]  # nobody asked the system for anything
    assert r.ax.apps[APP_PID].performed == []  # nothing raised, nothing set


def test_synthesized_activation_key_focus_events_sent_app(monkeypatch):
    r = rig(monkeypatch)
    enforced(r)
    assert rows(r) == ACTIVATION
    clicks = [e for e in r.events.posted if e["type"] in ("down", "up")]
    assert {e["point"] for e in clicks} == {(130.0, 116.0)}
    assert {e["pid"] for e in r.events.posted} == {APP_PID}


def test_activation_without_a_window_is_the_event_alone(monkeypatch):
    r = rig(monkeypatch)
    with r.focus.enforcer(r.app.target).active(None):
        pass
    assert rows(r) == [("notice", 13, 1, 0, 0)]


def test_activation_without_an_activation_point_sends_no_click(monkeypatch):
    r = rig(monkeypatch)
    del r.window.attrs["AXActivationPoint"]
    enforced(r)
    assert rows(r) == ACTIVATION[:1]


def test_enforcer_tracks_three_separate_truths_about_activation(monkeypatch):
    r = rig(monkeypatch)
    enforcer = r.focus.enforcer(r.app.target)

    def truths():
        return (
            enforcer.really_active,
            enforcer.believes_active,
            enforcer.believes_focus,
        )

    assert truths() == (False, False, False)
    enforced(r)
    assert truths() == (False, True, True)
    r.front[0] = APP_PID  # the person switched to the app
    assert truths() == (True, True, True)
    r.front[0] = OTHER_PID  # and away again: the system told the app so
    r.root.attrs["AXFrontmost"] = False
    assert truths() == (False, False, False)


def test_already_focused_app_gets_no_event(monkeypatch):
    r = rig(monkeypatch)
    enforced(r)
    r.events.posted.clear()
    enforced(r)
    assert r.events.posted == []
    r.focus.release(r.app.target)
    r.front[0] = APP_PID  # really active: nothing to make believe
    enforced(r)
    assert [e for e in r.events.posted if e["subtype"] == 1] == []


def test_active_but_unfocused_app_gets_the_key_focus_notice(monkeypatch):
    r = rig(monkeypatch)
    enforcer = r.focus.enforcer(r.app.target)
    enforcer._active = True
    r.root.attrs["AXFrontmost"] = True
    enforced(r)
    assert rows(r) == [("notice", 21, 0x8000, 0, 0)]
    assert enforcer.believes_focus


def test_wait_until_target_app_believes_frontmost(monkeypatch):
    """Up to 0.25 s, asked every 10 ms. An app that never answers is not an error."""
    r = rig(monkeypatch)
    r.events.app = None  # the fake app no longer reacts to the activation
    start = r.clock.now
    enforcer = enforced(r)
    assert 0.25 <= r.clock.now - start < 0.27
    assert enforcer.believes_focus
    quick = rig(monkeypatch)
    start = quick.clock.now
    enforced(quick)
    assert quick.clock.now == start  # it said yes at once


def test_activation_waits_until_the_clicked_window_is_main(monkeypatch):
    """The app says "frontmost" before it has worked through the activation click.
    What comes next must not overtake that click: a menu opened too early is closed
    by it."""
    r = rig(monkeypatch)
    post, sleep = r.events.post, r.clock.sleep

    def late_post(pid, event, delay=0.0):
        post(pid, event, delay)
        r.window.attrs["AXMain"] = False  # the click is still in the app's queue

    def later(seconds):
        sleep(seconds)
        r.window.attrs["AXMain"] = r.clock.now >= 1000.05

    monkeypatch.setattr(r.events, "post", late_post)
    monkeypatch.setattr(r.clock, "sleep", later)
    enforced(r)
    assert 0.04 < r.clock.now - 1000.0 < 0.08


def test_separate_component_stops_other_processes_stealing_focus(monkeypatch):
    """The activation leaked: the target became the real front app. The app that
    was in front before is asked to be frontmost again."""
    r = rig(monkeypatch)
    person = El("AXApplication")
    r.ax.apps[OTHER_PID] = person
    monkeypatch.setattr(r.focus, "_person_went", lambda pid, since: False)
    real_set = r.ax.set

    def set_(el, name, value):
        real_set(el, name, value)
        if el is person and name == "AXFrontmost":
            r.front[0] = OTHER_PID

    monkeypatch.setattr(r.ax, "set", set_)
    with r.focus.enforcer(r.app.target).active(r.windows.window):
        r.front[0] = APP_PID  # the leak
    assert person.performed == ["set AXFrontmost"]
    assert r.front == [OTHER_PID]


def test_focus_steal_prevention_has_callbacks_lost_regained_focus(monkeypatch):
    """No leak, no restore: the earlier front app is left alone. And an app the
    person made frontmost themselves is not pushed back."""
    r = rig(monkeypatch)
    r.ax.apps[OTHER_PID] = person = El("AXApplication")
    enforced(r)
    r.front[0] = APP_PID
    enforced(r)
    assert person.performed == []


def test_an_activation_that_leaks_later_is_undone_unless_the_person_acted(
    monkeypatch,
):
    """Seen: Pages came to the front 1.9 s after we told it it is active for a key,
    with no click or key of the person. For 3 s after an activation a real switch to
    the app is undone; one the person made (input since then) is left alone."""
    from macos_harness import person

    r = rig(monkeypatch)
    r.ax.apps[OTHER_PID] = front = El("AXApplication")
    monkeypatch.setattr(r.focus, "_person_went", person.went_to)
    monkeypatch.setattr(person, "_tap", object())
    monkeypatch.setattr(person, "_switched", float("-inf"))
    monkeypatch.setattr(person, "_clicked", (0, float("-inf")))
    enforced(r)
    assert front.performed == [] and len(r.background) == 1  # no leak yet: watched
    r.front[0] = APP_PID  # 1.9 s later the app comes to the front by itself
    r.clock.sleep(1.9)
    real_set = r.ax.set

    def set_(el, name, value):
        real_set(el, name, value)
        r.front[0] = OTHER_PID

    monkeypatch.setattr(r.ax, "set", set_)
    r.background.pop()()
    assert front.performed == ["set AXFrontmost"] and r.front == [OTHER_PID]
    r.focus.release(r.app.target)
    enforced(r)
    monkeypatch.setattr(person, "_clicked", (APP_PID, r.clock.now + 0.5))  # their click
    r.front[0] = APP_PID
    r.background.pop()()
    assert front.performed == ["set AXFrontmost"]  # not pushed back again


def test_the_person_who_switched_to_the_app_during_an_action_is_not_pulled_out(
    monkeypatch,
):
    """The agent typed into Notes in the background, the person clicked Notes to take
    over, and at the end of the call the app they had left was put back in front."""
    r = rig(monkeypatch)
    r.ax.apps[OTHER_PID] = front = El("AXApplication")
    asked = []
    monkeypatch.setattr(
        r.focus, "_person_went", lambda pid, since: asked.append(pid) or True
    )
    with r.focus.enforcer(r.app.target).active(r.windows.window):
        r.front[0] = APP_PID  # the person's own click brought it to the front
    assert front.performed == [] and r.front == [APP_PID] and r.background == []
    assert asked == [APP_PID]


def test_an_app_that_came_to_the_front_by_itself_is_put_back_while_the_person_types(
    monkeypatch,
):
    """The person typed in another app while the agent's action ran, the driven app
    really came to the front, and the repair was skipped because "the person acted":
    their next keys went into the driven app. Only their own switch to it counts."""
    from macos_harness import person

    r = rig(monkeypatch)
    r.ax.apps[OTHER_PID] = front = El("AXApplication")
    monkeypatch.setattr(r.focus, "_person_went", person.went_to)
    monkeypatch.setattr(person, "_tap", object())
    monkeypatch.setattr(person, "_switched", float("-inf"))
    monkeypatch.setattr(person, "_clicked", (0, float("-inf")))
    monkeypatch.setattr(person, "_bundle_of", lambda pid: "")
    real_set = r.ax.set

    def set_(el, name, value):
        real_set(el, name, value)
        r.front[0] = OTHER_PID

    monkeypatch.setattr(r.ax, "set", set_)
    with r.focus.enforcer(r.app.target).active(r.windows.window):
        person.saw(person.KEY_DOWN, 0, 0, OTHER_PID)  # typing where they are
        person.saw(1, 0, 0, OTHER_PID)  # and a click there
        r.front[0] = APP_PID  # the leak
    assert front.performed == ["set AXFrontmost"] and r.front == [OTHER_PID]


def test_a_focus_only_notice_is_not_watched(monkeypatch):
    r = rig(monkeypatch)
    enforcer = enforced(r)
    r.background.clear()
    enforcer._focus = False  # believes active, lost key focus: only the notice
    with enforcer.active(r.windows.window):
        pass
    assert r.background == []


def test_no_attention_asks_the_process_number_then_says_false(monkeypatch):
    calls = []

    class Services:
        def GetProcessForPID(self, pid, number):
            calls.append(("psn", pid))
            return 0

        def _SetApplicationDesiresAttention(self, number, wants):
            calls.append(("desires", wants))

    from macos_harness import focus

    monkeypatch.setattr(focus, "_services", Services)
    focus.no_attention(4242)
    assert calls == [("psn", 4242), ("desires", False)]


def test_no_attention_is_skipped_without_the_call_or_the_process(monkeypatch):
    from macos_harness import focus

    class Gone:
        def GetProcessForPID(self, pid, number):
            return -600  # procNotFound

        def _SetApplicationDesiresAttention(self, number, wants):
            raise AssertionError("not for a process that is gone")

    monkeypatch.setattr(focus, "_services", Gone)
    focus.no_attention(4242)
    monkeypatch.setattr(focus, "_services", lambda: object())  # no such call here
    focus.no_attention(4242)


def test_front_app_that_does_not_come_back_is_a_timeout(monkeypatch):
    r = rig(monkeypatch)
    r.ax.apps[OTHER_PID] = El("AXApplication")
    start = r.clock.now
    enforcer = r.focus.enforcer(r.app.target)
    with pytest.raises(errors.HarnessError) as caught, enforcer.active(None):
        r.front[0] = APP_PID
    assert caught.value.message.endswith("The app opened no window in time")
    assert 2.0 <= r.clock.now - start < 2.1


def test_other_private_window_server_calls_wrapped_their_use(monkeypatch):
    """Screen updates are frozen for the moment of the activation and always let
    go again. A system without the calls skips the step."""
    r = rig(monkeypatch)
    calls = []
    found = {
        "CGSMainConnectionID": lambda: 5,
        "CGSDisableUpdate": lambda c: calls.append(("freeze", c, len(r.events.posted))),
        "CGSReenableUpdate": lambda c: calls.append(("thaw", c, len(r.events.posted))),
    }
    monkeypatch.setattr(r.events, "private", lambda name, signature: found.get(name))
    enforced(r)
    assert calls == [("freeze", 5, 0), ("thaw", 5, 3)]
    calls.clear()
    r.focus._ENFORCERS.clear()
    r.root.attrs["AXFrontmost"] = False
    monkeypatch.setattr(r.events, "post", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        enforced(r)
    assert [c[0] for c in calls] == ["freeze", "thaw"]


def test_at_turn_end_the_app_is_told_it_is_inactive_once(monkeypatch):
    """Turn end: the app is told it is inactive again, once."""
    r = rig(monkeypatch)
    enforced(r)
    r.events.posted.clear()
    r.focus.release(r.app.target)
    assert rows(r) == [("notice", 21, 0x4000, 0, 0), ("notice", 13, 2, 0, 0)]
    r.focus.release(r.app.target)
    assert len(r.events.posted) == 2
    assert not r.focus.enforcer(r.app.target).believes_focus


def test_release_leaves_an_app_alone_that_is_really_active_or_untouched(monkeypatch):
    r = rig(monkeypatch)
    r.focus.enforcer(r.app.target)
    r.focus.release_all()
    assert r.events.posted == []
    enforced(r)
    r.events.posted.clear()
    r.front[0] = APP_PID
    r.focus.release_all()
    assert r.events.posted == [] and r.focus._ENFORCERS == {}


def test_one_enforcer_per_app_and_one_input_lock(monkeypatch):
    r = rig(monkeypatch)
    assert r.focus.enforcer(r.app.target) is r.focus.enforcer(r.app.target)
    held = []
    with r.focus.enforcer(r.app.target).active(None):
        other = threading.Thread(
            target=lambda: held.append(r.focus.INPUT_LOCK.acquire(timeout=0.05))
        )
        other.start()
        other.join()
    assert held == [False]  # another thread cannot post while one action runs


def test_synthetic_activation_decided_click(monkeypatch):
    """The three tiers of an event click, seen from the enforcer's side."""
    r = rig(monkeypatch)
    field = r.add(El("AXTextField", rect=(200, 200, 100, 20)))
    row = r.add(El("AXRow", rect=(200, 300, 100, 20)))
    r.actions.click(r.app, Line(1, "text field", field, "AXTextField"))
    assert "notice" not in r.events.types()  # Command tier: no activation
    assert not r.focus.enforcer(r.app.target).believes_focus
    r.actions.click(r.app, Line(1, "row", row, "AXRow"))
    assert r.focus.enforcer(r.app.target).believes_focus  # activation tier
    r.events.posted.clear()
    r.actions.click(r.app, Line(1, "text field", field, "AXTextField"))
    assert {e["flags"] for e in r.events.posted} == {0}  # plain tier from now on


def test_element_level_focus_helper_takes_focus_enforcer(monkeypatch):
    r = rig(monkeypatch, refuse=["AXSelected"])
    field = El("AXTextField")
    assert r.focus.focus_element(field) and field.attrs["AXFocused"] is True
    stubborn = rig(monkeypatch, refuse=["AXFocused"])
    assert stubborn.focus.focus_element(El("AXTextField")) is False


def test_no_real_activation():
    """Nothing in this module makes an app really frontmost for input."""
    for call in ("activateWithOptions", "SetFrontProcess", "activateIgnoringOtherApps"):
        assert call not in SOURCE


def test_no_cursor_updates():
    assert "SetCursor" not in SOURCE and "Cursor" not in SOURCE


def test_front_process_calls_take_option_flags():
    """Not used for input: the front process is only read, never set."""
    assert "GetFrontProcess" in SOURCE and "SetFront" not in SOURCE


def test_system_wide_observer_focused_element_context_frontmost_app():
    """Not needed: the focused element is read when keys are sent."""
    assert "observe" not in SOURCE
