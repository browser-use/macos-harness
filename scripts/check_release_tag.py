#!/usr/bin/env python3
"""Fails unless a release tag equals ``v`` + the package's own version.

The publish workflow runs this as its first step, before any dependency
install or build work, so a mistagged release fails in under a second
instead of after minutes of building and signing a universal2 binary.

Reads the version directly out of ``src/macos_harness/_version.py`` --
the same file Hatchling's ``[tool.hatch.version]`` regex source reads --
rather than importing the package, so this works on a bare checkout before
anything is installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "src" / "macos_harness" / "_version.py"
_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def package_version(version_file: Path = VERSION_FILE) -> str:
    match = _VERSION_PATTERN.search(version_file.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"check_release_tag: no __version__ assignment found in {version_file}")
    return match.group(1)


def check(tag: str, version_file: Path = VERSION_FILE) -> None:
    expected = f"v{package_version(version_file)}"
    if tag != expected:
        raise SystemExit(
            f"check_release_tag: release tag {tag!r} does not match package version {expected!r}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0]:
        print("usage: check_release_tag.py <tag>", file=sys.stderr)
        return 2
    check(argv[0])
    print(f"check_release_tag: release tag {argv[0]!r} matches package version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
