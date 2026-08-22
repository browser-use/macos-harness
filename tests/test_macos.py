from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest
from PIL import Image

import macos_harness.macos as macos_module
from macos_harness.macos import (
    _KEYCODES,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOS,
    MacOSError,
    _png_size,
    _split_scroll_delta,
)


def test_png_size(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x80\x00\x00\x01\xe0"
    )
    assert _png_size(path) == (640, 480)


def test_render_tree() -> None:
    text = MacOS._render_tree(
        [
            {"element_index": 0, "depth": 0, "role": "AXApplication", "title": "Notes"},
            {
                "element_index": 1,
                "depth": 1,
                "role": "AXButton",
                "title": "Save",
                "url": "https://example.com/save",
                "actions": ["AXPress"],
            },
        ]
    )
    assert text.splitlines() == [
        '0 AXApplication title="Notes"',
        '  1 AXButton title="Save" actions=AXPress',
    ]


def test_split_scroll_delta_preserves_exact_total() -> None:
    assert _split_scroll_delta(-235, 100) == [-100, -100, -35]
    assert _split_scroll_delta(21, 10) == [10, 10, 1]
    assert _split_scroll_delta(0, 10) == [0]


def test_common_navigation_keys_are_supported() -> None:
    assert {_KEYCODES[name] for name in ("home", "end", "pageup", "pagedown")} == {
        115,
        116,
        119,
        121,
    }


def test_agent_surface_is_flat_and_explicit() -> None:
    mac = MacOS()

    for verb in ("see", "key", "type", "click", "script"):
        assert callable(getattr(mac, verb))
    for verb in (
        "at",
        "query",
        "query_all",
        "wait",
        "wait_gone",
        "press",
        "get",
        "set",
        "perform",
    ):
        assert callable(getattr(mac.ax, verb))
    assert not hasattr(mac, "mouse")
    assert not hasattr(mac, "keyboard")


class _FakeRunningApp:
    """Minimal stand-in for NSRunningApplication: only what `_app_info`
    and `_resolve_app`'s exact-pid fast path ever touch."""

    def __init__(self, pid: int, *, name: str = "HelperApp", terminated: bool = False) -> None:
        self._pid = pid
        self._name = name
        self._terminated = terminated

    def localizedName(self) -> str:
        return self._name

    def bundleIdentifier(self) -> None:
        return None

    def bundleURL(self) -> None:
        return None

    def processIdentifier(self) -> int:
        return self._pid

    def isTerminated(self) -> bool:
        return self._terminated


def test_resolve_app_exact_pid_hit_never_enumerates_workspace(monkeypatch) -> None:
    """An int pid resolves directly by identity -- it must never scan
    NSWorkspace.runningApplications(), which is not just wasteful but can
    return stale, notification-cache-backed data in a process that never
    pumps its own run loop."""
    mac = MacOS()
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            assert pid == 4242
            return fake_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    app, info = mac._resolve_app(4242)

    assert app is fake_app
    assert info["pid"] == 4242
    assert info["name"] == "HelperApp"


def test_resolve_app_exact_pid_miss_raises_without_enumerating_workspace(
    monkeypatch,
) -> None:
    mac = MacOS()

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            return None

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    with pytest.raises(ApplicationNotFoundError, match="99999"):
        mac._resolve_app(99999)


def test_resolve_app_exact_pid_rejects_a_terminated_process_without_enumerating(
    monkeypatch,
) -> None:
    mac = MacOS()
    terminated_app = _FakeRunningApp(4242, terminated=True)

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            return terminated_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    with pytest.raises(ApplicationNotFoundError, match="4242"):
        mac._resolve_app(4242)


def test_resolve_app_string_query_still_enumerates_workspace(monkeypatch) -> None:
    """A name/bundle-id/path/stringified-pid query is unchanged: it still
    scans NSWorkspace.runningApplications(), never the exact-pid fast
    path."""
    mac = MacOS()
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> type[_FakeWorkspace]:
            return _FakeWorkspace

        @staticmethod
        def runningApplications() -> list[_FakeRunningApp]:
            return [fake_app]

    def _boom(pid: int) -> Never:
        raise AssertionError("string queries must never use the exact-pid fast path")

    class _FakeRunningApplication:
        runningApplicationWithProcessIdentifier_ = staticmethod(_boom)

    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)
    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)

    app, info = mac._resolve_app("HelperApp")

    assert app is fake_app
    assert info["pid"] == 4242


