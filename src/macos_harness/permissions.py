"""The macOS grants this tool needs, and how the person gives them.

macOS gives a grant to the app that started us (a terminal, the agent's own app), not
to this process. `doctor` names that app. A call with a grant missing shows the right
pane of System Settings and waits. The person does the rest.
"""

from __future__ import annotations

import ctypes
import os
import time
from typing import NamedTuple

import ApplicationServices
import Quartz
from AppKit import NSRunningApplication, NSWorkspace
from Foundation import NSURL

from . import errors

SETTINGS_URL = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"
SETTINGS_APP = "com.apple.systempreferences"
POLL_S = 0.25
GIVE_UP_S = 10.0  # stop waiting this long before the client does, and say "call again"


class Kind(NamedTuple):
    granted: object  # () -> bool, the preflight. It never shows a prompt.
    request: object  # () -> anything. Makes macOS list our app in the pane.
    pane: str
    why: str  # shown by doctor


KINDS = {
    "accessibility": Kind(
        lambda: ApplicationServices.AXIsProcessTrusted(),
        lambda: ApplicationServices.AXIsProcessTrustedWithOptions(
            {"AXTrustedCheckOptionPrompt": True}
        ),
        "Privacy_Accessibility",
        f"Allows {errors.PRODUCT} to read and operate app interfaces",
    ),
    "screen_recording": Kind(
        lambda: Quartz.CGPreflightScreenCaptureAccess(),
        lambda: Quartz.CGRequestScreenCaptureAccess(),
        "Privacy_ScreenCapture",
        f"{errors.PRODUCT} reads screenshots to find where to click",
    ),
    "post_events": Kind(
        lambda: Quartz.CGPreflightPostEventAccess(),
        lambda: Quartz.CGRequestPostEventAccess(),
        "Privacy_Accessibility",
        f"Allows {errors.PRODUCT} to send clicks and keys to one app",
    ),
    "input_monitoring": Kind(
        lambda: Quartz.CGPreflightListenEventAccess(),
        lambda: Quartz.CGRequestListenEventAccess(),
        "Privacy_ListenEvent",
        f"Lets {errors.PRODUCT} notice when you use an app yourself, and step back",
    ),
}
REQUIRED = ("accessibility", "screen_recording")  # no call works without these two

_seen_granted: set[str] = set()  # a grant does not go away while the helper lives


def status() -> dict[str, bool]:
    return {kind: bool(row.granted()) for kind, row in KINDS.items()}


def url(kind: str) -> str:
    return SETTINGS_URL + KINDS[kind].pane


def open_settings(kind: str) -> None:
    """Ask macOS to list our app under `kind`, then show that pane."""
    KINDS[kind].request()
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url(kind)))


def _settings_open() -> bool:
    running = NSRunningApplication.runningApplicationsWithBundleIdentifier_
    return bool(running(SETTINGS_APP))


def _missing() -> list[str]:
    _seen_granted.update(
        kind for kind in REQUIRED if kind not in _seen_granted and KINDS[kind].granted()
    )
    return [kind for kind in REQUIRED if kind not in _seen_granted]


def gate(deadline: float, clock=time) -> None:
    """Return once the required grants are there. Else show Settings and wait.

    Still missing shortly before the client gives up: the pending text, so the model
    calls again. The person closed Settings without granting: not granted.
    """
    shown, settings_seen = None, False
    while missing := _missing():
        if missing[0] != shown:
            shown, settings_seen = missing[0], False
            open_settings(shown)
        elif _settings_open():
            settings_seen = True
        elif settings_seen:
            raise errors.permissions_not_granted()
        if clock.time() >= deadline - GIVE_UP_S:
            raise errors.permissions_pending()
        clock.sleep(POLL_S)


def responsible_app() -> str:
    """The app macOS holds responsible for this process. Its name is in the Settings list."""
    try:
        pid = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid(os.getpid())
    except (AttributeError, OSError):
        pid = os.getppid()
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str(app.localizedName()) if app else "the app that started macos-harness"


def doctor() -> dict:
    """What is granted, what is missing and where to grant it. Shows no prompt."""
    granted = status()
    missing = {
        kind: {"why": KINDS[kind].why, "settings": url(kind)}
        for kind, ok in granted.items()
        if not ok
    }
    return {
        "ready": all(granted[kind] for kind in REQUIRED),
        "app": responsible_app(),
        "granted": granted,
        "missing": missing,
    }
