"""Packaging tests for the bundled universal2 native agent.

These exercise the release packaging path end to end: pyproject.toml's
wheel `artifacts`/build-hook configuration and hatch_build.py's validation
and tag logic, built with a real `uv build --sdist --wheel` -- exactly what
the publish workflow runs -- so a passing test proves the hook actually
runs, and fails closed, under a normal release build rather than only in
isolation. Every test uses a tiny synthetic universal2 executable built on
the fly with clang -- never the real Swift agent, which is git-ignored by
design and covered by the `native`-marked tests elsewhere -- so none of
this requires a committed binary.
"""

from __future__ import annotations

import contextlib
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hatch_build.py"
AGENT_PATH = REPO_ROOT / "src" / "macos_harness" / "bin" / "macos-harness-agent"
AGENT_MEMBER = "macos_harness/bin/macos-harness-agent"

_REQUIRED_TOOLS = ("clang", "lipo", "codesign", "otool", "uv")
_MISSING_TOOLS = [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]

pytestmark = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"missing required tool(s) for packaging tests: {', '.join(_MISSING_TOOLS)}",
)

# A driver script run inside a `uv run --with hatchling` subprocess: hatchling
# is a build-system dependency (pyproject.toml [build-system]), not a project
# dev dependency, so hatch_build.py -- which imports it -- cannot be loaded
# in-process here. `--no-project` skips installing macos-harness itself, so
# this never depends on the concurrent state of the rest of the package.
#
# Prints one of:
#   "absent"            -- nothing bundled; the wheel stays py3-none-any
#   "ok <major>_<minor>" -- a validated binary; the derived tag suffix
#   "rejected <message>" -- a bundled binary failed validation
#
# `fake_platform`, if non-empty, monkeypatches platform.system() before the
# hook ever runs, to exercise the non-macOS-host rejection without needing a
# second OS. `fake_otool_path`, if non-empty, points at a file whose text
# hatch_build.py's `_run` returns for any `otool` invocation instead of
# actually running it -- letting minos-parsing failures be exercised
# precisely without hand-forging a Mach-O with a broken load command; lipo
# and codesign still run for real against the given binary.
_HOOK_DRIVER = """
import importlib.util, platform, sys
from pathlib import Path
hook_path, binary_path, fake_platform, fake_otool_path = sys.argv[1:5]
if fake_platform:
    platform.system = lambda: fake_platform
spec = importlib.util.spec_from_file_location("hatch_build", hook_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if fake_otool_path:
    fake_output = Path(fake_otool_path).read_text()
    real_run = module._run
    def _fake_run(command):
        return fake_output if command[0] == "otool" else real_run(command)
    module._run = _fake_run
binary = Path(binary_path)
if not module._bundled_agent_present(binary):
    print("absent")
else:
    try:
        print("ok", module._validate_bundled_universal2(binary))
    except module.BundledAgentValidationError as exc:
        print("rejected", exc)
"""


# A second driver, run the same way, that calls `NativeAgentTagHook.initialize()`
# itself -- the real entry point hatchling invokes for every build, including an
# editable one -- rather than the lower-level helpers `_HOOK_DRIVER` exercises
# directly. `version` is hatchling's own "standard"/"editable" build_data
# version string. Prints "ok <pure_python> <tag>" (the resulting build_data)
# or "rejected <message>".
_INITIALIZE_DRIVER = """
import importlib.util, sys
from pathlib import Path
hook_path, root, version = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("hatch_build", hook_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
hook = module.NativeAgentTagHook(root, {}, None, None, root, "wheel")
build_data = {"pure_python": True, "tag": "py3-none-any"}
try:
    hook.initialize(version, build_data)
    print("ok", build_data["pure_python"], build_data["tag"])
except module.BundledAgentValidationError as exc:
    print("rejected", exc)
"""


def _build_fixture_binary(dest: Path, *, deployment_target: str, archs: Sequence[str]) -> None:
    """Builds a tiny real Mach-O executable for the given architectures and
    deployment target, then ad-hoc signs it -- enough for lipo/otool/codesign
    to treat it exactly like a real release build of the agent. clang's own
    automatic "linker-signed" ad-hoc signature does not satisfy `codesign
    --verify` (only an explicit `codesign --sign -` pass does), which is
    itself why the real build script re-signs after combining architectures.
    """
    source = dest.with_suffix(".c")
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    command = ["clang"]
    for arch in archs:
        command += ["-arch", arch]
    command += [f"-mmacosx-version-min={deployment_target}", "-o", str(dest), str(source)]
    subprocess.run(command, check=True, capture_output=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(dest)], check=True, capture_output=True)
    dest.chmod(0o755)


def _codesign_verifies(path: Path) -> bool:
    result = subprocess.run(["codesign", "--verify", "--strict", str(path)], check=False, capture_output=True)
    return result.returncode == 0


