"""The shared receipt, postcondition, and operation-error contract for
``mac.do``.

``mac.do`` (``Operations``, layered on top of the raw six primitives) is
the recommended way to perform a mutation. Once a call's arguments pass
preflight and it actually begins resolving or dispatching something, it
always ends in -- or raises with -- a ``Receipt``: a small, immutable,
JSON-safe record of exactly what was requested, what actually happened,
and how thoroughly that was confirmed, instead of a bare return value a
caller has to trust blindly. A call rejected before that point -- bad
argument shape, a fork-boundary violation, an invalidly-shaped deadline,
or a ``once`` token already recorded for a different request -- raises
a plain ``MacOSError`` instead, with no receipt to carry: nothing was
ever attempted, so there is nothing yet to report. A ``Postcondition``
(``Present`` or ``Gone``) lets a caller ask ``mac.do`` to confirm the
*effect* of a mutation, not just that the underlying call returned
without raising. ``OperationError`` is what a failed, receipted
operation raises; it always carries the one ``Receipt`` describing that
failure, so a caller never has to choose between "catch the exception"
and "get the structured detail" -- both come from the same place.

This module imports nothing beyond the standard library and
``errors.py``, so it (and everything that in turn imports only from it)
loads on any host, with or without PyObjC installed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Self, TypeAlias, TypedDict

from .errors import ErrorCode, MacOSError

__all__ = [
    "Acted",
    "ErrorPayload",
    "Executor",
    "Gone",
    "JSONValue",
    "OperationError",
    "Outcome",
    "Postcondition",
    "Present",
    "Receipt",
    "canonical_json",
    "canonicalize",
    "gone",
    "present",
    "request_fingerprint",
]


# --- canonical JSON values --------------------------------------------------

#: A JSON-compatible value, recursively. Both an immutable "canonical"
#: shape (``tuple`` for an array, ``Mapping`` -- in practice a
#: ``MappingProxyType`` -- for an object) and a plain, mutable,
#: ``json``-module-native shape (``list``, ``dict``) type-check here:
#: `canonicalize` produces the former for anything a `Receipt` stores,
#: `Receipt.to_json()` produces the latter for anything handed back to a
#: caller. `dict` and `MappingProxyType` both satisfy `Mapping`, so only
#: the array case needs both alternatives spelled out explicitly.
JSONValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JSONValue", ...]
    | list["JSONValue"]
    | Mapping[str, "JSONValue"]
)


def canonicalize(value: object) -> JSONValue:
    """Recursively normalize ``value`` into the immutable ``JSONValue``
    shape a ``Receipt`` stores: every mapping becomes a read-only
    ``MappingProxyType``, every ``list`` or ``tuple`` becomes a
    ``tuple``, and every primitive is returned unchanged once confirmed
    JSON-safe.

    Deliberately does not accept an arbitrary ``Iterable`` (a ``set``, a
    generator, a ``dict_keys`` view, ...): a ``set``'s iteration order
    is not guaranteed to reproduce its insertion order, which would
    silently break the determinism `request_fingerprint` depends on.

    Raises `TypeError` for anything with no JSON representation at all
    (a ``set``, a raw AX element, a custom object, ...) or for a mapping
    key that is not already a `str` (silently coercing, say, the int
    ``1`` and the str ``"1"`` to the same key would collapse two
    distinct entries into one and lose data), and `ValueError` for a
    non-finite `float` (``nan``, ``inf``, ``-inf`` all round-trip
    through no JSON encoder). Either way this fails at
    receipt-construction time instead of silently accepting a value
    that only breaks much later, the first time something tries to
    serialize or fingerprint it.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"{value!r} is not JSON-safe: NaN and infinities have no "
                "JSON representation"
            )
        return value
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(
                    f"Mapping keys must be str, not {type(key).__name__}: {key!r} "
                    "-- coercing it could silently collide with an existing or "
                    "future str key"
                )
        return MappingProxyType(
            {key: canonicalize(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(canonicalize(item) for item in value)
    raise TypeError(f"{type(value).__name__} is not JSON-safe: {value!r}")


def _plain(value: JSONValue) -> JSONValue:
    """The inverse of the array/object half of `canonicalize`: turn every
    `Mapping` into a plain `dict` and every `tuple` into a plain `list`,
    recursively.

    A canonical `JSONValue` (as `Receipt` stores it) is not itself safe
    to hand to `json.dumps` -- `MappingProxyType` is not one of the
    types the stdlib encoder knows -- so `Receipt.to_json()` and
    `canonical_json` both run every value through this first.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def canonical_json(value: JSONValue) -> str:
    """A deterministic JSON string for ``value``.

    Every mapping is serialized with sorted keys at every nesting depth,
    so two logically identical structures built with different key
    insertion order -- different keyword-argument order at a call site,
    a nested dict populated in a different sequence -- always produce
    this same string, and therefore the same `request_fingerprint`.
    """
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_fingerprint(request: Mapping[str, JSONValue]) -> str:
    """A short, stable digest identifying one canonical request.

    ``Operations`` uses this to detect a caller reusing a ``once`` token
    with a request that does not match the one already on record for it
    -- "same token plus another canonical request fails" -- without
    keeping every past request payload around just for comparison.
    """
    digest = hashlib.sha256(canonical_json(request).encode("utf-8"))
    return digest.hexdigest()


# --- receipt vocabulary ------------------------------------------------------


class Outcome(StrEnum):
    """What a ``mac.do`` operation ultimately produced.

    ``PLANNED`` is dry-run only: the operation validated, resolved, and
    (for ``run``) compiled its script, but never dispatched anything and
    never touched the ``once``-token ledger. ``ALREADY`` is the
    convergent-operation fast path: ``set``/``toggle`` found the target
    already in the requested state and returned without mutating
    anything. ``DONE`` is a real, dispatched mutation. ``FAILED`` means
    the operation could not complete; the receipt's ``error`` carries
    why.
    """

    PLANNED = "planned"
    DONE = "done"
    ALREADY = "already"
    FAILED = "failed"


class Acted(StrEnum):
    """Whether a ``mac.do`` operation actually dispatched a mutating call.

    ``NO`` covers both a dry run and the ``ALREADY``-satisfied
    convergent fast path -- nothing was ever dispatched. ``YES`` means a
    mutating call was dispatched, whether or not it was later confirmed
    by a postcondition. ``UNKNOWN`` is reserved for the one case where
    dispatch may or may not have happened and there is no safe way to
    tell: an interrupted or still in-progress ``once``-token reservation
    revisited before its real outcome was ever recorded.
    """

    NO = "no"
    YES = "yes"
    UNKNOWN = "unknown"


class Executor(StrEnum):
    """Which underlying mechanism actually carried out one operation.

    Distinct from a `Receipt`'s ``backend`` -- the caller's configured
    ``MacOS(backend=...)`` choice (``python``/``native``/``auto``) --
    this is what *actually* ran for this one receipt: ``python`` or
    ``native`` for an Accessibility call routed in-process or through
    the native agent, ``input`` for synthetic keyboard/pointer events
    posted with Core Graphics, and ``script`` for an AppleScript
    dispatched through ``osascript``.
    """

    PYTHON = "python"
    NATIVE = "native"
    INPUT = "input"
    SCRIPT = "script"


class ErrorPayload(TypedDict):
    """The JSON-safe ``{"code", "message", "details"}`` shape produced by
    ``MacOSError.to_json()`` in ``errors.py``.

    Carried on a failed `Receipt` so a stored receipt alone -- without
    keeping the original exception object alive -- is enough to
    reconstruct an equivalent `OperationError` later; see
    `OperationError.from_receipt`.
    """

    code: str
    message: str
    details: Mapping[str, JSONValue]


# --- postconditions -----------------------------------------------------


def _freeze_apps(
    apps: str | int | Iterable[str | int] | None,
) -> str | int | tuple[str | int, ...] | None:
    if apps is None or isinstance(apps, (str, int)):
        return apps
    return tuple(apps)


def _validate_postcondition(
    *,
    app: str | int | None,
    all_apps: bool,
    apps: str | int | tuple[str | int, ...] | None,
    direction: str,
    timeout: float | None,
    interval: float,
    allow_zero_timeout: bool = True,
) -> None:
    scoped = sum((app is not None, all_apps, apps is not None))
    if scoped > 1:
        raise MacOSError(
            "Postcondition scope: pass at most one of app, all_apps=True, "
            "or apps -- leave all three unset to inherit the operation's "
            "own scope",
            code=ErrorCode.BAD_REQUEST,
        )
    if direction.casefold() not in {"next", "previous"}:
        raise MacOSError(
            f"Postcondition direction must be 'next' or 'previous', not {direction!r}",
            code=ErrorCode.BAD_REQUEST,
        )
    if timeout is not None and not math.isfinite(timeout):
        raise MacOSError(
            "Postcondition timeout must be finite", code=ErrorCode.BAD_REQUEST
        )
    if timeout is not None and timeout < 0:
        raise MacOSError(
            "Postcondition timeout must not be negative", code=ErrorCode.BAD_REQUEST
        )
    if timeout == 0 and not allow_zero_timeout:
        raise MacOSError(
            "Gone's postcondition needs two consecutive empty polls to "
            "confirm absence, and a zero timeout can never allow the "
            "second one -- pass a positive timeout, or omit it to "
            "inherit the operation's own deadline",
            code=ErrorCode.BAD_REQUEST,
        )
    if not math.isfinite(interval):
        raise MacOSError(
            "Postcondition interval must be finite", code=ErrorCode.BAD_REQUEST
        )
    if interval <= 0:
        raise MacOSError(
            "Postcondition interval must be positive", code=ErrorCode.BAD_REQUEST
        )


@dataclass(frozen=True, slots=True)
class _PostconditionBase:
    """Fields and construction-time validation shared by `Present` and
    `Gone`: scope (`app`/`apps`/`all_apps`), the search itself
    (`text`/`role`/`search_key`/`visible_only`/`direction`/
    `immediate_descendants_only`), and the two knobs (`timeout`/
    `interval`) governing how it is verified.

    `apps` accepts any `Iterable` -- a `list`, a generator, an
    already-frozen `tuple` -- and is normalized to a `tuple` in
    `_validate` regardless of whether the concrete subclass was built
    directly or through the `present`/`gone` factory functions, so a
    constructed `Present`/`Gone` is always genuinely immutable, not
    just immutable by convention.
    """

    text: str | None = None
    role: str | None = None
    search_key: str | None = None
    app: str | int | None = None
    apps: str | int | Iterable[str | int] | None = None
    all_apps: bool = False
    visible_only: bool = True
    direction: str = "next"
    immediate_descendants_only: bool = False
    #: `None` inherits whatever remains of the operation's own single
    #: monotonic deadline. A finite value can only further shorten that
    #: wait, never extend past it -- one deadline still covers
    #: resolution, action, and verification together.
    timeout: float | None = None
    interval: float = 0.1

    def _validate(self, *, allow_zero_timeout: bool) -> None:
        object.__setattr__(self, "apps", _freeze_apps(self.apps))
        _validate_postcondition(
            app=self.app,
            all_apps=self.all_apps,
            apps=self.apps,
            direction=self.direction,
            timeout=self.timeout,
            interval=self.interval,
            allow_zero_timeout=allow_zero_timeout,
        )


@dataclass(frozen=True, slots=True)
class Present(_PostconditionBase):
    """A postcondition satisfied once exactly one AX match appears.

    Verified with `MacOS.ax.wait`'s own one-match,
    fail-closed-on-ambiguity semantics. Any scope left unset (`app`,
    `apps`, `all_apps`) inherits the enclosing operation's own scope
    instead of searching every running app; at most one of the three
    may be set explicitly. A zero `timeout` stays valid here -- unlike
    `Gone`, this only ever needs one look.
    """

    def __post_init__(self) -> None:
        self._validate(allow_zero_timeout=True)


@dataclass(frozen=True, slots=True)
class Gone(_PostconditionBase):
    """A postcondition satisfied once an AX match is absent for two
    consecutive polls, mirroring `MacOS.ax.wait_gone`.

    Field-for-field identical to `Present`; see its docstring for scope
    inheritance, deadline behavior, and how `apps` is normalized.

    A zero `timeout` is rejected at construction: confirming absence
    needs two consecutive empty polls, and a zero-length deadline can
    never let the second one happen. `Present(timeout=0)` stays valid
    -- it only ever needs one look.
    """

    def __post_init__(self) -> None:
        self._validate(allow_zero_timeout=False)


#: What a caller hands `Operations` to confirm a mutation's effect, not
#: just that the underlying call returned without raising.
Postcondition: TypeAlias = Present | Gone


def present(
    text: str | None = None,
    *,
    role: str | None = None,
    search_key: str | None = None,
    app: str | int | None = None,
    apps: str | int | Iterable[str | int] | None = None,
    all_apps: bool = False,
    visible_only: bool = True,
    direction: str = "next",
    immediate_descendants_only: bool = False,
    timeout: float | None = None,
    interval: float = 0.1,
) -> Present:
    """Build a `Present` postcondition; mirrors `MacOS.ax.wait`'s call shape."""
    return Present(
        text=text,
        role=role,
        search_key=search_key,
        app=app,
        apps=apps,
        all_apps=all_apps,
        visible_only=visible_only,
        direction=direction,
        immediate_descendants_only=immediate_descendants_only,
        timeout=timeout,
        interval=interval,
    )


def gone(
    text: str | None = None,
    *,
    role: str | None = None,
    search_key: str | None = None,
    app: str | int | None = None,
    apps: str | int | Iterable[str | int] | None = None,
    all_apps: bool = False,
    visible_only: bool = True,
    direction: str = "next",
    immediate_descendants_only: bool = False,
    timeout: float | None = None,
    interval: float = 0.1,
) -> Gone:
    """Build a `Gone` postcondition; mirrors `MacOS.ax.wait_gone`'s call shape."""
    return Gone(
        text=text,
        role=role,
        search_key=search_key,
        app=app,
        apps=apps,
        all_apps=all_apps,
        visible_only=visible_only,
        direction=direction,
        immediate_descendants_only=immediate_descendants_only,
        timeout=timeout,
        interval=interval,
    )


# --- receipt -----------------------------------------------------------


def _freeze_error(error: ErrorPayload | None) -> Mapping[str, JSONValue] | None:
    """Recursively freeze ``error`` the same way `canonicalize` freezes
    `Receipt.request`/`target`/`observed`: the whole structure --
    including the top-level ``code`` and ``message``, not just
    ``details`` -- becomes a read-only, JSON-safe `Mapping`, so a
    failed `Receipt` is genuinely immutable, not just immutable one
    level deep.
    """
    if error is None:
        return None
    if not isinstance(error["details"], Mapping):  # pragma: no cover - defensive
        raise TypeError(
            f"ErrorPayload details must be a mapping, got {error['details']!r}"
        )
    return canonicalize(error)


@dataclass(frozen=True, slots=True, kw_only=True)
class Receipt:
    """An immutable, JSON-safe record of exactly one ``mac.do`` operation:
    what was asked, what actually happened, and how thoroughly that was
    confirmed.

    Every field is required except the five that only make sense
    sometimes (`target`, `observed`, `once`, `replayed`, `error`), which
    default to "nothing here". `request`/`target`/`observed`/
    `error` are canonicalized (see `canonicalize`) in
    `__post_init__` regardless of what was passed in, so constructing a
    `Receipt` from plain `dict`/`list` values is always safe and the
    result is always genuinely immutable -- not merely immutable by
    caller convention.

    Fields:
        op: The `mac.do` verb this receipt is for (``"press"``,
            ``"set"``, ``"toggle"``, ``"run"``, ``"key"``, ``"recall"``).
        outcome: What ultimately happened; see `Outcome`.
        acted: Whether a mutating call was actually dispatched; see `Acted`.
        backend: The configured `MacOS(backend=...)` choice in effect
            (``"python"``, ``"native"``, or ``"auto"``).
        executor: Which mechanism actually carried this operation out;
            see `Executor`.
        request: The normalized, canonical request this operation was
            given.
        target: A normalized description of what was targeted (an
            app/element descriptor, a plain value, or `None` when not
            applicable, e.g. for `run`).
        observed: A normalized description of what was observed while
            confirming this operation's effect, or `None` when nothing
            was observed.
        changed: Whether this operation actually changed anything --
            `False` on the `Outcome.ALREADY`-satisfied convergent fast
            path (`set`/`toggle`) or any predispatch failure, the real
            before/after comparison once `set`/`toggle` has read both
            (`True` or `False`), and `None` whenever a dispatch (or its
            own readback) is ambiguous -- `press`/`key` with no
            postcondition, one that never verified, or a `set`/`toggle`
            dispatch/readback failure -- rather than a confident but
            unconfirmed guess.
        verified: Whether a postcondition was checked and confirmed the
            intended effect actually took hold.
        duration_s: Wall-clock seconds this operation spent, start to finish.
        once: The idempotency token this operation was dispatched under,
            if any.
        replayed: Whether this exact receipt was returned from the
            `once`-token ledger without dispatching anything new, rather
            than freshly produced.
        error: The structured error this operation failed with, when
            `outcome` is `Outcome.FAILED`.
    """

    op: str
    outcome: Outcome
    acted: Acted
    backend: str
    executor: Executor
    request: Mapping[str, JSONValue]
    target: JSONValue = None
    observed: JSONValue = None
    changed: bool | None
    verified: bool
    duration_s: float
    once: str | None = None
    replayed: bool = False
    error: Mapping[str, JSONValue] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request", canonicalize(self.request))
        object.__setattr__(self, "target", canonicalize(self.target))
        object.__setattr__(self, "observed", canonicalize(self.observed))
        object.__setattr__(self, "error", _freeze_error(self.error))

    def replayed_as(self) -> Self:
        """Return a copy of this receipt stamped as a replay.

        The one path a ``once``-token cache hit needs: return the exact
        receipt already on record for a finished operation, without
        dispatching anything new, so a caller cannot tell a replay from
        a fresh result except by this flag. `Receipt` is frozen, so
        `dataclasses.replace` is the only way to derive one receipt
        from another.
        """
        return dataclasses.replace(self, replayed=True)

    def to_json(self) -> dict[str, JSONValue]:
        """A JSON-safe snapshot of this receipt.

        `Outcome`/`Acted`/`Executor` become their plain string `.value`,
        and `request`/`target`/`observed`/`error` become plain
        `dict`/`list`, recursively (undoing `canonicalize`'s
        `MappingProxyType`/`tuple` wrapping) -- safe to `json.dumps`
        directly, including a failed receipt's structured `error`.
        """
        return {
            "op": self.op,
            "outcome": self.outcome.value,
            "acted": self.acted.value,
            "backend": self.backend,
            "executor": self.executor.value,
            "request": _plain(self.request),
            "target": _plain(self.target),
            "observed": _plain(self.observed),
            "changed": self.changed,
            "verified": self.verified,
            "duration_s": self.duration_s,
            "once": self.once,
            "replayed": self.replayed,
            "error": _plain(self.error),
        }


class OperationError(MacOSError):
    """Raised when a ``mac.do`` operation was actually attempted --
    resolution, dispatch, or verification began -- and failed.

    Always carries the one `Receipt` describing exactly what was
    attempted, what actually happened, and how far it got -- everything
    a caller needs to decide whether it is safe to retry, and with what
    ``once`` token. Distinct from the plain `MacOSError` a call raises
    for a failure caught before any of that ever began -- bad argument
    shape, the fork-safety owner-pid check, an invalidly-shaped
    deadline, or a ``once`` token already recorded for a different
    request -- none of which ever produces a `Receipt`, since nothing
    was dispatched or even resolved yet.
    """

    def __init__(
        self,
        message: str,
        *,
        receipt: Receipt,
        code: str | ErrorCode | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.receipt = receipt

    @classmethod
    def from_receipt(cls, receipt: Receipt) -> Self:
        """Reconstruct the `OperationError` a failed receipt originally
        raised, purely from the receipt itself.

        The one path a caller needs when replaying a stored
        `Outcome.FAILED` receipt without dispatching anything new: the
        original exception object is long gone, but its
        `code`/`message`/`details` survive intact in `receipt.error`, so
        this always reproduces an equivalent exception -- never a
        generic, less useful one.

        `details` is converted back through `_plain` before it reaches
        `MacOSError.__init__`, so the reconstructed exception's
        `to_json()` -- like `Receipt.to_json()` -- is always safe to
        `json.dumps` directly, and `exc.details` ends up an ordinary,
        mutable `dict`/`list` copy, never the same frozen object
        `receipt.error` still holds.
        """
        if receipt.error is None:
            raise ValueError(
                f"Receipt has no structured error to reconstruct from: {receipt!r}"
            )
        return cls(
            receipt.error["message"],
            receipt=receipt,
            code=receipt.error["code"],
            details=_plain(receipt.error["details"]),
        )
