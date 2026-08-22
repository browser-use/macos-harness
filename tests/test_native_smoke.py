from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

import macos_harness.macos as macos_module
from macos_harness.errors import ErrorCode
from macos_harness.macos import (
    AccessibilityPermissionError,
    FocusChangedError,
    MacOS,
    MacOSError,
)


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
        # Queue of exceptions `press()` raises (in order) before falling through
        # to `press_result`; empty by default, so every existing test that never
        # sets this keeps seeing a single unconditional success.
        self.press_errors: list[BaseException] = []

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
        if self.press_errors:
            raise self.press_errors.pop(0)
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


class _StubProcess:
    """A ``Popen``-like stand-in complete enough for ``_close_session``
    to run against without raising ``AttributeError`` in these
    routing-focused tests -- which never exercise cleanup escalation
    itself (that is ``test_native_lifecycle.py``'s job). Reports
    "already exited" immediately, so cleanup never blocks or escalates
    to a signal here.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _StubClient:
    """A ``NativeClient``-like stand-in exposing only ``connected`` and
    ``close()`` -- enough for every routing-focused test's own
    (often implicit, finalizer-triggered) cleanup to run without
    raising, with an optional ``on_close`` hook for tests that assert
    close() actually happened.
    """

    def __init__(self, *, expected_pid: int | None = None, on_close=None) -> None:
        self.expected_pid = expected_pid
        self.connected = True
        self.close_calls = 0
        self._on_close = on_close

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False
        if self._on_close is not None:
            self._on_close()


def _native_agent_status() -> dict[str, Any] | None:
    """Launch a throwaway session and return its ping result when the
    toolchain/agent is reachable, else None."""
    try:
        from macos_harness import agent
    except ImportError:
        return None
    try:
        session = agent.launch()
    except agent.AgentUnavailableError:
        return None
    try:
        return session.client.ping()
    finally:
        session.close()


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
    """Caching applies to any error, for any backend that isn't `auto`
    (or that is `auto` with a non-``AgentUnavailableError``) -- it
    always re-raises the exact cached instance. Only a cached
    ``AgentUnavailableError`` under ``auto`` may ever resolve to
    ``None`` instead."""
    from macos_harness import agent as agent_module

    mac = MacOS(backend="native")
    sentinel_error = MacOSError("agent unreachable")
    mac._native_error = sentinel_error

    with pytest.raises(MacOSError, match="agent unreachable"):
        mac._acquire_native()
    with pytest.raises(MacOSError, match="agent unreachable"):
        mac._acquire_native()

    mac_auto = MacOS(backend="auto")
    mac_auto._native_error = agent_module.AgentUnavailableError("agent unreachable")
    assert mac_auto._acquire_native() is None


def test_acquire_native_threads_the_verified_popen_pid_into_the_client(
    monkeypatch,
) -> None:
    """The pid ``agent.launch()`` itself verified via its own handshake is
    the one pid the returned session's client is bound to — macos.py
    never re-derives or re-checks a pid of its own, and never re-launches
    once a session is cached.
    """
    from macos_harness import agent as agent_module

    launch_count = 0

    def _fake_launch(**kwargs: object) -> agent_module.AgentSession:
        nonlocal launch_count
        launch_count += 1
        process = _StubProcess()
        return agent_module.AgentSession(
            process=process, client=_StubClient(expected_pid=process.pid)
        )

    monkeypatch.setattr(agent_module, "launch", _fake_launch)

    mac = MacOS(backend="native")
    client = mac._acquire_native()

    assert client.expected_pid == 4242
    assert client.connected is True
    assert mac._acquire_native() is client
    assert launch_count == 1


def test_acquire_native_auto_falls_back_only_before_a_response_is_received(
    monkeypatch,
) -> None:
    """An ``AgentUnavailableError`` from ``agent.launch()`` — no usable
    executable, a spawn failure, or the freshly spawned child never
    producing a valid handshake response — is the one native failure
    ``backend="auto"`` may treat as "no native agent available". It is
    cached, so a second call never re-launches.
    """
    from macos_harness import agent as agent_module

    launch_count = 0

    def _fake_launch(**kwargs: object) -> agent_module.AgentSession:
        nonlocal launch_count
        launch_count += 1
        raise agent_module.AgentUnavailableError("no toolchain")

    monkeypatch.setattr(agent_module, "launch", _fake_launch)

    mac_native = MacOS(backend="native")
    with pytest.raises(agent_module.AgentUnavailableError):
        mac_native._acquire_native()
    with pytest.raises(agent_module.AgentUnavailableError):
        mac_native._acquire_native()
    assert launch_count == 1

    mac_auto = MacOS(backend="auto")
    assert mac_auto._acquire_native() is None
    assert isinstance(mac_auto._native_error, agent_module.AgentUnavailableError)
    assert mac_auto._acquire_native() is None
    assert launch_count == 2


def test_acquire_native_caches_hard_failures_and_never_respawns(monkeypatch) -> None:
    """A protocol-version or pid mismatch means ``agent.launch()`` itself
    already received a real handshake response — never fallback-eligible
    for either backend. Unlike ``AgentUnavailableError``, it *is* cached:
    a later call must never spawn another child, and ``backend="auto"``
    must keep raising it forever instead of quietly resolving to the
    Python engine.
    """
    from macos_harness import agent as agent_module
    from macos_harness import native as native_module

    launch_count = 0

    def _fake_launch(**kwargs: object) -> agent_module.AgentSession:
        nonlocal launch_count
        launch_count += 1
        raise native_module.NativeProtocolError("reported pid 999, expected 4242")

    monkeypatch.setattr(agent_module, "launch", _fake_launch)

    mac_native = MacOS(backend="native")
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_native._acquire_native()
    assert mac_native._native_error is not None
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_native._acquire_native()
    assert launch_count == 1  # cached: never spawns a second child

    mac_auto = MacOS(backend="auto")
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_auto._acquire_native()
    assert mac_auto._native_error is not None
    with pytest.raises(native_module.NativeProtocolError, match="expected 4242"):
        mac_auto._acquire_native()  # never silently resolves to None either
    assert launch_count == 2


def test_acquire_native_closes_a_session_orphaned_by_baseexception_during_storage(
    monkeypatch,
) -> None:
    """A ``KeyboardInterrupt`` landing after ``agent.launch()`` already
    returned a live session, but before that session is fully stored in
    ``_native_session_box``/``_native_client``, must close that exact
    session (never leaking its child) and leave no partial client or
    session recorded -- exercised here by making ``.client`` access
    itself raise, simulating an interrupt landing mid-assignment."""
    from macos_harness import agent as agent_module

    close_calls = 0

    class _StubProcess:
        pid = 4242

    class _FakeSession:
        process = _StubProcess()

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

        @property
        def client(self) -> object:
            raise KeyboardInterrupt()

    fake_session = _FakeSession()
    monkeypatch.setattr(agent_module, "launch", lambda **kwargs: fake_session)

    mac = MacOS(backend="native")
    with pytest.raises(KeyboardInterrupt):
        mac._acquire_native()

    assert close_calls == 1
    assert mac._native_session_box[0] is None
    assert mac._native_client is None


def test_macos_close_is_idempotent_and_safe_when_never_launched() -> None:
    mac = MacOS(backend="python")
    mac.close()
    mac.close()  # idempotent, no error


def test_macos_close_tears_down_the_launched_session(monkeypatch) -> None:
    from macos_harness import agent as agent_module

    closed: list[str] = []

    monkeypatch.setattr(
        agent_module,
        "launch",
        lambda **kwargs: agent_module.AgentSession(
            process=_StubProcess(),
            client=_StubClient(on_close=lambda: closed.append("client")),
        ),
    )

    mac = MacOS(backend="native")
    mac._acquire_native()

    mac.close()

    assert closed == ["client"]
    mac.close()  # idempotent: never double-closes
    assert closed == ["client"]


def test_macos_context_manager_closes_on_normal_and_exceptional_exit(monkeypatch) -> None:
    from macos_harness import agent as agent_module

    closed: list[str] = []

    monkeypatch.setattr(
        agent_module,
        "launch",
        lambda **kwargs: agent_module.AgentSession(
            process=_StubProcess(),
            client=_StubClient(on_close=lambda: closed.append("client")),
        ),
    )

    with MacOS(backend="native") as mac:
        mac._acquire_native()
    assert closed == ["client"]

    closed.clear()
    with pytest.raises(ValueError, match="boom"), MacOS(backend="native") as mac:
        mac._acquire_native()
        raise ValueError("boom")
    assert closed == ["client"]


def test_macos_post_close_native_call_raises_explicitly_without_relaunching(
    monkeypatch,
) -> None:
    from macos_harness import agent as agent_module

    launch_count = 0



    def _fake_launch(**kwargs: object) -> agent_module.AgentSession:
        nonlocal launch_count
        launch_count += 1
        return agent_module.AgentSession(process=_StubProcess(), client=_StubClient())

    monkeypatch.setattr(agent_module, "launch", _fake_launch)

    mac = MacOS(backend="native")
    mac._acquire_native()
    assert launch_count == 1
    mac.close()

    with pytest.raises(MacOSError, match=r"close\(\) already tore down"):
        mac._acquire_native()
    assert launch_count == 1  # never relaunched after close()

    # The same explicit failure applies even under backend="auto" -- a
    # post-close call is a usage bug, never grounds for a silent fallback.
    mac_auto = MacOS(backend="auto")
    mac_auto.close()
    with pytest.raises(MacOSError, match=r"close\(\) already tore down"):
        mac_auto._acquire_native()
    assert launch_count == 1


def test_dropping_macos_promptly_closes_its_native_session_under_cpython(monkeypatch) -> None:
    """``Accessibility`` holds a *weak* reference back to its ``MacOS``
    host (breaking the ``MacOS.ax`` <-> ``Accessibility._host`` strong
    cycle): dropping the last strong reference to a ``MacOS`` must let
    CPython's plain refcounting collect it -- and run its finalizer --
    immediately, with no ``gc.collect()`` anywhere in this test. A
    lingering cycle would instead leave it uncollected until some later,
    unpredictable cyclic-gc pass, keeping its native agent child alive
    for however long that takes.
    """
    import weakref

    from macos_harness import agent as agent_module
    from macos_harness.macos import MacOSError

    closed: list[str] = []

    monkeypatch.setattr(
        agent_module,
        "launch",
        lambda **kwargs: agent_module.AgentSession(
            process=_StubProcess(),
            client=_StubClient(on_close=lambda: closed.append("client")),
        ),
    )

    mac = MacOS(backend="native")
    ax = mac.ax  # retained separately -- must not itself keep `mac` alive
    mac._acquire_native()
    assert closed == []

    weak_mac = weakref.ref(mac)
    del mac
    assert weak_mac() is None, (
        "MacOS must be collected by refcounting alone the instant the "
        "last strong reference drops -- no cycle to wait on gc.collect() for"
    )
    assert closed == ["client"]  # the finalizer already reaped the session

    with pytest.raises(MacOSError, match="already been closed or garbage collected"):
        ax.raw(0)


def test_macos_concurrent_first_use_launches_exactly_one_child(monkeypatch) -> None:
    import threading
    import time as time_module

    from macos_harness import agent as agent_module

    launch_count = 0
    launch_lock = threading.Lock()


    def _fake_launch(**kwargs: object) -> agent_module.AgentSession:
        nonlocal launch_count
        with launch_lock:
            launch_count += 1
        time_module.sleep(0.05)  # widen the race window
        return agent_module.AgentSession(process=_StubProcess(), client=_StubClient())

    monkeypatch.setattr(agent_module, "launch", _fake_launch)

    mac = MacOS(backend="native")
    results: list[Any] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        client = mac._acquire_native()
        with results_lock:
            results.append(client)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert launch_count == 1
    assert len(results) == 8
    assert len({id(client) for client in results}) == 1


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

    with MacOS(backend="python") as python_mac, MacOS(backend="native") as native_mac:
        try:
            python_matches = python_mac.ax.query(
                role="menu item", app="Finder", visible_only=False, limit=40
            )
        except MacOSError:
            pytest.skip("Finder menu bar was not queryable locally in this environment")
        if not python_matches:
            pytest.skip("Finder has no queryable menu items locally in this environment")
        try:
            native_matches = native_mac.ax.query(
                role="menu item", app="Finder", visible_only=False, limit=40
            )
        except MacOSError:
            pytest.skip("Finder menu bar was not queryable natively in this environment")
        if not native_matches:
            pytest.skip("Finder has no queryable menu items natively in this environment")

        def _field_names(matches: Sequence[Mapping[str, object]]) -> set[str]:
            names: set[str] = set()
            for match in matches:
                names.update(key for key in match if key != "element_index")
            return names

        assert _field_names(python_matches) == _field_names(native_matches)


# A small, real AppKit target app -- not an osascript dialog, which is not
# reliably a running NSApplication `_resolve_app` can bind to. Accessory
# activation policy keeps it out of the Dock/Cmd+Tab and off the frontmost
# app without making it any less real: it is a genuine NSApplication with a
# genuine NSWindow and NSButton, discoverable and pressable through the
# exact same Accessibility APIs as any regular app. `orderFrontRegardless`
# shows the window without ever activating (and so never focus-stealing)
# the helper app itself. The button's target-action really flips its own
# title on press, so a round trip through AXPress is verified by an actual
# state change, not just a non-error return.
_HARNESS_PROBE_HELPER = r'''
import os

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSMakeRect,
    NSObject,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)


class _Target(NSObject):
    def pressed_(self, sender):
        sender.setTitle_("harness-pressed")


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
        | NSWindowStyleMaskResizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(100.0, 100.0, 240.0, 120.0), style, NSBackingStoreBuffered, False
    )
    window.setTitle_("macos-harness-probe")

    button = NSButton.alloc().initWithFrame_(NSMakeRect(40.0, 40.0, 160.0, 32.0))
    button.setTitle_("harness-ok")
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setEnabled_(True)

    target = _Target.alloc().init()
    button.setTarget_(target)
    button.setAction_("pressed:")

    window.contentView().addSubview_(button)
    window.orderFrontRegardless()

    # Keep strong references alive for the whole run -- not merely
    # implied by the still-active `main()` frame while `app.run()`
    # blocks, but explicit and unambiguous.
    global _window, _button, _target_ref
    _window, _button, _target_ref = window, button, target

    print(f"READY {os.getpid()}", flush=True)

    app.run()


main()
'''


def _read_ready_line(proc: subprocess.Popen[str], *, timeout: float = 10.0) -> str:
    """Block on the probe helper's first stdout line, bounded by ``timeout``
    via a background reader thread -- a bare ``readline()`` has no timeout
    of its own and could hang forever if the helper never produces output.
    """
    result: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        assert proc.stdout is not None
        result.put(proc.stdout.readline())

    threading.Thread(target=_reader, daemon=True).start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(
            f"harness probe helper (pid {proc.pid}) never printed READY "
            f"within {timeout}s"
        ) from None
    if not line:
        raise RuntimeError(
            f"harness probe helper (pid {proc.pid}) exited before printing "
            f"READY (returncode={proc.poll()})"
        )
    return line.strip()

@pytest.mark.native
@pytest.mark.smoke
def test_native_press_round_trip_without_activation() -> None:
    """A native single-shot press on a background AppKit helper window
    never steals focus, and its button's target-action really fires: the
    title flips from "harness-ok" to "harness-pressed", confirmed via
    wait/wait_gone -- never by polling -- and the frontmost app is
    unchanged before and after."""
    _require_trusted_native_agent()

    before = MacOS._frontmost_app()
    proc = subprocess.Popen(
        [sys.executable, "-c", _HARNESS_PROBE_HELPER],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        ready = _read_ready_line(proc)
        assert ready.startswith("READY "), f"unexpected helper output: {ready!r}"
        helper_pid = int(ready.split(" ", 1)[1])

        with MacOS(backend="native") as mac:
            before_match = mac.ax.wait("harness-ok", app=helper_pid, timeout=10.0)
            old_title = mac.ax.get(before_match["element_index"], "AXTitle")

            pressed = mac.ax.press("harness-ok", app=helper_pid, timeout=10.0)
            assert pressed["role"] == "AXButton"

            mac.ax.wait_gone("harness-ok", app=helper_pid, timeout=10.0)
            after_match = mac.ax.wait("harness-pressed", app=helper_pid, timeout=10.0)
            new_title = mac.ax.get(after_match["element_index"], "AXTitle")

        assert old_title == "harness-ok"
        assert new_title == "harness-pressed"

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


def test_native_press_retries_only_on_delayed_appearance(monkeypatch) -> None:
    """A native targeted press that finds no unique match yet -- the
    agent's own ``PressCoordinator`` always reports this as
    ``element.unknown`` before ``AXPress`` is ever dispatched -- is
    retried until the deadline, exactly mirroring the local ``ax_wait``
    retry shape instead of failing on the first empty search."""
    client = _FakeNativeClient()
    client.press_errors = [
        MacOSError(
            "AX press expected exactly one match for pid 55, found 0",
            code=ErrorCode.ELEMENT_UNKNOWN,
        ),
        MacOSError(
            "AX press expected exactly one match for pid 55, found 0",
            code=ErrorCode.ELEMENT_UNKNOWN,
        ),
    ]
    client.press_result = {"role": "AXButton", "title": "Not Now"}
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    match = mac.ax_press(app="Chrome", text="Not Now", timeout=1.0, interval=0.01)

    assert match["title"] == "Not Now"
    assert [call[0] for call in client.calls] == ["press", "press", "press"]


def test_native_press_gives_up_with_a_timeout_code_once_the_deadline_passes(
    monkeypatch,
) -> None:
    """A single ``element.unknown`` with ``timeout=0`` is promoted to a
    dedicated ``timeout`` error -- the same machine-readable outcome
    ``ax_wait`` reports for the local backend -- rather than surfacing
    the raw last search failure, and never retries past the deadline."""
    client = _FakeNativeClient()
    client.press_errors = [
        MacOSError(
            "AX press expected exactly one match for pid 55, found 0",
            code=ErrorCode.ELEMENT_UNKNOWN,
        ),
    ]
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    with pytest.raises(MacOSError, match="timed out") as exc_info:
        mac.ax_press(app="Chrome", text="Not Now", timeout=0, interval=0.01)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details["timeout"] == 0
    assert [call[0] for call in client.calls] == ["press"]  # timeout=0 still tries once


def test_native_press_never_retries_after_a_possibly_applied_failure(monkeypatch) -> None:
    """Any native press failure other than a pre-dispatch
    ``element.unknown`` -- here ``focus.changed``, reported only after
    the agent's own ``AXPress`` already ran -- propagates immediately:
    retrying could fire the action a second time."""
    client = _FakeNativeClient()
    client.press_errors = [
        FocusChangedError("Process 55 became frontmost during AX press; stopped")
    ]
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    monkeypatch.setattr(
        macos_module.time,
        "sleep",
        lambda seconds: pytest.fail("must not retry a possibly-applied failure"),
    )

    with pytest.raises(FocusChangedError, match="became frontmost"):
        mac.ax_press(app="Chrome", text="Not Now", timeout=5.0)

    assert [call[0] for call in client.calls] == ["press"]


def test_native_press_never_retries_an_ambiguous_match(monkeypatch) -> None:
    """A native press whose search settles on more than one match is
    ``bad_request`` -- an ambiguous target, not an unknown one -- and
    retrying could never resolve it: the same search criteria will find
    the same matches again. Propagates on the very first call, exactly
    like ``focus.changed``, spending none of the caller's timeout."""
    client = _FakeNativeClient()
    client.press_errors = [
        MacOSError(
            "AX press expected exactly one match for pid 55, found 2 (ambiguous)",
            code=ErrorCode.BAD_REQUEST,
            details={"count": 2},
        )
    ]
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    monkeypatch.setattr(
        macos_module.time,
        "sleep",
        lambda seconds: pytest.fail("must not retry an ambiguous match"),
    )

    with pytest.raises(MacOSError, match="ambiguous") as exc_info:
        mac.ax_press(app="Chrome", text="Not Now", timeout=5.0)

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["count"] == 2
    assert [call[0] for call in client.calls] == ["press"]