def test_resolve_app_last_app_reuse_passes_an_int_pid(monkeypatch) -> None:
    """Reusing `_last_app` (a falsy query with a prior resolution cached)
    must feed the exact-pid fast path an int, not a stringified one -- it
    benefits from the same identity lookup as an explicit int query."""
    mac = MacOS()
    mac._last_app = {"name": "HelperApp", "bundle_id": None, "pid": 4242, "path": None}
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    seen: list[int] = []

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            seen.append(pid)
            return fake_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError("must not enumerate for a cached last_app pid reuse")

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    app, info = mac._resolve_app(None)

    assert app is fake_app
    assert info["pid"] == 4242
    assert seen == [4242]
    assert isinstance(seen[0], int)


def test_doctor_reports_real_permission_preflights(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "is_accessibility_trusted", lambda: True)
    monkeypatch.setattr(
        mac,
        "_preflight_permission",
        lambda name: {
            "CGPreflightScreenCaptureAccess": True,
            "CGPreflightPostEventAccess": False,
        }[name],
    )
    monkeypatch.setattr(mac, "list_apps", lambda: [{"name": "Test"}])

    result = mac.doctor()

    assert result["permissions"] == {
        "accessibility": True,
        "screen_recording": True,
        "post_events": False,
        "automation": "per-target; requested by macOS on first Apple Event",
    }
    assert result["input_monitoring_required"] is False


def test_application_element_enables_enhanced_ax(monkeypatch) -> None:
    root = object()
    writes = []
    monkeypatch.setattr(
        macos_module.AS, "AXUIElementCreateApplication", lambda pid: root
    )
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyAttributeValue",
        lambda element, attribute, error: (0, False),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementSetAttributeValue",
        lambda element, attribute, value: writes.append((element, attribute, value)),
    )
    timeouts = []
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementSetMessagingTimeout",
        lambda element, timeout: timeouts.append((element, timeout)) or 0,
    )
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    assert MacOS._application_element(42, messaging_timeout=0.5) is root
    assert timeouts == [(root, 0.5)]
    assert writes == [(root, "AXEnhancedUserInterface", True)]

    writes.clear()
    assert MacOS._application_element(42, enhance=False) is root
    assert writes == []


def test_ax_query_falls_back_to_a_bounded_tree(monkeypatch) -> None:
    mac = MacOS()
    root, button, group, match, too_deep = (object() for _ in range(5))
    children = {root: [button, group], group: [match, too_deep]}
    data = {
        button: {"AXRole": "AXButton", "AXTitle": "Save"},
        group: {"AXRole": "AXGroup"},
        match: {"AXRole": "AXStaticText", "AXValue": "Alessia playlist"},
        too_deep: {"AXRole": "AXStaticText", "AXValue": "Alessia too deep"},
    }
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    monkeypatch.setattr(mac, "_is_ax_element", lambda value: value in data)
    monkeypatch.setattr(
        mac,
        "_copy_attribute",
        lambda element, attribute: (
            children.get(element, [])
            if attribute in {"AXChildren", "AXWindows"}
            else None
        ),
    )

    def fake_attributes(element, attributes):
        values = {
            **data.get(element, {}),
            "AXChildren": children.get(element),
            "AXWindows": None,
        }
        return {attribute: values.get(attribute) for attribute in attributes}

    monkeypatch.setattr(mac, "_copy_attributes", fake_attributes)
    monkeypatch.setattr(mac, "_actions", lambda element: [])
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyParameterizedAttributeValue",
        lambda *args: (
            macos_module.AS.kAXErrorParameterizedAttributeUnsupported,
            None,
        ),
    )

    results = mac.ax.query(
        app="Spotify",
        text="Alessia",
        limit=20,
        max_nodes=4,
        include_actions=False,
    )

    assert results == [
        {
            "element_index": 3,
            "role": "AXStaticText",
            "value": "Alessia playlist",
        }
    ]


