"""Public Python interface for macOS Harness."""

from __future__ import annotations

__all__ = [
    "AccessibilityPermissionError",
    "BrowserHarness",
    "FocusChangedError",
    "MacOS",
    "MacOSError",
]


def __getattr__(name: str):
    if name in {"MacOS", "MacOSError", "AccessibilityPermissionError", "FocusChangedError"}:
        from . import macos

        return getattr(macos, name)
    if name == "BrowserHarness":
        from . import browser

        return browser.BrowserHarness
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
