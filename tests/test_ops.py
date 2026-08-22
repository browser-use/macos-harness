"""Hermetic contract tests for the receipted ``mac.do`` surface."""

from __future__ import annotations

import gc
import hashlib
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from macos_harness.errors import ErrorCode, FocusChangedError, MacOSError
from macos_harness.ops import Operations
from macos_harness.receipts import (
    Acted,
    Executor,
    OperationError,
    Outcome,
    canonical_json,
    canonicalize,
    gone,
    present,
)


class FakeAX:
    _ALIASES: ClassVar[dict[str, str]] = {
        "button": "AXButtonSearchKey",
        "checkbox": "AXCheckBoxSearchKey",
        "textfield": "AXTextFieldSearchKey",
    }

    def _search_key(self, search_key: str | None, role: str | None) -> str:
        if search_key is not None and role is not None:
            raise MacOSError("Pass role or search_key, not both", code=ErrorCode.BAD_REQUEST)
        if role is None:
            return search_key or "AXAnyTypeSearchKey"
        normalized = role.casefold().replace(" ", "").replace("-", "").replace("_", "")
        try:
            return self._ALIASES[normalized]
        except KeyError as exc:
            raise MacOSError("Unsupported role", code=ErrorCode.BAD_REQUEST) from exc


class FakeHost:
    def __init__(self) -> None:
        self._backend = "python"
        self.ax = FakeAX()
        self.app_info: dict[str, object] = {
            "pid": 41,
            "name": "Demo",
            "bundle_id": "com.example.demo",
        }
        self.match: dict[str, object] = {
            "element_index": 7,
            "role": "AXButton",
            "title": "Save",
            "description": None,
            "identifier": "save",
            "actions": ["AXPress"],
            "app": self.app_info,
        }
        self.wait_results: deque[dict[str, object] | MacOSError] = deque()
        self.wait_calls: list[dict[str, object]] = []
        self.gone_calls: list[dict[str, object]] = []
        self.gone_error: MacOSError | None = None
        self.value: object = False
        self.get_results: deque[object | MacOSError] = deque()
        self.set_error: MacOSError | None = None
        self.action_error: BaseException | None = None
        self.key_error: BaseException | None = None
        self.validation_error: MacOSError | None = None
        self.guard_error: FocusChangedError | None = None
        self.action_hook: Callable[[], None] | None = None
        self.key_hook: Callable[[], None] | None = None
        self.wait_hook: Callable[[], None] | None = None
        self.toggle_on_press = False
        self.action_calls = 0
        self.set_calls = 0
        self.key_calls: list[tuple[str, str | int | None]] = []
        self.validated_keys: list[str] = []
        self.ensure_accessibility_calls = 0
        self.ensure_post_events_calls = 0
        self.guard_focus_calls = 0
        self.press_calls: list[dict[str, object]] = []
        self.press_results: deque[dict[str, object] | BaseException] = deque()
        self.press_hook: Callable[[], None] | None = None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        if query is None:
            raise MacOSError("missing app", code=ErrorCode.BAD_REQUEST)
        return object(), dict(self.app_info)

    def _ensure_accessibility(self) -> None:
        self.ensure_accessibility_calls += 1

    def _ensure_post_events(self) -> None:
        self.ensure_post_events_calls += 1

    def _validate_key(self, key: str) -> None:
        self.validated_keys.append(key)
        if self.validation_error is not None:
            raise self.validation_error

    def _frontmost_app(self) -> dict[str, object]:
        return dict(self.app_info)

    def _guard_focus(
        self,
        before: dict[str, object] | None,
        target_pid: int,
        operation: str,
    ) -> None:
        del before, target_pid, operation
        self.guard_focus_calls += 1
        if self.guard_error is not None:
            raise self.guard_error

    def _element(self, element_index: int) -> object:
        assert element_index == 7
        return object()

    def _is_ax_element(self, value: object) -> bool:
        del value
        return True

    def ax_wait(self, **kwargs: object) -> dict[str, object]:
        self.wait_calls.append(dict(kwargs))
        if self.wait_hook is not None:
            self.wait_hook()
        if (kwargs.get("all_apps") or kwargs.get("apps") is not None) and kwargs.get("text") is None:
            raise MacOSError(
                "Cross-app search requires text",
                code=ErrorCode.BAD_REQUEST,
            )
        if self.wait_results:
            result = self.wait_results.popleft()
            if isinstance(result, MacOSError):
                raise result
            return dict(result)
        return dict(self.match)

    def ax_wait_gone(self, **kwargs: object) -> None:
        self.gone_calls.append(dict(kwargs))
        if self.gone_error is not None:
            raise self.gone_error

    def ax_press(self, **kwargs: object) -> dict[str, object]:
        self.press_calls.append(dict(kwargs))
        if self.press_hook is not None:
            self.press_hook()
        if self.press_results:
            result = self.press_results.popleft()
            if isinstance(result, BaseException):
                raise result
            return dict(result)
        return dict(self.match)

    def get(self, element_index: int, attribute: str = "AXValue") -> object:
        assert element_index == 7
        assert attribute
        if self.get_results:
            result = self.get_results.popleft()
            if isinstance(result, MacOSError):
                raise result
            return result
        return self.value

    def set(self, element_index: int, value: object, attribute: str = "AXValue") -> None:
        assert element_index == 7
        assert attribute
        self.set_calls += 1
        if self.set_error is not None:
            raise self.set_error
        self.value = value

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        assert element_index == 7
        assert action == "AXPress"
        self.action_calls += 1
        if self.action_hook is not None:
            self.action_hook()
        if self.action_error is not None:
            raise self.action_error
        if self.toggle_on_press:
            self.value = not bool(self.value)

    def key(self, key: str, *, app: str | int | None = None) -> None:
        self.key_calls.append((key, app))
        if self.key_hook is not None:
            self.key_hook()
        if self.key_error is not None:
            raise self.key_error


def _exit_script(returncode: int = 0, *, stdout: bytes = b"", stderr: bytes = b"") -> str:
    """A tiny, fully-controlled child: drain stdin, write ``stdout``/
    ``stderr``, exit ``returncode``. `PopenFactory` execs this (or a
    caller-supplied variant) in place of the literal ``osascript``/
    ``osacompile`` ``command[0]``, so `_run_osascript`'s real pipe
    draining and process handling run against a genuine subprocess.
    """
    return (
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.flush()\n"
        f"sys.exit({returncode})\n"
    )


def _flood_script(total_bytes: int) -> str:
    """A child that writes exactly ``total_bytes`` of ``b"x"`` to stdout in
    64KiB chunks, then exits 0 -- proving `_run_osascript` hashes and
    counts a large stream correctly without ever buffering all of it.
    """
    return (
        "import sys\n"
        f"total = {total_bytes}\n"
        "chunk = b'x' * 65536\n"
        "written = 0\n"
        "while written < total:\n"
        "    n = min(len(chunk), total - written)\n"
        "    sys.stdout.buffer.write(chunk[:n])\n"
        "    written += n\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(0)\n"
    )


