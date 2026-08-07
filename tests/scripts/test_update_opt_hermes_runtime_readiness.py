"""CLAWD-3507 — readiness must judge against what `clean -fd` can actually remove.

THE DEFECT
----------
`_ready` ANDed a gitignore-AWARE clean check (`git status`, `clean -nd`) with a
gitignore-BLIND provenance walk. The walk excludes only a short hardcoded list
(`.git, venv, .venv, __pycache__, node_modules, .pytest_cache, .ruff_cache,
.mypy_cache, test_durations.json`). Any path git ignores but that list does not
name is therefore invisible to `clean -fd` — which will never remove it — and yet
counted as `only_in_tree`, which readiness requires to be zero. `-x` is banned
outright because it would delete the venv, so there is no way to clear it.

That is not hypothetical. The fork's `.gitignore` covers `logs/`, `data/`, `.env`
— files the gateway writes AT LAUNCH. So the first gateway start after a successful
conversion jams every later `apply`, and `rollback` (which shares this preflight)
refuses in precisely the incident it exists for. The tool would solve "a merged fix
cannot reach the fleet" approximately once, then wedge.

WHAT THE FIX MUST NOT DO
------------------------
Make readiness permissive. Two of the tests here are negative controls: a non-ignored
orphan and tracked-file drift must both still refuse.

THE ONE DELIBERATE PERMISSIVENESS, STATED RATHER THAN HIDDEN
------------------------------------------------------------
An ignored-but-IMPORTABLE orphan used to refuse as well, on the reasoning that a stray
module on the import path can shadow real code. Under CLAWD-3655 it no longer does, and
that is a REAL BEHAVIOUR CHANGE, not a deferral — an independent reviewer found it
sitting behind a conditional skip, with this docstring still asserting the old property
the code no longer had. A doc that claims a guard the code dropped is worse than no doc.

The change is deliberate because the old refusal was UNSATISFIABLE. The path is ignored,
so `clean -fd` will never remove it, and `-x` is banned outright because it would delete
the live venv. Refusing therefore produced a state with no operator remedy — the exact
wedge this narrowing exists to remove — while doing nothing about the shadowing risk.

What survives is the SIGNAL, which is the part that was actually load-bearing: `audit`
still reports the orphan under `provenance.counts.only_in_tree_importable`, so an
operator still sees it. What was removed is only its VETO over proceeding. The test
below pins exactly that split, so neither half can be lost quietly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _subject():
    import importlib.util

    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runtime(tmp_path: Path) -> Path:
    """A converted runtime holding exactly the state a gateway writes at launch."""
    source = tmp_path / "src.git"
    subprocess.run(["git", "init", "-q", "--bare", str(source)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(source), str(seed)], check=True)
    (seed / "gateway").mkdir()
    # Mirrors the real fork: the gateway's own runtime state is gitignored.
    (seed / ".gitignore").write_text("venv/\nlogs/\n.env\n__pycache__/\n")
    (seed / "gateway" / "run.py").write_text("A\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    head = _git(seed, "rev-parse", "HEAD").strip()
    _git(seed, "push", "-q", "origin", "HEAD:main")

    rt = tmp_path / "opt-hermes-agent"
    rt.mkdir()
    subprocess.run(
        f"git -C {seed} archive {head} | tar -x -C {rt}", shell=True, check=True
    )
    (rt / "venv" / "bin").mkdir(parents=True)
    (rt / "venv" / "bin" / "python").symlink_to(sys.executable)
    _git(rt, "init", "-q", "-b", "main")
    _git(rt, "remote", "add", "origin", str(source))
    _git(rt, "fetch", "-q", "origin")
    _git(rt, "reset", "--hard", "-q", head)

    # The gateway starts and writes its normal runtime state.
    (rt / "logs").mkdir(exist_ok=True)
    (rt / "logs" / "agent.log").write_text("gateway started\n")
    (rt / ".env").write_text("TOKEN=x\n")
    return rt


def _ready(rt: Path) -> bool:
    mod = _subject()
    return mod._ready(mod._build_audit(rt, mod._head(rt)))


def test_gateway_runtime_state_does_not_jam_readiness(runtime: Path):
    """THE defect: ignored files the gateway writes at launch must not wedge apply."""
    mod = _subject()
    audit = mod._build_audit(runtime, mod._head(runtime))

    # The census still reports them honestly — the walk stays gitignore-blind.
    assert sorted(audit["provenance"]["only_in_tree"]) == [".env", "logs/agent.log"]

    assert _ready(runtime), (
        "readiness is false because of files git ignores and `clean -fd` can never "
        "remove. The gateway writes these at launch, so the first start after a "
        "conversion would jam every later apply and rollback would refuse in the "
        "incident it exists for."
    )


def test_a_non_ignored_orphan_still_refuses(runtime: Path):
    """Negative control: the fix must not blanket-excuse orphans."""
    (runtime / "gateway" / "STRAY.txt").write_text("not ignored\n")
    assert not _ready(runtime)


def test_an_ignored_importable_orphan_is_REPORTED_but_no_longer_VETOES(runtime: Path):
    """The CLAWD-3655 behaviour change, pinned on both sides.

    Was: `_ready` returned False, which the tool could never clear (ignored ⇒ invisible
    to `clean -fd`; `-x` banned because it would delete the live venv). A refusal with
    no remedy is the wedge, not a guard.

    Is: still fully REPORTED so the operator sees the shadowing risk, but it no longer
    blocks. If either half of this test starts failing, say which: losing the report is
    a regression, and restoring the veto re-creates the wedge.
    """
    (runtime / "logs" / "shadow.py").write_text("x = 1\n")
    mod = _subject()
    audit = mod._build_audit(runtime, mod._head(runtime))

    assert audit["provenance"]["counts"]["only_in_tree_importable"] == 1, (
        "the importable orphan is no longer REPORTED — the narrowing removed the veto, "
        "not the signal, and an operator now has no way to see a module that can "
        "shadow real code"
    )
    assert "logs/shadow.py" in audit["provenance"]["only_in_tree"]

    assert mod._ready(audit), (
        "readiness refuses on a gitignored path again. `clean -fd` cannot remove it and "
        "`-x` is banned, so this is a refusal with no operator remedy — the wedge "
        "CLAWD-3655 removed"
    )


def test_tracked_file_drift_still_refuses(runtime: Path):
    """Negative control: real divergence must still block."""
    (runtime / "gateway" / "run.py").write_text("TAMPERED\n")
    assert not _ready(runtime)
    _git(runtime, "checkout", "--", "gateway/run.py")
    assert _ready(runtime), "restoring the file should restore readiness"


def test_git_ignored_helper_is_exact(runtime: Path):
    """The helper must answer about THIS repo's rules, not guess from names."""
    mod = _subject()
    ignored = mod._git_ignored(
        runtime, [".env", "logs/agent.log", "gateway/run.py", "nope.txt"]
    )
    assert ".env" in ignored
    assert "logs/agent.log" in ignored
    assert "gateway/run.py" not in ignored
    assert "nope.txt" not in ignored
    assert mod._git_ignored(runtime, []) == set()
