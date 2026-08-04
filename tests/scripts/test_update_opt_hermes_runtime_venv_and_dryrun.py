"""CLAWD-3507 — the venv must survive any target, and `recover --dry-run` must be inert.

WHY THIS FILE EXISTS
--------------------
Two independent adversarial gates and one abandoned prior run all derived the same
BLOCKING defect, and the 34-test suite was green through every one of them.

**The defect.** `_apply` establishes "venv/ is ignored, therefore `clean -fd` is safe"
BEFORE `git reset --hard <target>` replaces `.gitignore` with the target commit's copy.
`git clean -fd` then consults the NEW rules. If the target no longer lists `venv/` — an
ordinary tidy-up commit — the live interpreter that all 11 Hermes gateways execute is
untracked-and-not-ignored, and the clean deletes it. No `-x` is required. Git cannot
restore it, because it was never tracked; the tool's own `recover` fails and the fleet
has no Python.

The original suite structurally could not see this: it writes the SAME `.gitignore`
string to both source and runtime, so ignore rules are identical across every reset in
all 34 tests. That is why these fixtures deliberately make the target's `.gitignore`
DIFFER from the runtime's — that difference is the entire bug.

**The second defect.** `_recover` never read `args.dry_run`, so `recover --dry-run`
performed a real `reset --hard` + `clean -fd`, rolled the live runtime back unannounced,
and consumed the transaction while reporting success. `recover` is the verb an operator
reaches for mid-incident and `--dry-run` is what a careful one types first.

WHAT THESE TESTS PIN
--------------------
Not "the code contains `-e /venv`" — that is a signature, and a signature is not a
property. They drive real git trees and assert the interpreter is still there afterwards.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"

IGNORES_VENV = "venv/\n__pycache__/\n*.pyc\n"
DROPS_VENV = "__pycache__/\n*.pyc\n"  # the ordinary tidy-up commit that triggers it


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


@pytest.fixture()
def world(tmp_path: Path):
    """A bare source plus a NON-git runtime holding a gitignored venv.

    Mirrors production: /opt/hermes-agent has no .git, and its venv/ is both
    gitignored and the interpreter the fleet runs.
    """
    source = tmp_path / "source.git"
    subprocess.run(["git", "init", "-q", "--bare", str(source)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(source), str(seed)], check=True)

    (seed / "gateway").mkdir()
    (seed / ".gitignore").write_text(IGNORES_VENV)
    (seed / "gateway" / "run.py").write_text("VERSION_A\n")
    old = _commit(seed, "baseline: venv is ignored")

    # The target: carries a real fix AND stops ignoring venv/.
    (seed / ".gitignore").write_text(DROPS_VENV)
    (seed / "gateway" / "run.py").write_text("VERSION_B\n")
    target_drops = _commit(seed, "fix + tidy .gitignore (drops venv/)")

    # Control: same fix, venv/ still ignored.
    _git(seed, "checkout", "-q", "-b", "control", old)
    (seed / ".gitignore").write_text(IGNORES_VENV)
    (seed / "gateway" / "run.py").write_text("VERSION_B\n")
    target_keeps = _commit(seed, "fix, venv still ignored")
    _git(seed, "push", "-q", "origin", "--all")

    runtime = tmp_path / "opt-hermes-agent"
    runtime.mkdir()
    subprocess.run(
        f"git -C {seed} archive {old} | tar -x -C {runtime}", shell=True, check=True
    )
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    (runtime / "venv" / "CANARY").write_text("DO-NOT-DELETE-ME\n")
    (runtime / "gateway" / "ORPHAN_FILE.py").write_text("orphan\n")

    # Convert to a pinned checkout the way `init` does: metadata only.
    _git(runtime, "init", "-q", "-b", "main")
    _git(runtime, "remote", "add", "origin", str(source))
    _git(runtime, "fetch", "-q", "origin")
    _git(runtime, "reset", "--mixed", "-q", old)

    return {
        "runtime": runtime,
        "old": old,
        "target_drops": target_drops,
        "target_keeps": target_keeps,
    }


def _subject():
    import importlib.util

    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _advance(runtime: Path, target: str) -> None:
    """The two mutating steps `_apply` runs, using the PRODUCTION clean.

    An earlier version of this helper rebuilt the clean itself from
    `mod.VENV_EXCLUDE`. That re-implemented the invariant instead of exercising it,
    so deleting the exclusion from the production call sites left every test in this
    file green — a decorative suite over a fleet-destroying defect. It now calls
    `_clean_runtime`, which is the single home of the invariant.
    """
    mod = _subject()
    _git(runtime, "reset", "--hard", "-q", target)
    mod._clean_runtime(runtime)


def test_venv_survives_a_target_that_stops_ignoring_it(world):
    """THE defect. Break the exclusion and this deletes the fleet's interpreter."""
    rt = world["runtime"]
    assert (rt / "venv" / "CANARY").exists()

    _advance(rt, world["target_drops"])

    assert (rt / "venv" / "CANARY").exists(), (
        "the live venv was deleted by advancing to a target whose .gitignore no longer "
        "lists venv/. In production this is all 11 gateways losing their interpreter, "
        "and git cannot restore it because it was never tracked."
    )
    assert (rt / "venv" / "bin" / "python").exists()
    # and the advance still did its job
    assert (rt / "gateway" / "run.py").read_text() == "VERSION_B\n"


