"""A tiny click-through AppKit overlay for the harness pointer."""

from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
import time
from typing import Any, TextIO

from .pointer import (
    POINTER_HEIGHT,
    POINTER_HOTSPOT,
    POINTER_WIDTH,
    pointer_points,
)


class LivePointerOverlay:
    """Send pointer updates to a disposable AppKit helper process."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._visible = True
        atexit.register(self.close)

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def move(self, x: float, y: float, *, duration: float = 0.16) -> None:
        self._visible = True
        self._send(
            {
                "cmd": "move",
                "x": float(x),
                "y": float(y),
                "duration": max(0.0, float(duration)),
            }
        )

    def show(self, x: float, y: float) -> None:
        self._visible = True
        self._send({"cmd": "show", "x": float(x), "y": float(y)})

    def hide(self) -> None:
        self._visible = False
        if self.running:
            self._send({"cmd": "hide"}, start=False)

    def click(self) -> None:
        if self._visible:
            self._send({"cmd": "click"})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"cmd":"quit"}\n')
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def _start(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-m", "macos_harness.overlay", "--helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._process = process
        return process

    def _send(self, payload: dict[str, Any], *, start: bool = True) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            if not start:
                return
            process = self._start()
        stream = process.stdin
        if stream is None:
            return
        try:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stream.flush()
        except (BrokenPipeError, OSError):
            if not start:
                return
            self._process = None
            retry = self._start()
            assert retry.stdin is not None
            retry.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            retry.stdin.flush()


def _read_commands(stream: TextIO, controller: Any) -> None:
    for line in stream:
        try:
            command = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "handleCommand:", command, False
        )
    controller.performSelectorOnMainThread_withObject_waitUntilDone_(
        "handleCommand:", {"cmd": "quit"}, False
    )


def _run_helper() -> None:  # pragma: no cover - exercised by the live smoke test
    import AppKit
    import objc
    import Quartz

    class PointerView(AppKit.NSView):
        pressed = False

        def isOpaque(self) -> bool:
            return False

        def drawRect_(self, rect: Any) -> None:
            bounds = self.bounds()
            AppKit.NSColor.clearColor().set()
            AppKit.NSRectFill(bounds)

            points = pointer_points(pressed=self.pressed)
            path = AppKit.NSBezierPath.bezierPath()
            first_x, first_y = points[0]
            path.moveToPoint_(AppKit.NSMakePoint(first_x, POINTER_HEIGHT - first_y))
            for x, y in points[1:]:
                path.lineToPoint_(AppKit.NSMakePoint(x, POINTER_HEIGHT - y))
            path.closePath()
            path.setLineJoinStyle_(AppKit.NSRoundLineJoinStyle)

            AppKit.NSGraphicsContext.saveGraphicsState()
            shadow = AppKit.NSShadow.alloc().init()
            shadow.setShadowOffset_(AppKit.NSMakeSize(0.0, -1.0))
            shadow.setShadowBlurRadius_(2.5)
            shadow.setShadowColor_(
                AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.42)
            )
            shadow.set()
            AppKit.NSColor.blackColor().setFill()
            path.fill()
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.94).setStroke()
            path.setLineWidth_(2.5)
            path.stroke()
            AppKit.NSGraphicsContext.restoreGraphicsState()

            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.9).setStroke()
            path.setLineWidth_(0.65)
            path.stroke()

        def endPress_(self, sender: Any) -> None:
            self.pressed = False
            self.setNeedsDisplay_(True)

    class OverlayController(AppKit.NSObject):
        def init(self) -> Any:
            controller = objc.super(OverlayController, self).init()
            if controller is None:
                return None
            frame = AppKit.NSMakeRect(-100, -100, POINTER_WIDTH, POINTER_HEIGHT)
            style = (
                AppKit.NSWindowStyleMaskBorderless
                | AppKit.NSWindowStyleMaskNonactivatingPanel
            )
            controller.panel = (
                AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                    frame, style, AppKit.NSBackingStoreBuffered, False
                )
            )
            controller.view = PointerView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, POINTER_WIDTH, POINTER_HEIGHT)
            )
            controller.panel.setContentView_(controller.view)
            controller.panel.setTitle_("macOS Harness Pointer")
            controller.panel.setOpaque_(False)
            controller.panel.setBackgroundColor_(AppKit.NSColor.clearColor())
            controller.panel.setHasShadow_(False)
            controller.panel.setIgnoresMouseEvents_(True)
            controller.panel.setHidesOnDeactivate_(False)
            controller.panel.setReleasedWhenClosed_(False)
            controller.panel.setLevel_(AppKit.NSStatusWindowLevel + 1)
            controller.panel.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorStationary
            )
            controller.shown = False
            controller.animation_timer = None
            controller.animation_started = 0.0
            controller.animation_duration = 0.0
            controller.animation_start = (0.0, 0.0)
            controller.animation_end = (0.0, 0.0)
            return controller

        @staticmethod
        @objc.python_method
        def _origin(x: float, y: float) -> Any | None:
            for screen in AppKit.NSScreen.screens():
                display_id = int(screen.deviceDescription()["NSScreenNumber"])
                cg_bounds = Quartz.CGDisplayBounds(display_id)
                min_x = float(cg_bounds.origin.x)
                min_y = float(cg_bounds.origin.y)
                max_x = min_x + float(cg_bounds.size.width)
                max_y = min_y + float(cg_bounds.size.height)
                if min_x <= x < max_x and min_y <= y < max_y:
                    local_x = x - min_x
                    local_y = y - min_y
                    ns_frame = screen.frame()
                    hot_x, hot_y = POINTER_HOTSPOT
                    return AppKit.NSMakePoint(
                        float(ns_frame.origin.x) + local_x - hot_x,
                        float(ns_frame.origin.y)
                        + float(ns_frame.size.height)
                        - local_y
                        - (POINTER_HEIGHT - hot_y),
                    )
            return None

        @objc.python_method
        def _stop_animation(self) -> None:
            if self.animation_timer is not None:
                self.animation_timer.invalidate()
                self.animation_timer = None

        def tick_(self, timer: Any) -> None:
            elapsed = time.monotonic() - self.animation_started
            progress = min(1.0, elapsed / self.animation_duration)
            eased = 1.0 - (1.0 - progress) ** 3
            start_x, start_y = self.animation_start
            end_x, end_y = self.animation_end
            self.panel.setFrameOrigin_(
                AppKit.NSMakePoint(
                    start_x + (end_x - start_x) * eased,
                    start_y + (end_y - start_y) * eased,
                )
            )
            if progress >= 1.0:
                self._stop_animation()

        @objc.python_method
        def _place(self, command: dict[str, Any], *, animate: bool) -> None:
            origin = self._origin(float(command["x"]), float(command["y"]))
            if origin is None:
                self._stop_animation()
                self.panel.orderOut_(None)
                self.shown = False
                return
            duration = max(0.0, float(command.get("duration", 0.0)))
            if animate and self.shown and duration > 0:
                self._stop_animation()
                current = self.panel.frame().origin
                self.animation_start = (float(current.x), float(current.y))
                self.animation_end = (float(origin.x), float(origin.y))
                self.animation_started = time.monotonic()
                self.animation_duration = duration
                self.animation_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / 60.0, self, "tick:", None, True
                )
            else:
                self._stop_animation()
                self.panel.setFrameOrigin_(origin)
            self.panel.orderFrontRegardless()
            self.shown = True

        def handleCommand_(self, command: dict[str, Any]) -> None:
            action = command.get("cmd")
            if action in {"move", "show"}:
                self._place(command, animate=action == "move")
            elif action == "hide":
                self._stop_animation()
                self.panel.orderOut_(None)
                self.shown = False
            elif action == "click" and self.shown:
                self.view.pressed = True
                self.view.setNeedsDisplay_(True)
                self.view.performSelector_withObject_afterDelay_(
                    "endPress:", None, 0.11
                )
            elif action == "quit":
                self._stop_animation()
                self.panel.orderOut_(None)
                AppKit.NSApplication.sharedApplication().terminate_(None)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    controller = OverlayController.alloc().init()
    reader = threading.Thread(
        target=_read_commands, args=(sys.stdin, controller), daemon=True
    )
    reader.start()
    app.run()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--helper"]:
        print("macos_harness.overlay is an internal helper", file=sys.stderr)
        return 2
    _run_helper()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
