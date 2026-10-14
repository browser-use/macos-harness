"""Fakes of the capture module: fake apps built from the JSON files in fixtures/trees.

A tree file holds one fake app:
  {"title": header title or null, "app": name, "kind": {...}, "window": element,
   "menu_bar": element, "focus": path, "menu": path}
An element is {"role": "AXButton", "AXTitle": "OK", ..., "actions": {"AXPress": "press"},
"settable": ["AXValue"], "rect": [x, y, w, h], "unlisted": ["AXExpanded"] (attributes
the element answers for but does not list), "children": [...]}. A value of the form
{"$path": [0, 2]} is the element at that child path below the window, {"$paths": [...]}
is a list of them, {"$range": [0, 1]} prints the way a range value prints. A child
{"$dup": [0, 2]} is the element at that path a second time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fakes import El, FakeAx

from macos_harness import errors, render, tree
from macos_harness.model import AppKind, AppTarget, Capture, WindowRef

TREES = Path(__file__).parent / "fixtures" / "trees"
PID = 100
OWN_KEYS = ("role", "children", "actions", "settable", "rect")


class Ax(FakeAx):
    """FakeAx whose batch read fails for an element that went away, as the real one."""

    def get_many(self, el: El, names):
        if el.dead:
            raise errors.accessibility(self.error_name(-25202))
        return super().get_many(el, names)


def el(spec: dict) -> El:
    return El(
        role=spec.get("role", "AXGroup"),
        attrs={k: v for k, v in spec.items() if k not in OWN_KEYS},
        children=[el(child) for child in spec.get("children", ())],
        acts=dict(spec.get("actions", {})),
        settable_names=set(spec.get("settable", ())),
        rect=tuple(spec["rect"]) if "rect" in spec else None,
    )


def at(root: El, path: list[int]) -> El:
    for index in path:
        root = root.children[index]
    return root


def _resolve(root: El) -> None:
    """Turn the {"$path": ...} values into the elements they name."""
    for node in list(root.walk()):  # a {"$dup": path} child is that element once more
        node.children = [
            at(root, kid.attrs["$dup"]) if "$dup" in kid.attrs else kid
            for kid in node.children
        ]
    for node in root.walk():
        for name, value in node.attrs.items():
            if isinstance(value, dict) and "$path" in value:
                node.attrs[name] = at(root, value["$path"])
            elif isinstance(value, dict) and "$paths" in value:
                node.attrs[name] = [at(root, path) for path in value["$paths"]]
            elif isinstance(value, dict) and "$range" in value:
                first, length = value["$range"]
                node.attrs[name] = f"location:{first} length:{length}"


class App:
    """One fake app with one window, ready to be read."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.window = el(spec["window"]) if spec.get("window") else El(dead=True)
        _resolve(self.window)
        attrs: dict[str, Any] = {}
        if spec.get("menu_bar"):
            attrs["AXMenuBar"] = el(spec["menu_bar"])
        if spec.get("focus") is not None:
            attrs["AXFocusedUIElement"] = at(self.window, spec["focus"])
        self.app = El(role="AXApplication", attrs=attrs)
        self.ax = Ax({PID: self.app})
        self.target = AppTarget("/Applications/Demo.app", "dev.demo", spec["app"], PID)
        self.kind = AppKind(**spec.get("kind", {}))

    def read(self, monkeypatch, menu: El | None = None) -> Capture:
        monkeypatch.setattr(tree, "ax", self.ax)
        frame = self.window.rect or (0.0, 0.0, 800.0, 600.0)
        ref = WindowRef(self.window, 7, PID, self.spec.get("title"), frame)
        if menu is None and self.spec.get("menu") is not None:
            menu = at(self.app.attrs["AXMenuBar"], self.spec["menu"])
        return tree.read(self.target, ref, self.kind, menu)


def load(name: str) -> App:
    return App(json.loads((TREES / f"{name}.json").read_text()))


def expected(name: str) -> str:
    return (TREES / f"{name}.txt").read_text()


def text(capture: Capture) -> str:
    """The full state text, numbered the way a first read is: from 0, or 1 for a menu."""
    first = 1 if capture.menu_open else 0
    for number, line in enumerate(capture.lines, first):
        line.id = number
    spare = first + len(capture.lines)
    for line in [capture.focused, *capture.selected]:
        if line is not None and line.id is None:
            twin = next((x for x in capture.lines if x.element == line.element), None)
            line.id = twin.id if twin else spare
            spare += twin is None
    body = "\n".join(
        "\t" * x.depth + f"{x.id} {x.text}" + (f" {x.detail}" if x.detail else "")
        for x in capture.lines
    )
    head = render.header(capture.window_title, capture.app_name)
    foot = render.footer(capture, True)
    return f"{head}\n{body}\n" + (f"\n{foot}" if foot else "")
