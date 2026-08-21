from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

import macos_harness.macos as macos_module
from macos_harness.macos import AccessibilityPermissionError, MacOS, MacOSError


class _FakeNativeClient:
    """A minimal stand-in for native.NativeClient's call surface.

    Exercises MacOS's own dispatch/interning logic (macos.py) without a
    Swift toolchain, a running agent, or a socket. Real wire framing and
    protocol errors are covered by test_native_protocol.py; real lifecycle
    is covered by test_native_lifecycle.py.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.generation = 3
        self._next_handle = 0
        self.apps: list[dict[str, Any]] = [
            {
                "name": "Finder",
                "bundle_id": "com.apple.finder",
                "pid": 111,
                "path": "/System/Library/CoreServices/Finder.app",
            }
        ]
        self.query_result: list[dict[str, Any]] = [
            {"role": "AXButton", "title": "Not Now"}
        ]
        self.press_result: dict[str, Any] = {"role": "AXButton", "title": "Not Now"}

    def _mint(self) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    def list_apps(self) -> list[dict[str, Any]]:
        self.calls.append(("list_apps", None))
        return [dict(app) for app in self.apps]

    def query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append(("query", params))
        return [{**match, "handle": self._mint()} for match in self.query_result]

    def press(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("press", params))
        return {**self.press_result, "handle": self._mint()}

    def get(self, handle: Any, attribute: str) -> Any:
        self.calls.append(("get", {"handle": handle, "attribute": attribute}))
        return f"value:{attribute}"

    def get_attributes(
        self, handle: Any, attributes: tuple[str, ...]
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_attributes", {"handle": handle, "attributes": attributes})
        )
        return {name: f"value:{name}" for name in attributes}

    def set(self, handle: Any, attribute: str, value: Any) -> None:
        self.calls.append(
            ("set", {"handle": handle, "attribute": attribute, "value": value})
        )

    def perform(self, handle: Any, action: str) -> None:
        self.calls.append(("perform", {"handle": handle, "action": action}))


def _native_agent_status() -> dict[str, Any] | None:
    """Return agent.status() when the toolchain/agent is reachable, else None."""
    try:
        from macos_harness import agent
    except ImportError:
        return None
    try:
        agent.ensure_running()
        return agent.status()
    except agent.AgentUnavailableError:
        return None


def _require_trusted_native_agent() -> None:
    if os.environ.get("MACOS_HARNESS_RUN_NATIVE_SMOKE") != "1":
        pytest.skip("set MACOS_HARNESS_RUN_NATIVE_SMOKE=1 to run native smoke tests")
    status = _native_agent_status()
    if status is None:
        pytest.skip("native agent toolchain/runtime is unavailable")
    if not status.get("trusted"):
        pytest.skip("native agent lacks Accessibility trust")


# ---------------------------------------------------------------------------
# CI-safe routing assertions: exercise MacOS's own dispatch logic through a
# fake NativeClient. No Swift toolchain, running agent, or socket required.
# ---------------------------------------------------------------------------


def test_backend_resolves_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("MACOS_HARNESS_BACKEND", raising=False)
    assert MacOS()._backend == "python"

    monkeypatch.setenv("MACOS_HARNESS_BACKEND", "native")
    assert MacOS()._backend == "native"

    monkeypatch.setenv("MACOS_HARNESS_BACKEND", "AUTO")
    assert MacOS()._backend == "auto"

    assert MacOS(backend="python")._backend == "python"  # explicit wins over env

    with pytest.raises(MacOSError, match="Unknown backend"):
        MacOS(backend="bogus")


def test_python_backend_never_acquires_a_native_client(monkeypatch) -> None:
    mac = MacOS(backend="python")
    calls: list[str] = []
    monkeypatch.setattr(mac, "_acquire_native", lambda: calls.append("called"))

    mac.list_apps()

    assert calls == []


def test_native_backend_construction_is_lazy() -> None:
    mac = MacOS(backend="native")

    assert mac._native_client is None
    assert mac._native_error is None


def test_native_acquire_caches_a_successful_client() -> None:
    mac = MacOS(backend="native")
    client = _FakeNativeClient()
    mac._native_client = client

    assert mac._acquire_native() is client
    assert mac._acquire_native() is client


def test_native_acquire_caches_failure_and_never_reprobes() -> None:
    mac = MacOS(backend="native")
    sentinel_error = MacOSError("agent unreachable")
    mac._native_error = sentinel_error

    with pytest.raises(MacOSError, match="agent unreachable"):
        mac._acquire_native()
    with pytest.raises(MacOSError, match="agent unreachable"):
        mac._acquire_native()

    mac_auto = MacOS(backend="auto")
    mac_auto._native_error = sentinel_error
    assert mac_auto._acquire_native() is None


def test_acquire_native_threads_the_verified_ping_pid_into_the_client(
    monkeypatch,
) -> None:
    """The pid agent.ensure_running() itself verified is the one pid passed
    to NativeClient — never a second, separately-fetched status() call,
    which could race a concurrent agent restart between the two reads.
    """
    from macos_harness import agent as agent_module
    from macos_harness import native as native_module

    construct_count = 0

    class _StubClient:
        def __init__(
            self,
            socket_path: Any,
            *,
            expected_pid: int | None = None,
            timeout: float = 5.0,
        ) -> None:
            nonlocal construct_count
            construct_count += 1
            self.socket_path = socket_path
            self.expected_pid = expected_pid
            self.connected = False

        def connect(self) -> None:
            self.connected = True

    monkeypatch.setattr(
        agent_module, "ensure_running", lambda **kwargs: {"running": True, "pid": 4242}
    )
    monkeypatch.setattr(native_module, "NativeClient", _StubClient)

    mac = MacOS(backend="native")
    client = mac._acquire_native()

    assert client.expected_pid == 4242
    assert client.connected is True
    assert mac._acquire_native() is client
    assert construct_count == 1


def test_acquire_native_auto_falls_back_only_on_a_raw_connection_failure(
    monkeypatch,
) -> None:
    """A NativeConnectionError (nothing listening on the socket at all) is
    the one native failure backend="auto" may treat as "no native agent
    available" — before any request, including the handshake ping, was
    dispatched. Both backends never dispatched anything, and the failure
    is cached so a second call does not reconnect.
    """
    from macos_harness import agent as agent_module
    from macos_harness import native as native_module

    construct_count = 0

    class _StubClient:
        def __init__(
            self,
            socket_path: Any,
            *,
            expected_pid: int | None = None,
            timeout: float = 5.0,
        ) -> None:
            nonlocal construct_count
            construct_count += 1

        def connect(self) -> None:
            raise native_module.NativeConnectionError("nothing is listening")

    monkeypatch.setattr(
        agent_module, "ensure_running", lambda **kwargs: {"running": True, "pid": 4242}
    )
    monkeypatch.setattr(native_module, "NativeClient", _StubClient)

    mac_native = MacOS(backend="native")
    with pytest.raises(native_module.NativeConnectionError):
        mac_native._acquire_native()
    with pytest.raises(native_module.NativeConnectionError):
        mac_native._acquire_native()
    assert construct_count == 1

    mac_auto = MacOS(backend="auto")
    assert mac_auto._acquire_native() is None
    assert isinstance(mac_auto._native_error, native_module.NativeConnectionError)
    assert mac_auto._acquire_native() is None
    assert construct_count == 2


def test_acquire_native_never_falls_back_on_a_handshake_mismatch(monkeypatch) -> None:
    """A protocol-version or expected_pid mismatch means a real request
    (the handshake ping) already reached *some* process on the other end
    of the socket. auto must not silently paper over that by falling back
    to the local Accessibility APIs — both backends hard-fail identically,
    and the failure is left uncached so a transient race (e.g. the agent
    mid-restart) gets a fresh attempt on the next call.
    """
    from macos_harness import agent as agent_module
    from macos_harness import native as native_module

    class _StubClient:
        def __init__(
            self,
            socket_path: Any,
            *,
            expected_pid: int | None = None,
            timeout: float = 5.0,
        ) -> None:
            pass

        def connect(self) -> None:
            raise native_module.NativeProtocolError("reported pid 999, expected 4242")

    monkeypatch.setattr(
        agent_module, "ensure_running", lambda **kwargs: {"running": True, "pid": 4242}
    )
    monkeypatch.setattr(native_module, "NativeClient", _StubClient)

    mac_native = MacOS(backend="native")
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_native._acquire_native()
    assert mac_native._native_error is None

    mac_auto = MacOS(backend="auto")
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_auto._acquire_native()
    assert mac_auto._native_error is None


def test_native_backend_routes_list_apps_through_the_client(monkeypatch) -> None:
    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)

    apps = mac.list_apps()

    assert apps == client.apps
    assert [call[0] for call in client.calls] == ["list_apps"]


def test_native_backend_routes_targeted_ax_search_and_interns_handles(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()
    client.query_result = [
        {"role": "AXButton", "title": "Not Now"},
        {"role": "AXButton", "title": "Never"},
    ]
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 4242)

    matches = mac.ax_search(app="Chrome", text="N", limit=5, reset_elements=True)

    assert [match["role"] for match in matches] == ["AXButton", "AXButton"]
    assert [match["title"] for match in matches] == ["Not Now", "Never"]
    element_indices = [match["element_index"] for match in matches]
    assert element_indices == sorted(element_indices)
    assert len(set(element_indices)) == 2

    assert len(client.calls) == 1
    op, params = client.calls[0]
    assert op == "query"
    assert params["app_pid"] == 4242
    assert params["text"] == "N"
    assert params["limit"] == 5
    assert params["reset_elements"] is True


def test_native_handles_share_the_monotonic_element_registry_with_local_elements(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 99)

    local_index = mac._remember_element(object())
    native_matches = mac.ax_search(app="Chrome", text="Save", reset_elements=False)
    native_index = native_matches[0]["element_index"]

    assert native_index > local_index
    assert mac._element(local_index) is not None
    assert mac._element(native_index) is not None

    mac._elements = {}
    with pytest.raises(MacOSError, match="Unknown element index"):
        mac._element(local_index)
    with pytest.raises(MacOSError, match="Unknown element index"):
        mac._element(native_index)


def test_subtree_search_on_a_native_handle_fails_explicitly(monkeypatch) -> None:
    from macos_harness.native import _NativeHandle

    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    sentinel = _NativeHandle(client, 7, client.generation)
    index = mac._remember_element(sentinel)

    with pytest.raises(MacOSError, match="native agent handle"):
        mac.ax_search(element_index=index, text="anything")


def test_element_actions_route_through_the_native_handle_client(monkeypatch) -> None:
    from macos_harness.native import _NativeHandle

    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    sentinel = _NativeHandle(client, 11, client.generation)
    index = mac._remember_element(sentinel)

    assert mac.get(index, "AXValue") == "value:AXValue"
    assert mac.get_attributes(index, ["AXRole", "AXTitle"]) == {
        "AXRole": "value:AXRole",
        "AXTitle": "value:AXTitle",
    }
    mac.set(index, "hello", attribute="AXValue")
    mac.perform_action(index, "press")

    assert [call[0] for call in client.calls] == [
        "get",
        "get_attributes",
        "set",
        "perform",
    ]
    assert client.calls[2][1] == {
        "handle": sentinel,
        "attribute": "AXValue",
        "value": "hello",
    }
    assert client.calls[3][1] == {"handle": sentinel, "action": "AXPress"}


def test_targeted_ax_press_dispatches_once_through_native_and_never_locally(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()
    client.press_result = {"role": "AXButton", "title": "Not Now"}
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    stale_index = mac._remember_element(object())

    def _fail_wait(**kwargs: Any) -> None:
        raise AssertionError(
            "targeted native press must not call ax_wait (redundant traversal)"
        )

    def _fail_search(**kwargs: Any) -> None:
        raise AssertionError(
            "targeted native press must not call ax_search (redundant traversal)"
        )

    def _fail_perform(element_index: int, action: str) -> None:
        raise AssertionError("targeted native press must not also perform locally")

    monkeypatch.setattr(mac, "ax_wait", _fail_wait)
    monkeypatch.setattr(mac, "ax_search", _fail_search)
    monkeypatch.setattr(mac, "perform_action", _fail_perform)

    match = mac.ax_press(app="Chrome", text="Not Now")

    assert match["title"] == "Not Now"
    assert [call[0] for call in client.calls] == ["press"]
    with pytest.raises(MacOSError, match="Unknown element index"):
        mac._element(stale_index)


def test_cross_app_press_never_routes_to_native_directly(monkeypatch) -> None:
    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    fake_match = {
        "element_index": mac._remember_element(object()),
        "role": "AXButton",
        "app": {"name": "Chrome", "pid": 7},
    }
    monkeypatch.setattr(mac, "ax_wait", lambda **kwargs: fake_match)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Chrome", "pid": 7})
    performed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        mac,
        "perform_action",
        lambda element_index, action: performed.append((element_index, action)),
    )

    result = mac.ax_press(text="Not Now", all_apps=True)

    assert result is fake_match
    assert performed == [(fake_match["element_index"], "AXPress")]
    assert client.calls == []


def test_auto_backend_falls_back_to_python_when_native_client_is_none(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()
    mac = MacOS(backend="auto")
    monkeypatch.setattr(mac, "_acquire_native", lambda: None)
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: object())
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyParameterizedAttributeValue",
        lambda *args: (macos_module.AS.kAXErrorParameterizedAttributeUnsupported, None),
    )
    monkeypatch.setattr(
        mac,
        "_snapshot_tree",
        lambda *args, **kwargs: [
            {"element_index": 0, "depth": 0, "role": "AXApplication"}
        ],
    )

    matches = mac.ax_search(app="Finder", text="whatever")

    assert matches == []
    assert client.calls == []


def test_auto_backend_never_falls_back_after_a_request_is_dispatched(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()

    def _boom(params: dict[str, Any]) -> list[dict[str, Any]]:
        raise MacOSError("agent-side failure")

    client.query = _boom  # type: ignore[method-assign]
    mac = MacOS(backend="auto")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)

    with pytest.raises(MacOSError, match="agent-side failure"):
        mac.ax_search(app="Finder", text="whatever")


def test_native_search_still_updates_last_app_for_later_local_calls(
    monkeypatch,
) -> None:
    client = _FakeNativeClient()
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    info = {
        "name": "Chrome",
        "bundle_id": "com.google.Chrome",
        "pid": 321,
        "path": "/Chrome",
    }
    monkeypatch.setattr(mac, "_resolve_app", lambda query: (object(), info))

    mac.ax_search(app="Chrome", text="Save")

    assert mac._last_app == info
    assert mac._pid(None) == 321


def test_ax_search_all_reraises_accessibility_permission_error_unwrapped(
    monkeypatch,
) -> None:
    mac = MacOS(backend="auto")
    info = {
        "name": "Finder",
        "bundle_id": "com.apple.finder",
        "pid": 111,
        "path": "/System/Library/CoreServices/Finder.app",
    }
    monkeypatch.setattr(mac, "_resolve_app", lambda query: (object(), info))

    def _boom(**kwargs: Any) -> list[dict[str, Any]]:
        raise AccessibilityPermissionError("Accessibility permission is required.")

    monkeypatch.setattr(mac, "ax_search", _boom)

    with pytest.raises(AccessibilityPermissionError):
        mac.ax_search_all(apps="Finder", text="Not Now")


def test_ax_search_all_still_wraps_non_permission_errors_when_strict(
    monkeypatch,
) -> None:
    mac = MacOS(backend="auto")
    info = {
        "name": "Finder",
        "bundle_id": "com.apple.finder",
        "pid": 111,
        "path": "/System/Library/CoreServices/Finder.app",
    }
    monkeypatch.setattr(mac, "_resolve_app", lambda query: (object(), info))

    def _boom(**kwargs: Any) -> list[dict[str, Any]]:
        raise MacOSError("agent-side failure")

    monkeypatch.setattr(mac, "ax_search", _boom)

    with pytest.raises(MacOSError, match="AX search failed for Finder"):
        mac.ax_search_all(apps="Finder", text="Not Now")


# ---------------------------------------------------------------------------
# Local-only smoke tests: real Swift agent, real Accessibility trust. Skip
# everywhere else. See bench/ax_smoke.py for repeated-query timing.
# ---------------------------------------------------------------------------


@pytest.mark.native
@pytest.mark.smoke
def test_query_parity_on_finder_bounded_fallback() -> None:
    """Local and native bounded-search fallbacks report the same node fields."""
    _require_trusted_native_agent()

    python_mac = MacOS(backend="python")
    native_mac = MacOS(backend="native")

    try:
        python_matches = python_mac.ax.query(
            role="menu item", app="Finder", visible_only=False, limit=40
        )
    except MacOSError:
        pytest.skip("Finder menu bar was not queryable locally in this environment")
    try:
        native_matches = native_mac.ax.query(
            role="menu item", app="Finder", visible_only=False, limit=40
        )
    except MacOSError:
        pytest.skip("Finder menu bar was not queryable natively in this environment")

    assert python_matches, "expected at least one Finder menu item locally"
    assert native_matches, "expected at least one Finder menu item natively"

    def _field_names(matches: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for match in matches:
            names.update(key for key in match if key != "element_index")
        return names

    assert _field_names(python_matches) == _field_names(native_matches)


@pytest.mark.native
@pytest.mark.smoke
def test_native_press_round_trip_without_activation() -> None:
    """A native single-shot press on a background AppleScript dialog never
    steals focus, and wait_gone confirms the dialog actually closed."""
    _require_trusted_native_agent()

    before = MacOS._frontmost_app()
    script = 'display dialog "harness-probe" buttons {"harness-ok"}'
    proc = subprocess.Popen(["/usr/bin/osascript", "-e", script])
    try:
        mac = MacOS(backend="native")
        match = mac.ax.press("harness-ok", app=str(proc.pid), timeout=10.0)
        assert match["role"] == "AXButton"

        mac.ax.wait_gone("harness-ok", app=str(proc.pid), timeout=10.0)

        after = MacOS._frontmost_app()
        before_pid = before["pid"] if before else None
        after_pid = after["pid"] if after else None
        assert before_pid == after_pid
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