def test_safe_ax_fallback_does_not_read_values(monkeypatch) -> None:
    mac = MacOS()
    root, button = object(), object()
    requested = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    monkeypatch.setattr(mac, "_is_ax_element", lambda value: value is button)

    def fake_attributes(element, attributes):
        requested.append(tuple(attributes))
        values = {
            root: {"AXRole": "AXApplication", "AXChildren": [button]},
            button: {"AXRole": "AXButton", "AXTitle": "Use Password"},
        }[element]
        return {attribute: values.get(attribute) for attribute in attributes}

    monkeypatch.setattr(mac, "_copy_attributes", fake_attributes)
    monkeypatch.setattr(mac, "_actions", lambda element: [])
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyParameterizedAttributeValue",
        lambda *args: (
            macos_module.AS.kAXErrorParameterizedAttributeUnsupported,
            None,
        ),
    )

    results = mac.ax.query(
        app="Chrome",
        text="Use Password",
        attributes=mac.ax._SAFE_ATTRIBUTES,
        include_actions=False,
    )

    assert results[0]["title"] == "Use Password"
    assert all("AXValue" not in attributes for attributes in requested)


def test_snapshot_tree_can_preserve_earlier_element_handles(monkeypatch) -> None:
    mac = MacOS()
    first, second = object(), object()
    monkeypatch.setattr(
        mac,
        "_copy_attributes",
        lambda element, attributes: {
            attribute: ("AXApplication" if attribute == "AXRole" else None)
            for attribute in attributes
        },
    )
    monkeypatch.setattr(mac, "_actions", lambda element: [])

    mac._snapshot_tree(
        first,
        max_depth=1,
        max_nodes=2,
        include_menu_bar=False,
        attributes=("AXRole",),
    )
    mac._snapshot_tree(
        second,
        max_depth=1,
        max_nodes=2,
        include_menu_bar=False,
        attributes=("AXRole",),
        reset_elements=False,
    )

    assert mac._element(0) is first
    assert mac._element(1) is second


def test_cleared_element_handles_never_alias() -> None:
    mac = MacOS()
    old_index = mac._remember_element(object())
    mac._elements = {}
    new_element = object()
    new_index = mac._remember_element(new_element)

    assert new_index > old_index
    assert mac._element(new_index) is new_element
    with pytest.raises(MacOSError, match="Unknown element index"):
        mac._element(old_index)


def test_ax_query_all_scans_every_app_without_mutating_targets(monkeypatch) -> None:
    mac = MacOS()
    apps = [
        {"name": "First", "bundle_id": "one", "pid": 1, "path": "/First"},
        {"name": "Blocked", "bundle_id": "two", "pid": 2, "path": "/Blocked"},
        {
            "name": "Messages",
            "bundle_id": "com.apple.MobileSMS",
            "pid": 99,
            "path": "/Messages",
        },
        {"name": "Third", "bundle_id": "three", "pid": 3, "path": "/Third"},
    ]
    elements = {1: object(), 99: object(), 3: object()}
    calls = []
    original_target = {"name": "Current", "bundle_id": "current", "pid": 7}
    mac._last_app = original_target
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "list_apps", lambda: apps)

    def fake_search(*, app_pid, limit, reset_elements, attributes, **kwargs):
        calls.append(
            (
                app_pid,
                limit,
                reset_elements,
                tuple(attributes),
                kwargs["messaging_timeout"],
                kwargs["enhance"],
            )
        )
        if app_pid == 2:
            raise MacOSError("inaccessible")
        index = mac._remember_element(elements[app_pid])
        return [{"element_index": index, "role": "AXButton"}]

    monkeypatch.setattr(mac, "ax_search", fake_search)

    results = mac.ax.query_all("Not Now", limit=2)

    assert [result["app"]["pid"] for result in results] == [1, 99]
    assert [call[:3] for call in calls] == [
        (1, 2, False),
        (2, 1, False),
        (99, 1, False),
    ]
    assert all("AXValue" not in call[3] for call in calls)
    assert all(call[4:] == (0.5, False) for call in calls)
    assert mac._element(0) is elements[1]
    assert mac._element(1) is elements[99]
    assert mac._last_app is original_target

    with pytest.raises(MacOSError, match="requires non-empty text"):
        mac.ax.query_all()
    with pytest.raises(MacOSError, match="at least one"):
        mac.ax.query_all("Not Now", apps=[])


