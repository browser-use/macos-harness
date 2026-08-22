"""Hermetic tests for ``macos_harness.receipts``: the shared receipt,
postcondition, and operation-error contract ``mac.do`` is built on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from macos_harness.errors import MacOSError
from macos_harness.receipts import (
    Acted,
    ErrorPayload,
    Executor,
    Gone,
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

# --- enum values -------------------------------------------------------


def test_outcome_values() -> None:
    assert [member.value for member in Outcome] == ["planned", "done", "already", "failed"]


def test_acted_values() -> None:
    assert [member.value for member in Acted] == ["no", "yes", "unknown"]


def test_executor_values() -> None:
    assert [member.value for member in Executor] == ["python", "native", "input", "script"]


# --- canonicalize(): normalizing arbitrary values into JSONValue -----------


def test_canonicalize_freezes_mappings_and_sequences() -> None:
    result = canonicalize({"b": [1, 2, {"z": 1, "a": 2}], "a": "x"})
    assert isinstance(result, MappingProxyType)
    assert isinstance(result["b"], tuple)
    assert isinstance(result["b"][2], MappingProxyType)
    assert result == {"b": (1, 2, {"z": 1, "a": 2}), "a": "x"}


def test_canonicalize_passes_through_primitives_unchanged() -> None:
    assert canonicalize(None) is None
    assert canonicalize(True) is True
    assert canonicalize(3) == 3
    assert canonicalize(3.5) == 3.5
    assert canonicalize("text") == "text"


def test_canonicalize_rejects_non_json_safe_values() -> None:
    with pytest.raises(TypeError):
        canonicalize({1, 2, 3})
    with pytest.raises(TypeError):
        canonicalize(object())


def test_canonicalize_rejects_non_string_mapping_keys() -> None:
    with pytest.raises(TypeError):
        canonicalize({1: "a"})
    with pytest.raises(TypeError):
        canonicalize({1.5: "a"})
    with pytest.raises(TypeError):
        canonicalize({("tuple", "key"): "a"})
    # Coercing every key through `str()` would silently collapse these two
    # distinct entries into one and lose whichever value lost the race --
    # canonicalize must reject the whole mapping instead of losing data.
    with pytest.raises(TypeError):
        canonicalize({1: "int key", "1": "str key"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonicalize_rejects_non_finite_floats(bad: float) -> None:
    with pytest.raises(ValueError):
        canonicalize(bad)


# --- canonical fingerprint: deterministic, order-independent --------------


def test_request_fingerprint_is_independent_of_mapping_order() -> None:
    request_a = {"app": "Notes", "opts": {"x": 1, "y": 2}, "text": "Save"}
    request_b = {"text": "Save", "opts": {"y": 2, "x": 1}, "app": "Notes"}
    assert request_fingerprint(canonicalize(request_a)) == request_fingerprint(
        canonicalize(request_b)
    )


def test_request_fingerprint_is_sensitive_to_real_differences() -> None:
    request_a = {"app": "Notes", "text": "Save"}
    request_b = {"app": "Notes", "text": "Save As"}
    assert request_fingerprint(canonicalize(request_a)) != request_fingerprint(
        canonicalize(request_b)
    )


def test_request_fingerprint_is_deterministic_across_calls() -> None:
    request = {"app": "Notes", "text": "Save"}
    assert request_fingerprint(canonicalize(request)) == request_fingerprint(canonicalize(request))


def test_canonical_json_sorts_keys_at_every_depth() -> None:
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 1}}) == '{"a":{"x":1,"y":2},"b":1}'


# --- Present / Gone postconditions --------------------------------------


def test_present_and_gone_factories_build_the_right_type() -> None:
    assert isinstance(present("Saved"), Present)
    assert isinstance(gone("Not Now"), Gone)


def test_present_and_gone_default_to_inheriting_scope() -> None:
    p = Present()
    assert p.app is None
    assert p.apps is None
    assert p.all_apps is False


def test_present_and_gone_helpers_freeze_app_scope() -> None:
    assert present("x", apps=["Spotify", 123]).apps == ("Spotify", 123)
    assert gone("x", apps=("Spotify",)).apps == ("Spotify",)
    assert present("x", apps="Spotify").apps == "Spotify"
    assert present("x", apps=None).apps is None


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_rejects_more_than_one_scope_selector(
    postcondition_class: type[Present | Gone],
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(app="Notes", all_apps=True)
    assert excinfo.value.code == "bad_request"

    with pytest.raises(MacOSError):
        postcondition_class(app="Notes", apps=["Notes"])

    with pytest.raises(MacOSError):
        postcondition_class(all_apps=True, apps=["Notes"])


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_accepts_a_single_scope_selector(
    postcondition_class: type[Present | Gone],
) -> None:
    postcondition_class(app="Notes")
    postcondition_class(all_apps=True)
    postcondition_class(apps=["Notes"])


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_rejects_bad_direction(
    postcondition_class: type[Present | Gone],
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(direction="sideways")
    assert excinfo.value.code == "bad_request"


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_accepts_direction_case_insensitively(
    postcondition_class: type[Present | Gone],
) -> None:
    postcondition_class(direction="Next")
    postcondition_class(direction="PREVIOUS")


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_rejects_negative_timeout(
    postcondition_class: type[Present | Gone],
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(timeout=-1)
    assert excinfo.value.code == "bad_request"


def test_present_allows_a_zero_timeout() -> None:
    """`Present` only ever needs one look, so a zero timeout is a
    normal, satisfiable "check right now" request."""
    assert Present(timeout=0).timeout == 0


def test_gone_rejects_a_zero_timeout() -> None:
    """`Gone` needs two *consecutive* empty polls to confirm absence;
    a zero-length deadline can never let the second one happen."""
    with pytest.raises(MacOSError) as excinfo:
        Gone(timeout=0)
    assert excinfo.value.code == "bad_request"


def test_gone_allows_a_positive_or_inherited_timeout() -> None:
    assert Gone(timeout=0.5).timeout == 0.5
    assert Gone().timeout is None


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
def test_postcondition_rejects_non_positive_interval(
    postcondition_class: type[Present | Gone],
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(interval=0)
    assert excinfo.value.code == "bad_request"
    with pytest.raises(MacOSError):
        postcondition_class(interval=-0.1)


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_postcondition_rejects_nonfinite_timeout(
    postcondition_class: type[Present | Gone], bad: float
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(timeout=bad)
    assert excinfo.value.code == "bad_request"


@pytest.mark.parametrize("postcondition_class", [Present, Gone])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_postcondition_rejects_nonfinite_interval(
    postcondition_class: type[Present | Gone], bad: float
) -> None:
    with pytest.raises(MacOSError) as excinfo:
        postcondition_class(interval=bad)
    assert excinfo.value.code == "bad_request"


def test_postcondition_type_alias_covers_both_variants() -> None:
    def describe(postcondition: Postcondition) -> str:
        return type(postcondition).__name__

    assert describe(present("x")) == "Present"
    assert describe(gone("x")) == "Gone"


# --- Receipt -------------------------------------------------------------


def _make_receipt(**overrides: object) -> Receipt:
    fields: dict[str, object] = {
        "op": "press",
        "outcome": Outcome.DONE,
        "acted": Acted.YES,
        "backend": "python",
        "executor": Executor.PYTHON,
        "request": {"text": "Save", "app": "Notes"},
        "target": {"role": "AXButton", "title": "Save"},
        "changed": True,
        "verified": True,
        "duration_s": 0.123,
        "once": "tok-1",
    }
    fields.update(overrides)
    return Receipt(**fields)  # type: ignore[arg-type]


def test_receipt_requires_only_its_core_fields() -> None:
    receipt = Receipt(
        op="run",
        outcome=Outcome.PLANNED,
        acted=Acted.NO,
        backend="auto",
        executor=Executor.SCRIPT,
        request={},
        changed=False,
        verified=False,
        duration_s=0.0,
    )
    assert receipt.target is None
    assert receipt.observed is None
    assert receipt.once is None
    assert receipt.replayed is False
    assert receipt.error is None


def test_receipt_is_frozen() -> None:
    receipt = _make_receipt()
    with pytest.raises(FrozenInstanceError):
        receipt.changed = False  # type: ignore[misc]


def test_receipt_changed_accepts_none_for_an_unconfirmed_effect() -> None:
    """`changed` is `bool | None`: `None` means "dispatched (or not),
    but whether it actually changed anything is unconfirmed" -- distinct
    from the confident `True`/`False` a real before/after comparison or
    a predispatch failure gives."""
    receipt = _make_receipt(changed=None)
    assert receipt.changed is None
    assert receipt.to_json()["changed"] is None


def test_receipt_canonicalizes_request_target_observed_on_construction() -> None:
    receipt = _make_receipt(observed=["a", "b"])
    assert isinstance(receipt.request, MappingProxyType)
    assert isinstance(receipt.target, MappingProxyType)
    assert isinstance(receipt.observed, tuple)
    assert receipt.observed == ("a", "b")


def test_receipt_freezes_a_structured_error() -> None:
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4},
    }
    receipt = _make_receipt(outcome=Outcome.FAILED, error=error)
    assert isinstance(receipt.error["details"], MappingProxyType)
    with pytest.raises(TypeError):
        receipt.error["details"]["element_index"] = 5  # type: ignore[index]


def test_receipt_freezes_the_outer_error_mapping_too() -> None:
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4},
    }
    receipt = _make_receipt(outcome=Outcome.FAILED, error=error)
    assert isinstance(receipt.error, MappingProxyType)
    with pytest.raises(TypeError):
        receipt.error["code"] = "different"  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.error["message"] = "different"  # type: ignore[index]
    with pytest.raises(TypeError):
        del receipt.error["code"]  # type: ignore[index]


def test_receipt_error_runtime_shape_uses_mapping_proxy() -> None:
    """`Receipt.error` is typed `Mapping[str, JSONValue] | None` -- the
    same read-only shape `_freeze_error` actually produces -- not the
    `ErrorPayload` `TypedDict` a constructor accepts. `ErrorPayload` is
    a plain `dict` at runtime; a frozen `Receipt` never hands one back,
    even though a `dict`-shaped `ErrorPayload` is exactly what
    constructed it."""
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4},
    }
    assert isinstance(error, dict)  # the public constructor input shape

    receipt = _make_receipt(outcome=Outcome.FAILED, error=error)

    assert isinstance(receipt.error, Mapping)
    assert not isinstance(receipt.error, dict)
    assert isinstance(receipt.error, MappingProxyType)


def test_receipt_to_json_is_json_safe() -> None:
    receipt = _make_receipt()
    payload = receipt.to_json()
    assert json.loads(json.dumps(payload)) == payload
    assert isinstance(payload["request"], dict)
    assert isinstance(payload["target"], dict)
    assert payload["outcome"] == "done"
    assert payload["acted"] == "yes"
    assert payload["executor"] == "python"


def test_receipt_to_json_is_json_safe_for_a_failed_receipt() -> None:
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4, "nested": {"a": [1, 2, 3]}},
    }
    receipt = _make_receipt(
        outcome=Outcome.FAILED, changed=False, verified=False, error=error
    )
    payload = receipt.to_json()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["error"] == {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4, "nested": {"a": [1, 2, 3]}},
    }
    assert isinstance(payload["error"], dict)
    assert isinstance(payload["error"]["details"], dict)
    assert isinstance(payload["error"]["details"]["nested"]["a"], list)


def test_receipt_to_json_matches_every_field() -> None:
    receipt = _make_receipt()
    payload = receipt.to_json()
    assert payload == {
        "op": "press",
        "outcome": "done",
        "acted": "yes",
        "backend": "python",
        "executor": "python",
        "request": {"text": "Save", "app": "Notes"},
        "target": {"role": "AXButton", "title": "Save"},
        "observed": None,
        "changed": True,
        "verified": True,
        "duration_s": 0.123,
        "once": "tok-1",
        "replayed": False,
        "error": None,
    }


# --- replayed_as(): the once-token replay copy -----------------------------


def test_replayed_as_stamps_replayed_without_changing_anything_else() -> None:
    receipt = _make_receipt()
    replayed = receipt.replayed_as()
    assert replayed.replayed is True
    assert receipt.replayed is False  # original untouched
    assert replayed.to_json() == {**receipt.to_json(), "replayed": True}


def test_replayed_as_preserves_request_equality() -> None:
    receipt = _make_receipt()
    replayed = receipt.replayed_as()
    assert replayed.request == receipt.request
    assert replayed.once == receipt.once


# --- OperationError: carries exactly one receipt ----------------------------


def test_operation_error_is_a_macos_error_and_carries_its_receipt() -> None:
    receipt = _make_receipt(outcome=Outcome.FAILED, changed=False, verified=False)
    exc = OperationError("press failed", receipt=receipt, code="element.unknown")
    assert isinstance(exc, MacOSError)
    assert exc.receipt is receipt
    assert exc.code == "element.unknown"
    assert str(exc) == "press failed"


def test_operation_error_from_receipt_reconstructs_an_equivalent_exception() -> None:
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "Element 4 is stale; take a fresh snapshot first",
        "details": {"element_index": 4},
    }
    receipt = _make_receipt(outcome=Outcome.FAILED, changed=False, verified=False, error=error)

    exc = OperationError.from_receipt(receipt)

    assert exc.code == "element.unknown"
    assert str(exc) == "Element 4 is stale; take a fresh snapshot first"
    assert dict(exc.details) == {"element_index": 4}
    assert exc.receipt is receipt


def test_operation_error_from_receipt_is_json_safe_with_nested_details() -> None:
    """A `Receipt`'s frozen `error["details"]` can nest further
    mappings and sequences (canonicalized to `MappingProxyType`/
    `tuple`); `from_receipt` must convert all of that back to plain,
    ordinary `dict`/`list` so `json.dumps(exc.to_json())` -- the same
    guarantee `Receipt.to_json()` already makes -- actually holds."""
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"element_index": 4, "nested": {"a": [1, 2, {"b": 3}]}},
    }
    receipt = _make_receipt(
        outcome=Outcome.FAILED, changed=False, verified=False, error=error
    )

    exc = OperationError.from_receipt(receipt)

    assert json.loads(json.dumps(exc.to_json())) == exc.to_json()
    assert isinstance(exc.details["nested"], dict)
    assert isinstance(exc.details["nested"]["a"], list)


def test_operation_error_from_receipt_details_are_isolated_copies() -> None:
    """The reconstructed exception's nested `details` are brand-new
    `dict`/`list` objects, not the same frozen `MappingProxyType`/
    `tuple` objects the `Receipt` still holds -- mutating one must
    never reach the other."""
    error: ErrorPayload = {
        "code": "element.unknown",
        "message": "stale element",
        "details": {"nested": {"a": [1, 2, 3]}},
    }
    receipt = _make_receipt(
        outcome=Outcome.FAILED, changed=False, verified=False, error=error
    )

    exc = OperationError.from_receipt(receipt)

    assert exc.details["nested"] is not receipt.error["details"]["nested"]
    exc.details["nested"]["a"].append(4)
    assert receipt.error["details"]["nested"]["a"] == (1, 2, 3)
    assert exc.details["nested"]["a"] == [1, 2, 3, 4]


def test_operation_error_from_receipt_requires_a_structured_error() -> None:
    receipt = _make_receipt()  # outcome=DONE, error=None
    with pytest.raises(ValueError):
        OperationError.from_receipt(receipt)


# --- import-time independence from PyObjC -----------------------------------

_NO_PYOBJC_PROBE = (
    "import sys\n"
    "sys.modules['ApplicationServices'] = None\n"
    "sys.modules['AppKit'] = None\n"
    "import macos_harness.receipts as receipts\n"
    "print(receipts.Outcome.DONE.value)\n"
)


def test_imports_cleanly_with_pyobjc_unavailable() -> None:
    """``receipts.py`` only imports the standard library and ``errors.py``,
    so, like ``errors.py`` itself, it never touches
    ``ApplicationServices``/``AppKit`` -- confirmed here in a fresh
    interpreter with both blacklisted from import.
    """
    result = subprocess.run(
        [sys.executable, "-c", _NO_PYOBJC_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"
