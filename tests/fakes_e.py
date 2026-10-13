"""Fakes for the tests of the input ladder: events that are only written down,
windows and apps that are given, a pointer that records where it was sent."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

from fakes import BackgroundApp, Clock, El, FakeAx

from macos_harness import errors
from macos_harness.model import (
    AgentSession,
    AppKind,
    AppSession,
    AppTarget,
    Line,
    WindowRef,
)

NEIGHBOURS = ("ax", "apps", "windows", "richtext", "presence", "runloop")
APP_PID, OTHER_PID = 100, 999


def load(name: str):
    """Import one of our modules. A neighbour that is not written yet is an empty
    module: every test puts its own fake in its place anyway."""
    package = importlib.import_module("macos_harness")
    for neighbour in NEIGHBOURS:
        full = f"macos_harness.{neighbour}"
        if full not in sys.modules and importlib.util.find_spec(full) is None:
            stub = types.ModuleType(full)
            stub.call_on_main = lambda fn: fn()
            sys.modules[full] = stub
            setattr(package, neighbour, stub)
    return importlib.import_module(f"macos_harness.{name}")


def link(root: El) -> El:
    """Give every child its parent, as the real tree has it."""
    for child in root.children:
        child.attrs["AXParent"] = root
        link(child)
    return root


class Events:
    """Stands in for the events module. Every event is a dict, every post is kept
    with its pid, its delay and the time on the fake clock."""

    OWN_TAG, CLICK_HOLD, MODIFIERS = 1, 0.05, 0x1E0000

    def __init__(self, clock: Clock, app: El | None = None):
        self.clock, self.app, self.posted, self.held = clock, app, [], 0

    def mouse(self, kind, point, window, button=0, click=1, flags=0):
        return {
            "type": kind,
            "point": point,
            "window": window.window_id,
            "button": button,
            "click": click,
            "flags": flags,
        }

    def wheel(self, point, dx, dy, window):
        w = window.window_id
        return {"type": "wheel", "point": point, "dx": dx, "dy": dy, "window": w}

    def key(self, code, flags, text=None, down=True):
        kind = "key_down" if down else "key_up"
        return {"type": kind, "code": code, "flags": flags, "text": text}

    def flags_changed(self, flags):
        return {"type": "flags", "flags": flags}

    def notice(self, kind, subtype, window_id=0, flags=0):
        row = {"type": "notice", "kind": kind, "subtype": subtype}
        return {**row, "window": window_id, "flags": flags}

    def held_flags(self):
        return self.held

    def private(self, name, signature):
        return None

    def post(self, pid, event, delay=0.0):
        self.posted.append({**event, "pid": pid, "delay": delay, "t": self.clock.now})
        if event["type"] == "notice" and event["kind"] == 13 and self.app is not None:
            # the fake app believes what it is told, and its windows follow
            for el in (self.app, *self.app.children):
                el.attrs["AXFrontmost" if el is self.app else "AXMain"] = (
                    event["subtype"] == 1
                )
        self.clock.sleep(delay)

    def types(self) -> list[str]:
        return [event["type"] for event in self.posted]


class Windows:
    """Stands in for the windows module: one window, and the menu a press opens."""

    def __init__(self, window: WindowRef):
        self.window, self.menu, self.opens, self.calls = window, None, {}, []

    def at(self, target, point):
        x, y, w, h = self.window.frame
        if not (x <= point[0] < x + w and y <= point[1] < y + h):
            raise errors.window_not_found_at(point)
        return self.window

    def ref(self, element):
        return self.window

    def key_window(self, target):
        return self.window

    def open_menu(self, target):
        return self.menu

    shown_menu = open_menu

    def wait_for_menu(self, target, opener):
        self.calls.append(("wait", opener))
        self.menu = self.opens.get(id(opener))
        return self.menu

    def keep_menu(self, target, title):
        self.calls.append(("keep", title))
        self.menu = title

    def close_menu(self, target):
        self.calls.append("close")
        self.menu = None


class Apps:
    FILE_PANELS = frozenset({"save-panel", "open-panel", "GoToWindow"})

    def __init__(self, kind: AppKind | None = None):
        self.app_kind = kind or AppKind()
        self.service: int | None = None  # the pid of the app's file panel service
        self.bundles: dict[
            int, str
        ] = {}  # pid -> bundle id, for processes that have one

    def kind(self, target):
        return self.app_kind

    def panel_service(self, pid):
        return self.service

    def bundle_id(self, pid):
        return self.bundles.get(pid, "")


class Policy:
    """Refuses the bundle ids in `refused`, and remembers every one it was asked about."""

    def __init__(self):
        self.asked, self.refused = [], set()

    def check_recipient(self, bundle_id):
        self.asked.append(bundle_id)
        if bundle_id in self.refused:
            raise errors.security_process(bundle_id)


class Presence:
    def __init__(self):
        self.calls, self.windows = [], []

    def pointer_move(self, point, window=None):
        self.calls.append(("move", point))
        self.windows.append(window)

    def pointer_press(self, count=1):
        self.calls.append(("press", count))

    def flash(self, rect, window=None):
        self.calls.append(("flash", rect))
        self.windows.append(window)


class Richtext:
    """Finds the one place where prefix + target + suffix stands in a plain text."""

    def find(self, text, offsets, target, prefix="", suffix=""):
        needle = prefix + target + suffix
        if text.count(needle) != 1:
            return None
        start = text.find(needle) + len(prefix)
        return (start, start + len(target))


class Tree:
    """A plain text element: what is printed is its value, with no offsets."""

    def __init__(self, ax):
        self.ax = ax

    def printed_text(self, el):
        return str(self.ax.get(el, "AXValue") or ""), None


class RefusingAx(FakeAx):
    """An app that answers the named actions and attribute writes with an error.
    `refuse` is a list of names, or a dict name -> error name."""

    def __init__(self, apps, refuse=()):
        super().__init__(apps)
        unsupported = dict.fromkeys(refuse, "AXError.actionUnsupported")
        self.refuse = refuse if isinstance(refuse, dict) else unsupported

    def perform(self, el, action):
        if action in self.refuse:
            raise errors.accessibility(self.refuse[action])
        super().perform(el, action)

    def set(self, el, name, value):
        if name in self.refuse:
            raise errors.accessibility(self.refuse[name])
        super().set(el, name, value)

    def param(self, el, name, arg):
        if name not in el.attrs:
            raise errors.accessibility("AXError.parameterizedAttributeUnsupported")
        return super().param(el, name, arg)


@dataclass
class Rig:
    """One fake app with one window at (100, 100), 600 by 400, and every module the
    ladder calls replaced by a fake."""

    actions: types.ModuleType
    focus: types.ModuleType
    ax: RefusingAx
    events: Events
    windows: Windows
    apps: Apps
    presence: Presence
    clock: Clock
    app: AppSession
    root: El
    window: El
    front: list[int] = field(default_factory=lambda: [OTHER_PID])
    background: list = field(default_factory=list)  # focus's background watchers

    def add(self, el: El) -> El:
        self.window.children.append(el)
        link(self.root)
        return el


def rig(monkeypatch, kind: AppKind | None = None, refuse=()) -> Rig:
    actions, focus = load("actions"), load("focus")
    clock = Clock()
    window_el = El("AXWindow", rect=(100, 100, 600, 400))
    window_el.attrs["AXActivationPoint"] = (130.0, 116.0)
    root = link(El("AXApplication", children=[window_el]))
    root.attrs["AXWindows"] = [window_el]
    root.attrs["AXMainWindow"] = window_el
    ax = RefusingAx({APP_PID: root}, refuse)
    events = Events(clock, root)
    ref = WindowRef(window_el, 7, APP_PID, "Fixture", (100, 100, 600, 400))
    made = Rig(
        actions=actions,
        focus=focus,
        ax=ax,
        events=events,
        windows=Windows(ref),
        apps=Apps(kind),
        presence=Presence(),
        clock=clock,
        app=AppSession(
            AgentSession("agent"),
            AppTarget("/Applications/Fixture.app", "test.fixture", "Fixture", APP_PID),
        ),
        root=root,
        window=window_el,
    )
    for name in ("ax", "events", "windows", "apps", "presence"):
        monkeypatch.setattr(actions, name, getattr(made, name))
    made.policy = Policy()
    monkeypatch.setattr(actions, "policy", made.policy)
    monkeypatch.setattr(actions, "richtext", Richtext())
    monkeypatch.setattr(actions, "tree", Tree(ax))
    monkeypatch.setattr(actions, "time", clock)
    monkeypatch.setattr(actions, "_range", lambda location, length: (location, length))
    for name in ("ax", "events"):
        monkeypatch.setattr(focus, name, getattr(made, name))
    monkeypatch.setattr(focus, "time", clock)
    monkeypatch.setattr(focus, "front_pid", lambda: made.front[0])
    monkeypatch.setattr(focus, "_ENFORCERS", {})
    monkeypatch.setattr(focus, "_background", made.background.append)
    monkeypatch.setattr(focus, "_person_went", lambda pid, since: False)  # nobody there
    return made


# --- live: a real app of our own, driven in the background --------------------------


class LiveApp:
    """Our own instance of the event-logging test app. It never activates itself.
    Files in its folder: events.jsonl (every event and action it saw), layout.json,
    state.json (written after each command), cmd.txt (polled)."""

    def __init__(self, folder: Path, process):
        self.folder, self.process = folder, process
        self.layout = json.loads((folder / "layout.json").read_text())
        self.pid, self.seen = self.layout["pid"], 0
        target = AppTarget(f"#pid:{self.pid}", "", "test app", self.pid)
        self.session = AppSession(AgentSession("live"), target)

    @classmethod
    def start(cls, script: str, origin=(200, -2500)) -> LiveApp:
        folder = Path(tempfile.mkdtemp(prefix="mh-live-"))
        command = [sys.executable, script, str(folder), str(origin[0]), str(origin[1])]
        process = BackgroundApp(command, folder)
        deadline = time.time() + 15
        while not (folder / "layout.json").exists():
            if time.time() > deadline or not process.running():
                raise RuntimeError("the test app did not start")
            time.sleep(0.05)
        time.sleep(0.5)
        return cls(folder, process)

    def walk(self, el=None, depth=0):
        ax = load("ax")
        el = ax.app(self.pid) if el is None else el
        yield el
        if depth < 12:
            for child in ax.get(el, "AXChildren") or []:
                yield from self.walk(child, depth + 1)

    def find(self, **want):
        """The first element whose attributes match: find(Identifier="btn1")."""
        ax = load("ax")
        for el in self.walk():
            if all(ax.get(el, "AX" + name) == value for name, value in want.items()):
                return el
        raise LookupError(want)

    def line(self, el, **more) -> Line:
        return Line(1, "", el, str(load("ax").get(el, "AXRole")), **more)

    def point(self, name: str) -> tuple[float, float]:
        x, y = self.layout["points"][name]
        return (float(x), float(y))

    def command(self, name: str) -> dict:
        """Send one command to the app and return the state it wrote after it."""
        path = self.folder / "state.json"
        before = path.stat().st_mtime_ns if path.exists() else 0
        (self.folder / "cmd.txt").write_text(name)
        deadline = time.time() + 5
        while not path.exists() or path.stat().st_mtime_ns == before:
            if time.time() > deadline:
                raise TimeoutError(f"the test app did not answer {name!r}")
            time.sleep(0.02)
        return json.loads(path.read_text())

    def state(self) -> dict:
        return self.command("state")

    def mark(self) -> None:
        self.seen = (self.folder / "events.jsonl").stat().st_size

    def since(self) -> list[dict]:
        """What the app logged since mark()."""
        text = (self.folder / "events.jsonl").read_bytes()[self.seen :].decode()
        return [json.loads(row) for row in text.splitlines()]

    def open_menus(self) -> list:
        """Menus the app shows right now, the ones of the menu bar left out."""
        ax, found = load("ax"), []
        for el in self.walk():
            parent = ax.get(el, "AXParent")
            role = str(ax.get(parent, "AXRole")) if parent is not None else ""
            if str(ax.get(el, "AXRole")) == "AXMenu" and "AXMenu" not in role:
                found.append(el)
        return found

    def quit(self) -> None:
        for menu in self.open_menus():  # whatever a test did: no menu stays open
            load("ax").perform(menu, "AXCancel")
        (self.folder / "cmd.txt").write_text("quit")
        if not self.process.wait(5):
            self.process.kill()


class Watch:
    """Samples the real pointer every millisecond and reads the front app before
    and after. `person` says the hardware saw input meanwhile: then a moved pointer
    or a changed front app proves nothing and the action is run again."""

    def __enter__(self):
        self.start, self.moved, self.began = self.where(), 0.0, time.monotonic()
        self.front = load("focus").front_pid()
        self.running = True
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        return self

    def where(self) -> tuple[float, float]:
        import Quartz

        at = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (at.x, at.y)

    def sample(self) -> None:
        while self.running:
            x, y = self.where()
            self.moved = max(
                self.moved, abs(x - self.start[0]) + abs(y - self.start[1])
            )
            time.sleep(0.001)

    def __exit__(self, *exc) -> None:
        import Quartz

        time.sleep(0.3)  # what the action caused has happened by now
        self.running = False
        self.thread.join()
        took = time.monotonic() - self.began
        hardware = Quartz.kCGEventSourceStateHIDSystemState
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(hardware, 0xFFFFFFFF)
        self.person = idle < took + 0.1
        self.front_after = load("focus").front_pid()
        self.clean = self.moved < 0.5 and self.front_after == self.front
