"""Thin wrappers over the accessibility calls. Any thread may call them.

No function sets a messaging timeout: the system default applies to every call.
PyObjC is loaded on first use, so importing this module costs nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import cache
from typing import Any

from . import errors
from .model import Rect

log = logging.getLogger(__name__)
Element = Any

MAX_ARRAY = 100  # entries read of one array attribute by the one-call subtree copy
# The one-call copy of a window. Off: measured on Lab (12 Oct) it took 11 ms against 30 ms
# for the walk but left out the page of a web view (15 elements, the walk 53 lines). On
# only once a read with it prints byte for byte what the walk prints.
SUBTREE = False
# The options of the call: children only, never the selection.
SUBTREE_SKIP = ["AXSelectedRows", "AXSelectedCells", "AXSelectedChildren"]
HIERARCHY_KEYS = ("ArrayAttributes", "SkipInspectionForAttributes", "MaxArrayCount")
ERROR_VALUE = 5  # kAXValueAXErrorType: the placeholder for one failed name of a batch
CANNOT_COMPLETE, INVALID_ELEMENT = -25204, -25202
# Codes that mean "this element has no such thing". Every other code is a failure.
MISSING = {-25212, -25205, -25213, -25206, -25200, -25201, -25208}
ERRORS = {
    -25200: "failure",
    -25201: "illegalArgument",
    -25202: "invalidUIElement",
    -25203: "invalidUIElementObserver",
    -25204: "cannotComplete",
    -25205: "attributeUnsupported",
    -25206: "actionUnsupported",
    -25207: "notificationUnsupported",
    -25208: "notImplemented",
    -25209: "notificationAlreadyRegistered",
    -25210: "notificationNotRegistered",
    -25211: "apiDisabled",
    -25212: "noValue",
    -25213: "parameterizedAttributeUnsupported",
    -25214: "notEnoughPrecision",
}
HISERVICES = (
    "/System/Library/Frameworks/ApplicationServices.framework"
    "/Frameworks/HIServices.framework/HIServices"
)


@cache
def _api() -> Any:
    import ApplicationServices

    return ApplicationServices


@cache
def _private(name: str) -> Any | None:
    """A private call, looked up once. A missing one switches its step off."""
    try:
        import ctypes

        return getattr(ctypes.CDLL(HISERVICES), name)
    except (OSError, AttributeError):
        log.warning("private accessibility call %s is missing: its step is off", name)
        return None


def error_name(code: int) -> str:
    name = ERRORS.get(code)
    return f"AXError.{name}" if name else "(undocumented AXError)"


def _check(code: int) -> None:
    if code:
        raise errors.accessibility(error_name(code))


def _value(value: Any) -> Any | None:
    """A batch read puts an error placeholder where a name failed. That is None."""
    api = _api()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    is_placeholder = (
        api.CFGetTypeID(value) == api.AXValueGetTypeID()
        and api.AXValueGetType(value) == ERROR_VALUE
    )
    return None if is_placeholder else value


def app(pid: int) -> Element:
    return _api().AXUIElementCreateApplication(pid)


def get(el: Element, name: str) -> Any | None:
    code, value = _api().AXUIElementCopyAttributeValue(el, name, None)
    if code and code not in MISSING:
        _check(code)
    return None if code else _value(value)


def names(el: Element) -> list[str]:
    """The attributes the element says it has. A read can answer for others too:
    tree.py tells a real AXExpanded from the False an element answers for an attribute
    it does not have."""
    code, found = _api().AXUIElementCopyAttributeNames(el, None)
    return [] if code or found is None else [str(name) for name in found]


def get_many(el: Element, names: Sequence[str]) -> list[Any | None]:
    """One batched call with options 0: a name that fails gives None, the rest arrive."""
    code, values = _api().AXUIElementCopyMultipleAttributeValues(
        el, list(names), 0, None
    )
    _check(code)  # a failure of the whole batch is not retried name by name
    return [_value(value) for value in values]


def set(el: Element, name: str, value: Any) -> None:
    _check(_api().AXUIElementSetAttributeValue(el, name, value))


def settable(el: Element, name: str) -> bool:
    code, flag = _api().AXUIElementIsAttributeSettable(el, name, None)
    return bool(flag) and not code


class _Actions(Mapping):
    """Action name -> the app's description, which is read when first asked for."""

    def __init__(self, el: Element, names: Sequence[str]):
        self._el, self._names, self._read = el, [str(n) for n in names], {}

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)

    def __contains__(self, name: object) -> bool:
        return name in self._names  # asking for a name reads nothing

    def __getitem__(self, name: str) -> str:
        if name not in self._names:
            raise KeyError(name)
        if name not in self._read:
            code, text = _api().AXUIElementCopyActionDescription(self._el, name, None)
            self._read[name] = "" if code or text is None else str(text)
        return self._read[name]


def actions(el: Element) -> Mapping[str, str]:
    code, names = _api().AXUIElementCopyActionNames(el, None)
    return _Actions(el, [] if code or names is None else names)


def perform(el: Element, action: str) -> None:
    _check(_api().AXUIElementPerformAction(el, action))


def param(el: Element, name: str, arg: Any) -> Any | None:
    code, value = _api().AXUIElementCopyParameterizedAttributeValue(el, name, arg, None)
    return None if code else _value(value)