def test_native_press_never_retries_a_nonpressable_match(monkeypatch) -> None:
    """A native press whose unique match has no ``AXPress`` action is
    ``unsupported_op``: the element was found just fine, and retrying
    the same search will find the same unpressable element again, so
    this also propagates on the very first call."""
    client = _FakeNativeClient()
    client.press_errors = [
        MacOSError(
            "AX press target does not expose AXPress; available actions: ['AXShowMenu']",
            code=ErrorCode.UNSUPPORTED_OP,
        )
    ]
    mac = MacOS(backend="native")
    monkeypatch.setattr(mac, "_acquire_native", lambda: client)
    monkeypatch.setattr(mac, "_pid", lambda app: 55)
    monkeypatch.setattr(
        macos_module.time,
        "sleep",
        lambda seconds: pytest.fail("must not retry a non-pressable match"),
    )

    with pytest.raises(MacOSError, match="does not expose AXPress") as exc_info:
        mac.ax_press(app="Chrome", text="Not Now", timeout=5.0)

    assert exc_info.value.code == ErrorCode.UNSUPPORTED_OP
    assert [call[0] for call in client.calls] == ["press"]


def test_ax_press_validates_timeout_and_interval_before_backend_dispatch(
    monkeypatch,
) -> None:
    """Timeout/interval validation happens once, before either backend
    branch runs -- native and local callers see the identical rejection
    without ever touching the native agent."""
    mac = MacOS(backend="native")
    monkeypatch.setattr(
        mac,
        "_acquire_native",
        lambda: pytest.fail("must not reach the native backend"),
    )

    with pytest.raises(MacOSError, match="timeout") as exc_info:
        mac.ax_press(app="Chrome", text="Not Now", timeout=-1)
    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == "timeout"

    with pytest.raises(MacOSError, match="interval") as exc_info:
        mac.ax_press(app="Chrome", text="Not Now", interval=0)
    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == "interval"
