"""ax.py against a fake of the system's accessibility calls, and live against one app."""

from __future__ import annotations

import inspect
import os
import sys
import time
import types

import pytest
from fakes import BackgroundApp

from macos_harness import ax, errors

OK, FAILURE, INVALID, CANNOT_COMPLETE, UNSUPPORTED, NO_VALUE = (
    0,
    -25200,
    -25202,
    -25204,
    -25205,
    -25212,
)


class Placeholder:
    """What a batch read puts where one name failed."""

    def __init__(self, code: int):
        self.code = code


class System:
    """The calls ax.py makes, answering from a dict per element."""

    kAXValueCGPointType, kAXValueCGSizeType = 1, 2
    kCFRunLoopDefaultMode = "default"

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: dict[str, int] = {}  # call name -> the code it answers
        self.observed: list[tuple] = []

    def _log(self, name: str, *args) -> int:
        self.calls.append((name, *args))
        return self.fail.get(name, OK)

    def AXUIElementCreateApplication(self, pid):
        return {"pid": pid}

    def AXUIElementCreateSystemWide(self):
        return {"system": True}

    def AXUIElementCopyAttributeValue(self, el, name, _):
        code = self._log("get", name) or el.get("codes", {}).get(name, OK)
        return code, None if code else el.get(name)

    def AXUIElementCopyMultipleAttributeValues(self, el, names, options, _):
        code = self._log("get_many", tuple(names), options)
        codes = el.get("codes", {})
        return code, [Placeholder(codes[n]) if n in codes else el.get(n) for n in names]

    def AXUIElementCopyAttributeNames(self, el, _):
        return self._log("names"), [k for k in el if k.startswith("AX")]

    def AXUIElementSetAttributeValue(self, el, name, value):
        el[name] = value
        return self._log("set", name, value)

    def AXUIElementIsAttributeSettable(self, el, name, _):
        return self._log("settable", name), name in el.get("settable", ())

    def AXUIElementCopyActionNames(self, el, _):
        return self._log("action names"), list(el.get("actions", {}))

    def AXUIElementCopyActionDescription(self, el, name, _):
        return self._log("describe", name), el["actions"][name]

    def AXUIElementPerformAction(self, el, name):
        return self._log("perform", name)

    def AXUIElementCopyParameterizedAttributeValue(self, el, name, arg, _):
        return self._log("param", name, arg), (name, arg)

    def AXUIElementCopyElementAtPosition(self, root, x, y, _):
        return self._log("at", tuple(root), x, y), {"hit": (x, y)}

    def AXUIElementGetPid(self, el, _):
        return self._log("pid"), el.get("pid", 0)

    def CFEqual(self, a, b):
        return a == b

    def CFGetTypeID(self, value):
        return "error" if isinstance(value, Placeholder) else type(value).__name__

    def AXValueGetTypeID(self):
        return "error"

    def AXValueGetType(self, value):
        return ax.ERROR_VALUE

    def AXValueGetValue(self, value, kind, _):
        return True, value

    def AXObserverCreate(self, pid, callback, _):
        self.callback = callback
        return self._log("observer", pid), {"observer": pid}

    def AXObserverAddNotification(self, observer, el, name, _):
        self.calls.append(("add", name))
        return el.get("codes", {}).get(name, OK)

    def AXObserverRemoveNotification(self, observer, el, name):
        self.calls.append(("remove", name))
        return OK

    def AXObserverGetRunLoopSource(self, observer):
        return "source"

    def CFRunLoopGetMain(self):
        return "main loop"

    def CFRunLoopAddSource(self, loop, source, mode):
        self.calls.append(("add source", loop, source))

    def CFRunLoopRemoveSource(self, loop, source, mode):
        self.calls.append(("remove source", loop, source))


@pytest.fixture
def system(monkeypatch):
    fake = System()
    monkeypatch.setattr(ax, "_api", lambda: fake)
    return fake


def name_of(failure) -> str:
    return failure.value.message