def _hang_with_descendant_script(marker_path: Path) -> str:
    """A child that spawns a real grandchild (``/bin/sleep 60``, left in
    the *same* process group since it never calls ``setsid`` itself),
    writes that grandchild's pid to ``marker_path``, then also hangs --
    proving a `run` timeout kills the *entire* process group, not just
    the direct ``osascript`` child.
    """
    return (
        "import subprocess, sys, time\n"
        f"marker = {str(marker_path)!r}\n"
        "child = subprocess.Popen(['/bin/sleep', '60'])\n"
        "with open(marker, 'w') as f:\n"
        "    f.write(str(child.pid))\n"
        "time.sleep(60)\n"
    )


class PopenFactory:
    """A `_Spawner` for hermetic ``run`` tests.

    Each call execs a real, small, fully-controlled Python subprocess
    (``sys.executable -c <script>``) in place of the literal
    ``command[0]`` -- a real child with real OS pipes and its own real
    process group -- instead of hand-faking `Popen`'s surface, so
    `_run_osascript`'s actual draining, deadline, and process-group-kill
    logic all run against a genuine subprocess. ``command``/kwargs for
    every call are still recorded in `.calls`, for assertions against
    exactly what `Operations.run` itself built.
    """

    def __init__(self, scripts: Sequence[str] = (), *, error: OSError | None = None) -> None:
        self.scripts = deque(scripts)
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.error = error

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        self.calls.append((list(command), dict(kwargs)))
        if self.error is not None:
            raise self.error
        script = self.scripts.popleft() if self.scripts else _exit_script()
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)  # type: ignore[call-overload]


class RaisingWaitProcess:
    """A `_Process` double whose `wait` always raises -- the one
    deterministic way to exercise `_run_osascript`'s own unexpected-
    failure cleanup path (its ``except (RuntimeError, OSError)``, and
    `_abandon`'s own ``except (subprocess.TimeoutExpired,
    ProcessLookupError)`` reap), without depending on a genuine,
    unrepeatable OS-level race. `ProcessLookupError` is itself a
    realistic `wait()` failure -- the same race `_abandon`'s reap is
    built to tolerate: the process already reaped elsewhere. `pid` is a
    made-up, never-real process id, so `_kill_process_group`'s
    `os.killpg` calls are harmless no-ops (`ProcessLookupError`, caught)
    rather than signaling anything real.
    """

    def __init__(self) -> None:
        self.pid = 2**31 - 1
        self.returncode: int | None = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        raise ProcessLookupError("unexpected wait failure")

    def kill(self) -> None:
        pass


class RaisingWaitSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.process = RaisingWaitProcess()

    def __call__(self, command: list[str], **kwargs: object) -> RaisingWaitProcess:
        self.calls.append((list(command), dict(kwargs)))
        return self.process


