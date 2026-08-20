from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import macos_harness.macos as macos_module
from macos_harness.macos import (
    _KEYCODES,
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
    for verb in ("at", "query", "get", "set", "perform"):
        assert callable(getattr(mac.ax, verb))
    assert not hasattr(mac, "mouse")
    assert not hasattr(mac, "keyboard")


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
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    assert MacOS._application_element(42) is root
    assert writes == [(root, "AXEnhancedUserInterface", True)]


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
    monkeypatch.setattr(mac, "_application_element", lambda pid: root)
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


def test_default_click_warns_that_pid_transport_is_discarded(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: None)

    with pytest.warns(RuntimeWarning, match="AppKit discards"):
        mac.click(10, 20, app="Slack", coordinate_space="screen")


def test_pointer_move_click_uses_hid_tap_and_restores_cursor(monkeypatch) -> None:
    mac = MacOS()
    warps: list[object] = []
    globals_posted: list[object] = []
    origin = object()

    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_cursor_position", staticmethod(lambda: origin))
    monkeypatch.setattr(mac, "_owner_pid_at", staticmethod(lambda point: 42))
    monkeypatch.setattr(
        mac, "_post", lambda event, pid: pytest.fail("pointer_move must not use PID")
    )
    monkeypatch.setattr(
        mac,
        "_guard_focus",
        lambda *a: pytest.fail("pointer_move must not apply the focus guard"),
    )
    monkeypatch.setattr(MacOS, "_post_global", staticmethod(globals_posted.append))
    monkeypatch.setattr(macos_module.AS, "CGWarpMouseCursorPosition", warps.append)

    mac.click(10, 20, app="Slack", coordinate_space="screen", pointer_move=True)

    assert len(globals_posted) == 2
    assert warps[0] == (10.0, 20.0)
    assert warps[-1] is origin


def test_pointer_move_refuses_to_click_an_occluded_target(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_owner_pid_at", staticmethod(lambda point: 99))
    monkeypatch.setattr(
        MacOS,
        "_post_global",
        staticmethod(lambda event: pytest.fail("must not click an occluded target")),
    )

    with pytest.raises(MacOSError, match="would click pid 99, not 42"):
        mac.click(10, 20, app="Slack", coordinate_space="screen", pointer_move=True)


def test_pointer_move_fails_closed_when_owner_is_unknown(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_owner_pid_at", staticmethod(lambda point: None))
    monkeypatch.setattr(
        MacOS,
        "_post_global",
        staticmethod(lambda event: pytest.fail("must not click an unresolved point")),
    )

    with pytest.raises(MacOSError, match="Cannot resolve which application owns"):
        mac.click(10, 20, app="Slack", coordinate_space="screen", pointer_move=True)


def test_pointer_move_rechecks_ownership_after_warping(monkeypatch) -> None:
    """A window that covers the point between preflight and post must abort."""
    mac = MacOS()
    owners = iter([42, 99])
    warps: list[object] = []
    origin = object()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_owner_pid_at", staticmethod(lambda point: next(owners)))
    monkeypatch.setattr(mac, "_cursor_position", staticmethod(lambda: origin))
    monkeypatch.setattr(
        MacOS,
        "_post_global",
        staticmethod(lambda event: pytest.fail("must not click after ownership moved")),
    )
    monkeypatch.setattr(macos_module.AS, "CGWarpMouseCursorPosition", warps.append)

    with pytest.raises(MacOSError, match="would click pid 99, not 42"):
        mac.click(10, 20, app="Slack", coordinate_space="screen", pointer_move=True)

    assert warps[-1] is origin, "cursor must still be restored when the recheck aborts"
