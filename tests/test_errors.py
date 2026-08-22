"""Hermetic tests for ``macos_harness.errors``: the machine-readable wire
error taxonomy shared by the raw primitives and ``mac.do``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from macos_harness.errors import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    ErrorCode,
    FocusChangedError,
    MacOSError,
)

# --- ErrorCode: the frozen nine-code vocabulary -----------------------------


def test_error_code_is_exactly_the_nine_wire_codes() -> None:
    assert {member.value for member in ErrorCode} == {
        "permission.accessibility",
        "app.not_found",
        "app.ambiguous",
        "focus.changed",
        "ax.error",
        "element.unknown",
        "timeout",
        "bad_request",
        "unsupported_op",
    }


# --- default / explicit / unknown wire code behavior ------------------------


@pytest.mark.parametrize(
    ("error_class", "expected_code"),
    [
        (MacOSError, ErrorCode.AX_ERROR),
        (AccessibilityPermissionError, ErrorCode.PERMISSION_ACCESSIBILITY),
        (ApplicationNotFoundError, ErrorCode.APP_NOT_FOUND),
        (FocusChangedError, ErrorCode.FOCUS_CHANGED),
    ],
)
def test_default_code_matches_class(
    error_class: type[MacOSError], expected_code: ErrorCode
) -> None:
    exc = error_class("boom")
    assert exc.code == expected_code.value
    assert isinstance(exc.code, str)
    assert not isinstance(exc.code, ErrorCode)  # a plain str, not the enum member


def test_explicit_code_overrides_the_class_default() -> None:
    assert FocusChangedError("boom", code=ErrorCode.APP_AMBIGUOUS).code == "app.ambiguous"
    assert MacOSError("boom", code="timeout").code == "timeout"
    assert AccessibilityPermissionError("boom", code=ErrorCode.BAD_REQUEST).code == "bad_request"


def test_unknown_wire_code_is_accepted_forward_compatibly() -> None:
    # A newer native agent may one day speak a code this installed
    # version has never heard of; MacOSError must not reject it.
    exc = MacOSError("boom", code="future.code_this_version_has_never_seen")
    assert exc.code == "future.code_this_version_has_never_seen"


@pytest.mark.parametrize("error_class", [AccessibilityPermissionError, ApplicationNotFoundError, FocusChangedError])
def test_every_subclass_remains_a_macos_error(error_class: type[MacOSError]) -> None:
    assert issubclass(error_class, MacOSError)
    assert issubclass(error_class, RuntimeError)


# --- message / str / repr stay stable ---------------------------------------


def test_str_returns_the_plain_message_unchanged() -> None:
    assert str(MacOSError("Element 4 is stale")) == "Element 4 is stale"
    assert str(ApplicationNotFoundError("No running application matches 'Notes'")) == (
        "No running application matches 'Notes'"
    )


def test_repr_is_deterministic_and_shows_code() -> None:
    assert repr(MacOSError("boom")) == "MacOSError('boom', code='ax.error')"
    assert repr(MacOSError("boom", code="timeout")) == "MacOSError('boom', code='timeout')"
    # same inputs always produce the same repr
    assert repr(MacOSError("boom", code="timeout")) == repr(MacOSError("boom", code="timeout"))


def test_repr_includes_details_only_when_present() -> None:
    assert repr(MacOSError("boom")) == "MacOSError('boom', code='ax.error')"
    assert (
        repr(MacOSError("boom", details={"x": 1}))
        == "MacOSError('boom', code='ax.error', details={'x': 1})"
    )


def test_repr_uses_the_actual_subclass_name() -> None:
    assert repr(FocusChangedError("boom")).startswith("FocusChangedError(")


# --- details: immutability + defensive copying ------------------------------


def test_details_defaults_to_an_empty_mapping() -> None:
    assert dict(MacOSError("boom").details) == {}


def test_details_round_trips() -> None:
    exc = MacOSError("boom", details={"element_index": 4, "role": "AXButton"})
    assert dict(exc.details) == {"element_index": 4, "role": "AXButton"}


def test_details_rejects_mutation() -> None:
    exc = MacOSError("boom", details={"a": 1})
    with pytest.raises(TypeError):
        exc.details["a"] = 2  # type: ignore[index]


def test_details_is_defensively_copied_from_the_caller_mapping() -> None:
    source = {"a": 1}
    exc = MacOSError("boom", details=source)
    source["a"] = 999
    source["b"] = "leaked?"
    assert dict(exc.details) == {"a": 1}


# --- to_json(): JSON-safe structured payload --------------------------------


def test_to_json_is_json_safe_and_exactly_shaped() -> None:
    exc = ApplicationNotFoundError(
        "No running application matches 'Notes'",
        details={"query": "Notes", "candidates": []},
    )
    payload = exc.to_json()
    assert payload == {
        "code": "app.not_found",
        "message": "No running application matches 'Notes'",
        "details": {"query": "Notes", "candidates": []},
    }
    assert json.loads(json.dumps(payload)) == payload


def test_to_json_reflects_the_default_code_when_unset() -> None:
    assert MacOSError("boom").to_json()["code"] == "ax.error"


# --- import-time independence from PyObjC -----------------------------------

_NO_PYOBJC_PROBE = (
    "import sys\n"
    "sys.modules['ApplicationServices'] = None\n"
    "sys.modules['AppKit'] = None\n"
    "import macos_harness.errors as errors\n"
    "print(errors.MacOSError('boom').code)\n"
)


def test_imports_cleanly_with_pyobjc_unavailable() -> None:
    """``errors.py`` has no macOS-only import, so it (unlike ``macos.py``,
    which tolerates a missing PyObjC rather than avoiding it outright)
    never even touches ``ApplicationServices``/``AppKit`` -- confirmed
    here in a fresh interpreter with both blacklisted from import.
    """
    result = subprocess.run(
        [sys.executable, "-c", _NO_PYOBJC_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ax.error"
