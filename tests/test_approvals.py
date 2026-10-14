"""Approval: the prompt, the two stores, a no, the wait and the answer that comes late."""

from __future__ import annotations

import json
import os

import fakes_g
import pytest
from fakes import Clock
from fakes_g import agent, app

from macos_harness import approvals, errors
from macos_harness.model import AppTarget, Policy

LOW = Policy(decision="allowed", risk="low", allow_persistent=True, warning=None)
HIGH = Policy(
    decision="allowed", risk="high", allow_persistent=True, warning="Careful."
)
SESSION_ONLY = Policy(
    decision="allowed", risk="low", allow_persistent=False, warning=None
)
NOTES = app("Notes")


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    presence = fakes_g.FakePresence()
    monkeypatch.setattr(approvals, "presence", presence)
    monkeypatch.setattr(approvals, "protocol", fakes_g.FakeProtocol(tmp_path / "state"))
    monkeypatch.setattr(approvals, "_SESSION", set())
    monkeypatch.setattr(approvals, "_ASKING", {})
    return presence


def ask(who, policy=LOW, target=NOTES, clock=None):
    """One call that gives up 110 s after it began. Returns the refusal or None."""
    clock = clock or Clock()
    try:
        approvals.ensure(who, target, policy, clock.time() + 120.0, clock=clock)
    except errors.HarnessError as refusal:
        return refusal
    return None


def test_every_app_action_runs_through_policy_approval_wrapper(world):
    """With nothing stored every call asks, whichever tool it is for."""
    refusal = ask(agent())
    assert refusal.name == "approval_waiting"
    assert len(world.prompts) == 1


def test_approval_prompt_shown_person_app(world):
    ask(agent())
    ask(agent("agent-2"), HIGH, app("Surf"))
    assert world.prompts[0]["title"] == 'May macOS Harness control "Notes"?'
    assert world.prompts[0]["subtitle"] is None
    assert world.prompts[1]["title"] == 'May macOS Harness control "Surf"?'
    assert world.prompts[1]["subtitle"] == "Careful."


def test_approval_can_persist_session_always_policy_allows(world):
    ask(agent())
    ask(agent("agent-2"), SESSION_ONLY)
    assert world.prompts[0]["choices"] == [
        "Deny",
        "Allow for this session",
        "Always allow",
    ]
    assert world.prompts[1]["choices"] == ["Deny", "Allow for this session"]


def test_a_session_yes_holds_for_that_agent_only(world):
    first, second = agent("agent-1"), agent("agent-2")
    ask(first)
    world.prompts[0]["answer"]("Allow for this session")
    assert ask(first) is None
    assert ask(second).name == "approval_waiting"
    assert approvals.stored() == {"always": {}, "session": ["com.example.notes"]}


def test_persisted_approval_honoured_silently_anything_but_accept_throws(world):
    who = agent()
    ask(who)
    world.prompts[0]["answer"]("Always allow")
    assert ask(agent("a new agent")) is None  # stored always: silent, for everyone
    assert len(world.prompts) == 1

    for word in ("Deny", "", "closed"):  # anything but a yes is a no
        other = agent(f"agent {word}", signals_turns=True)
        ask(other, target=app("Mail"))
        world.prompts[-1]["answer"](word)
        refusal = ask(other, target=app("Mail"))
        assert refusal.message == errors.not_approved("Mail").message
        assert refusal.cancelled


def test_a_no_is_remembered_without_asking_again(world):
    who = agent(signals_turns=True)
    ask(who)
    world.prompts[0]["answer"]("Deny")
    assert ask(who).name == ask(who).name == "not_approved"
    assert len(world.prompts) == 1
    assert who.denied == {NOTES.path}


def test_without_turn_signals_a_no_refuses_one_call_and_the_next_asks_again(world):
    """Nothing would ever clear the no. So it ends the call that waited for it, or
    the next call when that one had already given up."""
    who = agent()
    ask(who)  # gave up waiting
    world.prompts[0]["answer"]("Deny")  # the person answers late
    assert ask(who).name == "not_approved"
    assert who.denied == set() and len(world.prompts) == 1
    assert ask(who).name == "approval_waiting"
    assert len(world.prompts) == 2