def test_venv_survives_the_control_target_too(world):
    """Control: proves the test above fails for the RIGHT reason, not by accident."""
    rt = world["runtime"]
    _advance(rt, world["target_keeps"])
    assert (rt / "venv" / "CANARY").exists()
    assert (rt / "gateway" / "run.py").read_text() == "VERSION_B\n"


def test_the_advance_still_sweeps_real_orphans(world):
    """The exclusion must not turn the clean into a no-op.

    Without this, 'never delete anything' would pass the test above while defeating
    the tool's actual purpose.
    """
    rt = world["runtime"]
    assert (rt / "gateway" / "ORPHAN_FILE.py").exists()
    _advance(rt, world["target_drops"])
    assert not (rt / "gateway" / "ORPHAN_FILE.py").exists(), (
        "orphan sweeping regressed — the venv exclusion must be anchored to the "
        "runtime root, not swallow the whole clean"
    )


def test_clean_preview_does_not_name_the_venv(world):
    """`--dry-run` must not advertise removing the interpreter."""
    mod = _subject()
    preview = mod._clean_preview(world["runtime"])
    assert not any("venv" in line for line in preview), preview
    assert any("ORPHAN_FILE" in line for line in preview), (
        "preview stopped reporting genuine removals — it must model the real clean"
    )


def test_venv_exclude_is_anchored_to_the_runtime_root():
    """`/venv`, not `venv` — an unanchored pattern would also spare a nested vendor dir."""
    mod = _subject()
    assert mod.VENV_EXCLUDE.startswith("/"), mod.VENV_EXCLUDE


def test_recover_dry_run_mutates_nothing(world, tmp_path):
    """`recover` is reached for mid-incident; --dry-run must be inert."""
    rt = world["runtime"]
    head_before = _git(rt, "rev-parse", "HEAD").strip()
    (rt / "SENTINEL").write_text("untracked, must survive a dry run\n")

    proc = subprocess.run(
        [
            sys.executable, str(SUBJECT), "recover",
            "--runtime", str(rt),
            "--dry-run",
            "--transaction-dir", str(tmp_path / "txn"),
            "--lock-file", str(tmp_path / "lock"),
        ],
        capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["dry_run"] is True
    assert payload["restart_performed"] is False

    assert _git(rt, "rev-parse", "HEAD").strip() == head_before, (
        "recover --dry-run moved HEAD — it performed a real rollback of a live runtime"
    )
    assert (rt / "SENTINEL").exists(), (
        "recover --dry-run deleted untracked files — it ran a real `git clean`"
    )
    assert (rt / "venv" / "CANARY").exists()
