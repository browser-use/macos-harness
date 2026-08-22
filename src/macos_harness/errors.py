"""Machine-readable error taxonomy shared by every macOS Harness surface.

One frozen vocabulary of wire error codes backs both the raw six
primitives (``MacOS``, ``Accessibility``) and the ``mac.do`` operation
layer described in ``receipts.py``: a caller -- human or another process
reading a serialized ``Receipt`` -- can dispatch on ``exc.code`` without
parsing prose, while ``str(exc)`` keeps the human-readable message every
existing caller already relies on.

This module imports nothing beyond the standard library, so it (and
everything that in turn imports only from it) loads on any host, with or
without PyObjC installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

__all__ = [
    "AccessibilityPermissionError",
    "ApplicationNotFoundError",
    "ErrorCode",
    "FocusChangedError",
    "MacOSError",
]


class ErrorCode(StrEnum):
    """Every wire error code the native agent protocol and ``mac.do`` speak.

    Mirrors the native agent's wire envelope 1:1 (see ``native.py``): a
    response's ``error.code`` is one of these strings today, or --
    forward compatibly -- some other string a newer agent invented that
    this installed version has not learned yet (see ``MacOSError.code``,
    which never rejects an unrecognized code).
    """

    PERMISSION_ACCESSIBILITY = "permission.accessibility"
    APP_NOT_FOUND = "app.not_found"
    APP_AMBIGUOUS = "app.ambiguous"
    FOCUS_CHANGED = "focus.changed"
    AX_ERROR = "ax.error"
    ELEMENT_UNKNOWN = "element.unknown"
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"
    UNSUPPORTED_OP = "unsupported_op"


def _normalize_code(code: str | ErrorCode) -> str:
    return code.value if isinstance(code, ErrorCode) else str(code)


def _normalize_details(details: Mapping[str, object] | None) -> Mapping[str, object]:
    # A fresh `dict` copy first, so later mutation of a mapping the
    # caller still holds a reference to never reaches back into this
    # error after construction, then a read-only view over that private
    # copy, so nothing can rebind or delete a top-level key afterward.
    # This is only a *shallow* freeze, though: a mutable value nested
    # inside `details` (a caller-owned list/dict passed as one of its
    # values) is not itself copied or frozen, and stays mutable after
    # construction -- unlike `receipts.py`'s `canonicalize`/
    # `_freeze_error`, which really does freeze recursively.
    return MappingProxyType(dict(details) if details else {})


class MacOSError(RuntimeError):
    """Base error for macOS discovery or control failures.

    Carries a machine-readable ``code`` alongside the human-readable
    message every existing caller already matches on with ``str(exc)``
    or ``isinstance``. ``code`` defaults to this class's ``default_code``
    when a raise site does not pass one explicitly, so every
    pre-existing ``raise MacOSError("...")`` call site keeps working
    unchanged and still gets a sensible code: ``ax.error``, the same
    generic bucket the native wire protocol already falls back to for a
    response that specifies no code of its own.

    A subclass with a more specific failure mode overrides
    ``default_code``; see ``AccessibilityPermissionError`` and friends
    below, and ``OperationError`` in ``receipts.py``.
    """

    #: The wire code an instance of this class gets when its raise site
    #: does not pass an explicit ``code=``. A subclass overrides this
    #: with its own single class attribute assignment -- no need to
    #: repeat the ``ClassVar[ErrorCode]`` annotation.
    default_code: ClassVar[ErrorCode] = ErrorCode.AX_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | ErrorCode | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = _normalize_code(code) if code is not None else self.default_code.value
        self.details: Mapping[str, object] = _normalize_details(details)

    def to_json(self) -> dict[str, object]:
        """A JSON-safe ``{"code", "message", "details"}`` payload.

        Safe to ``json.dumps`` as long as ``details`` was built from
        JSON-safe values (``str``, ``int``, ``float``, ``bool``,
        ``None``, and lists/dicts of the same) -- the same discipline
        the native wire protocol already requires of everything it puts
        on the socket. ``receipts.py``'s ``ErrorPayload`` documents this
        exact shape for a ``Receipt``'s own structured ``error`` field.
        """
        return {"code": self.code, "message": str(self), "details": dict(self.details)}

    def __repr__(self) -> str:
        parts = [repr(str(self)), f"code={self.code!r}"]
        if self.details:
            parts.append(f"details={dict(self.details)!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


class AccessibilityPermissionError(MacOSError):
    """The calling process lacks macOS Accessibility permission.

    Defaults to ``ErrorCode.PERMISSION_ACCESSIBILITY``.
    """

    default_code = ErrorCode.PERMISSION_ACCESSIBILITY


class ApplicationNotFoundError(MacOSError):
    """No running application matches a caller-provided selector.

    Defaults to ``ErrorCode.APP_NOT_FOUND``.
    """

    default_code = ErrorCode.APP_NOT_FOUND


class FocusChangedError(MacOSError):
    """A background-targeted action made its target frontmost.

    Defaults to ``ErrorCode.FOCUS_CHANGED``.
    """

    default_code = ErrorCode.FOCUS_CHANGED
