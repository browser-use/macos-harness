"""Lazy bridge to the Browser Harness Python SDK."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import time
from types import ModuleType
from typing import Any


def _approve_chrome_prompt(stop: threading.Event, timeout: float) -> None:
    """Press Chrome's exact remote-debugging Allow button through system-wide AX."""
    if platform.system() != "Darwin":
        return
    try:
        import ApplicationServices as AS
    except ImportError:  # pragma: no cover - macOS dependency failure
        return

    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        root = AS.AXUIElementCreateSystemWide()
        pressed = False
        windows = AS.CGWindowListCopyWindowInfo(
            AS.kCGWindowListOptionOnScreenOnly, AS.kCGNullWindowID
        )
        for window in windows:
            if window.get("kCGWindowOwnerName") != "Google Chrome":
                continue
            if window.get("kCGWindowName") != "Allow remote debugging?":
                continue
            bounds = window.get("kCGWindowBounds") or {}
            left = float(bounds.get("X", 0))
            top = float(bounds.get("Y", 0))
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            points = [(left + width - 58, top + height - 38)]
            points.extend(
                (x, y)
                for y in range(int(top + height - 16), int(top + height - 96), -16)
                for x in range(int(left + width - 16), int(left), -16)
            )
            for x, y in points:
                error, element = AS.AXUIElementCopyElementAtPosition(root, x, y, None)
                if error != 0 or element is None:
                    continue
                role_error, role = AS.AXUIElementCopyAttributeValue(
                    element, "AXRole", None
                )
                description_error, description = AS.AXUIElementCopyAttributeValue(
                    element, "AXDescription", None
                )
                if (
                    role_error == 0
                    and description_error == 0
                    and str(role) == "AXButton"
                    and str(description) == "Allow"
                ):
                    AS.AXUIElementPerformAction(element, "AXPress")
                    pressed = True
                    break
            if pressed:
                break
        stop.wait(0.2 if pressed else 0.05)


def _watch_chrome_prompts(timeout: float) -> None:
    """Start AX only after Chrome creates its protected permission window."""
    if platform.system() != "Darwin":
        return
    try:
        import ApplicationServices as AS
    except ImportError:  # pragma: no cover - macOS dependency failure
        return

    source = (
        "import threading; "
        "from macos_harness.browser import _approve_chrome_prompt; "
        "_approve_chrome_prompt(threading.Event(), 0.8)"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = AS.CGWindowListCopyWindowInfo(
            AS.kCGWindowListOptionOnScreenOnly, AS.kCGNullWindowID
        )
        prompt_exists = any(
            str(window.get("kCGWindowOwnerName") or "") == "Google Chrome"
            and str(window.get("kCGWindowName") or "") == "Allow remote debugging?"
            for window in windows
        )
        if prompt_exists:
            subprocess.run(
                [sys.executable, "-c", source],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        time.sleep(0.05)


class BrowserHarness:
    """Expose Browser Harness helpers without connecting until first use."""

    def __init__(self, *, name: str | None = None, wait: float = 60.0) -> None:
        self.name = name
        self.wait = wait
        self._helpers: ModuleType | None = None

    def connect(self) -> BrowserHarness:
        if self._helpers is not None:
            return self

        if self.name:
            os.environ["BU_NAME"] = self.name

        try:
            from browser_harness import admin, helpers
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError(
                "Browser Harness is unavailable. Install the project dependencies "
                "with `uv sync`."
            ) from exc

        approver = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from macos_harness.browser import _watch_chrome_prompts; "
                    f"_watch_chrome_prompts({self.wait!r})"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            admin.ensure_daemon(wait=self.wait, name=self.name)
        finally:
            if approver.poll() is None:
                approver.terminate()
                try:
                    approver.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    approver.kill()
        if self.name:
            helpers.NAME = self.name
        self._helpers = helpers
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self.connect()
        assert self._helpers is not None
        try:
            return getattr(self._helpers, name)
        except AttributeError as exc:
            raise AttributeError(f"Browser Harness has no helper {name!r}") from exc

    def __repr__(self) -> str:
        status = "connected" if self._helpers is not None else "lazy"
        suffix = f", name={self.name!r}" if self.name else ""
        return f"BrowserHarness({status}{suffix})"
