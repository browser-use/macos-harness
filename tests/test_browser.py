from __future__ import annotations

import threading

import macos_harness.browser as browser_module


def test_chrome_approver_uses_systemwide_ax_without_input(monkeypatch) -> None:
    root = object()
    button = object()
    pressed = []
    stop = threading.Event()
    bounds = {"X": 100, "Y": 200, "Width": 448, "Height": 240}

    class FakeAS:
        kCGWindowListOptionOnScreenOnly = 1
        kCGNullWindowID = 0

        @staticmethod
        def AXUIElementCreateSystemWide():
            return root

        @staticmethod
        def CGWindowListCopyWindowInfo(options, window_id):
            return [
                {
                    "kCGWindowOwnerName": "Google Chrome",
                    "kCGWindowName": "Allow remote debugging?",
                    "kCGWindowBounds": bounds,
                }
            ]

        @staticmethod
        def AXUIElementCopyElementAtPosition(element, x, y, error):
            assert element is root
            assert (x, y) == (490.0, 402.0)
            return 0, button

        @staticmethod
        def AXUIElementCopyAttributeValue(element, attribute, error):
            assert element is button
            return 0, {"AXRole": "AXButton", "AXDescription": "Allow"}[attribute]

        @staticmethod
        def AXUIElementPerformAction(element, action):
            pressed.append((element, action))
            stop.set()

    monkeypatch.setattr(browser_module.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(__import__("sys").modules, "ApplicationServices", FakeAS)

    browser_module._approve_chrome_prompt(stop, 1)

    assert pressed == [(button, "AXPress")]