def _archs_of(path: Path) -> set[str]:
    result = subprocess.run(["lipo", "-archs", str(path)], check=True, capture_output=True, text=True)
    return set(result.stdout.split())


def _run_hook_driver(
    binary: Path, *, fake_platform: str = "", fake_otool_output: str | None = None
) -> tuple[str, str]:
    """Runs hatch_build.py's validation against `binary` and returns
    (status, payload): status is "absent" (nothing bundled), "ok" (payload
    is the derived macOS version tag), or "rejected" (payload is the
    BundledAgentValidationError message)."""
    fake_otool_path = ""
    if fake_otool_output is not None:
        fake_otool_file = binary.with_name(binary.name + ".fake-otool-output")
        fake_otool_file.write_text(fake_otool_output)
        fake_otool_path = str(fake_otool_file)

    process = subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "hatchling",
            "--no-project",
            "python3",
            "-c",
            _HOOK_DRIVER,
            str(HOOK_PATH),
            str(binary),
            fake_platform,
            fake_otool_path,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    status, _, payload = process.stdout.strip().partition(" ")
    return status, payload


def _hook_tag(binary: Path, *, fake_platform: str = "") -> str | None:
    """Returns the macOS version tag hatch_build.py derives for `binary`
    ("<major>_<minor>"), or None if it leaves the wheel unbundled/untagged."""
    status, payload = _run_hook_driver(binary, fake_platform=fake_platform)
    assert status in ("absent", "ok"), f"expected no rejection, got status={status!r} payload={payload!r}"
    return payload or None


def _hook_rejection(binary: Path, *, fake_platform: str = "", fake_otool_output: str | None = None) -> str:
    """Asserts hatch_build.py rejects `binary` (raises
    BundledAgentValidationError) and returns the exception message."""
    status, payload = _run_hook_driver(binary, fake_platform=fake_platform, fake_otool_output=fake_otool_output)
    assert status == "rejected", f"expected a rejection, got status={status!r} payload={payload!r}"
    return payload


def _run_initialize_driver(*, version: str) -> tuple[str, str]:
    """Runs the real `NativeAgentTagHook.initialize(version, build_data)`
    -- hatchling's actual per-build entry point -- against whatever
    currently sits at the real bundled agent path (place it first with
    `_bundled_agent_binary`) and returns (status, payload): status is "ok"
    (payload is "<pure_python> <tag>", the resulting build_data) or
    "rejected" (payload is the BundledAgentValidationError message)."""
    process = subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "hatchling",
            "--no-project",
            "python3",
            "-c",
            _INITIALIZE_DRIVER,
            str(HOOK_PATH),
            str(REPO_ROOT),
            version,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    status, _, payload = process.stdout.strip().partition(" ")
    return status, payload


@contextlib.contextmanager
def _bundled_agent_binary(source: Path) -> Iterator[Path]:
    """Temporarily copies `source` to the real bundled agent path, where the
    wheel's `artifacts` glob and hatch_build.py both look, then removes it --
    and the bin/ directory, if this created it -- afterward."""
    bin_dir = AGENT_PATH.parent
    bin_dir_existed = bin_dir.is_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, AGENT_PATH)
    AGENT_PATH.chmod(0o755)
    try:
        yield AGENT_PATH
    finally:
        AGENT_PATH.unlink(missing_ok=True)
        if not bin_dir_existed:
            shutil.rmtree(bin_dir, ignore_errors=True)


def _build_release_dists(out_dir: Path) -> tuple[Path, Path]:
    """Runs the exact command the publish workflow runs -- `uv build --sdist
    --wheel`, building both distributions directly from the source tree in
    one invocation -- and returns (sdist_path, wheel_path). Building them
    together like this, rather than one target at a time, is what makes it
    the release form: bare `uv build` (neither flag) instead builds the
    wheel from the intermediate sdist archive, which would silently drop
    anything -- like the bundled native binary -- that exists in the
    working tree but is intentionally excluded from the sdist.
    """
    subprocess.run(
        ["uv", "build", "--sdist", "--wheel", "-o", str(out_dir), str(REPO_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    (sdist_path,) = out_dir.glob("*.tar.gz")
    (wheel_path,) = out_dir.glob("*.whl")
    return sdist_path, wheel_path


def _dist_info_member(archive: zipfile.ZipFile, filename: str) -> str:
    (match,) = (name for name in archive.namelist() if name.endswith(f".dist-info/{filename}"))
    return match


# ---------------------------------------------------------------------------
# Hook logic: does hatch_build.py derive the right version, or correctly
# reject, for each kind of binary it might find at the bundled path?
# ---------------------------------------------------------------------------


def test_hook_derives_version_from_genuine_universal2_binary(tmp_path: Path) -> None:
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="12.3", archs=("arm64", "x86_64"))

    assert _hook_tag(binary) == "12_3"


def test_hook_rejects_missing_binary(tmp_path: Path) -> None:
    assert _hook_tag(tmp_path / "does-not-exist") is None


def test_hook_rejects_single_arch_binary(tmp_path: Path) -> None:
    """A binary missing one architecture must never be tagged universal2 --
    that would advertise a Mac the wheel cannot actually run on."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64",))

    message = _hook_rejection(binary)
    assert "arm64" in message and "x86_64" in message


def test_hook_rejects_superset_architecture(tmp_path: Path) -> None:
    """A binary with an extra architecture beyond arm64+x86_64 is rejected
    too -- the wheel tag promises exactly a universal2 binary, not whatever
    happened to get linked into it."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64", "arm64e"))

    assert "arm64e" in _hook_rejection(binary)