def test_read_each_node_batched_call(system):
    el = {"AXRole": "AXButton", "AXTitle": "OK", "codes": {"AXHelp": UNSUPPORTED}}
    assert ax.get_many(el, ["AXRole", "AXHelp", "AXTitle"]) == ["AXButton", None, "OK"]
    assert system.calls == [
        ("get_many", ("AXRole", "AXHelp", "AXTitle"), 0)
    ]  # one call


def test_remove_single_read_fallback_top(system):
    system.fail["get_many"] = CANNOT_COMPLETE
    with pytest.raises(errors.HarnessError) as failure:
        ax.get_many({}, ["AXRole", "AXTitle"])
    assert (
        name_of(failure)
        == "The app did not answer an accessibility request (AXError.cannotComplete). Call `get_app_state` again; if it fails again, click by pixel or tell the user."
    )
    assert len(system.calls) == 1  # no retry name by name


def test_bulk_hierarchy_read_through_private_accessibility_call_plus(
    system, monkeypatch
):
    """Off by default: callers fall back to get_many and nothing is looked up."""
    monkeypatch.setattr(ax, "_hierarchy", lambda: pytest.fail("looked up while off"))
    assert ax.SUBTREE is False and ax.subtree({}, ["AXRole"]) is None


def test_the_copy_is_read_as_elements_with_their_values(system, monkeypatch):
    """As measured on Lab: {element: {name: {"value", "count"}}}, a name the element
    lacks is left out. An element whose children were cut is left to get_many."""
    asked = []

    def copy(el, names, options, _):
        asked.append((el, names, options))
        return 0, {
            "window": {
                "AXRole": {"value": "AXWindow"},
                "AXChildren": {"value": ["a"], "count": 1},
            },
            "a": {
                "AXRole": {"value": "AXButton"},
                "AXChildren": {"value": [], "count": 0},
            },
            "big": {
                "AXRole": {"value": "AXList"},
                "AXChildren": {"value": ["x"] * 2, "count": 300},
            },
        }

    keys = {k: "key " + k for k in ax.HIERARCHY_KEYS}
    monkeypatch.setattr(ax, "SUBTREE", True)
    monkeypatch.setattr(ax, "_hierarchy", lambda: (copy, keys))
    got = ax.subtree("window", ["AXRole", "AXTitle", "AXChildren"], 2)
    assert got == [("window", ["AXWindow", None, ["a"]]), ("a", ["AXButton", None, []])]
    assert asked == [
        (
            "window",
            ["AXRole", "AXTitle", "AXChildren"],
            {
                "key ArrayAttributes": ["AXChildren"],
                "key SkipInspectionForAttributes": ax.SUBTREE_SKIP,
                "key MaxArrayCount": 2,
            },
        )
    ]
    monkeypatch.setattr(ax, "_hierarchy", lambda: (lambda *a: (-25204, None), keys))
    assert ax.subtree("window", ["AXRole"]) is None  # a failed copy: get_many
    monkeypatch.setattr(ax, "_hierarchy", lambda: None)
    assert ax.subtree("window", ["AXRole"]) is None  # no such call on this system


def test_whole_subtrees_fetched_private_call_options():
    assert inspect.signature(ax.subtree).parameters["max_children"].default == 100


def test_partial_array_reads_max_count():
    assert ax.MAX_ARRAY == 100


def test_a_missing_private_call_switches_its_step_off(monkeypatch):
    ax._private.cache_clear()
    monkeypatch.setattr(ax, "HISERVICES", "/nowhere/Missing.framework/Missing")
    assert (
        ax._private("AnyCall") is None
    )  # a missing private call switches its step off
    assert ax.window_id({"any": "element"}) == 0
    ax._private.cache_clear()


def test_unsupported_attributes_remembered_so_they_asked_again(system):
    el = {"AXRole": "AXGroup", "codes": {"AXTitle": UNSUPPORTED, "AXValue": NO_VALUE}}
    assert ax.get(el, "AXTitle") is None and ax.get(el, "AXValue") is None
    assert ax.get(el, "AXMissing") is None and ax.get(el, "AXRole") == "AXGroup"
    el["codes"]["AXRole"] = FAILURE
    assert ax.get(el, "AXRole") is None  # a plain failure of one name is "missing" too