def test_a_stored_always_is_not_used_when_the_policy_forbids_keeping_it(world):
    approvals.approve(NOTES.bundle_id, always=True, agent_key="someone else")
    assert ask(agent(), SESSION_ONLY).name == "approval_waiting"
    assert ask(agent()) is None


def test_action_runs_host_timeout_suspended(world):
    """The call waits 110 of the client's 120 s, then says call again. The prompt
    stays up, and the answer counts although the client has gone."""
    clock, who = Clock(), agent()
    refusal = ask(who, clock=clock)
    assert clock.time() == pytest.approx(1110.0, abs=0.25)
    assert refusal.message == (
        "The user has not yet answered the question on screen about macOS Harness and "
        '"Notes". Call this tool again now, and do not end your turn.'
    )
    assert ask(who).name == "approval_waiting"
    assert len(world.prompts) == 1  # the second call did not ask twice
    world.prompts[0]["answer"]("Allow for this session")
    assert ask(who) is None


def test_an_answer_ends_the_wait_at_the_next_poll(world):
    clock, who = Clock(), agent()

    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 1003.0 and (who.key, NOTES.bundle_id) in approvals._ASKING:
            world.prompts[0]["answer"]("Allow for this session")

    clock.sleep = sleep
    assert ask(who, clock=clock) is None
    assert clock.time() == pytest.approx(1003.0, abs=0.25)


def test_a_call_past_its_deadline_still_shows_the_prompt(world):
    clock = Clock()
    with pytest.raises(errors.HarnessError) as raised:
        approvals.ensure(agent(), NOTES, LOW, clock.time() + 5.0, clock=clock)
    assert raised.value.name == "approval_waiting"
    assert len(world.prompts) == 1 and clock.time() == 1000.0


def test_wrapper_refuses_run_without_host_approval_hooks(monkeypatch):
    """No prompt can be shown: the model is told at once and how the person can allow it."""
    monkeypatch.setattr(approvals, "presence", fakes_g.FakePresence(can_ask=False))
    clock = Clock()
    refusal = ask(agent(), clock=clock)
    assert clock.time() == 1000.0
    assert refusal.message == (
        'macOS Harness cannot ask the user about "Notes" because its on-screen '
        'prompt is switched off. Ask the user to run `macos-harness approve --app "Notes"`, '
        "then call this tool again."
    )


def test_a_session_approval_is_kept_in_memory(world, monkeypatch):
    """A session yes is kept in memory: later calls read no file and ask nobody."""
    approvals.approve(NOTES.bundle_id, always=False)
    monkeypatch.setattr(approvals, "_always", lambda: pytest.fail("read the disk"))
    assert ask(agent()) is None and ask(agent("agent-2")) is None
    assert world.prompts == []


def test_the_approve_command_lifts_an_earlier_no(world):
    who = agent(signals_turns=True)
    ask(who)
    world.prompts[0]["answer"]("Deny")
    approvals.approve(NOTES.bundle_id, always=False)
    assert ask(who) is None


def test_the_approve_command_takes_the_waiting_prompt_off_the_screen(world):
    """The person answered in a shell: the panel must not keep asking."""
    who, other = agent(), agent("agent-2")
    ask(who)
    ask(other, target=app("Surf"))
    assert len(world.prompts) == 2
    approvals.approve(NOTES.bundle_id, always=False)
    assert [p["title"] for p in world.prompts] == ['May macOS Harness control "Surf"?']
    assert ask(who) is None and len(world.prompts) == 1  # and it is not asked again
    world.prompts[0]["answer"]("Allow for this session")  # the panel's own yes
    assert [call[0] for call in world.calls].count("withdraw") == 1


def test_approval_files_have_fixed_locations(tmp_path):
    assert approvals.path() == tmp_path / "state" / "approvals.json"
    approvals.approve("com.example.notes", always=True)
    assert approvals.path().stat().st_mode & 0o777 == 0o600
    assert [file.name for file in approvals.path().parent.iterdir()] == [
        "approvals.json"
    ]