def test_ax_role_aliases_and_app_selectors_fail_closed(monkeypatch) -> None:
    mac = MacOS()
    arguments = {}

    def fake_search_all(**kwargs):
        arguments.update(kwargs)
        return []

    monkeypatch.setattr(mac, "ax_search_all", fake_search_all)

    mac.ax.query_all("Not Now", role="text_field", apps="Safari")

    assert arguments["text"] == "Not Now"
    assert arguments["search_key"] == "AXTextFieldSearchKey"
    assert arguments["apps"] == "Safari"
    assert mac.ax._search_key(None, "any") == "AXAnyTypeSearchKey"

    with pytest.raises(MacOSError, match="Unknown AX role"):
        mac.ax.query_all("Not Now", role="buton")
    with pytest.raises(MacOSError, match="role or search_key"):
        mac.ax.query_all(
            "Not Now",
            role="button",
            search_key="AXAnyTypeSearchKey",
        )
    with pytest.raises(MacOSError, match="at least one"):
        mac._normalize_apps([])


def test_ax_app_resolution_deduplicates_pids(monkeypatch) -> None:
    mac = MacOS()
    info = {"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 42}
    monkeypatch.setattr(mac, "_resolve_app", lambda selector: (object(), info))

    resolved = mac._resolve_apps(("Safari", "com.apple.Safari", "42"))

    assert resolved == [info]


def test_ax_wait_retries_until_one_match(monkeypatch) -> None:
    mac = MacOS()
    responses = iter([[], [{"element_index": 7, "role": "AXButton"}]])
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: next(responses))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    match = mac.ax.wait(app="Chrome", text="Use Password", timeout=1.0)

    assert match["element_index"] == 7


def test_ax_wait_fails_closed_on_ambiguity_and_timeout(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac,
        "ax_search_all",
        lambda **kwargs: [
            {"element_index": 1},
            {"element_index": 2},
        ],
    )
    with pytest.raises(MacOSError, match="found 2 matches"):
        mac.ax.wait(all_apps=True, text="Not Now")

    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: [])
    with pytest.raises(MacOSError, match="timed out"):
        mac.ax.wait(app="Chrome", text="Missing", timeout=0)

    with pytest.raises(MacOSError, match="exactly one"):
        mac.ax.wait(app="Chrome", all_apps=True, text="Not Now")
    with pytest.raises(MacOSError, match="all_apps=True or apps"):
        mac.ax.wait(all_apps=True, apps=["Chrome"], text="Not Now")
    with pytest.raises(MacOSError, match="requires non-empty text"):
        mac.ax.wait(all_apps=True)


def test_ax_press_supports_one_line_cross_app_use(monkeypatch) -> None:
    mac = MacOS()
    match = {
        "element_index": 4,
        "role": "AXButton",
        "actions": ["AXPress"],
        "app": {"name": "Chrome", "pid": 42},
    }
    wait_arguments = {}
    actions = []

    def fake_wait(**kwargs):
        wait_arguments.update(kwargs)
        return match

    monkeypatch.setattr(mac, "ax_wait", fake_wait)
    monkeypatch.setattr(
        mac,
        "_frontmost_app",
        lambda: {"name": "Ghostty", "pid": 1},
    )
    monkeypatch.setattr(
        mac,
        "perform_action",
        lambda element_index, action: actions.append((element_index, action)),
    )

    assert mac.ax.press("Not Now", role="button", all_apps=True) is match
    assert wait_arguments["text"] == "Not Now"
    assert wait_arguments["search_key"] == "AXButtonSearchKey"
    assert wait_arguments["include_actions"] is True
    assert actions == [(4, "AXPress")]


def test_ax_press_reports_target_activation(monkeypatch) -> None:
    mac = MacOS()
    frontmost = iter(
        [
            {"name": "Ghostty", "pid": 1},
            {"name": "Chrome", "pid": 42},
        ]
    )
    monkeypatch.setattr(
        mac,
        "ax_wait",
        lambda **kwargs: {
            "element_index": 4,
            "role": "AXButton",
            "app": {"name": "Chrome", "pid": 42},
        },
    )
    monkeypatch.setattr(mac, "_pid", lambda app: 99)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(frontmost))
    monkeypatch.setattr(mac, "perform_action", lambda element_index, action: None)

    with pytest.raises(FocusChangedError, match="became frontmost"):
        mac.ax.press(app="Chrome", text="Not Now")