def _ops(
    *,
    spawn: PopenFactory | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[FakeHost, Operations]:
    host = FakeHost()
    if spawn is None and monotonic is None:
        return host, Operations(host)
    if spawn is None:
        assert monotonic is not None
        return host, Operations(host, _monotonic=monotonic)
    if monotonic is None:
        return host, Operations(host, _spawn=spawn)
    return host, Operations(host, _spawn=spawn, _monotonic=monotonic)


def _failed(call: Callable[[], object]) -> OperationError:
    with pytest.raises(OperationError) as caught:
        call()
    return caught.value


def _value_summary(value: object) -> dict[str, object]:
    """Mirrors ``ops._value_summary``: the exact ``{"type", "length",
    "sha256"}`` shape a `set`/`toggle` receipt's ``request``/``observed``/
    error ``details`` value should reduce to, never the raw value
    itself."""
    text = canonical_json(canonicalize(value))
    return {
        "type": type(value).__name__,
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _delayed_clock(
    calls_before_jump: int, *, start: float = 100.0, jump_to: float = 1_000_000.0
) -> Callable[[], float]:
    """A fake monotonic clock that reads ``start`` for the first
    ``calls_before_jump`` calls, then freezes at ``jump_to`` forever.

    Simulates one operation's shared deadline running out mid-flight
    (between its own ``calls_before_jump``-th and next monotonic read)
    while leaving a *subsequent* operation on the same `Operations` --
    which gets its own fresh `_Deadline` starting from whatever the
    clock reads at that point, and every read after that is the same
    frozen ``jump_to`` -- a full, unexhausted budget of its own.
    """
    calls = 0

    def _monotonic() -> float:
        nonlocal calls
        calls += 1
        return start if calls <= calls_before_jump else jump_to

    return _monotonic


def test_operations_weak_host_matches_other_child_surfaces() -> None:
    host, operations = _ops()
    del host
    gc.collect()

    with pytest.raises(MacOSError) as caught:
        operations.recall("missing")

    assert caught.value.code == ErrorCode.UNSUPPORTED_OP


def test_operations_used_from_a_different_pid_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates the one thing a real ``fork()`` would do: hand this
    object to a "process" whose pid no longer matches the one that
    constructed it. Mirrors
    `test_client_used_from_a_different_pid_fails_closed` in
    `test_native_protocol.py` -- the creator pid is monkeypatched
    directly instead of really forking the test runner."""
    host, operations = _ops()
    monkeypatch.setattr(operations, "_creator_pid", operations._creator_pid + 1)

    calls: list[Callable[[], object]] = [
        lambda: operations.press(app="Demo", text="Save"),
        lambda: operations.set("x", app="Demo", text="Field"),
        lambda: operations.toggle(True, app="Demo", text="Enabled"),
        lambda: operations.key("return", app="Demo"),
        lambda: operations.recall("missing"),
    ]
    for call in calls:
        with pytest.raises(MacOSError, match="fork boundary") as caught:
            call()
        assert caught.value.code == ErrorCode.UNSUPPORTED_OP

    assert host.wait_calls == []
    assert host.action_calls == 0
    assert host.set_calls == 0
    assert host.key_calls == []
    assert host.press_calls == []


def test_ledger_is_scoped_to_one_operations_instance() -> None:
    """A ``once`` token reserved on one `Operations` is invisible to a
    second, independent `Operations` -- even one built on the very same
    host -- since each instance gets its own fresh ``_Ledger`` with no
    cross-instance or cross-process sharing."""
    host = FakeHost()
    first = Operations(host)
    second = Operations(host)

    first_receipt = first.press(app="Demo", text="Save", once="shared-token")

    assert first_receipt.outcome is Outcome.DONE
    assert first_receipt.replayed is False

    second_receipt = second.press(app="Demo", text="Save", once="shared-token")

    assert second_receipt.outcome is Outcome.DONE
    assert second_receipt.replayed is False
    assert len(host.press_calls) == 2


def test_ax_mutations_require_exactly_one_scope() -> None:
    _host, operations = _ops()

    with pytest.raises(MacOSError, match="exactly one"):
        operations.press(text="Save")
    with pytest.raises(MacOSError, match="exactly one"):
        operations.set("x", app="Demo", all_apps=True, text="Save")
    with pytest.raises(MacOSError, match="exactly one"):
        operations.toggle(True, app="Demo", apps=["Other"], text="Save")


@pytest.mark.parametrize("bad_app", ["", 0, -5])
def test_ax_mutations_reject_empty_or_nonpositive_app_selector(bad_app: object) -> None:
    host, operations = _ops()

    calls: list[Callable[[], object]] = [
        lambda: operations.press(app=bad_app, text="Save"),
        lambda: operations.set("x", app=bad_app, text="Field"),
        lambda: operations.toggle(True, app=bad_app, text="Enabled"),
        lambda: operations.key("return", app=bad_app),
    ]
    for call in calls:
        with pytest.raises(MacOSError) as caught:
            call()
        assert caught.value.code == ErrorCode.BAD_REQUEST

    assert host.wait_calls == []
    assert host.action_calls == 0
    assert host.set_calls == 0
    assert host.key_calls == []
    assert host.press_calls == []


def test_apps_iterable_rejects_empty_or_nonpositive_items_after_materializing_once() -> None:
    host, operations = _ops()
    yielded: list[object] = []

    def apps() -> Iterator[str]:
        for value in ("Demo", "", "Other"):
            yielded.append(value)
            yield value

    with pytest.raises(MacOSError) as caught:
        operations.press(apps=apps(), text="Save")

    assert caught.value.code == ErrorCode.BAD_REQUEST
    # Materialized exactly once (by `_freeze_apps`), not re-iterated by
    # the item validation that runs after it.
    assert yielded == ["Demo", "", "Other"]
    assert host.wait_calls == []


def test_apps_generator_is_materialized_once_and_reused() -> None:
    host, operations = _ops()
    yielded: list[str] = []

    def apps() -> Iterator[str]:
        for name in ("Demo", "Other"):
            yielded.append(name)
            yield name

    receipt = operations.press(apps=apps(), text="Save", dry_run=True)

    assert yielded == ["Demo", "Other"]
    scope = receipt.request["scope"]
    assert scope["apps"] == ("Demo", "Other")
    assert host.wait_calls[0]["apps"] == ("Demo", "Other")
    assert receipt.verified is False


def test_cross_app_scope_forwards_fail_closed_text_rule() -> None:
    _host, operations = _ops()

    error = _failed(lambda: operations.press(all_apps=True))

    assert error.code == ErrorCode.BAD_REQUEST
    assert error.receipt.acted is Acted.NO


def test_press_dry_run_does_not_dispatch_or_reserve_token() -> None:
    host, operations = _ops()

    planned = operations.press(app="Demo", text="Save", once="press-1", dry_run=True)
    done = operations.press(app="Demo", text="Save", once="press-1")

    assert planned.outcome is Outcome.PLANNED
    assert planned.acted is Acted.NO
    assert planned.verified is False
    assert done.outcome is Outcome.DONE
    assert host.action_calls == 0
    assert len(host.press_calls) == 1


def test_press_replay_does_not_resolve_again() -> None:
    host, operations = _ops()
    original = operations.press(app="Demo", text="Save", once="replay")
    host.press_results.append(MacOSError("target is gone", code=ErrorCode.TIMEOUT))

    replay = operations.press(app="Demo", text="Save", once="replay")

    assert original.replayed is False
    assert replay.replayed is True
    assert len(host.press_calls) == 1


def test_press_uses_one_deadline_and_verifies_present() -> None:
    clock = iter((100.0, 101.0, 102.0, 103.0, 104.0))
    host, operations = _ops(monotonic=lambda: next(clock))

    receipt = operations.press(
        app="Demo",
        text="Save",
        timeout=5.0,
        postcondition=present("Saved", role="button"),
    )

    assert host.press_calls[0]["timeout"] == pytest.approx(3.0)
    assert host.wait_calls[0]["timeout"] == pytest.approx(2.0)
    assert host.wait_calls[0]["app"] == "Demo"
    assert receipt.duration_s == pytest.approx(4.0)
    assert receipt.verified is True


def test_press_verifies_gone_with_inherited_scope() -> None:
    host, operations = _ops()

    receipt = operations.press(
        app="Demo",
        text="Save",
        postcondition=gone("Save", role="button"),
    )

    assert receipt.outcome is Outcome.DONE
    assert receipt.verified is True
    assert host.gone_calls[0]["app"] == "Demo"
    assert host.gone_calls[0]["search_key"] == "AXButtonSearchKey"


def test_press_focus_failure_is_finalized_and_never_redispatched() -> None:
    host, operations = _ops()
    host.press_results.append(FocusChangedError("focus moved"))

    first = _failed(lambda: operations.press(app="Demo", text="Save", once="focus"))
    second = _failed(lambda: operations.press(app="Demo", text="Save", once="focus"))

    assert first.code == ErrorCode.FOCUS_CHANGED
    assert first.receipt.acted is Acted.YES
    assert second.receipt.replayed is True
    assert len(host.press_calls) == 1


def test_press_verification_failure_is_finalized_without_redispatch() -> None:
    host, operations = _ops()
    host.gone_error = MacOSError("still present", code=ErrorCode.TIMEOUT)

    first = _failed(
        lambda: operations.press(
            app="Demo",
            text="Save",
            postcondition=gone("Save", role="button"),
            once="verify",
        )
    )
    second = _failed(
        lambda: operations.press(
            app="Demo",
            text="Save",
            postcondition=gone("Save", role="button"),
            once="verify",
        )
    )

    assert first.receipt.acted is Acted.YES
    assert first.receipt.verified is False
    assert second.receipt.replayed is True
    assert len(host.press_calls) == 1


def test_press_atomic_pre_dispatch_failure_is_replayed_not_redispatched_on_same_once() -> None:
    """Unlike the old resolve-then-dispatch pipeline, an atomic
    ``ax_press`` reserves its ``once`` token *before* the call, so even
    a resolution-only failure is already on record after the first
    attempt: a second call with the same token replays that failure
    instead of dispatching a fresh search."""
    host, operations = _ops()
    host.press_results.append(MacOSError("no AXPress action", code=ErrorCode.UNSUPPORTED_OP))

    error = _failed(lambda: operations.press(app="Demo", text="Save", once="retryable"))
    replay = _failed(lambda: operations.press(app="Demo", text="Save", once="retryable"))

    assert error.receipt.acted is Acted.NO
    assert error.receipt.replayed is False
    assert replay.receipt.replayed is True
    assert len(host.press_calls) == 1


def test_press_malformed_owner_pid_fails_without_burning_once_token() -> None:
    """Only reachable for `apps`/`all_apps` scope now: a single, explicit
    `app` dispatches atomically through `ax_press`, which resolves its
    own target pid internally (see `test_press_atomic_*` above)."""
    host, operations = _ops()
    host.match["app"] = {**host.app_info, "pid": "not-a-pid"}

    error = _failed(lambda: operations.press(apps=["Demo"], text="Save", once="bad-pid"))
    host.match["app"] = dict(host.app_info)
    receipt = operations.press(apps=["Demo"], text="Save", once="bad-pid")

    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.once == "bad-pid"
    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False
    assert host.action_calls == 1


def test_press_deadline_exhausted_before_dispatch_fails_without_acting_or_burning_token() -> None:
    host, operations = _ops(monotonic=_delayed_clock(1))

    error = _failed(
        lambda: operations.press(app="Demo", text="Save", once="deadline-press", timeout=5.0)
    )

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.error["code"] == ErrorCode.TIMEOUT.value
    assert error.receipt.error["details"]["reason"] == "deadline_exhausted_before_dispatch"
    assert host.press_calls == []

    # The earlier deadline failure never reserved the once token (it
    # returned before the ledger's `reserve()` call), so a fresh
    # attempt with the same token -- and a clock that has stopped
    # advancing, so this second call's own deadline is unexhausted --
    # dispatches normally instead of colliding with a stuck reservation.
    receipt = operations.press(app="Demo", text="Save", once="deadline-press")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False
    assert len(host.press_calls) == 1


def test_press_gone_postcondition_deadline_exhausted_fails_without_polling() -> None:
    host, operations = _ops(monotonic=_delayed_clock(3))

    error = _failed(
        lambda: operations.press(
            app="Demo",
            text="Save",
            postcondition=gone("Save", role="button"),
            once="gone-deadline",
            timeout=5.0,
        )
    )

    assert error.receipt.acted is Acted.YES
    assert error.receipt.changed is None
    assert error.receipt.verified is False
    assert error.receipt.error["code"] == ErrorCode.TIMEOUT.value
    assert error.receipt.error["details"]["reason"] == "deadline_exhausted_before_verification"
    assert host.gone_calls == []
    assert len(host.press_calls) == 1


def test_press_changed_is_none_without_a_verified_postcondition_true_once_verified() -> None:
    host, operations = _ops()

    no_postcondition = operations.press(app="Demo", text="Save")
    assert no_postcondition.changed is None

    verified = operations.press(app="Demo", text="Save", postcondition=gone("Save", role="button"))
    assert verified.outcome is Outcome.DONE
    assert verified.changed is True

    host.gone_error = MacOSError("still present", code=ErrorCode.TIMEOUT)
    failed_verify = _failed(
        lambda: operations.press(app="Demo", text="Save", postcondition=gone("Save", role="button"))
    )
    assert failed_verify.receipt.changed is None


def test_press_changed_is_none_for_ambiguous_or_focus_changed_dispatch_failures() -> None:
    host, operations = _ops()
    host.press_results.append(FocusChangedError("focus moved"))
    focus_failure = _failed(lambda: operations.press(app="Demo", text="Save"))
    assert focus_failure.receipt.changed is None

    host2, operations2 = _ops()
    host2.press_results.append(MacOSError("press failed", code=ErrorCode.AX_ERROR))
    ambiguous_failure = _failed(lambda: operations2.press(app="Demo", text="Save"))
    assert ambiguous_failure.receipt.changed is None


# --- press(): atomic dispatch through MacOS.ax_press, single explicit app -


@pytest.mark.parametrize("backend", ["python", "native", "auto"])
def test_press_targeted_app_dispatches_through_ax_press_only(backend: str) -> None:
    host, operations = _ops()
    host._backend = backend

    receipt = operations.press(app="Demo", text="Save")

    assert receipt.outcome is Outcome.DONE
    assert len(host.press_calls) == 1
    assert host.wait_calls == []
    assert host.action_calls == 0
    assert host.guard_focus_calls == 0


def test_press_non_targeted_scopes_still_resolve_then_dispatch() -> None:
    host, operations = _ops()

    all_apps_receipt = operations.press(all_apps=True, text="Save")
    apps_receipt = operations.press(apps=["Demo"], text="Save")
    dry_run_receipt = operations.press(app="Demo", text="Save", dry_run=True)

    assert all_apps_receipt.outcome is Outcome.DONE
    assert apps_receipt.outcome is Outcome.DONE
    assert dry_run_receipt.outcome is Outcome.PLANNED
    assert host.press_calls == []
    assert len(host.wait_calls) == 3
    assert host.action_calls == 2


def test_press_atomic_reserves_once_token_before_dispatching() -> None:
    host, operations = _ops()
    seen: list[str] = []

    def observe() -> None:
        status, op, _receipt = operations._ledger.peek("order")
        seen.append(status)
        assert op == "press"

    host.press_hook = observe

    operations.press(app="Demo", text="Save", once="order")

    assert seen == ["in_flight"]


def test_press_atomic_focus_changed_is_acted_yes() -> None:
    host, operations = _ops()
    host.press_results.append(FocusChangedError("focus moved"))

    error = _failed(lambda: operations.press(app="Demo", text="Save"))

    assert error.code == ErrorCode.FOCUS_CHANGED
    assert error.receipt.acted is Acted.YES
    assert error.receipt.outcome is Outcome.FAILED
    assert len(host.press_calls) == 1


@pytest.mark.parametrize(
    "code", [ErrorCode.ELEMENT_UNKNOWN, ErrorCode.BAD_REQUEST, ErrorCode.UNSUPPORTED_OP]
)
def test_press_atomic_classifies_pre_dispatch_codes_as_acted_no(code: ErrorCode) -> None:
    host, operations = _ops()
    host.press_results.append(MacOSError("no match", code=code))

    error = _failed(lambda: operations.press(app="Demo", text="Save"))

    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.error["code"] == code.value


@pytest.mark.parametrize("code", [ErrorCode.AX_ERROR, ErrorCode.TIMEOUT])
def test_press_atomic_classifies_other_codes_as_acted_unknown(code: ErrorCode) -> None:
    host, operations = _ops()
    host.press_results.append(MacOSError("ambiguous outcome", code=code))

    error = _failed(lambda: operations.press(app="Demo", text="Save"))

    assert error.receipt.acted is Acted.UNKNOWN
    assert error.receipt.changed is None


def test_set_is_convergent_and_reads_back_changed_value() -> None:
    host, operations = _ops()
    host.value = "old"

    changed = operations.set("new", app="Demo", text="Field")
    already = operations.set("new", app="Demo", text="Field")

    assert changed.outcome is Outcome.DONE
    assert changed.acted is Acted.YES
    assert changed.observed == _value_summary("new")
    assert changed.changed is True
    assert changed.verified is False
    assert already.outcome is Outcome.ALREADY
    assert already.acted is Acted.NO
    assert already.changed is False
    assert already.verified is False
    assert host.set_calls == 1


def test_set_post_dispatch_read_failure_returns_failed_receipt() -> None:
    host, operations = _ops()
    host.get_results.extend(("old", MacOSError("readback failed", code=ErrorCode.AX_ERROR)))

    error = _failed(lambda: operations.set("new", app="Demo", text="Field"))

    assert error.receipt.acted is Acted.YES
    assert error.receipt.observed == _value_summary("old")
    assert error.receipt.changed is None
    assert host.set_calls == 1


def test_set_reports_failed_convergence() -> None:
    host, operations = _ops()
    host.get_results.extend(("old", "other"))

    error = _failed(lambda: operations.set("new", app="Demo", text="Field"))

    assert error.receipt.acted is Acted.YES
    assert error.receipt.observed == _value_summary("other")
    assert error.receipt.changed is True


def test_toggle_is_convergent_and_presses_only_when_needed() -> None:
    host, operations = _ops()
    host.toggle_on_press = True

    changed = operations.toggle(True, app="Demo", text="Enabled")
    already = operations.toggle(True, app="Demo", text="Enabled")

    assert changed.outcome is Outcome.DONE
    assert changed.observed == _value_summary(True)
    assert changed.changed is True
    assert already.outcome is Outcome.ALREADY
    assert already.changed is False
    assert host.action_calls == 1


def test_toggle_post_dispatch_read_failure_returns_failed_receipt() -> None:
    host, operations = _ops()
    host.get_results.extend((False, MacOSError("readback failed", code=ErrorCode.AX_ERROR)))

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled"))

    assert error.receipt.acted is Acted.YES
    assert error.receipt.observed == _value_summary(False)
    assert error.receipt.changed is None
    assert host.action_calls == 1


def test_toggle_malformed_owner_pid_returns_failed_receipt_without_raising_valueerror() -> None:
    host, operations = _ops()
    host.match["app"] = {**host.app_info, "pid": "not-a-pid"}

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled"))

    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert host.action_calls == 0


def test_set_deadline_exhausted_before_dispatch_fails_without_acting() -> None:
    host, operations = _ops(monotonic=_delayed_clock(2))
    host.value = "old"

    error = _failed(lambda: operations.set("new", app="Demo", text="Field", timeout=5.0))

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.error["code"] == ErrorCode.TIMEOUT.value
    assert error.receipt.error["details"]["reason"] == "deadline_exhausted_before_dispatch"
    assert host.set_calls == 0


def test_toggle_deadline_exhausted_before_dispatch_fails_without_acting() -> None:
    host, operations = _ops(monotonic=_delayed_clock(2))
    host.value = False

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled", timeout=5.0))

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.error["code"] == ErrorCode.TIMEOUT.value
    assert error.receipt.error["details"]["reason"] == "deadline_exhausted_before_dispatch"
    assert host.action_calls == 0


def test_set_receipt_never_contains_the_raw_secret_value() -> None:
    host, operations = _ops()
    host.value = "old-value"
    secret = "correct horse battery staple"

    receipt = operations.set(secret, app="Demo", text="Password")

    blob = repr(receipt.to_json())
    assert secret not in blob
    assert receipt.request["value"] == _value_summary(secret)
    assert receipt.observed == _value_summary(secret)
    assert host.value == secret  # the real value still reached the host


def test_set_convergence_failure_error_never_contains_the_raw_secret_value() -> None:
    host, operations = _ops()
    host.get_results.extend(("old", "still-old"))
    secret = "super-secret-password"

    error = _failed(lambda: operations.set(secret, app="Demo", text="Password"))

    blob = repr(error.receipt.to_json()) + repr(error.to_json())
    assert secret not in blob
    assert "still-old" not in blob
    assert error.receipt.error["details"]["requested"] == _value_summary(secret)
    assert error.receipt.error["details"]["observed"] == _value_summary("still-old")


def test_toggle_receipt_uses_value_summaries_not_raw_booleans() -> None:
    host, operations = _ops()
    host.get_results.extend((False, False))

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled"))

    assert error.receipt.request["desired"] == _value_summary(True)
    assert error.receipt.error["details"]["requested"] == _value_summary(True)
    assert error.receipt.error["details"]["observed"] == _value_summary(False)


def test_run_dry_run_compiles_only_and_does_not_reserve() -> None:
    factory = PopenFactory([_exit_script(0), _exit_script(0)])
    _host, operations = _ops(spawn=factory)

    planned = operations.run("return 1", once="script", dry_run=True)
    done = operations.run("return 1", once="script")

    compile_command, compile_kwargs = factory.calls[0]
    run_command, run_kwargs = factory.calls[1]
    assert compile_command[0] == "/usr/bin/osacompile"
    assert run_command == ["/usr/bin/osascript", "-l", "AppleScript", "-"]
    assert compile_kwargs["shell"] is False
    assert compile_kwargs["start_new_session"] is True
    assert run_kwargs["shell"] is False
    assert planned.outcome is Outcome.PLANNED
    assert planned.acted is Acted.NO
    assert planned.verified is False
    assert planned.changed is False
    assert done.outcome is Outcome.DONE
    assert done.executor is Executor.SCRIPT


def test_run_dry_run_failure_never_reserves_or_dispatches() -> None:
    factory = PopenFactory([_exit_script(1, stderr=b"syntax error")])
    _host, operations = _ops(spawn=factory)

    error = _failed(lambda: operations.run("bad syntax", once="compile-fail", dry_run=True))

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.NO
    assert len(factory.calls) == 1

    # dry_run never touches the once ledger: a fresh, non-dry-run call
    # reusing the same token dispatches for real instead of replaying or
    # colliding with an unfinished reservation.
    receipt = operations.run("return 1", once="compile-fail")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False


def test_run_passes_argv_without_shell_new_session_and_redacts_source_and_args() -> None:
    factory = PopenFactory([_exit_script(0, stdout=b"ok")])
    _host, operations = _ops(spawn=factory)
    source = "on run argv\n" + ('set secret to "x"\n' * 20) + "end run"
    argv = ["a; touch /tmp/no", "$(whoami)", "quoted value", "swordfish-secret"]

    receipt = operations.run(source, args=argv)

    command, kwargs = factory.calls[0]
    request = receipt.request
    source_summary = request["source"]
    args_summary = request["args"]
    assert command == ["/usr/bin/osascript", "-l", "AppleScript", "-", *argv]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["text"] is False
    assert source not in repr(request)
    assert "secret" not in repr(request)
    assert source_summary == {
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "length": len(source),
    }
    assert "head" not in source_summary
    assert args_summary == tuple(
        {"sha256": hashlib.sha256(item.encode()).hexdigest(), "length": len(item)} for item in argv
    )
    for item in argv:
        assert item not in repr(request)
        assert item not in repr(receipt)
    assert "swordfish-secret" not in str(receipt.to_json())
    assert receipt.outcome is Outcome.DONE
    assert receipt.observed["stdout"] == {
        "bytes": 2,
        "sha256": hashlib.sha256(b"ok").hexdigest(),
        "truncated": False,
    }


def test_run_output_flood_is_hashed_and_bounded_without_text_by_default() -> None:
    total = 3_000_000
    factory = PopenFactory([_flood_script(total)])
    _host, operations = _ops(spawn=factory)

    receipt = operations.run("return 1", timeout=15.0)

    stdout = receipt.observed["stdout"]
    assert stdout["bytes"] == total
    assert stdout["sha256"] == hashlib.sha256(b"x" * total).hexdigest()
    assert stdout["truncated"] is True
    assert "text" not in stdout


def test_run_capture_output_opts_into_bounded_text() -> None:
    factory = PopenFactory([_flood_script(9000)])
    _host, operations = _ops(spawn=factory)

    receipt = operations.run("return 1", capture_output=True)

    stdout = receipt.observed["stdout"]
    assert stdout["bytes"] == 9000
    assert stdout["sha256"] == hashlib.sha256(b"x" * 9000).hexdigest()
    assert stdout["truncated"] is True
    assert len(stdout["text"]) == 8000
    assert stdout["text"] == "x" * 7988 + "…(truncated)"


def test_run_secrets_stay_out_of_receipt_by_default() -> None:
    factory = PopenFactory([_exit_script(0, stdout=b"password123", stderr=b"leaked-secret")])
    _host, operations = _ops(spawn=factory)

    receipt = operations.run("return 1")

    observed = receipt.observed
    assert "text" not in observed["stdout"]
    assert "text" not in observed["stderr"]
    assert observed["stdout"]["bytes"] == len(b"password123")
    assert observed["stderr"]["bytes"] == len(b"leaked-secret")
    assert "password123" not in repr(receipt)
    assert "leaked-secret" not in repr(receipt)


def test_run_nonzero_exit_is_acted_yes_failed_and_replays() -> None:
    factory = PopenFactory([_exit_script(1, stderr=b"boom")])
    _host, operations = _ops(spawn=factory)

    first = _failed(lambda: operations.run("return 1", once="nonzero"))

    assert first.receipt.outcome is Outcome.FAILED
    assert first.receipt.acted is Acted.YES
    assert first.receipt.changed is None
    assert first.code == ErrorCode.AX_ERROR
    assert "boom" not in str(first)
    assert first.receipt.observed["returncode"] == 1
    assert "text" not in first.receipt.observed["stderr"]

    second = _failed(lambda: operations.run("return 1", once="nonzero"))

    assert second.receipt.replayed is True
    assert len(factory.calls) == 1


def test_run_capture_output_includes_failure_text_when_opted_in() -> None:
    factory = PopenFactory([_exit_script(1, stderr=b"boom")])
    _host, operations = _ops(spawn=factory)

    error = _failed(lambda: operations.run("return 1", capture_output=True))

    assert "boom" in str(error)
    assert error.receipt.observed["stderr"]["text"] == "boom"


def test_run_timeout_kills_entire_process_group_including_descendant(tmp_path: Path) -> None:
    marker_path = tmp_path / "grandchild.pid"
    factory = PopenFactory([_hang_with_descendant_script(marker_path)])
    _host, operations = _ops(spawn=factory)

    error = _failed(lambda: operations.run("repeat", once="timeout-group", timeout=1.0))

    assert error.code == ErrorCode.TIMEOUT
    assert error.receipt.acted is Acted.UNKNOWN
    assert error.receipt.changed is None
    assert len(factory.calls) == 1

    poll_deadline = time.monotonic() + 5.0
    grandchild_pid: int | None = None
    while time.monotonic() < poll_deadline:
        if marker_path.exists():
            content = marker_path.read_text().strip()
            if content:
                grandchild_pid = int(content)
                break
        time.sleep(0.05)
    assert grandchild_pid is not None, "grandchild never started"

    poll_deadline = time.monotonic() + 5.0
    dead = False
    while time.monotonic() < poll_deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            dead = True
            break
        time.sleep(0.05)
    assert dead, "grandchild survived the process-group kill"

    replay = _failed(lambda: operations.run("repeat", once="timeout-group", timeout=1.0))

    assert replay.receipt.replayed is True
    assert len(factory.calls) == 1


def test_run_spawn_failure_is_finalized_without_retry() -> None:
    factory = PopenFactory(error=OSError("missing osascript"))
    _host, operations = _ops(spawn=factory)

    first = _failed(lambda: operations.run("return 1", once="spawn"))
    second = _failed(lambda: operations.run("return 1", once="spawn"))

    assert first.receipt.acted is Acted.NO
    assert first.receipt.changed is False
    assert first.code == ErrorCode.AX_ERROR
    assert second.receipt.replayed is True
    assert len(factory.calls) == 1


def test_run_unexpected_exception_during_dispatch_is_cleaned_up_and_replayable() -> None:
    spawner = RaisingWaitSpawner()
    _host, operations = _ops(spawn=spawner)

    first = _failed(lambda: operations.run("return 1", once="base-exc"))

    assert first.receipt.outcome is Outcome.FAILED
    assert first.receipt.acted is Acted.UNKNOWN
    assert first.receipt.changed is None
    assert first.code == ErrorCode.AX_ERROR
    assert spawner.process.wait_calls == 2

    second = _failed(lambda: operations.run("return 1", once="base-exc"))

    assert second.receipt.replayed is True
    assert len(spawner.calls) == 1


@pytest.mark.parametrize(
    "source,args,detail",
    [
        ("ok\x00bad", (), "source"),
        ("ok\udcffbad", (), "source"),
        ("x" * 262_145, (), "source"),
        ("return 1", tuple(f"a{i}" for i in range(257)), "args"),
        ("return 1", ("y" * 65_537,), "args"),
        ("return 1", ("bad\x00arg",), "args"),
        ("return 1", ("bad\udcffarg",), "args"),
    ],
)
def test_run_rejects_invalid_or_oversized_input_before_spawn_or_reservation(
    source: str, args: tuple[str, ...], detail: str
) -> None:
    factory = PopenFactory()
    _host, operations = _ops(spawn=factory)

    with pytest.raises(MacOSError) as caught:
        operations.run(source, args=args, once="bad-input")

    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert caught.value.details["parameter"] == detail
    assert factory.calls == []

    # The once token was never reserved by the rejected call: a fresh,
    # valid call reusing the same token dispatches normally instead of
    # being treated as a collision or an unfinished reservation.
    receipt = operations.run("return 1", once="bad-input")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False


def test_run_rejects_invalid_language_before_spawn_or_reservation() -> None:
    factory = PopenFactory()
    _host, operations = _ops(spawn=factory)

    with pytest.raises(MacOSError) as caught:
        operations.run("return 1", language="bash", once="bad-language")

    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert caught.value.details["parameter"] == "language"
    assert factory.calls == []

    with pytest.raises(MacOSError) as nul_caught:
        operations.run("return 1", language="AppleScript\x00", once="bad-language")

    assert nul_caught.value.code == ErrorCode.BAD_REQUEST
    assert nul_caught.value.details["parameter"] == "language"
    assert factory.calls == []

    # Neither rejected call ever reserved the once token: a fresh, valid
    # call reusing the same token dispatches normally instead of being
    # treated as a collision or an unfinished reservation.
    receipt = operations.run("return 1", once="bad-language")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False


def test_run_changed_is_true_only_when_postcondition_verified() -> None:
    host, operations = _ops(spawn=PopenFactory([_exit_script(0), _exit_script(0), _exit_script(0)]))

    no_postcondition = operations.run("return 1")

    assert no_postcondition.outcome is Outcome.DONE
    assert no_postcondition.changed is None
    assert no_postcondition.verified is False

    verified = operations.run("return 1", postcondition=gone("Saved", app="Demo"))

    assert verified.outcome is Outcome.DONE
    assert verified.changed is True
    assert verified.verified is True

    host.gone_error = MacOSError("still there", code=ErrorCode.AX_ERROR)
    failed_to_verify = _failed(lambda: operations.run("return 1", postcondition=gone("Saved", app="Demo")))

    assert failed_to_verify.receipt.outcome is Outcome.FAILED
    assert failed_to_verify.receipt.changed is None
    assert failed_to_verify.receipt.verified is False


def test_run_requires_explicit_postcondition_scope_before_dispatch() -> None:
    factory = PopenFactory()
    _host, operations = _ops(spawn=factory)

    with pytest.raises(MacOSError, match="no scope"):
        operations.run("return 1", postcondition=present("Saved"))

    assert factory.calls == []


def test_key_dry_run_validates_resolves_and_does_not_reserve() -> None:
    host, operations = _ops()

    planned = operations.key("cmd+s", app="Demo", once="key", dry_run=True)
    done = operations.key("cmd+s", app="Demo", once="key")

    assert planned.outcome is Outcome.PLANNED
    assert planned.verified is False
    assert host.validated_keys == ["cmd+s", "cmd+s"]
    assert host.key_calls == [("cmd+s", 41)]
    assert done.outcome is Outcome.DONE
    assert done.changed is None


def test_key_replay_skips_validation_and_resolution() -> None:
    host, operations = _ops()
    original = operations.key("return", app="Demo", once="key-replay")
    host.validation_error = MacOSError("must not validate", code=ErrorCode.BAD_REQUEST)

    replay = operations.key("return", app="Demo", once="key-replay")

    assert original.replayed is False
    assert original.changed is None
    assert replay.replayed is True
    assert host.validated_keys == ["return"]
    assert host.key_calls == [("return", 41)]


def test_key_focus_failure_is_finalized_and_replayed() -> None:
    host, operations = _ops()
    host.key_error = FocusChangedError("focus moved")

    first = _failed(lambda: operations.key("return", app="Demo", once="key-focus"))
    second = _failed(lambda: operations.key("return", app="Demo", once="key-focus"))

    assert first.code == ErrorCode.FOCUS_CHANGED
    assert first.receipt.acted is Acted.YES
    assert first.receipt.changed is None
    assert second.receipt.replayed is True
    assert len(host.key_calls) == 1


def test_key_malformed_owner_pid_fails_without_burning_once_token() -> None:
    host, operations = _ops()
    host.app_info["pid"] = "not-a-pid"

    error = _failed(lambda: operations.key("return", app="Demo", once="bad-pid-key"))
    host.app_info["pid"] = 41
    receipt = operations.key("return", app="Demo", once="bad-pid-key")

    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False
    assert host.key_calls == [("return", 41)]


def test_key_deadline_exhausted_before_dispatch_fails_without_acting_or_burning_token() -> None:
    host, operations = _ops(monotonic=_delayed_clock(1))

    error = _failed(
        lambda: operations.key("return", app="Demo", once="deadline-key", timeout=5.0)
    )

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.NO
    assert error.receipt.changed is False
    assert error.receipt.error["code"] == ErrorCode.TIMEOUT.value
    assert error.receipt.error["details"]["reason"] == "deadline_exhausted_before_dispatch"
    assert host.key_calls == []

    receipt = operations.key("return", app="Demo", once="deadline-key")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False
    assert host.key_calls == [("return", 41)]


def test_key_changed_is_none_without_a_verified_postcondition_or_on_ambiguous_failure() -> None:
    host, operations = _ops()
    no_postcondition = operations.key("return", app="Demo")
    assert no_postcondition.changed is None

    host.key_error = FocusChangedError("focus moved")
    focus_failure = _failed(lambda: operations.key("return", app="Demo"))
    assert focus_failure.receipt.changed is None

    host2, operations2 = _ops()
    host2.key_error = MacOSError("key failed", code=ErrorCode.AX_ERROR)
    ambiguous_failure = _failed(lambda: operations2.key("return", app="Demo"))
    assert ambiguous_failure.receipt.changed is None


def test_key_changed_is_true_once_a_postcondition_verifies() -> None:
    _host, operations = _ops()

    receipt = operations.key("return", app="Demo", postcondition=gone("Save", role="button"))

    assert receipt.outcome is Outcome.DONE
    assert receipt.changed is True


def test_once_collision_rejects_different_request_without_dispatch() -> None:
    host, operations = _ops()
    operations.key("return", app="Demo", once="same")

    with pytest.raises(MacOSError) as caught:
        operations.key("escape", app="Demo", once="same")

    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert len(host.key_calls) == 1


def test_concurrent_duplicate_reports_in_progress_without_redispatch() -> None:
    host, operations = _ops()
    entered = threading.Event()
    release = threading.Event()
    result: list[object] = []

    def block() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    def dispatch() -> None:
        result.append(operations.press(app="Demo", text="Save", once="race"))

    host.press_hook = block
    worker = threading.Thread(target=dispatch)
    worker.start()
    assert entered.wait(timeout=1.0)

    duplicate = _failed(lambda: operations.press(app="Demo", text="Save", once="race"))
    recalled = _failed(lambda: operations.recall("race"))
    release.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert duplicate.receipt.acted is Acted.UNKNOWN
    assert recalled.receipt.acted is Acted.UNKNOWN
    assert len(host.press_calls) == 1
    assert len(result) == 1
    assert not isinstance(result[0], BaseException)


def test_dispatch_lock_serializes_concurrent_different_once_tokens() -> None:
    host, operations = _ops()
    first_dispatching = threading.Event()
    release_first = threading.Event()

    def block_first_dispatch() -> None:
        first_dispatching.set()
        assert release_first.wait(timeout=1.0)

    host.press_hook = block_first_dispatch
    first_worker = threading.Thread(
        target=lambda: operations.press(app="Demo", text="Save", once="lock-a")
    )
    first_worker.start()
    assert first_dispatching.wait(timeout=1.0)
    assert len(host.press_calls) == 1

    second_worker = threading.Thread(
        target=lambda: operations.press(app="Demo", text="Save", once="lock-b")
    )
    second_worker.start()

    # The first call already dispatched once and is now blocked mid-
    # dispatch, still holding `_dispatch_lock`. A second, differently-
    # tokened call must not even be able to start its own dispatch until
    # the first releases the lock -- proving the lock serializes every
    # mutating call on this one `Operations`, not just same-token
    # replays (already covered by
    # `test_concurrent_duplicate_reports_in_progress_without_redispatch`).
    second_worker.join(timeout=0.2)
    assert second_worker.is_alive() is True
    assert len(host.press_calls) == 1

    release_first.set()
    first_worker.join(timeout=1.0)
    second_worker.join(timeout=1.0)

    assert first_worker.is_alive() is False
    assert second_worker.is_alive() is False
    assert len(host.press_calls) == 2


def test_unexpected_interruption_leaves_incomplete_reservation() -> None:
    host, operations = _ops()
    host.press_results.append(RuntimeError("interrupted"))

    with pytest.raises(RuntimeError, match="interrupted"):
        operations.press(app="Demo", text="Save", once="incomplete")
    duplicate = _failed(
        lambda: operations.press(app="Demo", text="Save", once="incomplete")
    )
    recalled = _failed(lambda: operations.recall("incomplete"))

    assert duplicate.receipt.acted is Acted.UNKNOWN
    assert recalled.receipt.op == "recall"
    assert recalled.receipt.acted is Acted.UNKNOWN
    assert len(host.press_calls) == 1


def test_recall_returns_completed_receipt_as_replay() -> None:
    _host, operations = _ops()
    original = operations.press(app="Demo", text="Save", once="done")

    replay = operations.recall("done")

    assert replay.op == original.op
    assert replay.once == "done"
    assert replay.replayed is True


def test_recall_rejects_unknown_token() -> None:
    _host, operations = _ops()

    with pytest.raises(MacOSError) as caught:
        operations.recall("unknown")

    assert caught.value.code == ErrorCode.BAD_REQUEST


# --- defect regressions: timing validation, set-value canonicalization, ---
# --- guard-masking, and the timed-out kill/reap race -----------------------


@pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf"), float("-inf")])
def test_operation_timeout_rejects_non_finite_before_dispatch(bad_timeout: float) -> None:
    factory = PopenFactory()
    host, operations = _ops(spawn=factory)

    calls: list[Callable[[], object]] = [
        lambda: operations.press(app="Demo", text="Save", timeout=bad_timeout),
        lambda: operations.set("new", app="Demo", text="Field", timeout=bad_timeout),
        lambda: operations.toggle(True, app="Demo", text="Enabled", timeout=bad_timeout),
        lambda: operations.key("return", app="Demo", timeout=bad_timeout),
        lambda: operations.run("return 1", timeout=bad_timeout),
    ]
    for call in calls:
        with pytest.raises(MacOSError) as caught:
            call()
        assert caught.value.code == ErrorCode.BAD_REQUEST
        assert caught.value.details["parameter"] == "timeout"

    assert host.wait_calls == []
    assert host.action_calls == 0
    assert host.press_calls == []
    assert host.set_calls == 0
    assert host.key_calls == []
    assert factory.calls == []


@pytest.mark.parametrize("bad_interval", [float("nan"), float("inf"), 0.0, -0.5])
def test_operation_interval_rejects_non_finite_or_non_positive_before_dispatch(
    bad_interval: float,
) -> None:
    factory = PopenFactory()
    host, operations = _ops(spawn=factory)

    calls: list[Callable[[], object]] = [
        lambda: operations.press(app="Demo", text="Save", interval=bad_interval),
        lambda: operations.set("new", app="Demo", text="Field", interval=bad_interval),
        lambda: operations.toggle(True, app="Demo", text="Enabled", interval=bad_interval),
    ]
    for call in calls:
        with pytest.raises(MacOSError) as caught:
            call()
        assert caught.value.code == ErrorCode.BAD_REQUEST
        assert caught.value.details["parameter"] == "interval"

    assert host.wait_calls == []
    assert host.action_calls == 0
    assert host.press_calls == []
    assert host.set_calls == 0
    assert host.key_calls == []
    assert factory.calls == []


def test_operation_timing_validation_precedes_once_reservation() -> None:
    host, operations = _ops()

    with pytest.raises(MacOSError) as caught:
        operations.press(app="Demo", text="Save", once="timing", timeout=float("nan"))
    assert caught.value.code == ErrorCode.BAD_REQUEST

    # The once token was never reserved by the rejected call: a fresh, valid
    # call reusing the same token dispatches normally instead of being
    # rejected as a collision or replayed as an unfinished reservation.
    receipt = operations.press(app="Demo", text="Save", once="timing")

    assert receipt.outcome is Outcome.DONE
    assert receipt.replayed is False
    assert len(host.press_calls) == 1


def test_set_rejects_non_json_safe_value_before_any_host_effect() -> None:
    host, operations = _ops()

    with pytest.raises(MacOSError) as caught:
        operations.set(float("nan"), app="Demo", text="Field")

    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert caught.value.details["parameter"] == "value"
    assert host.wait_calls == []
    assert host.set_calls == 0


def test_set_dispatches_the_callers_raw_value_not_the_canonicalized_copy() -> None:
    host, operations = _ops()
    host.value = []
    raw_value = [1, 2, 3]

    receipt = operations.set(raw_value, app="Demo", text="Field")

    assert receipt.outcome is Outcome.DONE
    assert host.value is raw_value
    assert receipt.request["value"] == _value_summary((1, 2, 3))


def test_set_dispatch_failure_is_ambiguous_not_confirmed() -> None:
    host, operations = _ops()
    host.set_error = MacOSError("set failed", code=ErrorCode.AX_ERROR)

    error = _failed(lambda: operations.set("new", app="Demo", text="Field"))

    assert error.receipt.outcome is Outcome.FAILED
    assert error.receipt.acted is Acted.UNKNOWN
    assert error.receipt.changed is None
    assert host.set_calls == 1


def test_press_action_failure_is_ambiguous_not_confirmed() -> None:
    host, operations = _ops()
    host.press_results.append(MacOSError("press failed", code=ErrorCode.AX_ERROR))

    error = _failed(lambda: operations.press(app="Demo", text="Save", once="press-fail"))

    assert error.code == ErrorCode.AX_ERROR
    assert error.receipt.acted is Acted.UNKNOWN
    assert len(host.press_calls) == 1


def test_press_guard_never_masks_a_failed_action() -> None:
    """Only reachable for `apps`/`all_apps` scope now: a single, explicit
    `app` press dispatches atomically through `ax_press`, which owns its
    own dispatch-then-guard ordering (see `test_press_atomic_*` above)."""
    host, operations = _ops()
    host.action_error = MacOSError("press failed", code=ErrorCode.AX_ERROR)
    host.guard_error = FocusChangedError("focus moved")

    error = _failed(lambda: operations.press(apps=["Demo"], text="Save"))

    assert error.code == ErrorCode.AX_ERROR
    assert error.receipt.acted is Acted.UNKNOWN
    assert host.action_calls == 1
    assert host.guard_focus_calls == 0


def test_toggle_action_failure_is_ambiguous_not_confirmed() -> None:
    host, operations = _ops()
    host.action_error = MacOSError("press failed", code=ErrorCode.AX_ERROR)

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled"))

    assert error.receipt.acted is Acted.UNKNOWN
    assert host.action_calls == 1


def test_toggle_guard_never_masks_a_failed_action() -> None:
    host, operations = _ops()
    host.action_error = MacOSError("press failed", code=ErrorCode.AX_ERROR)
    host.guard_error = FocusChangedError("focus moved")

    error = _failed(lambda: operations.toggle(True, app="Demo", text="Enabled"))

    assert error.code == ErrorCode.AX_ERROR
    assert error.receipt.acted is Acted.UNKNOWN
    assert error.receipt.changed is None
    assert host.action_calls == 1
    assert host.guard_focus_calls == 0


def test_key_dispatch_failure_is_ambiguous_not_confirmed() -> None:
    host, operations = _ops()
    host.key_error = MacOSError("key failed", code=ErrorCode.AX_ERROR)

    error = _failed(lambda: operations.key("return", app="Demo"))

    assert error.receipt.acted is Acted.UNKNOWN
    assert len(host.key_calls) == 1
