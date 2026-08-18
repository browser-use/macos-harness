"""Direct macOS control through public ApplicationServices APIs.

No Codex or OpenAI Computer Use runtime is used here. Accessibility supplies
the semantic tree/actions; Core Graphics supplies raw input; the system
``screencapture`` executable captures a specific window.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path, PurePath
from typing import Any

from PIL import Image, ImageDraw

from .overlay import LivePointerOverlay
from .pointer import POINTER_HOTSPOT, pointer_points

try:
    import ApplicationServices as AS
    from AppKit import NSWorkspace
except ImportError as exc:  # pragma: no cover - exercised on non-macOS hosts
    AS = None  # type: ignore[assignment]
    NSWorkspace = None  # type: ignore[assignment]
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None


class MacOSError(RuntimeError):
    """Base error for macOS discovery or control failures."""


class AccessibilityPermissionError(MacOSError):
    """The calling process lacks macOS Accessibility permission."""


class FocusChangedError(MacOSError):
    """A background-targeted action made its target frontmost."""


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


def _require_macos() -> None:
    if AS is None or NSWorkspace is None:
        raise MacOSError(
            "macOS ApplicationServices bindings are unavailable. Run on macOS "
            "after installing project dependencies with `uv sync`."
        ) from _IMPORT_ERROR


def _truncate(value: str, limit: int = 160) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _short_label(info: dict[str, Any]) -> str:
    """Identify a process in a message without echoing its argv.

    `localizedName` is the full command line for processes without a bundle, and
    command lines routinely carry credentials (an MCP server started as
    `npx mcp-remote ... --header "Authorization:Bearer ..."` is a common shape).
    Error text ends up in logs, screenshots, and agent transcripts, so it should
    not repeat argv. The pid already disambiguates; the label only has to be
    recognisable.
    """
    bundle_id = info.get("bundle_id")
    if bundle_id:
        return _truncate(str(bundle_id), 80)
    path = info.get("path")
    if path:
        return _truncate(PurePath(str(path)).name, 80)
    # No bundle and no path: `name` may be a whole command line, so keep only
    # the leading token. Split on any whitespace, not just a space, so a command
    # line separated by tabs or newlines cannot slip its arguments through. A
    # binary path containing spaces degrades to its first segment, which is
    # unhelpful to read but never leaks an argument.
    name = str(info.get("name") or "").strip()
    parts = name.split()
    return _truncate(parts[0], 80) if parts else "?"


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise MacOSError(f"Screenshot is not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _ax_error(operation: str, error: int) -> MacOSError:
    return MacOSError(f"{operation} failed with AXError {error}")


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


class MacOS:
    """Low-level macOS observation and control for one persistent process."""

    def __init__(self) -> None:
        _require_macos()
        self._elements: dict[int, Any] = {}
        self._last_app: dict[str, Any] | None = None
        self._last_windows: list[dict[str, Any]] = []
        self._last_screenshot: dict[str, Any] | None = None
        self._event_source = AS.CGEventSourceCreate(AS.kCGEventSourceStatePrivate)
        if self._event_source is None:
            raise MacOSError("Could not create a private Core Graphics event source")

        from .controls import Accessibility

        self._pointer_position: tuple[float, float] | None = None
        self._overlay = LivePointerOverlay()
        self.ax = Accessibility(self)

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
                f"{after['name']} became frontmost during {operation}; stopped"
            )

    def _resolve_app(self, query: str | None) -> tuple[Any, dict[str, Any]]:
        if not query and self._last_app:
            query = str(self._last_app["pid"])
        if not query:
            raise MacOSError("Specify an app name, bundle ID, path, or PID")

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
            raise MacOSError(f"No running application matches {query!r}")
        if len(matches) > 1:
            names = ", ".join(
                f"{_short_label(item[1])} ({item[1]['pid']})" for item in matches[:8]
            )
            raise MacOSError(f"Application query {query!r} is ambiguous: {names}")
        return matches[0]

    # --- AX tree ---------------------------------------------------------

    def _ensure_accessibility(self) -> None:
        if not self.is_accessibility_trusted():
            raise AccessibilityPermissionError(
                "Accessibility permission is required. Grant it to the terminal or "
                "agent host in System Settings → Privacy & Security → Accessibility, "
                "or call mac.request_accessibility_permission() to show Apple's prompt."
            )

    def _ensure_screen_recording(self) -> None:
        if self._preflight_permission("CGPreflightScreenCaptureAccess") is False:
            raise AccessibilityPermissionError(
                "Screen Recording permission is required. Grant it to the terminal "
                "or agent host in System Settings → Privacy & Security → Screen & "
                "System Audio Recording, or call mac.request_permissions()."
            )

    def _ensure_post_events(self) -> None:
        if self._preflight_permission("CGPreflightPostEventAccess") is False:
            raise AccessibilityPermissionError(
                "Permission to post input events is required. Grant control access "
                "to the terminal or agent host, or call mac.request_permissions()."
            )

    @staticmethod
    def _application_element(pid: int) -> Any:
        root = AS.AXUIElementCreateApplication(pid)
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
    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
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
            return {"x": float(decoded.x), "y": float(decoded.y)}
        if kind == "size":
            return {"width": float(decoded.width), "height": float(decoded.height)}
        if kind == "rect":
            return {
                "x": float(decoded.origin.x),
                "y": float(decoded.origin.y),
                "width": float(decoded.size.width),
                "height": float(decoded.size.height),
            }
        return {field: int(getattr(decoded, field)) for field in fields}

    def _snapshot_tree(
        self,
        root: Any,
        *,
        max_depth: int,
        max_nodes: int,
        include_menu_bar: bool,
        extra_attributes: Iterable[str] = (),
        include_actions: bool = True,
        include_settable: bool = True,
    ) -> list[dict[str, Any]]:
        self._elements = {}
        nodes: list[dict[str, Any]] = []
        seen: set[int] = set()
        requested_attributes = tuple(
            dict.fromkeys((*_AX_ATTRIBUTES, *(str(item) for item in extra_attributes)))
        )
        standard_attributes = set(_AX_ATTRIBUTES)

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
            if raw["AXRole"] == "AXMenuBar" and not include_menu_bar:
                return
            index = len(nodes)
            self._elements[index] = element
            node: dict[str, Any] = {
                "element_index": index,
                "depth": depth,
                "role": self._jsonable(raw["AXRole"]),
            }
            for source, target in _AX_NODE_MAPPING.items():
                value = self._jsonable(raw[source])
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

            children = raw["AXChildren"]
            if not children and depth == 0:
                children = raw["AXWindows"]
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
        self._last_screenshot = None
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
                f"Unknown element index {element_index!r}; take a fresh snapshot first"
            ) from exc

    def _remember_element(self, element: Any) -> int:
        index = max(self._elements, default=-1) + 1
        self._elements[index] = element
        return index

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
        error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        if error != _AX_SUCCESS:
            raise _ax_error(f"Read {attribute} from element {element_index}", error)
        return self._jsonable(value)

    def get_attributes(
        self, element_index: int, attributes: Iterable[str]
    ) -> dict[str, Any | None]:
        """Read multiple raw AX attributes with Apple's batch API."""
        element = self._element(element_index)
        return {
            name: self._jsonable(value)
            for name, value in self._copy_attributes(element, attributes).items()
        }

    def ax_search(
        self,
        *,
        element_index: int | None = None,
        app: str | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = False,
        limit: int = -1,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_ATTRIBUTES,
        include_actions: bool = True,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """Search a Chromium/WebKit AX subtree, including virtualized nodes."""
        self._ensure_accessibility()
        if element_index is None:
            pid = self._pid(app)
            if pid is None:
                raise MacOSError("AX search requires an app or a prior app snapshot")
            root = self._application_element(pid)
        else:
            root = self._element(element_index)

        directions = {
            "next": "AXDirectionNext",
            "previous": "AXDirectionPrevious",
        }
        try:
            ax_direction = directions[direction.casefold()]
        except KeyError as exc:
            raise MacOSError(
                "AX search direction must be 'next' or 'previous'"
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
            )
        if error != _AX_SUCCESS:
            raise _ax_error("AXUIElementsForSearchPredicate", error)

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
    ) -> list[dict[str, Any]]:
        """Search a small ordinary AX tree when optimized search is unavailable."""
        if max_nodes <= 0:
            raise MacOSError("AX fallback max_nodes must be positive")
        result_limit = max_nodes if limit < 0 else int(limit)
        if result_limit <= 0:
            return []

        role = _AX_SEARCH_ROLES.get(search_key)
        needle = text.casefold() if text is not None else None
        nodes = self._snapshot_tree(
            root,
            max_depth=1 if immediate_descendants_only else 25,
            max_nodes=max_nodes,
            include_menu_bar=True,
            extra_attributes=attributes,
            include_actions=include_actions,
            include_settable=False,
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

    def set(self, element_index: int, value: Any, attribute: str = "AXValue") -> None:
        element = self._element(element_index)
        error = AS.AXUIElementSetAttributeValue(element, attribute, value)
        if error != _AX_SUCCESS:
            raise _ax_error(f"Set {attribute} on element {element_index}", error)

    set_value = set

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        element = self._element(element_index)
        normalized = _ACTION_ALIASES.get(action.casefold(), action)
        available = self._actions(element)
        if normalized not in available:
            raise MacOSError(
                f"Element {element_index} does not expose {normalized!r}; available actions: {available}"
            )
        error = AS.AXUIElementPerformAction(element, normalized)
        if error != _AX_SUCCESS:
            raise _ax_error(f"Perform {normalized} on element {element_index}", error)

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
            raise MacOSError(f"No capturable windows found for {app or self._last_app}")
        try:
            window = windows[window_index]
        except IndexError as exc:
            raise MacOSError(
                f"Window index {window_index} is out of range; found {len(windows)} windows"
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
            raise MacOSError(f"Window screenshot failed: {detail}")

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
            raise MacOSError("max_width and max_height must be positive")
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

    def _pid(self, app: str | None) -> int | None:
        if app is None and self._last_app is None:
            return None
        _, info = self._resolve_app(app)
        self._last_app = info
        return int(info["pid"])

    @staticmethod
    def _post(event: Any, pid: int | None) -> None:
        if pid is None:
            raise MacOSError("Input requires an app or prior app snapshot")
        AS.CGEventPostToPid(pid, event)

    def _screen_point(
        self, x: float, y: float, coordinate_space: str
    ) -> tuple[float, float]:
        if coordinate_space == "screen":
            return float(x), float(y)
        if self._last_screenshot is None:
            raise MacOSError(
                "Take a screenshot before using window or screenshot coordinates, "
                "or pass coordinate_space='screen'"
            )
        shot = self._last_screenshot
        bounds = shot["bounds"]
        if coordinate_space == "screenshot":
            x = float(x) / float(shot["scale_x"])
            y = float(y) / float(shot["scale_y"])
        elif coordinate_space != "window":
            raise MacOSError(
                "coordinate_space must be 'screenshot', 'window', or 'screen'"
            )
        return bounds["x"] + float(x), bounds["y"] + float(y)

    def _pointer_info(self) -> dict[str, Any] | None:
        if self._pointer_position is None:
            return None
        screen_x, screen_y = self._pointer_position
        result: dict[str, Any] = {"screen": {"x": screen_x, "y": screen_y}}
        shot = self._last_screenshot
        if shot is not None:
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
        coordinate_space: str = "screenshot",
        duration: float = 0.16,
    ) -> dict[str, Any]:
        """Animate the virtual pointer without moving the physical cursor."""
        self._pointer_position = self._screen_point(x, y, coordinate_space)
        self._overlay.move(*self._pointer_position, duration=duration)
        pointer = self._pointer_info()
        assert pointer is not None
        return pointer

    def show_pointer(self) -> dict[str, Any]:
        if self._pointer_position is None:
            raise MacOSError("Move the virtual pointer before showing it")
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
            raise MacOSError(f"Unknown mouse button {button!r}")
        point = self._screen_point(x, y, coordinate_space)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError("Pointer input requires an app or prior app snapshot")
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
        pointer = self._pointer_info()
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
            raise MacOSError(f"Unknown mouse button {button!r}")
        start = self._screen_point(from_x, from_y, coordinate_space)
        end = self._screen_point(to_x, to_y, coordinate_space)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError("Pointer input requires an app or prior app snapshot")
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
            raise MacOSError("Scroll unit must be 'pixel' or 'line'") from exc
        if (x is None) != (y is None):
            raise MacOSError("Provide both x and y when targeting a scroll point")
        point: tuple[float, float] | None = None
        if x is not None and y is not None:
            point = self._screen_point(x, y, coordinate_space)
            self._pointer_position = point
            self._overlay.move(*point)

        pid = self._pid(app)
        if pid is None:
            raise MacOSError("Scroll input requires an app or prior app snapshot")
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
            raise MacOSError("Keyboard input requires an app or prior app snapshot")
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

    def key(self, key: str, *, app: str | None = None) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        parts = [part.casefold() for part in re.split(r"[+-]", key) if part]
        if not parts:
            raise MacOSError("Key must not be empty")
        base = parts[-1]
        modifiers = parts[:-1]
        try:
            keycode = _KEYCODES[base]
        except KeyError as exc:
            raise MacOSError(
                f"Unsupported key {base!r}; use mac.type() for arbitrary text"
            ) from exc
        parsed_modifiers: list[tuple[int, int]] = []
        seen_keycodes: set[int] = set()
        for modifier in modifiers:
            try:
                modifier_keycode = _MODIFIER_KEYCODES[modifier]
                modifier_flag = _MODIFIER_FLAGS[modifier]
            except KeyError as exc:
                raise MacOSError(f"Unsupported modifier {modifier!r}") from exc
            if modifier_keycode not in seen_keycodes:
                seen_keycodes.add(modifier_keycode)
                parsed_modifiers.append((modifier_keycode, modifier_flag))
        pid = self._pid(app)
        if pid is None:
            raise MacOSError("Keyboard input requires an app or prior app snapshot")
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
            raise MacOSError((result.stderr or result.stdout).strip())
        return result.stdout.rstrip("\n")
