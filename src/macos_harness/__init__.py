"""Public Python interface for macOS Harness."""

from ._version import __version__
from .browser import BrowserHarness
from .errors import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    ErrorCode,
    FocusChangedError,
    MacOSError,
)
from .macos import MacOS
from .receipts import (
    Acted,
    ErrorPayload,
    Executor,
    Gone,
    JSONValue,
    OperationError,
    Outcome,
    Postcondition,
    Present,
    Receipt,
    canonical_json,
    canonicalize,
    gone,
    present,
    request_fingerprint,
)

__all__ = [
    "AccessibilityPermissionError",
    "Acted",
    "ApplicationNotFoundError",
    "BrowserHarness",
    "ErrorCode",
    "ErrorPayload",
    "Executor",
    "FocusChangedError",
    "Gone",
    "JSONValue",
    "MacOS",
    "MacOSError",
    "OperationError",
    "Outcome",
    "Postcondition",
    "Present",
    "Receipt",
    "__version__",
    "canonical_json",
    "canonicalize",
    "gone",
    "present",
    "request_fingerprint",
]
