"""Public Python interface for macOS Harness."""

from .browser import BrowserHarness
from .macos import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOS,
    MacOSError,
)

__all__ = [
    "AccessibilityPermissionError",
    "ApplicationNotFoundError",
    "BrowserHarness",
    "FocusChangedError",
    "MacOS",
    "MacOSError",
]