def test_ax_wait_gone_requires_two_empty_polls(monkeypatch) -> None:
    mac = MacOS()
    responses = iter(
        [
            [{"element_index": 1}],
            [],
            [{"element_index": 2}],
            [],
            [],
        ]
    )
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: next(responses))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    mac.ax.wait_gone("Not Now", app="Chrome", timeout=1.0)


def test_ax_wait_gone_handles_exit_and_timeout(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac,
        "ax_search",
        lambda **kwargs: (_ for _ in ()).throw(ApplicationNotFoundError("not running")),
    )
    mac.ax.wait_gone("Not Now", app="Chrome")

    monkeypatch.setattr(
        mac,
        "ax_search",
        lambda **kwargs: [{"element_index": 1}],
    )
    with pytest.raises(MacOSError, match="match remained"):
        mac.ax.wait_gone("Not Now", app="Chrome", timeout=0)


def test_background_click_posts_to_pid_without_warp_or_activate(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))
    monkeypatch.setattr(
        macos_module.AS,
        "CGWarpMouseCursorPosition",
        lambda point: pytest.fail("background click must not warp the cursor"),
    )

    mac.click(
        10,
        20,
        app="Slack",
        coordinate_space="screen",
    )

    assert posted == [42, 42]


def test_input_without_target_is_refused(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyElementAtPosition",
        lambda *args: pytest.fail("untargeted input must stop before AX hit testing"),
    )

    with pytest.raises(MacOSError, match="requires an app"):
        mac.type("hello")
    with pytest.raises(MacOSError, match="requires an app"):
        mac.click(10, 20, coordinate_space="screen")


def test_type_posts_one_physical_event_pair_per_character(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: event.update(length=length, text=text),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )

    mac.type("aB !🙂", app="Spotify")

    downs = [event for (event, pid) in posted if event["down"]]
    assert [event["keycode"] for event in downs] == [0, 11, 49, 18, 0]
    assert [event["text"] for event in downs] == ["a", "B", " ", "!", "🙂"]
    assert [event["length"] for event in downs] == [1, 1, 1, 1, 2]
    assert downs[0]["flags"] == 0
    assert downs[1]["flags"] == macos_module.AS.kCGEventFlagMaskShift
    assert downs[3]["flags"] == macos_module.AS.kCGEventFlagMaskShift
    assert all(pid == 42 for _, pid in posted)


def test_key_posts_real_modifier_transitions(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Other", "pid": 7})
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )

    mac.key("cmd+shift+a", app="Spotify")

    command = macos_module.AS.kCGEventFlagMaskCommand
    shift = macos_module.AS.kCGEventFlagMaskShift
    assert [event for event, _ in posted] == [
        {"keycode": 55, "down": True, "flags": command},
        {"keycode": 56, "down": True, "flags": command | shift},
        {"keycode": 0, "down": True, "flags": command | shift},
        {"keycode": 0, "down": False, "flags": command | shift},
        {"keycode": 56, "down": False, "flags": command},
        {"keycode": 55, "down": False, "flags": 0},
    ]
    assert all(pid == 42 for _, pid in posted)


def test_typing_stops_if_target_becomes_frontmost(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    focus = iter(
        [
            {"name": "Terminal", "pid": 7},
            {"name": "Spotify", "pid": 42},
        ]
    )
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(focus))
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: event.update(text=text),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )

    with pytest.raises(FocusChangedError, match="became frontmost during typing"):
        mac.type("ab", app="Spotify")

    assert len(posted) == 2


def test_background_click_uses_private_event_source(monkeypatch) -> None:
    mac = MacOS()
    sources = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateMouseEvent",
        lambda source, event_type, point, button: sources.append(source) or object(),
    )

    mac.click(
        10,
        20,
        app="Slack",
        coordinate_space="screen",
    )

    assert sources == [mac._event_source, mac._event_source]