def test_hook_refuses_native_tag_off_macos_even_with_valid_binary(tmp_path: Path) -> None:
    """A build host that is not macOS must never claim native support, even
    if a stray valid binary happens to sit at the bundled path."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64"))

    assert "macOS" in _hook_rejection(binary, fake_platform="Linux")


def test_hook_rejects_symlinked_binary(tmp_path: Path) -> None:
    """A symlink at the bundled path is rejected even when it resolves to a
    genuinely valid binary -- the hook must validate exactly the dirent that
    the wheel's `artifacts` glob will bundle, not whatever it points to."""
    real_binary = tmp_path / "real-agent"
    _build_fixture_binary(real_binary, deployment_target="13.0", archs=("arm64", "x86_64"))
    link = tmp_path / "macos-harness-agent"
    link.symlink_to(real_binary)

    assert "regular file" in _hook_rejection(link)


def test_hook_rejects_wrong_permission_mode(tmp_path: Path) -> None:
    """Only the exact mode the build script produces (0o755) is trusted --
    anything else did not necessarily come from a real release build."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64"))
    binary.chmod(0o644)

    assert "0o755" in _hook_rejection(binary)


def test_hook_parses_three_component_minos_using_major_minor_and_drops_the_patch(tmp_path: Path) -> None:
    """A genuine three-component minos (major.minor.patch, e.g. clang's own
    `-mmacosx-version-min=13.0.1`) is valid input, not a parse failure --
    the hook must derive the wheel tag from major.minor alone."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0.1", archs=("arm64", "x86_64"))

    assert _hook_tag(binary) == "13_0"


def test_hook_skips_validation_entirely_for_editable_wheel_version(tmp_path: Path) -> None:
    """`pip install -e .`/`uv pip install -e .` must never validate or tag
    against whatever binary happens to sit at the bundled path -- a broken
    or single-arch binary left over from local development must never fail
    an editable install the way it must fail a real release build."""
    fixture = tmp_path / "macos-harness-agent"
    _build_fixture_binary(fixture, deployment_target="13.0", archs=("arm64",))  # invalid: single-arch

    with _bundled_agent_binary(fixture):
        status, payload = _run_initialize_driver(version="editable")

    assert status == "ok", f"expected editable to skip validation, got status={status!r} payload={payload!r}"
    assert payload == "True py3-none-any", "editable build_data must be left completely untouched"


def test_hook_still_fails_closed_for_the_standard_wheel_version(tmp_path: Path) -> None:
    """The same invalid binary that an editable build must skip still fails
    a standard (non-editable) wheel build -- editable is the only version
    this hook ever skips."""
    fixture = tmp_path / "macos-harness-agent"
    _build_fixture_binary(fixture, deployment_target="13.0", archs=("arm64",))

    with _bundled_agent_binary(fixture):
        status, payload = _run_initialize_driver(version="standard")

    assert status == "rejected", f"expected the standard version to fail closed, got status={status!r}"
    assert "arm64" in payload and "x86_64" in payload


def test_hook_rejects_broken_signature(tmp_path: Path) -> None:
    """A binary that is otherwise a structurally valid universal2 Mach-O but
    whose signature no longer verifies -- e.g. modified after signing --
    must be rejected, not trusted just because lipo/otool still parse it."""
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64"))
    assert _codesign_verifies(binary)
    binary.write_bytes(binary.read_bytes() + b"\x00")
    assert not _codesign_verifies(binary)

    assert "codesign" in _hook_rejection(binary)


def test_hook_rejects_missing_minos_load_command(tmp_path: Path) -> None:
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64"))

    message = _hook_rejection(binary, fake_otool_output="      cmd LC_SEGMENT_64\n  cmdsize 72\n")
    assert "minos" in message


def test_hook_rejects_unparseable_minos_version(tmp_path: Path) -> None:
    binary = tmp_path / "macos-harness-agent"
    _build_fixture_binary(binary, deployment_target="13.0", archs=("arm64", "x86_64"))

    message = _hook_rejection(
        binary,
        fake_otool_output="      cmd LC_BUILD_VERSION\n platform 1\n    minos not-a-version\n      sdk 26.5\n",
    )
    assert "minos" in message and "not-a-version" in message


