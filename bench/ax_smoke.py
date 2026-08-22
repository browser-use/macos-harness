#!/usr/bin/env python3
"""Repeated background AX-query benchmark comparing the python and native backends.

Runs a fixed number of background accessibility queries against a stable
target (default: Finder) once per backend that is actually available, then
prints latency for each. This is diagnostic output only: the script never
asserts a latency threshold and never fails because a backend is slow.

The native backend owns exactly one ``MacOS(backend="native")`` instance
for the whole run: its first native call -- timed explicitly and reported
as "cold launch" -- is what actually spawns and hands off to the private
agent child (via that instance's own ``_acquire_native()``, the same lazy
path every public call already goes through; nothing here reaches into
its session storage directly). Every query after that, including the
first, reuses that one session, and it is closed in a ``finally`` no
matter how the run ends. The first query after cold launch is reported
separately from the rest, so a slow warm-up never buries the steady-state
median/p95 -- and never inflates them either.

It does assert two correctness invariants:

- The frontmost application never changes across the whole run. Every query
  here is background-only, so the process that was frontmost before the
  first query must still be frontmost after the last one.
- When both backends actually ran, they must agree on the same query: same
  match count, same multiset of AX roles. A native/python result mismatch on
  an identical query is a real protocol or search-parity bug, not noise.

The native half is entirely optional and skips itself, printing why, in any
of these cases:

- macOS Accessibility permission is not granted. Nothing in this script can
  run without it, so the whole benchmark (both backends) is skipped.
- ``macos_harness.agent`` is not importable in this checkout yet.
- The native agent cannot be launched and cannot be built on demand
  (missing Xcode command line tools, a build failure, or any other
  ``AgentUnavailableError``).
- The freshly launched agent's own ping reports ``trusted`` as anything
  other than ``True``.

Run directly; this is not a pytest test and takes no test dependency:

    python bench/ax_smoke.py
    python bench/ax_smoke.py --iterations 25 --app Finder
"""

from __future__ import annotations

import argparse
import math
import statistics
import time
from typing import Any

from macos_harness import AccessibilityPermissionError, MacOS

try:
    from macos_harness import agent as native_agent
except ImportError:  # native lifecycle module not present in this checkout yet
    native_agent = None

QueryRun = tuple[list[float], list[dict[str, Any]]]


def _frontmost_pid() -> int | None:
    """Best-effort frontmost PID, tolerant of the private helper moving."""
    frontmost = getattr(MacOS, "_frontmost_app", None)
    if frontmost is None:
        return None
    info = frontmost()
    return None if info is None else int(info["pid"])


def _time_queries(mac: MacOS, app: str, iterations: int) -> QueryRun:
    durations: list[float] = []
    matches: list[dict[str, Any]] = []
    for _ in range(iterations):
        started = time.perf_counter()
        matches = mac.ax.query(app=app, role="any", limit=10)
        durations.append(time.perf_counter() - started)
    return durations, matches


def _role_multiset(matches: list[dict[str, Any]]) -> list[str]:
    return sorted(str(match.get("role")) for match in matches)


def _percentile(durations: list[float], fraction: float) -> float:
    """Nearest-rank percentile; robust for the small samples this script runs."""
    if not durations:
        raise ValueError("no durations to compute a percentile from")
    ordered = sorted(durations)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _report(label: str, durations: list[float]) -> None:
    median_ms = statistics.median(durations) * 1000
    p95_ms = _percentile(durations, 0.95) * 1000
    print(
        f"{label} backend: {len(durations)} queries, "
        f"median {median_ms:.2f} ms, p95 {p95_ms:.2f} ms"
    )


def _run_python_backend(app: str, iterations: int) -> QueryRun | None:
    mac = MacOS(backend="python")
    if not mac.is_accessibility_trusted():
        print("skip: Accessibility permission is not granted; nothing to benchmark.")
        return None
    return _time_queries(mac, app, iterations)


def _run_native_backend(app: str, iterations: int) -> QueryRun | None:
    """Own exactly one native session for the whole run, cold launch and all.

    ``mac._acquire_native()`` is the *same* lazy path ``mac.ax.query(...)``
    would trigger on its own on first use; calling it directly here only
    lets the launch itself be timed apart from the first query, never
    bypasses or duplicates it. No session is ever injected into ``mac``'s
    own storage by hand.
    """
    if native_agent is None:
        print("skip native: macos_harness.agent is not available in this build.")
        return None

    mac = MacOS(backend="native")
    try:
        launch_started = time.perf_counter()
        try:
            client = mac._acquire_native()
        except native_agent.AgentUnavailableError as exc:
            print(f"skip native: {exc}")
            return None
        launch_ms = (time.perf_counter() - launch_started) * 1000
        if client is None:
            print("skip native: native agent is not available.")
            return None
        if client.ping().get("trusted") is not True:
            print("skip native: native agent is not Accessibility-trusted.")
            return None
        print(f"native backend: cold launch {launch_ms:.2f} ms")

        first_started = time.perf_counter()
        matches = mac.ax.query(app=app, role="any", limit=10)
        first_ms = (time.perf_counter() - first_started) * 1000
        print(f"native backend: first query {first_ms:.2f} ms")

        steady_iterations = iterations - 1
        if steady_iterations <= 0:
            return [], matches
        return _time_queries(mac, app, steady_iterations)
    finally:
        mac.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--app", default="Finder", help="Background app to query (default: Finder)"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=15,
        help="Queries per backend (default: 15)",
    )
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be positive")

    frontmost_before = _frontmost_pid()

    try:
        python_run = _run_python_backend(args.app, args.iterations)
    except AccessibilityPermissionError as exc:
        print(f"skip: {exc}")
        return 0
    if python_run is None:
        return 0
    python_durations, python_matches = python_run
    _report("python", python_durations)

    native_run = _run_native_backend(args.app, args.iterations)
    if native_run is not None:
        native_durations, native_matches = native_run
        if native_durations:
            _report("native", native_durations)
        else:
            print(
                "native backend: only cold launch + first query ran "
                "(pass --iterations >= 2 for steady-state median/p95)"
            )

        python_roles = _role_multiset(python_matches)
        native_roles = _role_multiset(native_matches)
        assert len(python_matches) == len(native_matches), (
            f"backend parity: python returned {len(python_matches)} matches, "
            f"native returned {len(native_matches)} for the same query"
        )
        assert python_roles == native_roles, (
            "backend parity: python and native returned different AX roles "
            f"for the same query: {python_roles} != {native_roles}"
        )

    frontmost_after = _frontmost_pid()
    if frontmost_before is not None and frontmost_after is not None:
        assert frontmost_before == frontmost_after, (
            f"frontmost invariant violated: pid {frontmost_before} before, "
            f"pid {frontmost_after} after a background-only benchmark run"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
