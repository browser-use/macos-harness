"""Tests for the single source of truth for the package version.

Every consumer -- the public ``macos_harness.__version__``, the CLI's
``--version`` flag, telemetry's payload, and Hatchling's own dynamic
version resolution -- reads the exact same ``src/macos_harness/_version.py``
constant, so a release can never ship with two disagreeing version strings.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

import macos_harness
from macos_harness import __version__, cli, telemetry

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "src" / "macos_harness" / "_version.py"


def test_version_file_defines_the_exported_constant() -> None:
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match is not None
    assert match.group(1) == __version__


def test_version_is_a_plain_release_version() -> None:
    """A bare ``X.Y.Z`` -- what the publish workflow's release-tag check
    expects after prefixing it with ``v``."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_package_exports_version_publicly() -> None:
    assert macos_harness.__version__ == __version__
    assert "__version__" in macos_harness.__all__


def test_cli_and_telemetry_read_the_same_constant_without_importlib_duplication() -> None:
    """``cli.py`` and ``telemetry.py`` both import the constant directly
    instead of re-deriving it via ``importlib.metadata``, which would let
    an editable/uninstalled checkout silently disagree with whatever
    distribution happens to be installed."""
    assert cli.__version__ == __version__
    assert telemetry.__version__ == __version__
    assert not hasattr(telemetry, "version")
    assert not hasattr(telemetry, "PackageNotFoundError")


def test_installed_distribution_metadata_matches() -> None:
    """End to end: Hatchling's dynamic version source derives the exact
    same string for the built/installed distribution, so ``pip show
    macos-harness`` and ``macos_harness.__version__`` can never drift."""
    try:
        dist_version = installed_version("macos-harness")
    except PackageNotFoundError:
        pytest.skip("macos-harness is not installed as a discoverable distribution")
    assert dist_version == __version__
