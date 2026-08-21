"""The explicit Accessibility escape hatch for the macOS harness."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import ApplicationServices as AS

from .macos import _AX_SAFE_ATTRIBUTES, MacOSError

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
    _SAFE_ATTRIBUTES = _AX_SAFE_ATTRIBUTES

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

    @staticmethod
    def _search_key(search_key: str | None, role: str | None) -> str:
        if search_key is not None and role is not None:
            raise MacOSError("Pass role or search_key, not both")
        if role is None:
            return search_key or "AXAnyTypeSearchKey"
        aliases = {
            "any": "AXAnyTypeSearchKey",
            "button": "AXButtonSearchKey",
            "checkbox": "AXCheckBoxSearchKey",
            "combobox": "AXComboBoxSearchKey",
            "image": "AXImageSearchKey",
            "link": "AXLinkSearchKey",
            "list": "AXListSearchKey",
            "menu": "AXMenuSearchKey",
            "menuitem": "AXMenuItemSearchKey",
            "radiobutton": "AXRadioButtonSearchKey",
            "statictext": "AXStaticTextSearchKey",
            "table": "AXTableSearchKey",
            "textarea": "AXTextAreaSearchKey",
            "textfield": "AXTextFieldSearchKey",
        }
        normalized = re.sub(r"[^a-z0-9]", "", role.casefold())
        try:
            return aliases[normalized]
        except KeyError as exc:
            valid = (
                "any, button, checkbox, combo box, image, link, list, menu, "
                "menu item, radio button, static text, table, text area, text field"
            )
            raise MacOSError(
                f"Unknown AX role {role!r}; choose one of: {valid}"
            ) from exc

    def query(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        element_index: int | None = None,
        role: str | None = None,
        search_key: str | None = None,
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
            search_key=self._search_key(search_key, role),
            text=text,
            visible_only=visible_only,
            limit=limit,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
        )

    def query_all(
        self,
        text: str | None = None,
        *,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        return self._host.ax_search_all(
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            visible_only=visible_only,
            limit=limit,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
        )

    def wait(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        return self._host.ax_wait(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )

    def wait_gone(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None:
        self._host.ax_wait_gone(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )

    def press(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        return self._host.ax_press(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
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
