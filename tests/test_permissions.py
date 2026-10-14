"""Grants: the preflights, the Settings pane, the wait, what doctor says."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import Clock

from macos_harness import errors, permissions

PANE = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"


class Mac:
    """A Mac whose grants a test sets. It records every prompt and every pane shown."""

    def __init__(self, monkeypatch):
        self.granted = dict.fromkeys(permissions.KINDS, True)
        self.checks, self.requests, self.opened = [], [], []
        self.settings_running = False
        self.settings_launches = True

        def check(kind):
            self.checks.append(kind)
            return self.granted[kind]

        def show(url):
            self.opened.append(url)
            self.settings_running = self.settings_launches

        monkeypatch.setattr(
            permissions,
            "ApplicationServices",
            SimpleNamespace(
                AXIsProcessTrusted=lambda: check("accessibility"),
                AXIsProcessTrustedWithOptions=lambda options: self.requests.append(
                    ("accessibility", options)
                ),
            ),
        )
        monkeypatch.setattr(
            permissions,
            "Quartz",
            SimpleNamespace(
                CGPreflightScreenCaptureAccess=lambda: check("screen_recording"),
                CGPreflightPostEventAccess=lambda: check("post_events"),
                CGPreflightListenEventAccess=lambda: check("input_monitoring"),
                CGRequestScreenCaptureAccess=lambda: self.requests.append(
                    "screen_recording"
                ),
                CGRequestPostEventAccess=lambda: self.requests.append("post_events"),
                CGRequestListenEventAccess=lambda: self.requests.append(
                    "input_monitoring"
                ),
            ),
        )
        workspace = SimpleNamespace(openURL_=show)
        monkeypatch.setattr(
            permissions,
            "NSWorkspace",
            SimpleNamespace(sharedWorkspace=lambda: workspace),
        )
        monkeypatch.setattr(permissions, "NSURL", SimpleNamespace(URLWithString_=str))
        monkeypatch.setattr(
            permissions, "_settings_open", lambda: self.settings_running
        )
        monkeypatch.setattr(permissions, "_seen_granted", set())
        monkeypatch.setattr(permissions, "responsible_app", lambda: "Terminal")
        self.clock = Clock()

    def after(self, seconds, change):
        """Run `change` once the waiting call has slept this long."""
        quiet_sleep, start = self.clock.sleep, self.clock.now

        def sleep(step):
            quiet_sleep(step)
            if self.clock.now - start >= seconds:
                self.clock.sleep = quiet_sleep
                change()

        self.clock.sleep = sleep

    def gate(self):
        """One call that gives up 110 s after it began. Returns its refusal or None."""
        try:
            permissions.gate(self.clock.time() + 120.0, clock=self.clock)
        except errors.HarnessError as refusal:
            return refusal
        return None


@pytest.fixture
def mac(monkeypatch):
    return Mac(monkeypatch)


def test_everything_granted_returns_at_once_and_is_checked_once(mac):
    assert mac.gate() is None and mac.gate() is None
    assert mac.checks == ["accessibility", "screen_recording"]  # the second call: none
    assert mac.opened == mac.requests == [] and mac.clock.time() == 1000.0


def test_while_permissions_pending_model_told_call_again_end(mac):
    mac.granted["accessibility"] = False
    refusal = mac.gate()
    assert mac.clock.time() == pytest.approx(1110.0, abs=0.25)
    assert refusal.name == "permissions_waiting" and refusal.code == 121
    assert refusal.message == (  # the grant missing and the app System Settings lists
        "The user is still allowing Terminal under Accessibility in System Settings. "
        "Call this tool again now, and do not end your turn."
    )
    assert mac.opened == [PANE + "Privacy_Accessibility"]  # shown once, not per poll
    assert mac.requests == [("accessibility", {"AXTrustedCheckOptionPrompt": True})]


def test_permission_window_has_pending_timeout_skip_path_abandon(mac):
    """Three ends of a wait: granted, still pending when time is up, and abandoned
    (the person closed System Settings with the grant still missing)."""
    mac.granted["screen_recording"] = False
    mac.after(3.0, lambda: mac.granted.update(screen_recording=True))
    assert mac.gate() is None
    assert mac.clock.time() == pytest.approx(1003.0, abs=0.25)

    permissions._seen_granted.clear()
    mac.granted["accessibility"] = False
    mac.after(2.0, lambda: setattr(mac, "settings_running", False))
    refusal = mac.gate()
    assert refusal.message == (
        "macOS Harness cannot work without Accessibility. Ask the user to allow "
        "Terminal under Accessibility in System Settings; `macos-harness doctor` "
        "lists them."
    )
    assert refusal.name == "permissions_missing" and refusal.code == 122
    assert mac.clock.time() == pytest.approx(1005.25, abs=0.25)

    assert permissions.GIVE_UP_S == 10.0 and permissions.POLL_S == 0.25


def test_settings_that_never_came_up_is_not_an_abandoned_prompt(mac):
    mac.granted["accessibility"] = False
    mac.settings_launches = False
    assert mac.gate().name == "permissions_waiting"


def test_system_settings_opened_exact_pane_floating_helper_beside(mac):
    """One pane at a time, the first missing grant first. No window of our own."""
    assert {kind: permissions.url(kind) for kind in permissions.KINDS} == {
        "accessibility": PANE + "Privacy_Accessibility",
        "screen_recording": PANE + "Privacy_ScreenCapture",
        "post_events": PANE + "Privacy_Accessibility",
        "input_monitoring": PANE + "Privacy_ListenEvent",
    }
    mac.granted.update(accessibility=False, screen_recording=False)
    mac.after(2.0, lambda: mac.granted.update(accessibility=True))
    assert (
        "allowing Terminal under Screen Recording in" in mac.gate().message
    )  # only it
    assert mac.opened == [
        PANE + "Privacy_Accessibility",
        PANE + "Privacy_ScreenCapture",
    ]
    assert [
        request if isinstance(request, str) else request[0] for request in mac.requests
    ] == [
        "accessibility",
        "screen_recording",
    ]
    permissions.open_settings("input_monitoring")
    assert mac.requests[-1] == "input_monitoring"
    assert mac.opened[-1] == PANE + "Privacy_ListenEvent"


def test_screen_recording_grant_validated_against_system_settings_service(mac):
    """A grant counts once this process has seen it. It is not asked for again, and
    System Settings is never kept from quitting."""
    assert mac.gate() is None
    mac.granted.update(accessibility=False, screen_recording=False)
    assert mac.gate() is None
    assert mac.opened == []


def test_permission_state_observable_object_five_flags(mac):
    mac.granted.update(screen_recording=False, input_monitoring=False)
    assert permissions.status() == {
        "accessibility": True,
        "screen_recording": False,
        "post_events": True,
        "input_monitoring": False,
    }
    assert permissions.REQUIRED == ("accessibility", "screen_recording")


def test_permission_window_texts_accessibility_screen_recording(mac):
    """doctor says which app needs the grant, what is missing, why, and where to
    switch it on. It shows no prompt and opens nothing."""
    mac.granted.update(screen_recording=False, input_monitoring=False)
    assert permissions.doctor() == {
        "ready": False,
        "app": "Terminal",
        "granted": permissions.status(),
        "missing": {
            "screen_recording": {
                "why": "macOS Harness reads screenshots to find where to click",
                "settings": PANE + "Privacy_ScreenCapture",
            },
            "input_monitoring": {
                "why": "Lets macOS Harness notice when you use an app yourself, and step back",
                "settings": PANE + "Privacy_ListenEvent",
            },
        },
    }
    mac.granted.update(screen_recording=True)
    assert permissions.doctor()["ready"] is True  # the tap's grant is not required
    assert mac.requests == mac.opened == []
    assert permissions.KINDS["accessibility"].why == (
        "Allows macOS Harness to read and operate app interfaces"
    )


def test_doctor_names_the_app_macos_holds_responsible(monkeypatch):
    lookup = SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: None)
    monkeypatch.setattr(permissions, "NSRunningApplication", lookup)
    assert permissions.responsible_app() == "the app that started macos-harness"
    app = SimpleNamespace(localizedName=lambda: "Terminal")
    seen = []
    lookup.runningApplicationWithProcessIdentifier_ = lambda pid: (
        seen.append(pid) or app
    )
    assert permissions.responsible_app() == "Terminal"
    assert seen[0] > 0
