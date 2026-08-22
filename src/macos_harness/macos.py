"""Direct macOS control through public ApplicationServices APIs.

No Codex or OpenAI Computer Use runtime is used here. Accessibility supplies
the semantic tree/actions; Core Graphics supplies raw input; the system
``screencapture`` executable captures a specific window.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from PIL import Image, ImageDraw

from .errors import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    ErrorCode,
    FocusChangedError,
    MacOSError,
)
from .overlay import LivePointerOverlay
from .pointer import POINTER_HOTSPOT, pointer_points

if TYPE_CHECKING:
    # Only for annotations -- `native.py` imports from this module at
    # runtime, so importing it back here for real would be circular.
    # `from __future__ import annotations` (above) already makes every
    # annotation in this file a deferred string, so this import never
    # needs to run outside a type checker.
    from .native import NativeClient

try:
    import ApplicationServices as AS
    from AppKit import NSRunningApplication, NSWorkspace
except ImportError as exc:  # pragma: no cover - exercised on non-macOS hosts
    AS = None  # type: ignore[assignment]
    NSRunningApplication = None  # type: ignore[assignment]
    NSWorkspace = None  # type: ignore[assignment]
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None


_AX_SUCCESS = 0
_AX_ATTRIBUTES = (
    "AXRole",
    "AXSubrole",
    "AXRoleDescription",
    "AXTitle",
    "AXDescription",
    "AXHelp",
    "AXIdentifier",
    "AXDOMIdentifier",
    "AXURL",
    "AXValue",
    "AXPlaceholderValue",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXHidden",
    "AXPosition",
    "AXSize",
    "AXFrame",
    "AXChildren",
    "AXWindows",
)
_AX_SAFE_ATTRIBUTES = (
    "AXRole",
    "AXTitle",
    "AXDescription",
    "AXPlaceholderValue",
    "AXHelp",
    "AXIdentifier",
    "AXDOMIdentifier",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXHidden",
    "AXFrame",
)
_AX_CROSS_APP_MESSAGING_TIMEOUT = 0.5
_AX_NODE_MAPPING = {
    "AXSubrole": "subrole",
    "AXRoleDescription": "role_description",
    "AXTitle": "title",
    "AXDescription": "description",
    "AXHelp": "help",
    "AXIdentifier": "identifier",
    "AXDOMIdentifier": "dom_identifier",
    "AXURL": "url",
    "AXValue": "value",
    "AXPlaceholderValue": "placeholder",
    "AXEnabled": "enabled",
    "AXFocused": "focused",
    "AXSelected": "selected",
    "AXHidden": "hidden",
    "AXPosition": "position",
    "AXSize": "size",
    "AXFrame": "frame",
}
_SETTABLE_CANDIDATES = ("AXValue", "AXFocused", "AXSelected")
_ACTION_ALIASES = {
    "press": "AXPress",
    "show menu": "AXShowMenu",
    "confirm": "AXConfirm",
    "cancel": "AXCancel",
    "increment": "AXIncrement",
    "decrement": "AXDecrement",
    "raise": "AXRaise",
}
_BUTTONS = {
    "left": (AS.kCGMouseButtonLeft if AS else 0),
    "right": (AS.kCGMouseButtonRight if AS else 1),
    "middle": (AS.kCGMouseButtonCenter if AS else 2),
}
_MOUSE_EVENTS = {
    "left": (
        AS.kCGEventLeftMouseDown if AS else 1,
        AS.kCGEventLeftMouseUp if AS else 2,
        AS.kCGEventLeftMouseDragged if AS else 6,
    ),
    "right": (
        AS.kCGEventRightMouseDown if AS else 3,
        AS.kCGEventRightMouseUp if AS else 4,
        AS.kCGEventRightMouseDragged if AS else 7,
    ),
    "middle": (
        AS.kCGEventOtherMouseDown if AS else 25,
        AS.kCGEventOtherMouseUp if AS else 26,
        AS.kCGEventOtherMouseDragged if AS else 27,
    ),
}
_MODIFIER_FLAGS = {
    "cmd": AS.kCGEventFlagMaskCommand if AS else 0,
    "command": AS.kCGEventFlagMaskCommand if AS else 0,
    "super": AS.kCGEventFlagMaskCommand if AS else 0,
    "ctrl": AS.kCGEventFlagMaskControl if AS else 0,
    "control": AS.kCGEventFlagMaskControl if AS else 0,
    "alt": AS.kCGEventFlagMaskAlternate if AS else 0,
    "option": AS.kCGEventFlagMaskAlternate if AS else 0,
    "shift": AS.kCGEventFlagMaskShift if AS else 0,
}
_MODIFIER_KEYCODES = {
    "cmd": 55,
    "command": 55,
    "super": 55,
    "shift": 56,
    "alt": 58,
    "option": 58,
    "ctrl": 59,
    "control": 59,
}
_AX_SEARCH_ROLES = {
    f"AX{name}SearchKey": f"AX{name}"
    for name in (
        "Button",
        "CheckBox",
        "ComboBox",
        "Image",
        "Link",
        "List",
        "Menu",
        "MenuItem",
        "RadioButton",
        "StaticText",
        "Table",
        "TextArea",
        "TextField",
    )
}
_KEYCODES = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "enter": 36,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "tab": 48,
    "space": 49,
    "`": 50,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "esc": 53,
    "home": 115,
    "pageup": 116,
    "page_up": 116,
    "forward_delete": 117,
    "end": 119,
    "pagedown": 121,
    "page_down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}
_SHIFTED_CHARACTERS = dict(
    zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./", strict=True)
)


def _parse_key(key: str) -> tuple[int, tuple[tuple[int, int], ...]]:
    parts = [part.casefold() for part in re.split(r"[+-]", key) if part]
    if not parts:
        raise MacOSError(
            "Key must not be empty",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "key"},
        )
    base = parts[-1]
    try:
        keycode = _KEYCODES[base]
    except KeyError as exc:
        raise MacOSError(
            f"Unsupported key {base!r}; use mac.type() for arbitrary text",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "key", "value": base},
        ) from exc

    parsed_modifiers: list[tuple[int, int]] = []
    seen_keycodes: set[int] = set()
    for modifier in parts[:-1]:
        try:
            modifier_keycode = _MODIFIER_KEYCODES[modifier]
            modifier_flag = _MODIFIER_FLAGS[modifier]
        except KeyError as exc:
            raise MacOSError(
                f"Unsupported modifier {modifier!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "modifier", "value": modifier},
            ) from exc
        if modifier_keycode not in seen_keycodes:
            seen_keycodes.add(modifier_keycode)
            parsed_modifiers.append((modifier_keycode, modifier_flag))
    return keycode, tuple(parsed_modifiers)


def _require_macos() -> None:
    if AS is None or NSWorkspace is None:
        raise MacOSError(
            "macOS ApplicationServices bindings are unavailable. Run on macOS "
            "after installing project dependencies with `uv sync`.",
            code=ErrorCode.AX_ERROR,
        ) from _IMPORT_ERROR


def _truncate(value: str, limit: int = 160) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise MacOSError(
            f"Screenshot is not a PNG: {path}",
            code=ErrorCode.AX_ERROR,
            details={"path": str(path)},
        )
    return struct.unpack(">II", header[16:24])


def _ax_error(operation: str, error: int, **details: object) -> MacOSError:
    return MacOSError(
        f"{operation} failed with AXError {error}",
        code=ErrorCode.AX_ERROR,
        details={"ax_error": int(error), "operation": operation, **details},
    )


_BACKENDS = ("python", "native", "auto")


def _resolve_backend(backend: str | None) -> str:
    """Resolve python/native/auto from an explicit value or MACOS_HARNESS_BACKEND."""
    if backend is None:
        backend = os.environ.get("MACOS_HARNESS_BACKEND", "python")
    normalized = str(backend).strip().casefold() or "python"
    if normalized not in _BACKENDS:
        raise MacOSError(
            f"Unknown backend {normalized!r}; choose one of: {', '.join(_BACKENDS)}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "backend", "value": normalized},
        )
    return normalized


def _split_scroll_delta(delta: int, maximum: int) -> list[int]:
    """Split a wheel delta into small exact steps accepted reliably by apps."""
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    remaining = int(delta)
    steps: list[int] = []
    while remaining:
        step = max(-maximum, min(maximum, remaining))
        steps.append(step)
        remaining -= step
    return steps or [0]


class _Unresolved:
    """Sentinel type for ``MacOS._resolved_native_client``: its one
    instance below marks "no cached client, error, or fallback yet" --
    distinct from an ``auto`` backend's already-resolved ``None``.
    """

    __slots__ = ()


#: The single ``_Unresolved`` instance ever constructed.
_UNRESOLVED = _Unresolved()


def _finalize_native_session(session_box: list[Any | None]) -> None:
    """Finalizer callback for one ``MacOS`` instance's native agent child.

    Deliberately a free function taking only a plain mutable box, never a
    bound method or closure over the owning ``MacOS`` instance itself:
    ``MacOS`` and ``Accessibility`` hold references to each other
    (``self.ax = Accessibility(self)``), and a ``weakref.finalize``
    callback that captures its own target, even indirectly, keeps that
    target permanently unreachable-but-never-collectable. ``MacOS.close()``
    calls this same finalizer directly (idempotent: a finalizer only ever
    fires once), and it also runs automatically once the owning instance
    is unreachable or the interpreter exits, even if ``close()`` was
    never called at all.
    """
    session, session_box[0] = session_box[0], None
    if session is not None:
        session.close()


#: Every ``MacOS`` instance that might still hold a live native-agent
#: finalizer, tracked weakly so this registry never keeps an instance (or
#: anything it in turn keeps alive) reachable a moment longer than it
#: otherwise would be. Exists solely so a ``fork()`` elsewhere in the
#: embedding process can disarm every copied instance's finalizer before
#: any of the child's own code resumes -- see
#: ``_disarm_inherited_native_state_after_fork`` below.
_live_macos_instances: weakref.WeakSet[MacOS] = weakref.WeakSet()


def _disarm_inherited_native_state_after_fork() -> None:
    """Disarm every live ``MacOS`` instance's copied native-session
    finalizer in a freshly forked child, before any of the child's own
    code resumes.

    Registered once, process-wide, via ``os.register_at_fork`` below.
    Runs with exactly one thread alive (the thread that called
    ``fork()``), so it deliberately never acquires any instance's own
    ``_native_lock`` -- some *other* thread could have held it at the
    exact moment of ``fork()``, and that thread does not exist in this
    child at all. ``weakref.finalize.detach()`` marks a finalizer dead
    without ever invoking its callback: the ``AgentSession`` this child
    inherited a copy of is never closed, signaled, or otherwise touched
    from here -- it is the *parent* process's child to tear down, on the
    parent's own schedule. ``MacOS._check_native_owner`` independently
    fails closed if this child's own code goes on to use the inherited
    native state anyway (``close()``, ``_acquire_native()``), and
    ``native.py``'s own ``os.register_at_fork`` hook independently slams
    shut every live ``NativeClient``'s raw socket the same way.
    """
    for instance in list(_live_macos_instances):
        instance._native_finalizer.detach()


if hasattr(os, "register_at_fork"):  # pragma: no branch - always true on macOS
    os.register_at_fork(after_in_child=_disarm_inherited_native_state_after_fork)


class MacOS:
    """Low-level macOS observation and control for one persistent process."""

    def __init__(self, *, backend: str | None = None) -> None:
        _require_macos()
        self._elements: dict[int, Any] = {}
        self._element_seq = 0
        self._last_app: dict[str, Any] | None = None
        self._last_windows: list[dict[str, Any]] = []
        self._last_screenshot: dict[str, Any] | None = None
        self._event_source = AS.CGEventSourceCreate(AS.kCGEventSourceStatePrivate)
        if self._event_source is None:
            raise MacOSError(
                "Could not create a private Core Graphics event source",
                code=ErrorCode.AX_ERROR,
            )

        from .controls import Accessibility
        from .ops import Operations

        self._pointer_position: tuple[float, float] | None = None
        self._overlay = LivePointerOverlay()
        self.ax = Accessibility(self)
        self.do = Operations(self)
        self._backend = _resolve_backend(backend)
        self._native_client: NativeClient | None = None
        self._native_error: Exception | None = None
        self._native_closed = False
        self._native_lock = threading.Lock()
        # A plain mutable box, not a `self._native_session` attribute: the
        # finalizer below must never capture `self` (see
        # `_finalize_native_session`), so it is handed this box instead,
        # and `close()`/`_acquire_native()` mutate its one slot in place.
        self._native_session_box: list[Any | None] = [None]
        self._native_finalizer = weakref.finalize(
            self, _finalize_native_session, self._native_session_box
        )
        # See `_check_native_owner` below: a forked child inherits a
        # byte-for-byte copy of this instance, including `_native_lock`,
        # without ever going through `__init__` again -- recorded here so
        # every later native-session entry point can tell.
        self._creator_pid = os.getpid()
        _live_macos_instances.add(self)

    def _check_native_owner(self) -> None:
        """Fail closed before touching ``_native_lock`` from a forked child.

        Called first thing by every method that acquires ``_native_lock``
        (``close()``, ``_resolved_native_client()`` on behalf of
        ``_acquire_native()``) -- never from inside the ``with
        self._native_lock:`` block itself. If some other thread of the
        parent process held that lock at the exact instant of a
        ``fork()`` elsewhere in the embedding application, it stays held
        forever in a forked child (that thread does not exist here to
        release it); checking ownership before ever attempting to
        acquire it turns what would otherwise be a silent, permanent
        hang into this immediate, explicit error instead.
        """
        pid = os.getpid()
        if pid != self._creator_pid:
            raise MacOSError(
                f"This MacOS instance was created in pid {self._creator_pid} "
                f"and cannot be used from pid {pid} (a fork boundary was "
                "crossed); construct a fresh MacOS in this process instead",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"creator_pid": self._creator_pid, "pid": pid},
            )

    def close(self) -> None:
        """Tear down this instance's private native agent child, if any.

        Idempotent: safe to call more than once, and safe when no native
        session was ever launched (the common, default ``python`` backend
        never opens a socket at all). Once closed, any further attempt to
        route a call through the native agent raises explicitly rather
        than silently relaunching a new child or falling back to another
        backend — construct a new ``MacOS`` to use ``native``/``auto``
        again. Also runs automatically, via the same finalizer this calls
        directly, if this instance becomes unreachable or the interpreter
        exits without an explicit ``close()``.

        Raises if called on a forked child's inherited copy of this
        instance (see ``_check_native_owner``); the automatic finalizer
        path a fork alone triggers is never affected by this raise, since
        ``_disarm_inherited_native_state_after_fork`` (module level, see
        above) already detaches every live instance's finalizer in the
        child before any of its own code -- including this method -- ever
        runs.
        """
        self._check_native_owner()
        with self._native_lock:
            self._native_closed = True
            self._native_client = None
        self._native_finalizer()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- permissions and app discovery ---------------------------------

    def is_accessibility_trusted(self) -> bool:
        return bool(AS.AXIsProcessTrusted())

    def request_accessibility_permission(self) -> bool:
        """Show Apple's Accessibility permission prompt and return current trust."""
        options = {AS.kAXTrustedCheckOptionPrompt: True}
        return bool(AS.AXIsProcessTrustedWithOptions(options))

    @staticmethod
    def _preflight_permission(name: str) -> bool | None:
        function = getattr(AS, name, None)
        return None if function is None else bool(function())

    def permissions(self) -> dict[str, bool | str | None]:
        """Return non-prompting checks for the permissions this harness uses."""
        return {
            "accessibility": self.is_accessibility_trusted(),
            "screen_recording": self._preflight_permission(
                "CGPreflightScreenCaptureAccess"
            ),
            "post_events": self._preflight_permission("CGPreflightPostEventAccess"),
            "automation": "per-target; requested by macOS on first Apple Event",
        }

    def request_permissions(self) -> dict[str, bool | str | None]:
        """Ask macOS for missing global permissions, then return current status."""
        self.request_accessibility_permission()
        for name in ("CGRequestScreenCaptureAccess", "CGRequestPostEventAccess"):
            function = getattr(AS, name, None)
            if function is not None:
                function()
        return self.permissions()

    def doctor(self) -> dict[str, Any]:
        return {
            "platform": "macOS",
            "permissions": self.permissions(),
            "input_monitoring_required": False,
        }

    def list_apps(self) -> list[dict[str, Any]]:
        if self._backend != "python":
            client = self._acquire_native()
            if client is not None:
                return client.list_apps()
        apps: list[dict[str, Any]] = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            info = self._app_info(app)
            if not info["name"]:
                continue
            apps.append(info)
        return sorted(apps, key=lambda item: (item["name"].casefold(), item["pid"]))

    @staticmethod
    def _app_info(app: Any) -> dict[str, Any]:
        name = app.localizedName()
        bundle_id = app.bundleIdentifier()
        path = app.bundleURL().path() if app.bundleURL() is not None else None
        return {
            "name": str(name or bundle_id or path or ""),
            "bundle_id": str(bundle_id) if bundle_id else None,
            "pid": int(app.processIdentifier()),
            "path": str(path) if path else None,
        }

    @classmethod
    def _frontmost_app(cls) -> dict[str, Any] | None:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return None if app is None else cls._app_info(app)

    def _guard_focus(
        self,
        before: dict[str, Any] | None,
        target_pid: int,
        operation: str,
    ) -> None:
        after = self._frontmost_app()
        if (
            before is not None
            and int(before["pid"]) != target_pid
            and after is not None
            and int(after["pid"]) == target_pid
        ):
            raise FocusChangedError(
                f"{after['name']} became frontmost during {operation}; stopped",
                details={
                    "operation": operation,
                    "target_pid": target_pid,
                    "frontmost": after,
                },
            )

    def _resolve_app(self, query: str | int | None) -> tuple[Any, dict[str, Any]]:
        if not query and self._last_app:
            query = self._last_app["pid"]
        if not query:
            raise MacOSError(
                "Specify an app name, bundle ID, path, or PID",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )

        if isinstance(query, int):
            # An exact pid resolves directly against the process table --
            # no need to enumerate every running application to find one
            # match by exact value, and no risk of the notification-cache
            # staleness `NSWorkspace.runningApplications()` can have in a
            # process that never pumps its own run loop (a freshly
            # launched app can otherwise appear to not exist yet, purely
            # as an artifact of when this process last observed a change).
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(query)
            if app is None or app.isTerminated():
                raise ApplicationNotFoundError(
                    f"No running application matches {query!r}",
                    details={"query": query},
                )
            return app, self._app_info(app)

        needle = str(query).casefold()
        candidates: list[tuple[Any, dict[str, Any]]] = []
        exact: list[tuple[Any, dict[str, Any]]] = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            info = self._app_info(app)
            values = [str(info["pid"]), info["name"], info["bundle_id"], info["path"]]
            lowered = [str(value).casefold() for value in values if value]
            if needle in lowered:
                exact.append((app, info))
            elif any(needle in value for value in lowered):
                candidates.append((app, info))

        matches = exact or candidates
        if not matches:
            raise ApplicationNotFoundError(
                f"No running application matches {query!r}",
                details={"query": query},
            )
        if len(matches) > 1:
            names = ", ".join(
                f"{item[1]['name']} ({item[1]['pid']})" for item in matches[:8]
            )
            raise MacOSError(
                f"Application query {query!r} is ambiguous: {names}",
                code=ErrorCode.APP_AMBIGUOUS,
                details={
                    "query": query,
                    "matches": [info for _, info in matches],
                },
            )
        return matches[0]

    # --- AX tree ---------------------------------------------------------

    def _ensure_accessibility(self) -> None:
        if not self.is_accessibility_trusted():
            raise AccessibilityPermissionError(
                "Accessibility permission is required. Grant it to the terminal or "
                "agent host in System Settings → Privacy & Security → Accessibility, "
                "or call mac.request_accessibility_permission() to show Apple's prompt.",
                details={"permission": "accessibility"},
            )

    def _ensure_screen_recording(self) -> None:
        if self._preflight_permission("CGPreflightScreenCaptureAccess") is False:
            raise AccessibilityPermissionError(
                "Screen Recording permission is required. Grant it to the terminal "
                "or agent host in System Settings → Privacy & Security → Screen & "
                "System Audio Recording, or call mac.request_permissions().",
                details={"permission": "screen_recording"},
            )

    def _ensure_post_events(self) -> None:
        if self._preflight_permission("CGPreflightPostEventAccess") is False:
            raise AccessibilityPermissionError(
                "Permission to post input events is required. Grant control access "
                "to the terminal or agent host, or call mac.request_permissions().",
                details={"permission": "post_events"},
            )

    @staticmethod
    def _application_element(
        pid: int,
        *,
        messaging_timeout: float | None = None,
        enhance: bool = True,
    ) -> Any:
        root = AS.AXUIElementCreateApplication(pid)
        if messaging_timeout is not None:
            error = AS.AXUIElementSetMessagingTimeout(root, messaging_timeout)
            if error != _AX_SUCCESS:
                raise _ax_error("Set AX messaging timeout", error, pid=pid)
        if not enhance:
            return root
        error, enhanced = AS.AXUIElementCopyAttributeValue(
            root, "AXEnhancedUserInterface", None
        )
        if error == _AX_SUCCESS and not enhanced:
            AS.AXUIElementSetAttributeValue(root, "AXEnhancedUserInterface", True)
            time.sleep(0.05)
        return root

    @staticmethod
    def _copy_attribute(element: Any, attribute: str) -> Any | None:
        error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == _AX_SUCCESS else None

    @staticmethod
    def _copy_attributes(
        element: Any, attributes: Iterable[str]
    ) -> dict[str, Any | None]:
        """Read AX attributes in one application round trip when supported."""
        names = tuple(dict.fromkeys(str(attribute) for attribute in attributes))
        if not names:
            return {}
        try:
            error, values = AS.AXUIElementCopyMultipleAttributeValues(
                element, names, 0, None
            )
        except (AttributeError, TypeError, ValueError):
            error, values = -1, None
        if error != _AX_SUCCESS or values is None or len(values) != len(names):
            return {name: MacOS._copy_attribute(element, name) for name in names}

        result: dict[str, Any | None] = {}
        for name, value in zip(names, values, strict=True):
            try:
                value_type = AS.AXValueGetType(value)
            except (TypeError, ValueError):
                pass
            else:
                if value_type == AS.kAXValueAXErrorType:
                    value = None
            result[name] = value
        return result

    @staticmethod
    def _actions(element: Any) -> list[str]:
        error, value = AS.AXUIElementCopyActionNames(element, None)
        return [str(item) for item in value] if error == _AX_SUCCESS and value else []

    @staticmethod
    def _settable(element: Any, attribute: str) -> bool:
        error, value = AS.AXUIElementIsAttributeSettable(element, attribute, None)
        return bool(value) if error == _AX_SUCCESS else False

    @staticmethod
    def _is_ax_element(value: Any) -> bool:
        try:
            return AS.CFGetTypeID(value) == AS.AXUIElementGetTypeID()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _finite_float(value: float, *, field: str | None = None) -> float:
        """Coerce ``value`` to a JSON-safe finite ``float``.

        ``AXValueGetValue`` and plain AX attribute reads can hand back a
        NaN or infinity (a mis-measured element geometry, a stale layout
        pass, ...); those have no JSON representation and
        ``Receipt.canonicalize`` raises ``ValueError`` on them well after
        the fact, with no machine-readable code. Reject them here, the
        single place raw AX values become ``dict``/``list``/scalar JSON
        values, so every caller -- ``mac.do``, snapshotting, attribute
        reads -- gets a structured ``MacOSError`` before the value ever
        reaches a ``Receipt``.
        """
        value = float(value)
        if math.isfinite(value):
            return value
        details: dict[str, object] = {"value": str(value)}
        if field is not None:
            details["field"] = field
        raise MacOSError(
            "Accessibility API returned a non-finite value with no JSON "
            "representation",
            code=ErrorCode.AX_ERROR,
            details=details,
        )

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return MacOS._finite_float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (list, tuple)):
            return [
                MacOS._jsonable(item)
                for item in value
                if not MacOS._is_ax_element(item)
            ]
        if isinstance(value, dict):
            return {str(key): MacOS._jsonable(item) for key, item in value.items()}

        try:
            value_type = AS.AXValueGetType(value)
        except (TypeError, ValueError):
            return str(value)

        type_specs = {
            AS.kAXValueCGPointType: ("point", ("x", "y")),
            AS.kAXValueCGSizeType: ("size", ("width", "height")),
            AS.kAXValueCGRectType: ("rect", ("origin", "size")),
            AS.kAXValueCFRangeType: ("range", ("location", "length")),
        }
        spec = type_specs.get(value_type)
        if spec is None:
            return str(value)
        ok, decoded = AS.AXValueGetValue(value, value_type, None)
        if not ok:
            return str(value)
        kind, fields = spec
        if kind == "point":
            return {
                "x": MacOS._finite_float(decoded.x, field="x"),
                "y": MacOS._finite_float(decoded.y, field="y"),
            }
        if kind == "size":
            return {
                "width": MacOS._finite_float(decoded.width, field="width"),
                "height": MacOS._finite_float(decoded.height, field="height"),
            }
        if kind == "rect":
            return {
                "x": MacOS._finite_float(decoded.origin.x, field="x"),
                "y": MacOS._finite_float(decoded.origin.y, field="y"),
                "width": MacOS._finite_float(decoded.size.width, field="width"),
                "height": MacOS._finite_float(decoded.size.height, field="height"),
            }
        return {field: int(getattr(decoded, field)) for field in fields}

    def _snapshot_tree(
        self,
        root: Any,
        *,
        max_depth: int,
        max_nodes: int,
        include_menu_bar: bool,
        attributes: Iterable[str] = _AX_ATTRIBUTES,
        extra_attributes: Iterable[str] = (),
        include_actions: bool = True,
        include_settable: bool = True,
        reset_elements: bool = True,
    ) -> list[dict[str, Any]]:
        if reset_elements:
            self._elements = {}
        nodes: list[dict[str, Any]] = []
        seen: set[int] = set()
        requested_attributes = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in attributes),
                    "AXRole",
                    "AXChildren",
                    "AXWindows",
                    *(str(item) for item in extra_attributes),
                )
            )
        )
        standard_attributes = {
            *(str(item) for item in attributes),
            "AXRole",
            "AXChildren",
            "AXWindows",
        }

        def visit(element: Any, depth: int) -> None:
            if depth > max_depth or len(nodes) >= max_nodes:
                return
            try:
                identity = hash(element)
            except TypeError:
                identity = id(element)
            if identity in seen:
                return
            seen.add(identity)

            raw = self._copy_attributes(element, requested_attributes)
            if raw.get("AXRole") == "AXMenuBar" and not include_menu_bar:
                return
            index = self._remember_element(element)
            node: dict[str, Any] = {
                "element_index": index,
                "depth": depth,
                "role": self._jsonable(raw.get("AXRole")),
            }
            for source, target in _AX_NODE_MAPPING.items():
                value = self._jsonable(raw.get(source))
                if value not in (None, "", [], {}):
                    node[target] = value
            extra = {
                name: self._jsonable(raw[name])
                for name in requested_attributes
                if name not in standard_attributes
                and self._jsonable(raw[name]) not in (None, "", [], {})
            }
            if extra:
                node["attributes"] = extra
            if include_actions:
                actions = self._actions(element)
                if actions:
                    node["actions"] = actions
            if include_settable:
                settable = [
                    name
                    for name in _SETTABLE_CANDIDATES
                    if self._settable(element, name)
                ]
                if settable:
                    node["settable"] = settable
            nodes.append(node)

            children = raw.get("AXChildren")
            if not children and depth == 0:
                children = raw.get("AXWindows")
            if isinstance(children, Iterable) and not isinstance(
                children, (str, bytes, dict)
            ):
                for child in children:
                    if self._is_ax_element(child):
                        visit(child, depth + 1)

        visit(root, 0)
        return nodes

    @staticmethod
    def _render_tree(nodes: list[dict[str, Any]], *, truncated: bool = False) -> str:
        lines: list[str] = []
        for node in nodes:
            parts = [str(node["element_index"]), str(node.get("role") or "AXUnknown")]
            for key in ("subrole", "title", "description", "value"):
                if key in node:
                    encoded = json.dumps(_truncate(str(node[key])), ensure_ascii=False)
                    parts.append(f"{key}={encoded}")
            if "frame" in node:
                frame = node["frame"]
                if isinstance(frame, dict):
                    parts.append(
                        "frame=({x:g},{y:g},{width:g},{height:g})".format(**frame)
                    )
            if node.get("settable"):
                parts.append(f"settable={','.join(node['settable'])}")
            if node.get("actions"):
                actions = ",".join(
                    _truncate(str(action), 80) for action in node["actions"]
                )
                parts.append(f"actions={actions}")
            lines.append("  " * int(node["depth"]) + " ".join(parts))
        if truncated:
            lines.append("… tree truncated by max_nodes or max_depth")
        return "\n".join(lines)

    def get_app_state(
        self,
        app: str,
        *,
        screenshot: bool = False,
        max_depth: int = 25,
        max_nodes: int = 5000,
        window_index: int = 0,
        include_menu_bar: bool = False,
        extra_attributes: Iterable[str] = (),
        include_actions: bool = True,
        include_settable: bool = True,
    ) -> dict[str, Any]:
        self._ensure_accessibility()
        _, info = self._resolve_app(app)
        root = self._application_element(info["pid"])
        nodes = self._snapshot_tree(
            root,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_menu_bar=include_menu_bar,
            extra_attributes=extra_attributes,
            include_actions=include_actions,
            include_settable=include_settable,
        )
        self._last_app = info
        self._last_windows = self.windows(app)
        state: dict[str, Any] = {
            "app": info,
            "nodes": nodes,
            "text": self._render_tree(nodes, truncated=len(nodes) >= max_nodes),
            "windows": self._last_windows,
        }
        if screenshot:
            try:
                self._last_screenshot = self.capture_screenshot(
                    app, window_index=window_index
                )
                state["screenshot"] = self._last_screenshot
            except MacOSError as exc:
                state["screenshot"] = None
                state["screenshot_error"] = str(exc)
        else:
            state["screenshot"] = None
        return state

    snapshot = get_app_state

    # --- AX actions ------------------------------------------------------

    def _element(self, element_index: int) -> Any:
        try:
            return self._elements[int(element_index)]
        except (KeyError, ValueError) as exc:
            raise MacOSError(
                f"Unknown element index {element_index!r}; take a fresh snapshot first",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"element_index": element_index},
            ) from exc

    def _remember_element(self, element: Any) -> int:
        index = self._element_seq
        self._element_seq += 1
        self._elements[index] = element
        return index

    def _local_element(self, element_index: int) -> Any:
        """Resolve element_index to a local AXUIElement; never a native handle."""
        element = self._element(element_index)
        if not self._is_ax_element(element):
            raise MacOSError(
                f"Element {element_index} is a native agent handle; this "
                "operation is local-only and unsupported for native handles",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"element_index": element_index},
            )
        return element

    def _resolved_native_client(self) -> NativeClient | None | _Unresolved:
        """Return the already-resolved client or ``auto`` fallback, or
        ``_UNRESOLVED`` when a fresh ``agent.launch()`` attempt is still
        needed.

        Raises directly for the four states that must never fall through
        to another launch attempt: this call crossed a fork boundary (see
        ``_check_native_owner``, checked first so a forked child's copy
        of ``_native_client`` is never silently handed back to it), this
        instance's ``close()`` already ran, ``backend == "native"``
        already knows the agent is unavailable from an earlier call, or
        ``backend == "auto"`` has a cached failure that is not an
        ``AgentUnavailableError`` (a hard handshake/protocol mismatch
        never becomes fallback-eligible just because a later call asks
        again).
        """
        self._check_native_owner()
        if self._native_client is not None:
            return self._native_client
        if self._native_closed:
            raise MacOSError(
                "This MacOS instance's close() already tore down its "
                f"native agent child; construct a new MacOS to use "
                f"backend={self._backend!r} again",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"backend": self._backend},
            )
        if self._native_error is not None:
            from . import agent

            if self._backend == "auto" and isinstance(
                self._native_error, agent.AgentUnavailableError
            ):
                return None
            raise self._native_error
        return _UNRESOLVED

    def _acquire_native(self) -> NativeClient | None:
        """Lazily launch this instance's own private native agent child.

        Returns the connected, handshake-verified client, or ``None``
        when ``backend == "auto"`` and the agent could not be made
        available at all — an ``AgentUnavailableError`` ``agent.launch()``
        raised before a real handshake response was ever received (no
        override, bundled, or buildable executable; a spawn failure; or
        the child crashing, closing its socket, or never answering
        before its own timeout). ``backend == "native"`` always raises
        instead of returning ``None``.

        Every other native failure — a protocol-version mismatch or an
        ``expected_pid`` identity mismatch discovered by the very
        handshake the freshly spawned child just answered — means a real
        response already came back from *some* process, so it hard-fails
        even under ``auto`` rather than silently falling back to what
        could be a wrong or unexpected agent. Every ``agent.launch()``
        failure is cached here, so repeated calls never re-attempt a
        launch already known to fail; only ``AgentUnavailableError`` is
        ever fallback-eligible under ``auto`` -- a cached hard mismatch
        keeps raising on every later call instead of silently spawning
        another child.

        Concurrent first use from multiple threads on the same instance
        launches exactly one child: the actual launch only ever happens
        while holding ``self._native_lock``, and every caller re-checks
        under that lock in case another thread already resolved (or
        failed) it while this one was waiting.

        Each ``MacOS`` instance launches — and, via ``close()``, tears
        down — its own private child, never a client shared with any
        other instance or process.

        Ownership transfer from a successful ``agent.launch()`` into
        this instance's own storage (``_native_session_box``,
        ``_native_client``) is itself ``BaseException``-safe: a
        ``KeyboardInterrupt`` landing after ``launch()`` has already
        returned a live session but before that storage step finishes
        would otherwise orphan it -- a real child process this instance
        never records anywhere, so nothing (not even ``close()``) would
        ever reap it. Any such interruption instead clears whatever
        partial state was written, closes that exact session, and
        re-raises unchanged.
        """
        resolved = self._resolved_native_client()
        if resolved is not _UNRESOLVED:
            return resolved
        from . import agent

        with self._native_lock:
            resolved = self._resolved_native_client()
            if resolved is not _UNRESOLVED:
                return resolved
            try:
                session = agent.launch()
            except Exception as exc:
                self._native_error = exc
                if self._backend == "auto" and isinstance(
                    exc, agent.AgentUnavailableError
                ):
                    return None
                raise
            try:
                self._native_session_box[0] = session
                self._native_client = session.client
            except BaseException:
                self._native_session_box[0] = None
                self._native_client = None
                session.close()
                raise
            return self._native_client

    def _intern_native_match(self, raw: dict[str, Any], client: Any) -> dict[str, Any]:
        """Turn one wire match descriptor into a client-side element_index."""
        from .native import _NativeHandle

        match = dict(raw)
        handle = match.pop("handle")
        sentinel = _NativeHandle(client, handle, client.generation)
        match["element_index"] = self._remember_element(sentinel)
        return match

    def _native_query(
        self,
        client: Any,
        *,
        pid: int,
        search_key: str,
        text: str | None,
        visible_only: bool,
        limit: int,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        include_actions: bool,
        max_nodes: int,
        reset_elements: bool,
        messaging_timeout: float | None,
        enhance: bool,
    ) -> list[dict[str, Any]]:
        if reset_elements:
            self._elements = {}
        params = {
            "app_pid": pid,
            "search_key": search_key,
            "text": text,
            "visible_only": bool(visible_only),
            "limit": int(limit),
            "direction": direction,
            "immediate_descendants_only": bool(immediate_descendants_only),
            "attributes": [str(item) for item in attributes],
            "include_actions": bool(include_actions),
            "max_nodes": int(max_nodes),
            "reset_elements": bool(reset_elements),
            "messaging_timeout": messaging_timeout,
            "enhance": bool(enhance),
        }
        return [self._intern_native_match(raw, client) for raw in client.query(params)]

    def _native_press(
        self,
        client: Any,
        *,
        pid: int,
        search_key: str,
        text: str | None,
        visible_only: bool,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        max_nodes: int,
        timeout: float,
        interval: float,
    ) -> dict[str, Any]:
        """Press via the agent, retrying only a not-yet-unique match.

        Mirrors ``ax_wait``'s own deadline/retry shape: ``element.unknown``
        is the one outcome ``PressCoordinator`` (agent-side) guarantees
        happens before any ``AXPress`` is ever dispatched, so it is the
        only failure retried here, from one monotonic deadline. Every
        other code -- ``focus.changed`` (the press already happened),
        ``permission.accessibility``, or any transport/protocol failure
        whose relation to dispatch is unknown -- propagates immediately:
        retrying those could fire ``AXPress`` a second time.
        """
        self._elements = {}
        params = {
            "app_pid": pid,
            "search_key": search_key,
            "text": text,
            "visible_only": bool(visible_only),
            "limit": 2,
            "direction": direction,
            "immediate_descendants_only": bool(immediate_descendants_only),
            "attributes": [str(item) for item in attributes],
            "include_actions": True,
            "max_nodes": int(max_nodes),
            "reset_elements": True,
            "messaging_timeout": None,
            "enhance": True,
        }
        deadline = time.monotonic() + timeout
        while True:
            try:
                match = client.press(params)
            except MacOSError as exc:
                if exc.code != ErrorCode.ELEMENT_UNKNOWN.value:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MacOSError(
                        "AX press timed out without a unique match",
                        code=ErrorCode.TIMEOUT,
                        details={"timeout": timeout, "pid": pid},
                    ) from exc
                time.sleep(min(interval, remaining))
                continue
            return self._intern_native_match(match, client)

    def _describe_element(
        self,
        element: Any,
        element_index: int,
        *,
        attributes: Iterable[str],
        include_actions: bool,
    ) -> dict[str, Any]:
        requested = tuple(
            dict.fromkeys(("AXRole", *(str(item) for item in attributes)))
        )
        raw = self._copy_attributes(element, requested)
        node: dict[str, Any] = {
            "element_index": element_index,
            "role": self._jsonable(raw.get("AXRole")),
        }
        for source, target in _AX_NODE_MAPPING.items():
            if source not in raw:
                continue
            value = self._jsonable(raw[source])
            if value not in (None, "", [], {}):
                node[target] = value
        extra = {
            name: self._jsonable(value)
            for name, value in raw.items()
            if name not in _AX_NODE_MAPPING
            and name != "AXRole"
            and self._jsonable(value) not in (None, "", [], {})
        }
        if extra:
            node["attributes"] = extra
        if include_actions:
            actions = self._actions(element)
            if actions:
                node["actions"] = actions
        return node

    def get(self, element_index: int, attribute: str = "AXValue") -> Any:
        element = self._element(element_index)
        if not self._is_ax_element(element):
            return element.client.get(element, attribute)
        error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Read {attribute} from element {element_index}",
                error,
                element_index=element_index,
                attribute=attribute,
            )
        return self._jsonable(value)

    def get_attributes(
        self, element_index: int, attributes: Iterable[str]
    ) -> dict[str, Any | None]:
        """Read multiple raw AX attributes with Apple's batch API."""
        element = self._element(element_index)
        if not self._is_ax_element(element):
            return element.client.get_attributes(element, tuple(attributes))
        return {
            name: self._jsonable(value)
            for name, value in self._copy_attributes(element, attributes).items()
        }

    def ax_search(
        self,
        *,
        element_index: int | None = None,
        app: str | int | None = None,
        app_pid: int | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = False,
        limit: int = -1,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_ATTRIBUTES,
        include_actions: bool = True,
        max_nodes: int = 500,
        reset_elements: bool = True,
        messaging_timeout: float | None = None,
        enhance: bool = True,
    ) -> list[dict[str, Any]]:
        """Search a Chromium/WebKit AX subtree, including virtualized nodes."""
        if element_index is not None and (app is not None or app_pid is not None):
            raise MacOSError(
                "AX search element_index cannot be combined with app",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "element_index"},
            )
        if app is not None and app_pid is not None:
            raise MacOSError(
                "AX search accepts app or app_pid, not both",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app_pid"},
            )

        if element_index is None and self._backend != "python":
            native_pid = app_pid if app_pid is not None else self._pid(app)
            if native_pid is None:
                raise MacOSError(
                    "AX search requires an app or a prior app snapshot",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            client = self._acquire_native()
            if client is not None:
                if direction.casefold() not in {"next", "previous"}:
                    raise MacOSError(
                        "AX search direction must be 'next' or 'previous'",
                        code=ErrorCode.BAD_REQUEST,
                        details={"parameter": "direction", "value": direction},
                    )
                return self._native_query(
                    client,
                    pid=native_pid,
                    search_key=search_key,
                    text=text,
                    visible_only=visible_only,
                    limit=limit,
                    direction=direction.casefold(),
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                    reset_elements=reset_elements,
                    messaging_timeout=messaging_timeout,
                    enhance=enhance,
                )

        self._ensure_accessibility()
        if element_index is None:
            pid = app_pid if app_pid is not None else self._pid(app)
            if pid is None:
                raise MacOSError(
                    "AX search requires an app or a prior app snapshot",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            root = self._application_element(
                pid,
                messaging_timeout=messaging_timeout,
                enhance=enhance,
            )
        else:
            root = self._local_element(element_index)
        if reset_elements:
            self._elements = {}

        directions = {
            "next": "AXDirectionNext",
            "previous": "AXDirectionPrevious",
        }
        try:
            ax_direction = directions[direction.casefold()]
        except KeyError as exc:
            raise MacOSError(
                "AX search direction must be 'next' or 'previous'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "direction", "value": direction},
            ) from exc
        predicate: dict[str, Any] = {
            "AXSearchKey": search_key,
            "AXVisibleOnly": bool(visible_only),
            "AXResultsLimit": int(limit),
            "AXDirection": ax_direction,
            "AXImmediateDescendantsOnly": bool(immediate_descendants_only),
        }
        if text is not None:
            predicate["AXSearchText"] = str(text)
        error, values = AS.AXUIElementCopyParameterizedAttributeValue(
            root, "AXUIElementsForSearchPredicate", predicate, None
        )
        if error == AS.kAXErrorParameterizedAttributeUnsupported:
            return self._bounded_ax_search(
                root,
                search_key=search_key,
                text=text,
                visible_only=visible_only,
                limit=limit,
                direction=direction,
                immediate_descendants_only=immediate_descendants_only,
                attributes=attributes,
                include_actions=include_actions,
                max_nodes=max_nodes,
                reset_elements=False,
            )
        if error != _AX_SUCCESS:
            raise _ax_error("AXUIElementsForSearchPredicate", error, element_index=element_index)

        matches: list[dict[str, Any]] = []
        for element in values or []:
            if not self._is_ax_element(element):
                continue
            index = self._remember_element(element)
            matches.append(
                self._describe_element(
                    element,
                    index,
                    attributes=attributes,
                    include_actions=include_actions,
                )
            )
        return matches

    def _bounded_ax_search(
        self,
        root: Any,
        *,
        search_key: str,
        text: str | None,
        visible_only: bool,
        limit: int,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        include_actions: bool,
        max_nodes: int,
        reset_elements: bool,
    ) -> list[dict[str, Any]]:
        """Search a small ordinary AX tree when optimized search is unavailable."""
        if max_nodes <= 0:
            raise MacOSError(
                "AX fallback max_nodes must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "max_nodes", "value": max_nodes},
            )
        result_limit = max_nodes if limit < 0 else int(limit)
        if result_limit <= 0:
            return []

        role = _AX_SEARCH_ROLES.get(search_key)
        needle = text.casefold() if text is not None else None
        traversal_attributes = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in attributes),
                    "AXHidden",
                )
            )
        )
        nodes = self._snapshot_tree(
            root,
            max_depth=1 if immediate_descendants_only else 25,
            max_nodes=max_nodes,
            include_menu_bar=True,
            attributes=traversal_attributes,
            include_actions=include_actions,
            include_settable=False,
            reset_elements=reset_elements,
        )[1:]
        if direction.casefold() == "previous":
            nodes.reverse()

        matches: list[dict[str, Any]] = []
        for node in nodes:
            values = (
                node.get("title"),
                node.get("description"),
                node.get("value"),
                node.get("help"),
                node.get("identifier"),
                node.get("dom_identifier"),
                node.get("placeholder"),
            )
            text_matches = needle is None or any(
                needle in str(value).casefold()
                for value in values
                if value not in (None, "")
            )
            if (
                (role is None or node.get("role") == role)
                and (not visible_only or not bool(node.get("hidden")))
                and text_matches
            ):
                index = int(node["element_index"])
                matches.append(
                    self._describe_element(
                        self._element(index),
                        index,
                        attributes=attributes,
                        include_actions=include_actions,
                    )
                )
                if len(matches) >= result_limit:
                    break
        return matches

    @staticmethod
    def _normalize_apps(
        apps: str | int | Iterable[str | int] | None,
    ) -> tuple[str, ...] | None:
        if apps is None:
            return None
        values = (apps,) if isinstance(apps, (str, int)) else tuple(apps)
        normalized = tuple(str(value).strip() for value in values)
        if not normalized or any(not value for value in normalized):
            raise MacOSError(
                "apps must contain at least one non-empty selector",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "apps"},
            )
        return normalized

    def _resolve_apps(self, selectors: tuple[str, ...] | None) -> list[dict[str, Any]]:
        if selectors is None:
            return self.list_apps()
        resolved: list[dict[str, Any]] = []
        seen: set[int] = set()
        for selector in selectors:
            _, info = self._resolve_app(selector)
            pid = int(info["pid"])
            if pid in seen:
                continue
            seen.add(pid)
            resolved.append(info)
        return resolved

    @classmethod
    def _ax_scope(
        cls,
        *,
        app: str | int | None,
        all_apps: bool,
        apps: str | int | Iterable[str | int] | None,
        text: str | None,
    ) -> tuple[tuple[str, ...] | None, bool]:
        selectors = cls._normalize_apps(apps)
        if app is not None and (all_apps or selectors is not None):
            raise MacOSError(
                "Pass exactly one of app, all_apps=True, or apps",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "scope"},
            )
        if all_apps and selectors is not None:
            raise MacOSError(
                "Pass all_apps=True or apps, not both",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "scope"},
            )
        cross_process = all_apps or selectors is not None
        if cross_process and not text:
            raise MacOSError(
                "Cross-app AX search requires non-empty text",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "text"},
            )
        return selectors, cross_process

    def ax_search_all(
        self,
        *,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """Search selected running AX trees without activation."""
        if self._backend == "python":
            self._ensure_accessibility()
        if not text:
            raise MacOSError(
                "Cross-app AX search requires non-empty text",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "text"},
            )
        if direction.casefold() not in {"next", "previous"}:
            raise MacOSError(
                "AX search direction must be 'next' or 'previous'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "direction", "value": direction},
            )
        if max_nodes <= 0:
            raise MacOSError(
                "AX fallback max_nodes must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "max_nodes", "value": max_nodes},
            )
        if limit <= 0:
            raise MacOSError(
                "Cross-app AX search limit must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "limit", "value": limit},
            )

        selectors = self._normalize_apps(apps)
        infos = self._resolve_apps(selectors)
        strict = selectors is not None
        self._elements = {}
        matches: list[dict[str, Any]] = []
        first_error: MacOSError | None = None
        searched = False
        for info in infos:
            remaining = limit - len(matches)
            if remaining == 0:
                break
            try:
                app_matches = self.ax_search(
                    app_pid=int(info["pid"]),
                    search_key=search_key,
                    text=text,
                    visible_only=visible_only,
                    limit=remaining,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                    reset_elements=False,
                    messaging_timeout=_AX_CROSS_APP_MESSAGING_TIMEOUT,
                    enhance=False,
                )
            except AccessibilityPermissionError:
                raise
            except MacOSError as exc:
                if strict:
                    raise MacOSError(
                        f"AX search failed for {info['name']} ({info['pid']}): {exc}",
                        code=exc.code,
                        details={**exc.details, "app": info},
                    ) from exc
                if first_error is None:
                    first_error = exc
                continue
            searched = True
            for match in app_matches:
                match["app"] = dict(info)
                matches.append(match)
        if not searched and first_error is not None:
            raise first_error
        return matches

    @staticmethod
    def _match_summary(match: dict[str, Any]) -> str:
        owner = match.get("app")
        app_name = (
            owner.get("name", "current app")
            if isinstance(owner, dict)
            else "current app"
        )
        role = str(match.get("role") or "AXUnknown")
        label = str(
            match.get("title")
            or match.get("description")
            or match.get("identifier")
            or "untitled"
        )
        return f"{app_name}: {role} {label!r}"

    def ax_wait(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        """Wait for exactly one AX match and fail closed on ambiguity."""
        selectors, cross_process = self._ax_scope(
            app=app,
            all_apps=all_apps,
            apps=apps,
            text=text,
        )
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX wait timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX wait interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )

        deadline = time.monotonic() + timeout
        while True:
            if cross_process:
                matches = self.ax_search_all(
                    apps=selectors,
                    search_key=search_key,
                    text=text,
                    visible_only=visible_only,
                    limit=2,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                )
            else:
                matches = self.ax_search(
                    app=app,
                    search_key=search_key,
                    text=text,
                    visible_only=visible_only,
                    limit=2,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                summary = "; ".join(self._match_summary(match) for match in matches[:4])
                # More than one match is the caller's search criteria being too loose,
                # not an unknown element -- and not worth retrying, since a second
                # identical search will not resolve the ambiguity either. Mirrors the
                # native agent's `PressCoordinator`, which reports the same condition as
                # `bad_request` rather than `element.unknown` for exactly this reason.
                raise MacOSError(
                    f"AX wait found {len(matches)} matches: {summary}",
                    code=ErrorCode.BAD_REQUEST,
                    details={
                        "count": len(matches),
                        "matches": [
                            {
                                "element_index": match.get("element_index"),
                                "role": match.get("role"),
                                "app": match.get("app"),
                            }
                            for match in matches[:4]
                        ],
                    },
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MacOSError(
                    "AX wait timed out without a match",
                    code=ErrorCode.TIMEOUT,
                    details={"timeout": timeout},
                )
            time.sleep(min(interval, remaining))

    def ax_wait_gone(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None:
        """Wait for an AX match to be absent in two consecutive polls."""
        selectors, cross_process = self._ax_scope(
            app=app,
            all_apps=all_apps,
            apps=apps,
            text=text,
        )
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX wait timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX wait interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )

        deadline = time.monotonic() + timeout
        empty_polls = 0
        while True:
            try:
                if cross_process:
                    matches = self.ax_search_all(
                        apps=selectors,
                        search_key=search_key,
                        text=text,
                        visible_only=visible_only,
                        limit=1,
                        direction=direction,
                        immediate_descendants_only=immediate_descendants_only,
                        attributes=attributes,
                        max_nodes=max_nodes,
                    )
                else:
                    matches = self.ax_search(
                        app=app,
                        search_key=search_key,
                        text=text,
                        visible_only=visible_only,
                        limit=1,
                        direction=direction,
                        immediate_descendants_only=immediate_descendants_only,
                        attributes=attributes,
                        max_nodes=max_nodes,
                    )
            except ApplicationNotFoundError:
                if app is not None or (selectors is not None and len(selectors) == 1):
                    return
                raise

            empty_polls = empty_polls + 1 if not matches else 0
            if empty_polls >= 2:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MacOSError(
                    "AX wait timed out before two consecutive empty polls "
                    "confirmed the match was gone",
                    code=ErrorCode.TIMEOUT,
                    details={
                        "timeout": timeout,
                        "consecutive_empty_polls": empty_polls,
                    },
                )
            time.sleep(min(interval, remaining))

    def ax_press(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        """Press one unique AX target and detect foreground activation."""
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX press timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX press interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )
        targeted = not all_apps and apps is None
        if targeted and self._backend != "python":
            if direction.casefold() not in {"next", "previous"}:
                raise MacOSError(
                    "AX search direction must be 'next' or 'previous'",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "direction", "value": direction},
                )
            target_pid = self._pid(app)
            if target_pid is None:
                raise MacOSError(
                    "AX press requires app, all_apps=True, or apps",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            client = self._acquire_native()
            if client is not None:
                # A single agent-side search-then-press request settles
                # this directly; a client-side ax_wait traversal first
                # would just be a second, redundant round trip for the
                # same uniqueness check the agent already performs.
                return self._native_press(
                    client,
                    pid=target_pid,
                    search_key=search_key,
                    text=text,
                    visible_only=visible_only,
                    direction=direction.casefold(),
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    max_nodes=max_nodes,
                    timeout=timeout,
                    interval=interval,
                )

        match = self.ax_wait(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=search_key,
            text=text,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=True,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )
        owner = match.get("app")
        target_pid = int(owner["pid"]) if isinstance(owner, dict) else self._pid(app)
        if target_pid is None:
            raise MacOSError(
                "AX press requires app, all_apps=True, or apps",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )

        before = self._frontmost_app()
        try:
            self.perform_action(int(match["element_index"]), "AXPress")
        finally:
            self._guard_focus(before, target_pid, "AX press")
        return match

    def set(self, element_index: int, value: Any, attribute: str = "AXValue") -> None:
        element = self._element(element_index)
        if not self._is_ax_element(element):
            element.client.set(element, attribute, value)
            return
        error = AS.AXUIElementSetAttributeValue(element, attribute, value)
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Set {attribute} on element {element_index}",
                error,
                element_index=element_index,
                attribute=attribute,
            )

    set_value = set

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        element = self._element(element_index)
        normalized = _ACTION_ALIASES.get(action.casefold(), action)
        if not self._is_ax_element(element):
            element.client.perform(element, normalized)
            return
        available = self._actions(element)
        if normalized not in available:
            raise MacOSError(
                f"Element {element_index} does not expose {normalized!r}; available actions: {available}",
                code=ErrorCode.UNSUPPORTED_OP,
                details={
                    "element_index": element_index,
                    "action": normalized,
                    "available_actions": available,
                },
            )
        error = AS.AXUIElementPerformAction(element, normalized)
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Perform {normalized} on element {element_index}",
                error,
                element_index=element_index,
                action=normalized,
            )

    # --- windows and screenshots ----------------------------------------

    def windows(self, app: str | None = None) -> list[dict[str, Any]]:
        _, info = self._resolve_app(app)
        values = AS.CGWindowListCopyWindowInfo(
            AS.kCGWindowListOptionAll, AS.kCGNullWindowID
        )
        windows: list[dict[str, Any]] = []
        for value in values or []:
            if int(value.get(AS.kCGWindowOwnerPID, -1)) != info["pid"]:
                continue
            if int(value.get(AS.kCGWindowLayer, -1)) != 0:
                continue
            bounds = value.get(AS.kCGWindowBounds) or {}
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            if width < 40 or height < 40:
                continue
            windows.append(
                {
                    "window_id": int(value[AS.kCGWindowNumber]),
                    "title": str(value.get(AS.kCGWindowName) or ""),
                    "bounds": {
                        "x": float(bounds.get("X", 0)),
                        "y": float(bounds.get("Y", 0)),
                        "width": width,
                        "height": height,
                    },
                    "on_screen": bool(value.get(AS.kCGWindowIsOnscreen, False)),
                    "alpha": float(value.get(AS.kCGWindowAlpha, 1.0)),
                }
            )
        return sorted(
            windows,
            key=lambda window: (
                not window["on_screen"],
                -(window["bounds"]["width"] * window["bounds"]["height"]),
                not bool(window["title"]),
            ),
        )

    def capture_screenshot(
        self,
        app: str | None = None,
        *,
        window_index: int = 0,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        self._ensure_screen_recording()
        _, info = self._resolve_app(app)
        windows = self.windows(str(info["pid"]))
        if not windows:
            raise MacOSError(
                f"No capturable windows found for {app or self._last_app}",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"app": info},
            )
        try:
            window = windows[window_index]
        except IndexError as exc:
            raise MacOSError(
                f"Window index {window_index} is out of range; found {len(windows)} windows",
                code=ErrorCode.BAD_REQUEST,
                details={
                    "parameter": "window_index",
                    "value": window_index,
                    "count": len(windows),
                },
            ) from exc

        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix="macos-harness-", suffix=".png", delete=False
            ) as handle:
                output = Path(handle.name)
            output.unlink(missing_ok=True)
        else:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "/usr/sbin/screencapture",
                "-x",
                "-o",
                "-l",
                str(window["window_id"]),
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not output.exists():
            output.unlink(missing_ok=True)
            detail = (result.stderr or result.stdout or "no image returned").strip()
            raise MacOSError(
                f"Window screenshot failed: {detail}",
                code=ErrorCode.AX_ERROR,
                details={"reason": detail},
            )

        width, height = _png_size(output)
        bounds = window["bounds"]
        screenshot = {
            "path": str(output),
            "app": info,
            "pid": info["pid"],
            "window_id": window["window_id"],
            "width": width,
            "height": height,
            "bounds": bounds,
            "scale_x": width / bounds["width"],
            "scale_y": height / bounds["height"],
        }
        self._last_app = info
        self._last_windows = windows
        self._last_screenshot = screenshot
        return screenshot

    def see(
        self,
        app: str | None = None,
        *,
        window_index: int = 0,
        path: str | Path | None = None,
        max_width: int = 1280,
        max_height: int = 1280,
        show_pointer: bool = True,
    ) -> dict[str, Any]:
        """Capture a bounded window image and draw the harness pointer onto it."""
        if max_width <= 0 or max_height <= 0:
            raise MacOSError(
                "max_width and max_height must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"max_width": max_width, "max_height": max_height},
            )
        screenshot = self.capture_screenshot(app, window_index=window_index, path=path)
        output = Path(screenshot["path"])
        raw_width = int(screenshot["width"])
        raw_height = int(screenshot["height"])

        with Image.open(output) as source:
            image = source.convert("RGBA")
        ratio = min(1.0, max_width / image.width, max_height / image.height)
        if ratio < 1.0:
            size = (
                max(1, round(image.width * ratio)),
                max(1, round(image.height * ratio)),
            )
            image = image.resize(size, Image.Resampling.LANCZOS)

        bounds = screenshot["bounds"]
        scale_x = image.width / float(bounds["width"])
        scale_y = image.height / float(bounds["height"])
        pointer = self._pointer_position
        pointer_info: dict[str, Any] | None = None
        if pointer is not None:
            image_x = (pointer[0] - float(bounds["x"])) * scale_x
            image_y = (pointer[1] - float(bounds["y"])) * scale_y
            inside = 0 <= image_x < image.width and 0 <= image_y < image.height
            pointer_info = {
                "screen": {"x": pointer[0], "y": pointer[1]},
                "image": {"x": image_x, "y": image_y},
                "inside": inside,
                "visible": self._overlay.visible,
            }
            if show_pointer and self._overlay.visible and inside:
                draw = ImageDraw.Draw(image)
                hot_x, hot_y = POINTER_HOTSPOT
                points = [
                    (
                        round(image_x + (x - hot_x) * scale_x),
                        round(image_y + (y - hot_y) * scale_y),
                    )
                    for x, y in pointer_points()
                ]
                shadow = [
                    (x + max(1, round(scale_x)), y + max(1, round(scale_y)))
                    for x, y in points
                ]
                draw.polygon(shadow, fill=(0, 0, 0, 72))
                outline_width = max(2, round(2.5 * (scale_x + scale_y) / 2))
                draw.polygon(
                    points,
                    fill=(0, 0, 0, 255),
                    outline=(255, 255, 255, 240),
                    width=outline_width,
                )
                draw.line(
                    [*points, points[0]],
                    fill=(0, 0, 0, 230),
                    width=max(1, round(0.65 * (scale_x + scale_y) / 2)),
                    joint="curve",
                )

        image.save(output, format="PNG", optimize=True)
        frontmost = self._frontmost_app()
        screenshot.update(
            {
                "raw_width": raw_width,
                "raw_height": raw_height,
                "width": image.width,
                "height": image.height,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "virtual_pointer": pointer_info,
                "focus": {
                    "frontmost": frontmost,
                    "target_is_frontmost": (
                        frontmost is not None
                        and int(frontmost["pid"]) == int(screenshot["pid"])
                    ),
                },
            }
        )
        self._last_screenshot = screenshot
        return screenshot

    # --- direct visual and keyboard input -------------------------------

    def _pid(self, app: str | int | None) -> int | None:
        if app is None and self._last_app is None:
            return None
        _, info = self._resolve_app(app)
        self._last_app = info
        return int(info["pid"])

    @staticmethod
    def _post(event: Any, pid: int | None) -> None:
        if pid is None:
            raise MacOSError(
                "Input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        AS.CGEventPostToPid(pid, event)

    def _screen_point(
        self, x: float, y: float, coordinate_space: str, *, pid: int | None = None
    ) -> tuple[float, float]:
        """Resolve a coordinate to a screen point, bound to ``pid`` when given.

        ``pid`` -- the already-resolved target of the action this point is
        for -- is optional: callers with no dispatch target at all (``move``,
        an AX hit test) simply skip the binding check below. Callers that
        *do* dispatch to a pid (``click``, ``drag``, ``scroll``) must pass
        it, so a window/screenshot-relative coordinate computed from a
        screenshot of one app can never be silently posted to another.
        """
        if coordinate_space == "screen":
            return float(x), float(y)
        if self._last_screenshot is None:
            raise MacOSError(
                "Take a screenshot before using window or screenshot coordinates, "
                "or pass coordinate_space='screen'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "coordinate_space", "value": coordinate_space},
            )
        shot = self._last_screenshot
        if pid is not None and int(shot["pid"]) != int(pid):
            raise MacOSError(
                f"Last screenshot targets pid {shot['pid']}, not the pid {pid} "
                "this action targets; take a fresh screenshot for this app or "
                "pass coordinate_space='screen'",
                code=ErrorCode.BAD_REQUEST,
                details={
                    "parameter": "coordinate_space",
                    "screenshot_pid": int(shot["pid"]),
                    "target_pid": int(pid),
                },
            )
        bounds = shot["bounds"]
        if coordinate_space == "screenshot":
            x = float(x) / float(shot["scale_x"])
            y = float(y) / float(shot["scale_y"])
        elif coordinate_space != "window":
            raise MacOSError(
                "coordinate_space must be 'screenshot', 'window', or 'screen'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "coordinate_space", "value": coordinate_space},
            )
        return bounds["x"] + float(x), bounds["y"] + float(y)

    def _pointer_info(self, *, pid: int | None = None) -> dict[str, object] | None:
        """Describe the current virtual pointer position.

        ``pid``, when given, is the OS process id the caller's action just
        targeted. ``self._last_screenshot`` only ever describes one specific
        app's window; a caller that already bound its own action to a
        different app (``click``, a non-screen ``move``) must never have
        ``image``/``inside`` silently computed against a screenshot of that
        *other* app, since those pixel coordinates would be meaningless (or
        worse, misleadingly plausible) for the app the caller actually
        targeted. Passing no ``pid`` (``show_pointer``, a screen-space
        ``move``, which target no particular app at all) keeps reporting
        against whatever screenshot happens to be retained, if any -- there
        is no other app identity to compare it against.
        """
        if self._pointer_position is None:
            return None
        screen_x, screen_y = self._pointer_position
        result: dict[str, object] = {"screen": {"x": screen_x, "y": screen_y}}
        shot = self._last_screenshot
        if shot is not None and (pid is None or int(shot["pid"]) == int(pid)):
            bounds = shot["bounds"]
            image_x = (screen_x - float(bounds["x"])) * float(shot["scale_x"])
            image_y = (screen_y - float(bounds["y"])) * float(shot["scale_y"])
            result["image"] = {"x": image_x, "y": image_y}
            result["inside"] = 0 <= image_x < float(
                shot["width"]
            ) and 0 <= image_y < float(shot["height"])
        return result

    def move(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        coordinate_space: str = "screenshot",
        duration: float = 0.16,
    ) -> dict[str, Any]:
        """Animate the virtual pointer without moving the physical cursor.

        ``coordinate_space="screen"`` stays app-free, exactly like every
        other screen-space call in this file. Any other space converts
        through ``self._last_screenshot``, and that screenshot belongs to
        exactly one app -- so this requires ``app`` (or a prior app
        snapshot, exactly like ``click``/``drag``/``scroll``) to bind the
        conversion to, rather than silently reusing whatever screenshot
        ``self._last_screenshot`` last happened to hold regardless of
        which app it actually came from.
        """
        pid = None
        if coordinate_space != "screen":
            pid = self._pid(app)
            if pid is None:
                raise MacOSError(
                    "Move requires an app or prior app snapshot for "
                    "screenshot/window coordinates, or pass coordinate_space='screen'",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
        self._pointer_position = self._screen_point(x, y, coordinate_space, pid=pid)
        self._overlay.move(*self._pointer_position, duration=duration)
        pointer = self._pointer_info(pid=pid)
        assert pointer is not None
        return pointer

    def show_pointer(self) -> dict[str, Any]:
        if self._pointer_position is None:
            raise MacOSError(
                "Move the virtual pointer before showing it",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "pointer_position"},
            )
        self._overlay.show(*self._pointer_position)
        pointer = self._pointer_info()
        assert pointer is not None
        return pointer

    def hide_pointer(self) -> None:
        self._overlay.hide()

    def click(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        button: str = "left",
        clicks: int = 1,
        coordinate_space: str = "screenshot",
    ) -> dict[str, Any]:
        """Send one raw coordinate click to an app PID; never guess an AX action."""
        self._ensure_accessibility()
        self._ensure_post_events()
        button = button.casefold()
        if button not in _BUTTONS:
            raise MacOSError(
                f"Unknown mouse button {button!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "button", "value": button},
            )
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Pointer input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        point = self._screen_point(x, y, coordinate_space, pid=pid)
        focus_before = self._frontmost_app()
        self._pointer_position = point
        self._overlay.move(*point)
        down_type, up_type, _ = _MOUSE_EVENTS[button]
        for click_count in range(1, max(1, int(clicks)) + 1):
            down = AS.CGEventCreateMouseEvent(
                self._event_source, down_type, point, _BUTTONS[button]
            )
            up = AS.CGEventCreateMouseEvent(
                self._event_source, up_type, point, _BUTTONS[button]
            )
            AS.CGEventSetIntegerValueField(
                down, AS.kCGMouseEventClickState, click_count
            )
            AS.CGEventSetIntegerValueField(up, AS.kCGMouseEventClickState, click_count)
            self._post(down, pid)
            time.sleep(0.03)
            self._post(up, pid)
            time.sleep(0.03)
            self._guard_focus(focus_before, pid, "click")
        self._overlay.click()
        pointer = self._pointer_info(pid=pid)
        assert pointer is not None
        return pointer

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        *,
        app: str | None = None,
        button: str = "left",
        coordinate_space: str = "screenshot",
        duration: float = 0.25,
        steps: int = 12,
    ) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        button = button.casefold()
        if button not in _BUTTONS:
            raise MacOSError(
                f"Unknown mouse button {button!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "button", "value": button},
            )
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Pointer input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        start = self._screen_point(from_x, from_y, coordinate_space, pid=pid)
        end = self._screen_point(to_x, to_y, coordinate_space, pid=pid)
        focus_before = self._frontmost_app()
        self._pointer_position = start
        self._overlay.move(*start, duration=0)
        self._overlay.move(*end, duration=duration)
        down_type, up_type, drag_type = _MOUSE_EVENTS[button]
        self._post(
            AS.CGEventCreateMouseEvent(
                self._event_source, down_type, start, _BUTTONS[button]
            ),
            pid,
        )
        self._pointer_position = end
        for index in range(1, max(1, steps) + 1):
            ratio = index / max(1, steps)
            point = (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
            self._post(
                AS.CGEventCreateMouseEvent(
                    self._event_source, drag_type, point, _BUTTONS[button]
                ),
                pid,
            )
            time.sleep(max(0.0, duration) / max(1, steps))
        self._post(
            AS.CGEventCreateMouseEvent(
                self._event_source, up_type, end, _BUTTONS[button]
            ),
            pid,
        )
        self._guard_focus(focus_before, pid, "drag")

    def scroll(
        self,
        delta_y: int,
        delta_x: int = 0,
        *,
        app: str | None = None,
        unit: str = "pixel",
        x: float | None = None,
        y: float | None = None,
        coordinate_space: str = "screenshot",
    ) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        units = {
            "pixel": AS.kCGScrollEventUnitPixel,
            "line": AS.kCGScrollEventUnitLine,
        }
        try:
            scroll_unit = units[unit]
        except KeyError as exc:
            raise MacOSError(
                "Scroll unit must be 'pixel' or 'line'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "unit", "value": unit},
            ) from exc
        if (x is None) != (y is None):
            raise MacOSError(
                "Provide both x and y when targeting a scroll point",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "x/y"},
            )
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Scroll input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        point: tuple[float, float] | None = None
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space, pid=pid)
            self._pointer_position = point
            self._overlay.move(*point)

        focus_before = self._frontmost_app()

        maximum = 100 if unit == "pixel" else 10
        y_steps = _split_scroll_delta(delta_y, maximum)
        x_steps = _split_scroll_delta(delta_x, maximum)
        count = max(len(y_steps), len(x_steps))
        y_steps.extend([0] * (count - len(y_steps)))
        x_steps.extend([0] * (count - len(x_steps)))

        for step_y, step_x in zip(y_steps, x_steps, strict=True):
            event = AS.CGEventCreateScrollWheelEvent(
                self._event_source, scroll_unit, 2, int(step_y), int(step_x)
            )
            if point:
                AS.CGEventSetLocation(event, point)
            self._post(event, pid)
            time.sleep(0.01)
            self._guard_focus(focus_before, pid, "scroll")

    def type(self, text: str, *, app: str | None = None) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Keyboard input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        focus_before = self._frontmost_app()
        aliases = {" ": "space", "\n": "return", "\r": "return", "\t": "tab"}
        for character in text:
            base = _SHIFTED_CHARACTERS.get(
                character, aliases.get(character, character.casefold())
            )
            flags = (
                AS.kCGEventFlagMaskShift
                if character.isupper() or character in _SHIFTED_CHARACTERS
                else 0
            )
            keycode = _KEYCODES.get(base, 0)
            down = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, True)
            up = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, False)
            length = len(character.encode("utf-16-le")) // 2
            AS.CGEventKeyboardSetUnicodeString(down, length, character)
            AS.CGEventKeyboardSetUnicodeString(up, length, character)
            AS.CGEventSetFlags(down, flags)
            AS.CGEventSetFlags(up, flags)
            self._post(down, pid)
            self._post(up, pid)
            time.sleep(0.01)
            self._guard_focus(focus_before, pid, "typing")

    @staticmethod
    def _validate_key(key: str) -> None:
        _parse_key(key)

    def key(self, key: str, *, app: str | int | None = None) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        keycode, parsed_modifiers = _parse_key(key)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Keyboard input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        focus_before = self._frontmost_app()
        active_flags = 0
        pressed: list[tuple[int, int]] = []
        try:
            for modifier_keycode, modifier_flag in parsed_modifiers:
                active_flags |= modifier_flag
                event = AS.CGEventCreateKeyboardEvent(
                    self._event_source, modifier_keycode, True
                )
                AS.CGEventSetFlags(event, active_flags)
                self._post(event, pid)
                pressed.append((modifier_keycode, modifier_flag))
                time.sleep(0.005)

            down = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, True)
            up = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, False)
            AS.CGEventSetFlags(down, active_flags)
            AS.CGEventSetFlags(up, active_flags)
            self._post(down, pid)
            self._post(up, pid)
        finally:
            for modifier_keycode, modifier_flag in reversed(pressed):
                active_flags &= ~modifier_flag
                event = AS.CGEventCreateKeyboardEvent(
                    self._event_source, modifier_keycode, False
                )
                AS.CGEventSetFlags(event, active_flags)
                self._post(event, pid)
        time.sleep(0.01)
        self._guard_focus(focus_before, pid, f"key {key!r}")

    # --- Apple Events escape hatch --------------------------------------

    def script(self, source: str, *, language: str = "AppleScript") -> str:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", language, "-"],
            input=source,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MacOSError(
                (result.stderr or result.stdout).strip(),
                code=ErrorCode.AX_ERROR,
                details={"returncode": result.returncode, "language": language},
            )
        return result.stdout.rstrip("\n")
