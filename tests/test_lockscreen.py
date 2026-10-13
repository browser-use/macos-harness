"""The lock screen: seen first, said plainly, never waited for, never opened by us."""

from __future__ import annotations

from pathlib import Path

import pytest

from macos_harness import errors, lockscreen


class FakeQuartz:
    def __init__(self, session):
        self.session, self.reads = session, 0

    def CGSessionCopyCurrentDictionary(self):
        self.reads += 1
        return self.session(self.reads) if callable(self.session) else self.session


@pytest.fixture
def lock(monkeypatch):
    def set_session(session):
        quartz = FakeQuartz(session)
        monkeypatch.setattr(lockscreen, "Quartz", quartz)
        return quartz

    return set_session


LOCKED = {"CGSSessionScreenIsLocked": True, "kCGSSessionOnConsoleKey": True}
UNLOCKED = {"kCGSSessionOnConsoleKey": True}


def test_lock_state_detected_from_the_session(lock):
    for session, expected in (
        (LOCKED, True),
        (UNLOCKED, False),
        (None, False),
        ({}, False),
    ):
        lock(session)
        assert lockscreen.locked() is expected


def test_four_distinct_locked_mac_texts_cause(lock):
    """We never unlock, so two of the four causes exist: a state read, an action."""
    lock(LOCKED)
    with pytest.raises(errors.HarnessError) as read:
        lockscreen.gate(state_read=True)
    with pytest.raises(errors.HarnessError) as act:
        lockscreen.gate(state_read=False)
    assert read.value.message == (
        "The Mac is locked, so its accessibility text cannot be read. It has to be unlocked first."
    )
    assert act.value.message == (
        "The Mac is locked. Ask the user to unlock it, then go on."
    )
    assert read.value.code == act.value.code == 134
    assert read.value.name == act.value.name == "mac_locked"


def test_check_lock_state_before_every_call(lock):
    """One read of the session and nothing else. The fake has no other function, so
    an accessibility call or any other system call before the refusal would fail here."""
    quartz = lock(UNLOCKED)
    assert lockscreen.gate(state_read=True) is None
    assert lockscreen.gate(state_read=False) is None
    assert quartz.reads == 2
    quartz = lock(LOCKED)
    with pytest.raises(errors.HarnessError):
        lockscreen.gate(state_read=False)
    assert quartz.reads == 1


# What would exist only to unlock the Mac. None of it is built: unlocking
# means typing the login password.
NOT_BUILT = [
    "a cover over the lock screen that says who is working",
    "automatic unlock with timings and a cap on attempts",
    "typing the login password",
    "pausing automatic unlock when the person touches the Mac",
    "a guard process around the unlock",
    "a switch and a prepare step for automatic unlock",
    "six pause states and three attempt outcomes",
    "locking the Mac again afterwards",
    "wallpaper and user picture on the cover",
    "reaching the guard process",
    "waiting for the unlock: a locked Mac answers at once instead",
]


@pytest.mark.parametrize("row", NOT_BUILT)
def test_nothing_here_unlocks_or_draws_over_the_lock_screen(row):
    public = {name for name in vars(lockscreen) if name[0].islower() and name[0] != "_"}
    modules = {"annotations", "errors"}
    assert public - modules == {"locked", "gate"}
    source = Path(lockscreen.__file__).read_text("utf-8")
    for word in ("password", "CGEventPost", "AXUIElement", "NSWindow"):
        assert word not in source.split('"""', 2)[2]
