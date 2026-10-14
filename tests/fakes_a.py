"""Fakes of every module that tools.py and helper.py call.

Each fake has exactly the functions of the build contract: a call to any other name
fails. Every call is written to one list, so a test can assert the order of a call.

    world = fakes_a.install(monkeypatch)
    tools.run(["get_app_state", "Demo"], None, world.agent, "/", world.deadline)
    assert world.names()[:2] == ["apps.resolve", "policy.decide"]
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

from macos_harness import errors, helper, tools
from macos_harness.model import (
    AgentSession,
    AppKind,
    AppTarget,
    Capture,
    Line,
    Policy,
    Shot,
    WindowRef,
)

TARGET = AppTarget("/Applications/Demo.app", "com.example.demo", "Demo")
RUNNING = replace(TARGET, pid=4242)
WINDOW = WindowRef("window", 7, 4242, "Demo", (100.0, 50.0, 400.0, 300.0))
SHOT = Shot("/tmp/shots/Demo Screenshot x7k2qd.jpeg", 2.0, (100.0, 50.0), (800, 600))
ACTIONS = [
    "click",
    "drag",
    "scroll",
    "press_key",
    "type_text",
    "set_value",
    "select_text",
    "secondary",
    "paste",
]
# module -> function -> what it answers. A callable is called, an exception is raised.
CONTRACT = {
    "apps": {"resolve": TARGET, "launch": RUNNING, "running": RUNNING}
    | {"kind": AppKind(), "list_apps": [], "prewarm": None, "panel_owner": None},
    "policy": {
        "decide": Policy("allowed", "low", True, None),
        "check_url": None,
        "watches_urls": False,
    },
    "approvals": {"ensure": None, "approve": None},
    "permissions": {"gate": None, "doctor": {"accessibility": True}},
    "lockscreen": {"gate": None},
    "person": dict.fromkeys(
        [
            "gate",
            "after_state_read",
            "turn_started",
            "turn_ended",
            "start_watch",
        ]
    )
    | {"interrupted": False, "driven": []},
    "windows": dict.fromkeys(["open_menu", "watch", "unwatch"])
    | {"key_window": WINDOW, "extra_windows": []},
    "tree": dict.fromkeys(["read", "enable", "disable", "disable_all", "ask_once"]),
    "settle": {"wait": None},
    "screenshot": {
        "capture": SHOT,
        "cleanup": None,
        "keep": "/Users/x/.macos-harness/proof/Demo-x7k2qd.jpeg",
    },
    "actions": dict.fromkeys([*ACTIONS, "cancel_menu"]),
    "focus": {"release": None, "release_all": None},
    "pasteboard": {"restore_saved": None},
    "instructions": {"prefix": None},
    "install": {"install": ["/home/skill/SKILL.md"], "uninstall": None}
    | {"skill_text": "the skill\n", "hook_note": None},
    "presence": dict.fromkeys(["pointer_activity", "halo", "live_view", "turn_done"]),
}


def capture(*texts: str, title: str = "Demo") -> Capture:
    lines = [
        Line(0, text, f"el-{text}", "AXButton") for text in texts or ("button OK",)
    ]
    return Capture(title, "Demo", WINDOW.window_id, lines)


class Module:
    """One fake module. `answers` may be changed by a test: world.apps.answers[...]."""

    def __init__(self, name: str, log: list, answers: dict):
        self.name, self.log, self.answers = name, log, dict(answers)

    def __getattr__(self, function: str):
        if function not in self.answers:
            raise AttributeError(f"{self.name}.{function} is not in the contract")

        def call(*args, **kwargs):
            self.log.append((f"{self.name}.{function}", args, kwargs))
            answer = self.answers[function]
            if isinstance(answer, BaseException):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer

        return call


class Revisions:
    """Stands in for elements.Revisions: numbers go up by one per line it was shown."""

    def __init__(self, log: list):
        self.log, self.next, self.lines = log, 0, []

    def present(self, capture: Capture, disable_diff: bool) -> str:
        self.log.append(("revisions.present", (capture, disable_diff), {}))
        if disable_diff:
            self.next = 0
        for line in capture.lines:
            line.id, self.next = self.next, self.next + 1
        self.lines = capture.lines
        body = "\n".join(f"{line.id} {line.text}" for line in capture.lines)
        return f'Window: "{capture.window_title}", App: {capture.app_name}.\n{body}\n'

    def resolve(self, index: int, reread, ignore_value: bool = False) -> Line:
        self.log.append(("revisions.resolve", (index,), {"ignore_value": ignore_value}))
        for line in self.lines:
            if line.id == index:
                return line
        raise errors.invalid_element_id(index)

    def watch(self, target) -> None:
        self.log.append(("revisions.watch", (target,), {}))

    def reset(self) -> None:
        self.log.append(("revisions.reset", (), {}))
        self.next, self.lines = 0, []


class World:
    """All fakes, one log, one agent."""

    def __init__(self):
        self.log: list[tuple] = []
        self.activity: list[int] = []  # one entry per "the Mac is in use" call
        self.modules = {
            name: Module(name, self.log, answers) for name, answers in CONTRACT.items()
        }
        self.tree.answers["read"] = lambda target, window, kind, menu=None: capture()
        self.agent = AgentSession("agent-1")
        self.deadline = time.time() + 120

    def __getattr__(self, name: str) -> Module:
        try:
            return self.modules[name]
        except KeyError:
            raise AttributeError(name) from None

    def names(self) -> list[str]:
        return [name for name, _, _ in self.log]

    def args(self, name: str) -> tuple:
        """The arguments of the last call of one function."""
        return [args for called, args, _ in self.log if called == name][-1]

    def run(self, *argv: str, stdin: str | None = None, agent=None):
        """(exit code, stdout, stderr) of one call, as the helper would run it."""
        return tools.run(list(argv), stdin, agent or self.agent, "/work", self.deadline)


def install(patch=None) -> World:
    """Put the fakes where tools.py and helper.py look for the other modules.

    `patch` is pytest's monkeypatch. Without it the change stays (a helper process).
    """
    world = World()
    put = patch.setattr if patch else setattr
    for name, module in world.modules.items():
        # Into the instance dict: asking tools.mods for the old value would import it.
        (patch.setitem if patch else dict.__setitem__)(vars(tools.mods), name, module)
    sessions = helper.Sessions()
    sessions.revisions = lambda: Revisions(world.log)
    put(helper, "SESSIONS", sessions)
    awake = helper.Awake(
        lambda: world.log.append(("awake.acquire", (), {})) or 1,
        lambda token: world.log.append(("awake.release", (token,), {})),
        lambda: world.activity.append(1),
    )
    put(helper, "AWAKE", awake)
    put(helper, "STATE", helper._State())
    return world


# --- a real helper process, and a real client, with every other module faked ----------

TESTS = os.path.dirname(os.path.abspath(__file__))
_PATH = f"import sys; sys.path.insert(0, {TESTS!r}); import fakes_a; "
# What the client starts in place of `python -m macos_harness.helper`.
HELPER = [sys.executable, "-c", _PATH + "fakes_a.helper_main()"]
# The real client, as its own process, that starts HELPER when it needs a helper.
CLIENT = [sys.executable, "-c", _PATH + "raise SystemExit(fakes_a.client_main())"]


def helper_main() -> None:
    """helper.main() with the fakes in place and no AppKit loop."""
    from macos_harness import protocol, runloop

    install()
    runloop.run = lambda then: then()  # main() then waits for shutdown without AppKit
    if os.environ.get("FAKES_A_VERSION"):  # a helper of another version
        protocol.VERSION = os.environ["FAKES_A_VERSION"]
    helper.main()


def client_main() -> int:
    from macos_harness import cli

    cli.HELPER = HELPER
    cli.START_WINDOW_S = float(os.environ.get("FAKES_A_START_WINDOW", "5"))
    return cli.main(sys.argv[1:])
