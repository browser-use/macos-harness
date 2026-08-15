"""The explicit Accessibility escape hatch for the macOS harness."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import ApplicationServices as AS

if TYPE_CHECKING:
    from .macos import MacOS


class Accessibility:
    """Thin, opt-in access to Apple's AXUIElement operations."""

    _COMPACT_ATTRIBUTES = (
        "AXRole",
        "AXTitle",
        "AXDescription",
        "AXValue",
        "AXFrame",
    )

    def __init__(self, host: MacOS) -> None:
        self._host = host

    def at(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        coordinate_space: str = "screenshot",
        attributes: Iterable[str] = _COMPACT_ATTRIBUTES,
        include_actions: bool = True,
    ) -> dict[str, Any]:
        self._host._ensure_accessibility()
        point = self._host._screen_point(x, y, coordinate_space)
        pid = self._host._pid(app)
        root = (
            self._host._application_element(pid)
            if pid is not None
            else AS.AXUIElementCreateSystemWide()
        )
        error, element = AS.AXUIElementCopyElementAtPosition(
            root, point[0], point[1], None
        )
        if error != 0 or element is None:
            raise RuntimeError(f"AX hit test failed with AXError {error}")
        index = self._host._remember_element(element)
        return self._host._describe_element(
            element,
            index,
            attributes=attributes,
            include_actions=include_actions,
        )

    def query(
        self,
        *,
        app: str | None = None,
        element_index: int | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _COMPACT_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        return self._host.ax_search(
            element_index=element_index,
            app=app,
            search_key=search_key,
            text=text,
            visible_only=visible_only,
            limit=limit,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
        )

    def get(
        self, element_index: int, attributes: str | Iterable[str] = "AXValue"
    ) -> Any:
        if isinstance(attributes, str):
            return self._host.get(element_index, attributes)
        return self._host.get_attributes(element_index, attributes)

    def set(self, element_index: int, attribute: str, value: Any) -> None:
        self._host.set(element_index, value, attribute)

    def actions(self, element_index: int) -> list[str]:
        return self._host._actions(self._host._element(element_index))

    def perform(self, element_index: int, action: str = "AXPress") -> None:
        self._host.perform_action(element_index, action)

    def parameterized(self, element_index: int, attribute: str, parameter: Any) -> Any:
        error, value = AS.AXUIElementCopyParameterizedAttributeValue(
            self._host._element(element_index), attribute, parameter, None
        )
        if error != 0:
            raise RuntimeError(
                f"Parameterized AX read {attribute!r} failed with AXError {error}"
            )
        return self._host._jsonable(value)

    def raw(self, element_index: int) -> Any:
        return self._host._element(element_index)

    def dump(self, app: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("screenshot", False)
        return self._host.get_app_state(app, **kwargs)