@cache
def _hierarchy() -> tuple[Callable, dict] | None:
    """The private copy call and its option keys. None when the system has none."""
    try:
        import objc

        bundle = objc.loadBundle("HIServices", {}, HISERVICES.rsplit("/", 1)[0])
        calls, keys = {}, {}
        objc.loadBundleFunctions(
            bundle, calls, [("AXUIElementCopyHierarchy", b"i@@@o^@")]
        )
        names = {k: f"kAXUIElementCopyHierarchy{k}Key" for k in HIERARCHY_KEYS}
        objc.loadBundleVariables(bundle, keys, [(n, b"@") for n in names.values()])
        return calls["AXUIElementCopyHierarchy"], {k: keys[n] for k, n in names.items()}
    except Exception:  # noqa: BLE001 - any failure switches the copy off
        log.warning("private accessibility call AXUIElementCopyHierarchy is missing")
        return None


def subtree(
    el: Element, names: Sequence[str], max_children: int = MAX_ARRAY
) -> list | None:
    """The one-call copy of a whole subtree: [(element, values in `names` order)] for
    the element and what the copy reached below it. None sends the caller to get_many,
    and so does an element left out: one whose children were cut at `max_children`
    (the walk must see them all to choose), or one the copy did not reach (a web
    view's page)."""
    found = _hierarchy() if SUBTREE else None
    if found is None:
        return None
    copy, key = found
    options = {
        key["ArrayAttributes"]: ["AXChildren"],
        key["SkipInspectionForAttributes"]: SUBTREE_SKIP,
        key["MaxArrayCount"]: max_children,
    }
    code, result = copy(el, list(names), options, None)
    if code or not result:
        return None
    out = []
    for element, read in result.items():
        entries = [read.get(name) or {} for name in names]
        cut = any("count" in e and len(e["value"]) < e["count"] for e in entries)
        if not cut:
            out.append((element, [_value(e.get("value")) for e in entries]))
    return out


def same(a: Element, b: Element) -> bool:
    return a is not None and b is not None and bool(_api().CFEqual(a, b))


def alive(el: Element) -> bool:
    try:
        code, _ = _api().AXUIElementCopyAttributeValue(el, "AXRole", None)
    except Exception:  # noqa: BLE001 this one never raises
        return False
    return code != INVALID_ELEMENT


def pid(el: Element) -> int:
    code, value = _api().AXUIElementGetPid(el, None)
    _check(code)
    return int(value)


def window_id(el: Element) -> int:
    call = _private("_AXUIElementGetWindow")
    if call is None:
        return 0
    import ctypes

    import objc

    call.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    call.restype = ctypes.c_int32
    number = ctypes.c_uint32(0)
    return 0 if call(objc.pyobjc_id(el), ctypes.byref(number)) else int(number.value)


def frame(el: Element) -> Rect | None:
    """Global points, origin top left."""
    api = _api()
    position, size = get_many(el, ("AXPosition", "AXSize"))
    if position is None or size is None:
        return None
    ok_p, point = api.AXValueGetValue(position, api.kAXValueCGPointType, None)
    ok_s, extent = api.AXValueGetValue(size, api.kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    return (point.x, point.y, extent.width, extent.height)


def at(app_el: Element | None, x: float, y: float) -> Element | None:
    """Hit test inside one app. With no app it asks the whole system."""
    api = _api()
    root = api.AXUIElementCreateSystemWide() if app_el is None else app_el
    code, found = api.AXUIElementCopyElementAtPosition(root, x, y, None)
    return None if code else found


class Observer:
    """Notifications of one process, delivered on the helper's main run loop."""

    def __init__(self, observer: Any, registered: list[tuple[Element, str]], keep: Any):
        self._observer, self._registered, self._keep = observer, registered, keep

    def stop(self) -> None:
        observer, self._observer = self._observer, None
        if observer is None:  # the second stop does nothing
            return
        from . import runloop

        api = _api()
        for el, name in self._registered:
            api.AXObserverRemoveNotification(observer, el, name)
        source = api.AXObserverGetRunLoopSource(observer)
        runloop.call_on_main(
            lambda: api.CFRunLoopRemoveSource(
                api.CFRunLoopGetMain(), source, api.kCFRunLoopDefaultMode
            )
        )


def _c_callback(function: Callable) -> Callable:
    """Mark a function as the C callback of an observer. A plain one is refused."""
    import objc

    return objc.callbackFor(_api().AXObserverCreate)(function)


def observe(
    pid: int, el: Element, names: Sequence[str], callback: Callable[[str, Any], None]
) -> Observer:
    """Hear `names` on `el`. The callback gets (name, element) and must only store."""
    from . import runloop

    api = _api()

    def heard(_observer: Any, element: Any, name: Any, _context: Any) -> None:
        callback(str(name), element)

    heard = _c_callback(heard)
    code, observer = api.AXObserverCreate(pid, heard, None)
    _check(code)
    registered = []
    for name in names:
        code = api.AXObserverAddNotification(observer, el, name, None)
        if code == CANNOT_COMPLETE:  # this app does not send that one: skip it
            continue
        _check(code)
        registered.append((el, name))
    source = api.AXObserverGetRunLoopSource(observer)
    runloop.call_on_main(
        lambda: api.CFRunLoopAddSource(
            api.CFRunLoopGetMain(), source, api.kCFRunLoopDefaultMode
        )
    )
    return Observer(observer, registered, heard)
