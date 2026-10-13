"""Fakes for the tests of settle.py, apps.py and windows.py."""

from __future__ import annotations

import contextlib
import importlib
import os
import plistlib
import signal
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path

from fakes import Clock

try:  # a stand-in until the capture module is written
    importlib.import_module("macos_harness.ax")
except ModuleNotFoundError as error:
    if error.name != "macos_harness.ax":
        raise
    sys.modules[error.name] = types.ModuleType(error.name)

from macos_harness import apps


class ScriptedClock(Clock):
    """A clock that runs what a test planned for a moment when a sleep passes it."""

    def __init__(self, now: float = 1000.0):
        super().__init__(now)
        self.start, self.plan, self.sleeps = now, [], []

    def at(self, offset: float, action) -> None:
        self.plan.append((self.start + offset, action))
        self.plan.sort(key=lambda entry: entry[0])

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        end = self.now + seconds
        while self.plan and self.plan[0][0] <= end:
            self.now, action = self.plan.pop(0)
            action()
        self.now = end

    @property
    def elapsed(self) -> float:
        return self.now - self.start


@dataclass(frozen=True)
class FakeProc(apps.Proc):
    regular: bool = True  # shows in the Dock


class FakeSystem:
    """Stands in for apps.SYSTEM: what runs, what is installed, what was used."""

    def __init__(self):
        self.procs: list[apps.Proc] = []
        self.by_bundle_id: dict[str, list[str]] = {}
        self.by_name: dict[str, str] = {}
        self.front: list[int] = []
        self.usage: list[apps.Used] = []
        self.opened: list[str] = []
        self.starts: apps.Proc | None = None  # what open() makes run

    def running(self):
        return list(self.procs)

    def runs(self, pid, path):
        paths = apps._spellings(path)
        return any(
            p.pid == pid and apps._proc_target(p).path in paths for p in self.procs
        )

    def regular(self, pid):
        return any(p.regular for p in self.procs if p.pid == pid)

    def bundle_paths(self, bundle_id):
        return list(self.by_bundle_id.get(bundle_id, []))

    def named(self, name):
        return self.by_name.get(name)

    def open(self, path):
        self.opened.append(path)
        if self.starts is None:
            return None
        self.procs.append(self.starts)
        return self.starts.pid

    def front_pids(self):
        return list(self.front)

    def used(self):
        return list(self.usage)


def proc(pid, name, path=None, bundle_id="", executable=None, regular=True):
    return FakeProc(pid, path, bundle_id, name, executable or name, regular)


def make_app(folder: Path, name: str, bundle_id: str | None = None, **info) -> str:
    """Write a small app bundle and give its path."""
    if bundle_id:
        info["CFBundleIdentifier"] = bundle_id
    contents = folder / f"{name}.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    return str(folder / f"{name}.app")


class FakeServer:
    """Stands in for the window server: rows as windows.server_windows gives them."""

    def __init__(self, *rows: dict):
        self.rows = list(rows)

    def add(self, window_id, pid, frame, title=None, layer=0, onscreen=True):
        row = {"id": window_id, "pid": pid, "frame": tuple(map(float, frame))}
        self.rows.append(row | {"title": title, "layer": layer, "onscreen": onscreen})
        return row

    def server_windows(self, pid=None):
        shown = [row for row in self.rows if row["onscreen"]]
        return [row for row in shown if pid is None or row["pid"] == pid]

    def server_window(self, window_id):
        return next((row for row in self.rows if row["id"] == window_id), None)


@contextlib.contextmanager
def background_app():
    """For live tests: one app, started in the background when it was not running.

    Only an app this test started is quit again. One that was running is left alone.
    MACOS_HARNESS_LIVE_APP names an app of your own (a path); the default is
    Dictionary.
    """
    import AppKit

    target = apps.resolve(os.environ.get("MACOS_HARNESS_LIVE_APP", "Dictionary"))
    started = target.pid is None
    if started:
        target = apps.launch(target)
    try:
        yield apps.running(target)
    finally:
        lookup = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_
        app = lookup(target.pid) if started else None
        if app is not None:
            app.terminate()
        elif started:  # this process has not heard of the app it started: ask plainly
            os.kill(target.pid, signal.SIGTERM)
        end = time.monotonic() + 3.0
        while started and target.pid in apps._pids() and time.monotonic() < end:
            time.sleep(0.05)