@pytest.mark.parametrize("code", [INVALID, CANNOT_COMPLETE, -25211])
def test_get_raises_when_the_app_cannot_answer(system, code):
    with pytest.raises(errors.HarnessError) as failure:
        ax.get({"codes": {"AXRole": code}}, "AXRole")
    assert name_of(failure) == errors.accessibility(ax.error_name(code)).message


def test_no_ax_messaging_timeout_is_set():
    source = inspect.getsource(ax)
    assert "SetMessagingTimeout" not in source  # the system default applies


def test_error_names():
    assert ax.error_name(-25204) == "AXError.cannotComplete"
    assert ax.error_name(-25202) == "AXError.invalidUIElement"
    assert ax.error_name(-25212) == "AXError.noValue"
    assert ax.error_name(-1) == "(undocumented AXError)"


def test_set_settable_perform_param(system):
    el = {"settable": ["AXValue"]}
    ax.set(el, "AXValue", "x")
    ax.perform(el, "AXPress")
    assert el["AXValue"] == "x" and ax.settable(el, "AXValue")
    assert not ax.settable(el, "AXTitle")
    assert ax.param(el, "AXStringForRange", (0, 2)) == ("AXStringForRange", (0, 2))
    for call in ("set", "perform"):
        system.fail[call] = INVALID
    with pytest.raises(errors.HarnessError):
        ax.set(el, "AXValue", "y")
    with pytest.raises(errors.HarnessError):
        ax.perform(el, "AXPress")
    system.fail.update(settable=INVALID, param=UNSUPPORTED)
    assert ax.settable(el, "AXValue") is False and ax.param(el, "AXX", 1) is None


def test_actions_read_a_description_only_when_asked(system):
    found = ax.actions(
        {"actions": {"AXPress": "press", "AXZoomWindow": "zoom the window"}}
    )
    assert list(found) == ["AXPress", "AXZoomWindow"] and "AXPress" in found
    assert system.calls == [("action names",)]  # no description read yet
    assert found["AXZoomWindow"] == "zoom the window" and found["AXZoomWindow"]
    assert system.calls.count(("describe", "AXZoomWindow")) == 1  # read once
    assert dict(found) == {"AXPress": "press", "AXZoomWindow": "zoom the window"}
    with pytest.raises(KeyError):
        found["AXNope"]
    system.fail["action names"] = CANNOT_COMPLETE
    assert dict(ax.actions({"actions": {"AXPress": "press"}})) == {}


def test_names_same_alive_pid_at(system):
    el = {"AXRole": "AXButton", "AXTitle": "OK", "pid": 42}
    assert ax.names(el) == ["AXRole", "AXTitle"]
    assert ax.same(el, dict(el)) and not ax.same(el, None) and not ax.same(el, {})
    assert ax.alive(el) and ax.pid(el) == 42
    assert not ax.alive({"codes": {"AXRole": INVALID}})
    assert ax.alive({"codes": {"AXRole": CANNOT_COMPLETE}})  # busy is not gone
    assert ax.at(None, 3.0, 4.0) == {"hit": (3.0, 4.0)}
    assert system.calls[-1] == ("at", ("system",), 3.0, 4.0)  # no app: the whole system
    system.fail["at"] = FAILURE
    assert ax.at(el, 1, 2) is None


def test_frame_is_position_and_size(system):
    point = types.SimpleNamespace(x=60.0, y=58.0)
    size = types.SimpleNamespace(width=560.0, height=592.0)
    assert ax.frame({"AXPosition": point, "AXSize": size}) == (60.0, 58.0, 560.0, 592.0)
    assert ax.frame({"AXPosition": point}) is None


