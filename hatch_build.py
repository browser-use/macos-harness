"""Tags macOS wheels honestly when the universal2 native agent is bundled.

Runs only for the wheel target (sdist and editable builds are untouched --
the sdist stays pure source, carrying native/macos-harness-agent's Swift
sources instead of a compiled binary). scripts/build-universal-agent.sh
places a real arm64+x86_64 binary at src/macos_harness/bin/macos-harness-agent
before release builds; this hook checks that the binary actually sitting
there is a genuine, signed universal2 Mach-O built on this machine and, only
then, tags the wheel py3-none-macosx_<major>_<minor>_universal2 (matching
the deployment target native/macos-harness-agent/Package.swift declares,
which is exactly what a Mach-O binary compiled from that manifest embeds as
its minimum OS version) instead of hatchling's default portable
py3-none-any.

No bundled binary at all leaves the wheel tagged py3-none-any: a wheel that
does not carry a native binary must never claim native macOS support. But a
binary that *is* present and fails validation -- wrong build host, wrong
file type, wrong permissions, the wrong architecture set, a broken
signature, or an unparseable deployment target -- fails the wheel build
outright instead of falling back to py3-none-any. Falling back would still
embed the unverified Mach-O in the wheel via the `artifacts` glob in
pyproject.toml while the wheel's own tag claims it is pure and
platform-independent: exactly the mislabeled-artifact failure this hook
exists to prevent.
"""

from __future__ import annotations

import platform
import stat
import subprocess
from pathlib import Path
from typing import TypedDict

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_AGENT_RELATIVE_PATH = Path("src/macos_harness/bin/macos-harness-agent")
_REQUIRED_ARCHS = frozenset({"arm64", "x86_64"})
_REQUIRED_MODE = 0o755


class BundledAgentValidationError(Exception):
    """The bundled agent binary exists but is not a genuine, signed
    universal2 Mach-O -- the wheel build must fail rather than ship it."""


class _WheelBuildData(TypedDict, total=False):
    """The subset of hatchling's per-target build_data this hook reads and
    writes: whether the wheel is pure Python, and its platform tag."""

    pure_python: bool
    tag: str


class NativeAgentTagHook(BuildHookInterface):
    def initialize(self, version: str, build_data: _WheelBuildData) -> None:
        if self.target_name != "wheel":
            return

        binary = Path(self.root) / _AGENT_RELATIVE_PATH
        if not _bundled_agent_present(binary):
            return  # no bundled binary: ship the existing pure py3-none-any wheel

        macos_version = _validate_bundled_universal2(binary)
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-macosx_{macos_version}_universal2"


def _bundled_agent_present(binary: Path) -> bool:
    """True if anything at all sits at the bundled agent path, including a
    dangling symlink -- which `Path.exists()` alone follows and would
    misreport as absent, letting an invalid dirent slip past unvalidated."""
    return binary.is_symlink() or binary.exists()


def _validate_bundled_universal2(binary: Path) -> str:
    """Returns "<major>_<minor>" for a genuine, ad-hoc-signed arm64+x86_64
    Mach-O built at exactly that deployment target. Raises
    BundledAgentValidationError for anything else."""
    if platform.system() != "Darwin":
        raise BundledAgentValidationError(
            f"{binary} is bundled but this build host is not macOS; cannot verify it is a genuine universal2 Mach-O"
        )

    info = binary.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BundledAgentValidationError(
            f"{binary} is not a regular file (symlinks and other special files are rejected)"
        )

    mode = stat.S_IMODE(info.st_mode)
    if mode != _REQUIRED_MODE:
        raise BundledAgentValidationError(f"{binary} has mode {oct(mode)}, expected {oct(_REQUIRED_MODE)}")

    archs = _run(["lipo", "-archs", str(binary)])
    if archs is None or set(archs.split()) != _REQUIRED_ARCHS:
        got = archs.split() if archs is not None else archs
        raise BundledAgentValidationError(
            f"{binary} must contain exactly {sorted(_REQUIRED_ARCHS)}, `lipo -archs` reported {got!r}"
        )

    if _run(["codesign", "--verify", "--strict", str(binary)]) is None:
        raise BundledAgentValidationError(f"{binary} failed `codesign --verify --strict`")

    load_commands = _run(["otool", "-l", str(binary)])
    if load_commands is None:
        raise BundledAgentValidationError(f"`otool -l {binary}` failed")

    return _max_minos_version(load_commands, binary)


def _max_minos_version(load_commands: str, binary: Path) -> str:
    """Parses every `minos` (LC_BUILD_VERSION) line out of `otool -l` output
    and returns the highest "<major>_<minor>" found. Both slices share one
    deployment target in practice; take the max so a future mismatch can
    never advertise a floor lower than either slice actually requires.
    Raises BundledAgentValidationError if a minos value fails to parse, or
    if the binary has no minos load command at all.
    """
    versions: list[tuple[int, int]] = []
    for line in load_commands.splitlines():
        stripped = line.strip()
        if not stripped.startswith("minos "):
            continue
        raw = stripped.split(maxsplit=1)[1]
        major_digits, _, minor_digits = raw.partition(".")
        if not major_digits.isdigit() or not (minor_digits == "" or minor_digits.isdigit()):
            raise BundledAgentValidationError(f"{binary} has an unparseable minos version: {raw!r}")
        versions.append((int(major_digits), int(minor_digits or 0)))
    if not versions:
        raise BundledAgentValidationError(f"{binary} has no `minos` (LC_BUILD_VERSION) load command")

    major, minor = max(versions)
    return f"{major}_{minor}"


def _run(command: list[str]) -> str | None:
    try:
        return subprocess.run(command, capture_output=True, check=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
