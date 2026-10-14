"""Shared test setup. No test touches the person's state folder, socket or pointer."""

from __future__ import annotations

import os
import shutil
import signal
import tempfile

import pytest

# The drawn pointer is off before any module is imported.
os.environ["MACOS_HARNESS_POINTER"] = "0"


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Each test gets its own state folder and its own socket path."""
    # A Unix socket path holds about 100 bytes, and tmp_path is longer than that.
    short = tempfile.mkdtemp(prefix="mh-", dir="/tmp")
    monkeypatch.setenv("MACOS_HARNESS_POINTER", "0")
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MACOS_HARNESS_SOCKET", os.path.join(short, "helper.sock"))
    # Nothing leaves the machine from a test. A test that wants the payload patches again.
    monkeypatch.setattr("macos_harness.telemetry._send", lambda payload: None)
    monkeypatch.delenv("MACOS_HARNESS_SESSION", raising=False)
    # No test holds the real display awake, and none sees another test's sessions.
    from macos_harness import helper

    quiet = helper.Awake(lambda: 1, lambda token: None, lambda: None)
    monkeypatch.setattr(helper, "AWAKE", quiet)
    monkeypatch.setattr(helper, "SESSIONS", helper.Sessions())
    yield tmp_path / "state"
    shutil.rmtree(short, ignore_errors=True)


TEST_LIMIT_S = 60.0  # the whole suite takes about 15 s. A live test may take 120 s


@pytest.fixture(autouse=True)
def time_limit(request):
    """A test that hangs fails with its traceback, and the tests after it still run."""
    limit = TEST_LIMIT_S * (2 if "live" in request.keywords else 1)

    def late(signum, frame):
        raise TimeoutError(f"the test ran longer than {limit:.0f} s")

    before = signal.signal(signal.SIGALRM, late)  # it interrupts a blocked call too
    signal.setitimer(signal.ITIMER_REAL, limit)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, before)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("MACOS_HARNESS_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set MACOS_HARNESS_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
