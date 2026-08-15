"""Public Python interface for macOS Harness."""

from .browser import BrowserHarness
from .macos import AccessibilityPermissionError, FocusChangedError, MacOS, MacOSError

__all__ = [
    "AccessibilityPermissionError",
    "BrowserHarness",
    "FocusChangedError",
    "MacOS",
    "MacOSError",
]
