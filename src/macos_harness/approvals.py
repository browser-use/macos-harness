"""The person says which apps the agent may drive.

A yes holds for one agent session or for good. A no holds until the turn ends; for
a host that never says when a turn starts it refuses one call. The prompt stays on
screen when a call gives up waiting, and its answer is kept, so the next call finds it.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from . import errors, presence, protocol
from .model import AgentSession, AppTarget, Policy
from .policy import display_name

PID_ONLY = "#pid:"  # the id of a running process that has no bundle (apps.PID_PREFIX)

DENY, SESSION, ALWAYS = "Deny", "Allow for this session", "Always allow"
POLL_S = 0.25
GIVE_UP_S = 10.0  # stop waiting this long before the client does, and say "call again"

_LOCK = threading.Lock()
# (agent key, bundle id). The key is None for an approval every agent may use: one
# given with `macos-harness approve`, kept until the helper stops.
_SESSION: set[tuple[str | None, str]] = set()
_ASKING: dict[tuple[str, str], str] = {}  # (agent key, bundle id) -> prompt on screen


def path() -> Path:
    return protocol.state_dir() / "approvals.json"


def _always() -> dict[str, str]:
    """Bundle id -> the day the person said always. A missing or broken file is empty."""
    try:
        return dict(json.loads(path().read_text("utf-8"))["always"])
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def stored() -> dict:
    """Every approval in force: the ones on disk and the ones of this helper's life."""
    return {"always": _always(), "session": sorted({b for _, b in _SESSION})}


def approve(bundle_id: str, always: bool, agent_key: str | None = None) -> None:
    """Record a yes. `always` also writes it to disk, with the date."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    # A process with no bundle is known by its pid, and a pid comes back as another
    # program: such a yes is never written down.
    always = always and not bundle_id.startswith(PID_ONLY)
    with _LOCK:
        _SESSION.add((agent_key, bundle_id))
        settled = [
            a for a in _ASKING if a[1] == bundle_id and agent_key in (None, a[0])
        ]
        for asked in settled:  # a yes from the command: the prompt has no more to ask
            presence.withdraw(_ASKING.pop(asked))
        if not always:
            return
        record = {"always": _always() | {bundle_id: now}, "modified": now}
        scratch = path().with_suffix(".tmp")
        try:
            scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch.write_text(json.dumps(record, indent=1), "utf-8")
            os.chmod(scratch, 0o600)
            os.replace(scratch, path())
        except OSError:
            raise errors.approval_not_saved(bundle_id) from None


def _approved(agent: AgentSession, bundle_id: str, policy: Policy) -> bool:
    return (
        (agent.key, bundle_id) in _SESSION
        or (None, bundle_id) in _SESSION
        or (policy.allow_persistent and bundle_id in _always())
    )


def ensure(
    agent: AgentSession, target: AppTarget, policy: Policy, deadline: float, clock=time
) -> None:
    """Return when the person has allowed this app. A stored yes returns at once, and
    so does every app when the policy does not ask (the default)."""
    if not policy.ask or _approved(agent, target.bundle_id, policy):
        return
    name, asked = display_name(target), (agent.key, target.bundle_id)

    def answer(choice: str) -> None:  # runs on the main thread, maybe after we left
        _ASKING.pop(asked, None)
        if choice not in (SESSION, ALWAYS):  # anything but a yes is a no
            agent.denied.add(target.path)
            return
        with contextlib.suppress(errors.HarnessError):  # the disk refused: the yes
            approve(target.bundle_id, choice == ALWAYS, agent.key)  # holds for now

    while not _approved(agent, target.bundle_id, policy):
        if target.path in agent.denied:
            if (
                not agent.signals_turns
            ):  # no turn end will clear it: ask again next time
                agent.denied.discard(target.path)
            raise errors.not_approved(name)
        if asked not in _ASKING:
            title = _ASKING[asked] = f'May {errors.PRODUCT} control "{name}"?'
            lasting = policy.allow_persistent and not asked[1].startswith(PID_ONLY)
            choices = [DENY, SESSION] + ([ALWAYS] if lasting else [])
            # False: the drawn parts are off, nobody will see the question.
            if presence.ask(title, policy.warning, choices, answer) is False:
                _ASKING.pop(asked, None)
                raise errors.approval_unavailable(name)
        if clock.time() >= deadline - GIVE_UP_S:
            raise errors.approval_pending(name)
        clock.sleep(POLL_S)
