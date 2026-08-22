"""Tests for the release-tag/package-version consistency seam the publish
workflow runs before any build work, so a real mismatch fails fast instead
of publishing a wheel under the wrong tag."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from macos_harness import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_release_tag.py"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _load_script():
    spec = importlib.util.spec_from_file_location("check_release_tag", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_release_tag = _load_script()


def test_package_version_reads_the_single_source_of_truth() -> None:
    assert check_release_tag.package_version() == __version__


def test_check_accepts_a_matching_tag() -> None:
    check_release_tag.check(f"v{__version__}")  # must not raise


def test_check_rejects_a_mismatched_tag() -> None:
    with pytest.raises(SystemExit, match="does not match"):
        check_release_tag.check("v0.0.0")


def test_check_rejects_a_tag_missing_the_v_prefix() -> None:
    with pytest.raises(SystemExit, match="does not match"):
        check_release_tag.check(__version__)


def test_main_requires_exactly_one_tag_argument() -> None:
    assert check_release_tag.main([]) == 2
    assert check_release_tag.main(["v1", "v2"]) == 2


def test_main_reports_success_for_the_real_version(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = check_release_tag.main([f"v{__version__}"])
    assert exit_code == 0
    assert "matches package version" in capsys.readouterr().out


def test_main_exits_nonzero_for_a_bad_tag() -> None:
    with pytest.raises(SystemExit):
        check_release_tag.main(["v9.9.9"])


def test_script_is_directly_executable_as_a_subprocess() -> None:
    """The publish workflow invokes this with plain `python3`, not pytest
    -- prove the file itself runs standalone, not just when imported."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), f"v{__version__}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "matches package version" in result.stdout


def _external_action_refs(workflow_path: Path) -> list[str]:
    """Every `uses:` reference to an external action (not a local `./`
    action) across all jobs and steps in a workflow file."""
    workflow = yaml.safe_load(workflow_path.read_text())
    refs: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                refs.append(uses)
    return refs


def test_workflow_actions_are_pinned_to_immutable_commit_shas() -> None:
    """A mutable tag (`@v4`) or branch ref can be repointed by the action's
    publisher after review, silently changing what CI/publish runs. Every
    external `uses:` reference in every workflow must be pinned to the exact
    40-character commit SHA it was reviewed at."""
    unpinned = []
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for ref in _external_action_refs(workflow_path):
            _action, _, pin = ref.partition("@")
            if not _COMMIT_SHA.match(pin):
                unpinned.append(f"{workflow_path.name}: {ref}")
    assert not unpinned, f"unpinned external action reference(s): {unpinned}"


def test_readme_receipt_field_list_names_the_real_duration_s_field() -> None:
    """`Receipt.duration_s` (receipts.py) is the actual dataclass field. The
    README's machine-first field list must name it, not a `duration` field
    that doesn't exist on the class."""
    readme = (REPO_ROOT / "README.md").read_text()
    match = re.search(
        r"Every call returns an immutable, JSON-safe `Receipt`.*?receipt\.to_json\(\)",
        readme,
        re.DOTALL,
    )
    assert match is not None, "could not find the Receipt field list in README.md"
    receipt_fields = match.group(0)
    assert "duration_s" in receipt_fields
    assert re.search(r"\bduration\b", receipt_fields) is None