# ---------------------------------------------------------------------------
# End to end: does a real `uv build --sdist --wheel` -- the exact command
# CI/release runs -- actually produce the right wheel tag, member,
# permissions, and signature, and actually fail closed on an invalid one?
# ---------------------------------------------------------------------------


def test_wheel_without_bundled_binary_stays_untagged(tmp_path: Path) -> None:
    if AGENT_PATH.exists():
        pytest.skip("a real bundled binary already sits at the agent path")

    _sdist_path, wheel_path = _build_release_dists(tmp_path / "dist")

    assert wheel_path.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel_path) as archive:
        assert AGENT_MEMBER not in archive.namelist()
        wheel_metadata = archive.read(_dist_info_member(archive, "WHEEL")).decode()
    assert "Root-Is-Purelib: true" in wheel_metadata


def test_wheel_with_bundled_universal2_binary_gets_honest_tag(tmp_path: Path) -> None:
    fixture = tmp_path / "macos-harness-agent"
    _build_fixture_binary(fixture, deployment_target="13.0", archs=("arm64", "x86_64"))
    assert _codesign_verifies(fixture)

    with _bundled_agent_binary(fixture):
        _sdist_path, wheel_path = _build_release_dists(tmp_path / "dist")

    assert wheel_path.name.endswith("-py3-none-macosx_13_0_universal2.whl")
    with zipfile.ZipFile(wheel_path) as archive:
        info = archive.getinfo(AGENT_MEMBER)
        mode = stat.S_IMODE(info.external_attr >> 16)
        assert mode == 0o755, f"expected the executable bit preserved in the wheel, got {oct(mode)}"

        extracted = tmp_path / "extracted-agent"
        extracted.write_bytes(archive.read(AGENT_MEMBER))
        extracted.chmod(0o755)

        wheel_metadata = archive.read(_dist_info_member(archive, "WHEEL")).decode()

    assert "Root-Is-Purelib: false" in wheel_metadata
    assert "Tag: py3-none-macosx_13_0_universal2" in wheel_metadata
    assert _archs_of(extracted) == {"arm64", "x86_64"}
    assert _codesign_verifies(extracted)


def test_wheel_build_fails_closed_on_invalid_bundled_binary(tmp_path: Path) -> None:
    """A single-arch binary at the bundled path must fail the actual `uv
    build --sdist --wheel` invocation outright -- never fall back to
    silently publishing it inside a py3-none-any wheel, which would ship an
    unverified macOS Mach-O tagged as pure and platform-independent."""
    fixture = tmp_path / "macos-harness-agent"
    _build_fixture_binary(fixture, deployment_target="13.0", archs=("arm64",))
    out_dir = tmp_path / "dist"

    with _bundled_agent_binary(fixture):
        process = subprocess.run(
            ["uv", "build", "--sdist", "--wheel", "-o", str(out_dir), str(REPO_ROOT)],
            check=False,
            capture_output=True,
            text=True,
        )

    assert process.returncode != 0, process.stderr
    assert not list(out_dir.glob("*.whl"))


def test_sdist_stays_pure_source_even_with_bundled_binary(tmp_path: Path) -> None:
    """The sdist ships native/macos-harness-agent's Swift sources, never a
    compiled binary, regardless of what happens to sit at the bundled path
    when it is built."""
    fixture = tmp_path / "macos-harness-agent"
    _build_fixture_binary(fixture, deployment_target="13.0", archs=("arm64", "x86_64"))

    with _bundled_agent_binary(fixture):
        sdist_path, _wheel_path = _build_release_dists(tmp_path / "dist")

    with tarfile.open(sdist_path) as archive:
        names = archive.getnames()
        assert not any(name.endswith(AGENT_MEMBER) for name in names)
        assert any(name.endswith("native/macos-harness-agent/Package.swift") for name in names)


def test_wheel_ships_py_typed_marker(tmp_path: Path) -> None:
    """PEP 561: a typed package ships an empty `py.typed` marker inside the
    installed package directory, so type checkers trust its own
    annotations instead of treating the package as untyped."""
    _sdist_path, wheel_path = _build_release_dists(tmp_path / "dist")

    with zipfile.ZipFile(wheel_path) as archive:
        member = "macos_harness/py.typed"
        assert member in archive.namelist()
        assert archive.read(member) == b""


def test_sdist_ships_py_typed_marker(tmp_path: Path) -> None:
    sdist_path, _wheel_path = _build_release_dists(tmp_path / "dist")

    with tarfile.open(sdist_path) as archive:
        names = archive.getnames()
        assert any(name.endswith("src/macos_harness/py.typed") for name in names)
