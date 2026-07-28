"""Invariants for scripts/run_tests.sh's interpreter selection.

Context (CLAWD-3136): the wrapper used to probe a third venv candidate,
``$HOME/.hermes/hermes-agent/venv``.  That path is a *different* checkout's live
runtime, on its own release cadence, and it can never be the correct interpreter
for this repo:

  * The checkout it belongs to can never select it *as* the fallback — that
    checkout's ``REPO_ROOT`` is ``~/.hermes/hermes-agent``, so ``$REPO_ROOT/venv``
    matches first.  The fallback therefore only ever fired from some *other*
    checkout: this dev fork, or one of its worktrees.
  * A worktree is the mandated workflow here and never has a venv of its own, so
    the fallback fired every time — silently running the gate against an
    interpreter that did not have this repo's declared dependencies installed
    (it was hermes-agent 0.14.0 with no ``Markdown`` while this repo was 0.18.0
    and declares ``Markdown==3.10.2``).  Import-guarded code then took its
    degraded branch, and the gate reported a failure that existed nowhere but
    the harness.

The invariant that matters is behavioural, not textual: **with no repo-local
venv, the runner must fail rather than borrow an interpreter from outside
REPO_ROOT** — even when a plausible-looking one exists under ``$HOME``.  These
tests stage a throwaway repo root plus a decoy ``$HOME/.hermes`` venv and assert
the runner refuses, then assert it *does* select each repo-local candidate in
priority order.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_tests.sh"


def _fake_venv(path: Path, marker: str) -> Path:
    """A directory that looks like a venv to the runner's probe.

    ``bin/python`` echoes ``marker`` and exits 0, so a run that gets as far as
    the final ``exec`` reveals *which* venv was chosen.
    """
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    py = path / "bin" / "python"
    py.write_text(f'#!/bin/sh\necho "{marker}"\n', encoding="utf-8")
    py.chmod(0o755)
    return path


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A throwaway repo root holding a real copy of the runner, and a fake HOME.

    ``REPO_ROOT`` is derived from ``BASH_SOURCE``, so relocating the script is
    the only way to control it.  ``$HOME/.hermes/hermes-agent/venv`` is created
    as a decoy: it is exactly what the removed fallback pointed at.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(RUNNER, root / "scripts" / "run_tests.sh")
    (root / "scripts" / "run_tests.sh").chmod(0o755)

    home = tmp_path / "home"
    _fake_venv(home / ".hermes" / "hermes-agent" / "venv", "DECOY-RUNTIME-VENV")
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(root.parent / "home")
    return subprocess.run(
        ["bash", str(root / "scripts" / "run_tests.sh")],
        capture_output=True, text=True, env=env, cwd=str(root),
    )


def test_refuses_to_borrow_an_interpreter_from_outside_the_repo(staged: Path):
    """No repo-local venv + a runtime venv under $HOME => fail, don't borrow."""
    proc = _run(staged)

    assert proc.returncode != 0, (
        "runner must fail when the repo has no venv; instead it exited 0 "
        f"with stdout={proc.stdout!r}"
    )
    # The decisive assertion: it never reached the exec, so it never ran the
    # foreign interpreter.
    assert "DECOY-RUNTIME-VENV" not in proc.stdout
    assert "no virtualenv found" in proc.stderr


def test_error_names_only_repo_local_candidates(staged: Path):
    """The failure must tell you where it looked — and it looked only in-repo."""
    proc = _run(staged)

    assert str(staged / ".venv") in proc.stderr
    assert str(staged / "venv") in proc.stderr
    # If a future edit re-adds a $HOME candidate, it would have to appear here.
    assert ".hermes" not in proc.stderr


@pytest.mark.parametrize("name", [".venv", "venv"])
def test_selects_each_repo_local_candidate(staged: Path, name: str):
    """Both repo-local candidates are still honoured (this is not a lockout)."""
    _fake_venv(staged / name, f"SELECTED-{name}")

    proc = _run(staged)

    assert f"SELECTED-{name}" in proc.stdout, proc.stderr
    assert "DECOY-RUNTIME-VENV" not in proc.stdout


def test_dot_venv_wins_over_venv(staged: Path):
    """Probe order is load-bearing: `.venv` before `venv`."""
    _fake_venv(staged / ".venv", "SELECTED-dotvenv")
    _fake_venv(staged / "venv", "SELECTED-venv")

    proc = _run(staged)

    assert "SELECTED-dotvenv" in proc.stdout, proc.stderr
    assert "SELECTED-venv" not in proc.stdout


def test_reports_which_venv_it_selected(staged: Path):
    """The chosen interpreter is echoed — silence is what hid CLAWD-3136."""
    _fake_venv(staged / ".venv", "SELECTED-dotvenv")

    proc = _run(staged)

    assert f"venv: {staged / '.venv'}" in proc.stdout