def test_coordinate_click_never_guesses_an_ax_action(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(
        mac,
        "_application_element",
        lambda pid: pytest.fail("raw click must not inspect AX"),
    )
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))

    mac.click(10, 20, app="Slack", coordinate_space="screen")

    assert posted == [42, 42]


def test_screen_point_requires_screenshot() -> None:
    mac = MacOS()
    with pytest.raises(MacOSError, match="Take a screenshot"):
        mac._screen_point(10, 20, "screenshot")


def test_screen_point_converts_retina_pixels() -> None:
    mac = MacOS()
    mac._last_screenshot = {
        "bounds": {"x": -100.0, "y": 50.0, "width": 800.0, "height": 600.0},
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    assert mac._screen_point(400, 200, "screenshot") == (100.0, 150.0)
    assert mac._screen_point(400, 200, "window") == (300.0, 250.0)
    assert mac._screen_point(400, 200, "screen") == (400.0, 200.0)


def test_move_is_logical_only(monkeypatch) -> None:
    mac = MacOS()
    overlay_moves = []
    mac._last_screenshot = {
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 800,
        "height": 600,
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    monkeypatch.setattr(
        mac,
        "_post",
        lambda event, pid: pytest.fail("logical movement must not post an event"),
    )
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: overlay_moves.append((x, y, duration)),
    )

    position = mac.move(200, 100, duration=0.3)

    assert position == {
        "screen": {"x": 200.0, "y": 250.0},
        "image": {"x": 200.0, "y": 100.0},
        "inside": True,
    }
    assert overlay_moves == [(200.0, 250.0, 0.3)]


def test_pointer_overlay_controls(monkeypatch) -> None:
    mac = MacOS()
    calls = []
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: calls.append(("move", x, y, duration)),
    )
    monkeypatch.setattr(
        mac._overlay,
        "show",
        lambda x, y: calls.append(("show", x, y)),
    )
    monkeypatch.setattr(mac._overlay, "hide", lambda: calls.append(("hide",)))

    mac.move(10, 20, coordinate_space="screen")
    mac.hide_pointer()
    mac.show_pointer()

    assert calls == [
        ("move", 10.0, 20.0, 0.16),
        ("hide",),
        ("show", 10.0, 20.0),
    ]


def test_see_bounds_image_and_draws_virtual_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    mac = MacOS()
    path = tmp_path / "window.png"
    Image.new("RGB", (800, 600), "white").save(path)

    def fake_capture(*args, **kwargs):
        return {
            "path": str(path),
            "app": {"name": "Test", "pid": 42},
            "pid": 42,
            "window_id": 7,
            "width": 800,
            "height": 600,
            "bounds": {
                "x": 100.0,
                "y": 200.0,
                "width": 400.0,
                "height": 300.0,
            },
            "scale_x": 2.0,
            "scale_y": 2.0,
        }

    monkeypatch.setattr(mac, "capture_screenshot", fake_capture)
    monkeypatch.setattr(
        mac,
        "_frontmost_app",
        lambda: {"name": "Test", "bundle_id": "test", "pid": 42, "path": None},
    )
    monkeypatch.setattr(mac._overlay, "move", lambda *args, **kwargs: None)
    mac.move(300, 350, coordinate_space="screen")

    result = mac.see("Test", max_width=400, max_height=400)

    assert (result["width"], result["height"]) == (400, 300)
    assert result["virtual_pointer"] == {
        "screen": {"x": 300.0, "y": 350.0},
        "image": {"x": 200.0, "y": 150.0},
        "inside": True,
        "visible": True,
    }
    assert result["focus"] == {
        "frontmost": {
            "name": "Test",
            "bundle_id": "test",
            "pid": 42,
            "path": None,
        },
        "target_is_frontmost": True,
    }
    with Image.open(path) as image:
        assert image.getpixel((205, 170)) != (255, 255, 255, 255)

    Image.new("RGB", (800, 600), "white").save(path)
    mac.hide_pointer()
    hidden = mac.see("Test", max_width=400, max_height=400)
    assert hidden["virtual_pointer"]["visible"] is False
    with Image.open(path) as image:
        assert image.getpixel((205, 170)) == (255, 255, 255, 255)
