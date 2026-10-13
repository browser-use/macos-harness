"""Is the Mac locked? We say so at once and leave it to the person. We never unlock it.

Unlocking would mean typing the login password, and this tool never types one. The
check is one read of the login session's dictionary, about 2 ms, so it runs first on
every call: behind the lock screen every accessibility read comes back empty.
"""

from __future__ import annotations

import Quartz

from . import errors

LOCKED_KEY = "CGSSessionScreenIsLocked"


def locked() -> bool:
    session = Quartz.CGSessionCopyCurrentDictionary()
    return bool(session and session.get(LOCKED_KEY))


def gate(state_read: bool) -> None:
    """Fail at once when the Mac is locked, before anything touches an app."""
    if locked():
        raise errors.locked(state_read=state_read)
