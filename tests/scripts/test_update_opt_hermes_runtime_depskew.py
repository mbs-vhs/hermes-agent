"""CLAWD-3507 — a source-only advance must notice the venv cannot run the new code.

THE GAP. This tool advances SOURCE ONLY and deliberately preserves the venv — the
venv is the interpreter all 11 gateways execute, so not touching it is correct. But
it means new code runs against the OLD environment, and the module had ZERO
dependency awareness: `grep -ci 'pyproject|dependenc|pip|requirement'` returned 0.

Measured on the live fleet: `/opt/hermes-agent/pyproject.toml` pins
`nemo-relay==0.3` as an OPTIONAL EXTRA, while fork `origin/main` declares
`nemo-relay>=0.6.0` as a MAIN dependency. Advancing source alone therefore produces
an import-time failure across every gateway, at the moment the tool reports success.

The check is local — it asks the runtime's own interpreter what it has — and
degrades rather than blocks when `packaging` is unavailable, because a missing
helper library must not make every apply impossible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"


def _subject():
    import importlib.util

    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout


def _runtime_with(tmp_path: Path, deps: list[str]) -> tuple[Path, str]:
    """A runtime whose TARGET commit declares `deps` as main dependencies."""
    rt = tmp_path / "rt"
    rt.mkdir()
    _git(rt.parent, "init", "-q", str(rt)) if False else None
    subprocess.run(["git", "init", "-q", "-b", "main", str(rt)], check=True)
    (rt / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\ndependencies = "
        + json.dumps(deps)
        + "\n"
    )
    _git(rt, "add", "-A")
    _git(rt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t")
    (rt / "venv" / "bin").mkdir(parents=True)
    (rt / "venv" / "bin" / "python").symlink_to(sys.executable)
    return rt, _git(rt, "rev-parse", "HEAD").strip()


def test_a_missing_main_dependency_is_reported(tmp_path: Path):
    """The nemo-relay case: declared by the target, absent from the venv."""
    rt, head = _runtime_with(tmp_path, ["definitely-not-a-real-package-xyz>=1.0"])
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "a dependency the venv does not have was not reported"
    assert "definitely-not-a-real-package-xyz" in skew[0]
    assert "NOT INSTALLED" in skew[0]


def test_a_satisfied_dependency_is_not_reported(tmp_path: Path):
    """Negative control: no false positives on something actually installed."""
    rt, head = _runtime_with(tmp_path, ["pytest"])
    assert _subject()._dependency_skew(rt, head) == []


def test_no_declared_dependencies_is_not_skew(tmp_path: Path):
    rt, head = _runtime_with(tmp_path, [])
    assert _subject()._dependency_skew(rt, head) == []


def test_the_target_pyproject_is_read_from_git_not_the_worktree(tmp_path: Path):
    """At check time the worktree is still at the OLD commit.

    Reading the worktree would consult exactly the pyproject whose staleness is the
    problem. This asserts the target commit's content is what is used.
    """
    rt, head = _runtime_with(tmp_path, ["definitely-not-a-real-package-xyz>=1.0"])
    # Make the WORKTREE claim no dependencies at all.
    (rt / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\ndependencies = []\n")
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "skew was read from the worktree, not from the target commit"


def test_skew_is_surfaced_in_the_plan_and_the_flag_exists():
    """It must be visible in --dry-run, and overridable when genuinely benign."""
    source = SUBJECT.read_text()
    assert '"dependency_skew": skew' in source
    assert "--accept-dependency-skew" in source
    assert '"importable_orphans_to_remove"' in source, (
        "the importable-orphan list must stay in the plan — it was previously "
        "computed and never acted on, gated only by runbook prose"
    )