def test_observe_skips_what_the_app_does_not_send(system, monkeypatch):
    ran = []
    runloop = types.SimpleNamespace(call_on_main=lambda fn: ran.append(fn()))
    monkeypatch.setitem(__import__("sys").modules, "macos_harness.runloop", runloop)
    monkeypatch.setattr("macos_harness.runloop", runloop, raising=False)
    monkeypatch.setattr(ax, "_c_callback", lambda function: function)
    el = {"codes": {"AXMenuOpened": CANNOT_COMPLETE}}
    heard = []
    observer = ax.observe(
        7, el, ["AXCreated", "AXMenuOpened"], lambda n, e: heard.append((n, e))
    )
    assert ("add source", "main loop", "source") in system.calls  # on the main run loop
    system.callback(None, "element", "AXCreated", None)
    assert heard == [("AXCreated", "element")]
    observer.stop()
    observer.stop()  # twice is fine
    assert system.calls.count(("remove", "AXCreated")) == 1
    assert ("remove", "AXMenuOpened") not in system.calls  # it was never registered
    el["codes"]["AXCreated"] = INVALID
    with pytest.raises(errors.HarnessError):
        ax.observe(7, el, ["AXCreated"], lambda n, e: None)


def test_platform_fact_ax_read_cost_has_regimes():
    assert "AXChildren" in __import__("macos_harness.tree", fromlist=["x"]).ATTRIBUTES


def test_platform_fact_tree_sizes_depths_real_apps():
    assert __import__("macos_harness.tree", fromlist=["x"]).MAX_DEPTH >= 60


@pytest.mark.live
def test_real_calls_on_this_process_parent_app():
    """Read our own process tree's first app with a window: no input, only reads."""
    from AppKit import NSWorkspace

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    app = ax.app(int(front.processIdentifier()))
    role, title = ax.get_many(app, ["AXRole", "AXTitle"])
    assert role == "AXApplication" and isinstance(title, str)
    assert ax.alive(app) and ax.same(app, ax.app(int(front.processIdentifier())))
    assert ax.pid(app) == int(front.processIdentifier()) and "AXRole" in ax.names(app)
    assert ax.get(app, "AXNoSuchAttribute") is None
    windows = ax.get(app, "AXWindows") or []
    if windows:
        assert ax.window_id(windows[0]) > 0 and len(ax.frame(windows[0])) == 4
    assert os.getpid() != ax.pid(app)


@pytest.fixture
def test_app(tmp_path):
    """Our own instance of the test app, in the background. It is ended after the test."""
    script = os.environ.get("MACOS_HARNESS_TESTAPP")
    if not script:
        pytest.skip("live test: set MACOS_HARNESS_TESTAPP to the test app's main file")
    command = [sys.executable, script, str(tmp_path), "200", "-2500"]
    process = BackgroundApp(command, tmp_path)
    deadline = time.time() + 15
    while not (tmp_path / "layout.json").exists():
        assert time.time() < deadline and process.running(), (
            "the test app did not start"
        )
        time.sleep(0.05)
    time.sleep(0.5)
    yield process.pid
    (tmp_path / "cmd.txt").write_text("quit")
    if not process.wait(5):
        process.kill()


@pytest.mark.live
def test_observe_hears_a_real_app(test_app):
    """One real notification arrives through the main run loop. No input is sent."""
    from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

    app = ax.app(test_app)
    window = ax.get(app, "AXWindows")[0]
    field = next(
        el
        for el in ax.get(window, "AXChildren")
        if ax.get(el, "AXRole") == "AXTextField"
    )
    heard = []
    names = ["AXValueChanged", "AXNoSuchNotification"]  # the second one is refused
    observer = ax.observe(
        test_app, app, names, lambda name, el: heard.append((name, el))
    )
    try:
        ax.set(field, "AXValue", "heard by the observer")
        deadline = time.time() + 5
        while not heard and time.time() < deadline:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
    finally:
        observer.stop()
        observer.stop()
    assert heard and heard[0][0] == "AXValueChanged" and ax.same(heard[0][1], field)


def test_the_shared_fake_has_every_function_of_this_module():
    """Every other module is tested against tests/fakes.FakeAx: it must not drift."""
    from fakes import FakeAx

    public = {
        name: found
        for name, found in vars(ax).items()
        if inspect.isfunction(found) and found.__module__ == ax.__name__
        if not name.startswith("_")
    }
    assert len(public) == 18
    for name, real in public.items():
        fake = list(inspect.signature(getattr(FakeAx, name)).parameters)[1:]
        assert len(fake) == len(inspect.signature(real).parameters), name
