"""Fakes that the tests of every module share.

FakeAx has exactly the functions of ax.py. A test builds a small tree of El objects and
puts FakeAx in place of the real module: monkeypatch.setattr(some_module, "ax", fakes.FakeAx()).
The fakes of one group of modules are in tests/fakes_<letter>.py.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BackgroundApp:
    """A program of a live test, started as an app in the background.

    A window program started as a child of the front app (the terminal the tests run
    in) is put in front by the system. Opened this way it never is, so a live test
    neither needs the front nor takes the person's keyboard.
    """

    def __init__(self, command: list[str], folder: Path, name: str = "LiveTestApp"):
        bundle = folder / f"{name}.app"
        run = bundle / "Contents" / "MacOS" / "run"
        run.parent.mkdir(parents=True)
        pid_file = folder / f"{name}.pid"
        # The shell becomes the program, so the number it wrote stays the right one.
        run.write_text(
            f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\n"
            f"exec {shlex.join(command)}\n"
        )
        run.chmod(0o755)
        info = {
            "CFBundleExecutable": "run",
            "CFBundleIdentifier": f"dev.macos-harness.test.{name.lower()}",
            "CFBundleName": name,
            "CFBundlePackageType": "APPL",
        }
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["/usr/bin/open", "-g", "-n", str(bundle)], check=True)
        deadline = time.time() + 10
        while not (pid_file.exists() and pid_file.read_text().strip()):
            if time.time() > deadline:
                raise RuntimeError(f"{name} did not start")
            time.sleep(0.02)
        self.pid = int(pid_file.read_text())

    def running(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait(self, seconds: float) -> bool:
        """True when the program ended within this time."""
        deadline = time.time() + seconds
        while self.running() and time.time() < deadline:
            time.sleep(0.05)
        return not self.running()

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)


@dataclass(eq=False)
class El:
    """One fake accessibility element. Two El are the same element only when they are the same object."""

    role: str = "AXGroup"
    attrs: dict[str, Any] = field(default_factory=dict)  # "AXTitle": "OK", ...
    children: list[El] = field(default_factory=list)
    acts: dict[str, str] = field(default_factory=dict)  # "AXPress": "press"
    settable_names: set[str] = field(default_factory=set)
    owner_pid: int = 100
    win_id: int = 1
    rect: tuple[float, float, float, float] | None = None
    dead: bool = False
    performed: list[str] = field(default_factory=list)  # what a test asserts on

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Clock:
    """A clock a test moves by hand. Pass it where a module takes `clock=`."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    monotonic = time

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeObserver:
    def __init__(self, names: Sequence[str], callback: Callable[[str, Any], None]):
        self.names, self.callback, self.stopped = list(names), callback, False

    def fire(self, name: str, element: Any = None) -> None:
        if not self.stopped and name in self.names:
            self.callback(name, element)

    def stop(self) -> None:
        self.stopped = True


class FakeAx:
    """Stands in for the ax module. `apps` maps a pid to its application element."""

    def __init__(self, apps: dict[int, El] | None = None):
        self.apps = apps or {}
        self.observers: list[FakeObserver] = []

    def app(self, pid: int) -> El:
        return self.apps[pid]

    def get(self, el: El, name: str) -> Any | None:
        if name == "AXRole":
            return el.role
        if name == "AXChildren":
            return el.children
        return el.attrs.get(name)

    def names(self, el: El) -> list[str]:
        """What the element lists. `unlisted` in attrs names what it only answers for."""
        return ["AXRole", *(set(el.attrs) - set(el.attrs.get("unlisted", ())))]

    def get_many(self, el: El, names: Sequence[str]) -> list[Any | None]:
        return [self.get(el, name) for name in names]

    def set(self, el: El, name: str, value: Any) -> None:
        el.attrs[name] = value
        el.performed.append(f"set {name}")

    def settable(self, el: El, name: str) -> bool:
        return name in el.settable_names

    def actions(self, el: El) -> dict[str, str]:
        return dict(el.acts)

    def perform(self, el: El, action: str) -> None:
        el.performed.append(action)

    def param(self, el: El, name: str, arg: Any) -> Any | None:
        value = el.attrs.get(name)
        return value(arg) if callable(value) else value

    def subtree(
        self, el: El, names: Sequence[str], max_children: int = 100
    ) -> list | None:
        # the private one-call copy is "missing": callers must fall back to get_many
        return None

    def same(self, a: Any, b: Any) -> bool:
        return a is b

    def alive(self, el: El) -> bool:
        return not el.dead

    def pid(self, el: El) -> int:
        return el.owner_pid

    def window_id(self, el: El) -> int:
        return el.win_id

    def frame(self, el: El):
        return el.rect

    def at(self, app_el: El | None, x: float, y: float) -> El | None:
        roots = [app_el] if app_el else list(self.apps.values())
        hit = None
        for root in roots:
            for el in root.walk():
                r = el.rect
                if r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                    hit = el  # the deepest one wins: walk is depth first
        return hit

    def observe(
        self,
        pid: int,
        el: El,
        names: Sequence[str],
        callback: Callable[[str, Any], None],
    ) -> FakeObserver:
        observer = FakeObserver(names, callback)
        self.observers.append(observer)
        return observer

    def error_name(self, code: int) -> str:
        return {
            -25204: "AXError.cannotComplete",
            -25202: "AXError.invalidUIElement",
        }.get(code, "(undocumented AXError)")
