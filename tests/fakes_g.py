"""Fakes for the tests of the person and safety modules.

Those modules import four neighbours by name: apps, presence, protocol, runloop. A
neighbour that is not written yet gets an empty stand-in here, so these
tests run on their own. Every test then puts one of the fakes below in its place.
"""

from __future__ import annotations

import importlib
import sys
import types

from macos_harness.model import AgentSession, AppKind, AppTarget

for _name in ("apps", "presence", "protocol", "runloop"):
    try:
        importlib.import_module(f"macos_harness.{_name}")
    except ImportError:  # not written yet
        sys.modules[f"macos_harness.{_name}"] = types.ModuleType(_name)
        setattr(
            sys.modules["macos_harness"], _name, sys.modules[f"macos_harness.{_name}"]
        )


class FakePresence:
    """Records every call. `ask` keeps the answer callback for the test to fire."""

    def __init__(self, can_ask: bool = True):
        self.calls: list[tuple] = []
        self.prompts: list[dict] = []
        self.can_ask = can_ask

    def ask(self, title, subtitle, choices, on_answer):
        if not self.can_ask:
            return False
        self.prompts.append(
            {
                "title": title,
                "subtitle": subtitle,
                "choices": choices,
                "answer": on_answer,
            }
        )
        return True

    def withdraw(self, title):
        self.calls.append(("withdraw", title))
        self.prompts[:] = [p for p in self.prompts if p["title"] != title]

    def pointer_activity(self, state):
        self.calls.append(("pointer_activity", state))

    def activity(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "pointer_activity"]


class FakeApps:
    """apps.kind by bundle id. An app that is not listed is a plain app."""

    def __init__(self, **kinds: AppKind):
        self.kinds = kinds

    def kind(self, target: AppTarget) -> AppKind:
        return self.kinds.get(target.bundle_id, AppKind())


class FakeProtocol:
    """state_dir, as the real one: it names the folder and does not make it."""

    def __init__(self, folder):
        self.folder = folder

    def state_dir(self):
        return self.folder


class FakeEvents:
    def __init__(self, own_tag: int):
        self.OWN_TAG = own_tag


class FakeRunloop:
    def on_main(self, call):
        call()

    def call_on_main(self, call):
        return call()


def app(name: str = "Notes", bundle_id: str | None = None, pid: int | None = 501):
    return AppTarget(
        path=f"/Applications/{name}.app",
        bundle_id=bundle_id or f"com.example.{name.lower()}",
        name=name,
        pid=pid,
    )


def agent(key: str = "agent-1", **fields) -> AgentSession:
    return AgentSession(key=key, **fields)