def test_older_gen_1_approval_texts_approval_store(monkeypatch):
    """The file holds the always list with a date per app and the date of the last
    change. A file that cannot be written leaves a session yes and says so."""
    approvals.approve("com.example.notes", always=True)
    approvals.approve("com.example.mail", always=True)
    saved = json.loads(approvals.path().read_text())
    assert sorted(saved) == ["always", "modified"]
    assert sorted(saved["always"]) == ["com.example.mail", "com.example.notes"]
    assert saved["always"]["com.example.mail"] == saved["modified"]
    assert saved["modified"].endswith("+00:00")

    approvals.path().write_text("not json")
    assert approvals.stored()["always"] == {}

    def refuse(*args):
        raise OSError("read-only")

    monkeypatch.setattr(approvals.os, "replace", refuse)
    with pytest.raises(errors.HarnessError) as raised:
        approvals.approve("com.example.chess", always=True)
    assert raised.value.message == (
        "macOS Harness could not save the approval of 'com.example.chess' to disk, "
        "so it lasts only until the helper stops. Go on; the user can approve it again "
        "later."
    )
    assert ask(agent(), target=app("Chess")) is None


def test_an_always_answer_that_cannot_be_saved_still_allows_the_session(
    world, monkeypatch
):
    def refuse(*args):
        raise OSError("read-only")

    monkeypatch.setattr(approvals.os, "replace", refuse)
    who = agent()
    ask(who)
    world.prompts[0]["answer"]("Always allow")
    assert ask(who) is None
    assert approvals.stored()["always"] == {}


def test_a_process_known_only_by_its_pid_is_never_approved_for_good(world):
    """ "Always allow" for a program with no bundle wrote `#pid:N` to disk, and a
    later process with that pid was approved before anyone asked."""
    bare = AppTarget("#pid:4242", "#pid:4242", "tool", 4242)
    ask(agent(), target=bare)
    assert world.prompts[0]["choices"] == ["Deny", "Allow for this session"]
    approvals.approve("#pid:4242", always=True)
    assert approvals.stored()["always"] == {}
    assert ask(agent(), target=bare) is None  # the session yes still holds


def test_a_policy_that_does_not_ask_lets_every_allowed_app_through(world):
    """The default: no prompt, no stored yes needed."""
    quiet = Policy(
        decision="allowed", risk="low", allow_persistent=True, warning=None, ask=False
    )
    assert ask(agent(), quiet) is None
    assert world.prompts == [] and approvals.stored() == {"always": {}, "session": []}


def test_allow_for_this_session_holds_for_the_agent_s_whole_session(world, monkeypatch):
    """It lived in the helper's memory only: after the helper's idle exit (15 minutes)
    the same chat was asked again. Now it lasts while the agent's process runs; one
    given with the command lasts while the helper lives."""
    running = {"501:1000.5"}
    monkeypatch.setattr(approvals, "_alive", lambda key: key in running)
    chat = agent("501:1000.5")
    ask(chat)
    world.prompts[0]["answer"]("Allow for this session")
    approvals.approve("com.example.other", always=False)  # by the command: no agent
    monkeypatch.setattr(approvals, "_SESSION", set())  # the helper is started again
    assert ask(chat) is None and len(world.prompts) == 1  # not asked again
    assert (
        ask(agent("502:2000.0"), target=NOTES).name == "approval_waiting"
    )  # another chat
    assert (
        ask(chat, target=app("Other", "com.example.other")).name == "approval_waiting"
    )
    running.clear()  # the chat ended
    assert ask(chat).name == "approval_waiting"


def test_a_session_approval_s_agent_is_found_by_the_key_the_helper_gives_it():
    from macos_harness import helper

    pid = os.getpid()
    start = helper.process(pid)[2]
    assert approvals._alive(f"{pid}:{start}") and approvals._alive("run-7")
    assert not approvals._alive(f"{pid}:{start + 1}")
