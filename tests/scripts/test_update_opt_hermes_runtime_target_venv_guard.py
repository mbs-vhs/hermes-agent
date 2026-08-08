"""CLAWD-3507 — coverage for two DEPLOY-half guards that nothing pinned.

WHY THIS FILE EXISTS
--------------------
Both guards below were added in response to real findings, and both were measured
DECORATIVE by an independent revert-validation pass against `cf6412cb8`: deleting
the production refusal left `scripts/run_tests.sh tests/scripts/` at
`12 files, 150 tests passed, 0 failed`.

1. **`_apply`'s `_target_tracks_under_venv` refusal** (subject ~L1312).
   `-e /venv` constrains `git clean` and NOTHING ELSE. `git reset --hard <target>`
   writes tracked files unconditionally, so a target that tracks anything under
   `venv/` overwrites the interpreter all 11 gateways run, and the automatic
   recovery then resets to a commit that does not track it — deleting it outright.

   The only test that names this hazard,
   `test_DEFECT_a_target_tracking_a_path_under_venv_destroys_the_interpreter`, is
   `xfail(strict=True)` and never calls `_apply`: it hand-runs
   `reset --hard` + `_clean_runtime` + `_restore_steady_transaction`. So it still
   xfails with the guard present AND with the guard deleted — it measures the raw
   git sequence, not the verb an operator invokes. A comment mapping a defect class
   to a test that does not cover it is exactly the shape that lets a fix rot.

2. **`_init`'s worktree fingerprint refusal** (subject ~L1194).
   `test_init_changes_only_git_metadata_and_seeds_exact_head` verifies the OUTCOME
   (`_tree_fingerprint(runtime) == before`) with its own assertion, so removing the
   production refusal changes nothing it can see. Its
   `assert output["worktree_unchanged"] is True` cannot establish that property at
   all: `"worktree_unchanged": True` is a hardcoded literal in the emitted JSON, not
   a measurement. This file pins the REFUSAL — that a seed step which does move a
   worktree byte is stopped and says why.

Nothing here greps the module's source text; every assertion drives the real code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import update_opt_hermes_runtime as updater

IGNORES_VENV = "venv/\n__pycache__/\nlogs/\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


def _backup_receipt(scratch: Path, runtime: Path, tag: str = "b") -> Path:
    """A receipt the tool accepts: fresh, byte-identical round-trip, on disk."""
    archive = scratch / f"{tag}.tar.zst"
    roundtrip = scratch / f"{tag}.roundtrip.tar.zst"
    subprocess.run(
        [
            "/usr/bin/tar", "--zstd", "--format=pax", "--numeric-owner",
            "--acls", "--xattrs", "--xattrs-include=*",
            "--file", str(archive), "--create", "--directory", str(runtime), ".",
        ],
        check=True,
    )
    shutil.copyfile(archive, roundtrip)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = scratch / f"receipt-{tag}.json"
    receipt.write_text(
        json.dumps({
            "runtime": str(runtime),
            "runtime_fingerprint": updater._tree_fingerprint(runtime),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "archive_profile": updater.BACKUP_ARCHIVE_PROFILE,
            "archive_root": ".",
            "archive_path": str(archive),
            "archive_sha256": digest,
            "remote_uri": "r2:test-bucket/opt-hermes-agent/backup.tar.zst",
            "roundtrip_path": str(roundtrip),
            "roundtrip_sha256": digest,
        }),
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    return receipt


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    """A source repo plus a non-git runtime holding the gitignored live venv.

    Mirrors production: `/opt/hermes-agent` has no `.git`, and `venv/bin/python` is
    a real interpreter that git has never tracked. The CANARY lets a test tell
    "the interpreter survived" from "the test happened not to look".
    """
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "pkg").mkdir()
    (source / ".gitignore").write_text(IGNORES_VENV)
    (source / "pkg" / "__init__.py").write_text("")
    (source / "pkg" / "live.py").write_text("VERSION_A\n")
    (source / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\nversion='0'\ndependencies = []\n"
    )
    t1 = _commit(source, "baseline")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(origin)], check=True)
    _git(source, "remote", "add", "origin", str(origin))

    runtime = tmp_path / "opt-hermes-agent"
    (runtime / "pkg").mkdir(parents=True)
    (runtime / ".gitignore").write_text(IGNORES_VENV)
    (runtime / "pkg" / "__init__.py").write_text("")
    (runtime / "pkg" / "live.py").write_text("VERSION_A\n")
    (runtime / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\nversion='0'\ndependencies = []\n"
    )
    # An untracked orphan: the initial-evidence apply refuses an already-clean
    # runtime, so the conversion needs something real to sweep.
    (runtime / "pkg" / "orphan.py").write_text("ORPHAN = True\n")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    (runtime / "venv" / "CANARY").write_text("the interpreter all 11 gateways run\n")
    runtime.chmod(0o755)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return {
        "runtime": runtime,
        "source": source,
        "origin": origin,
        "scratch": scratch,
        "t1": t1,
        "receipts": tmp_path / "receipts",
        "transactions": tmp_path / "transactions",
        "lock": tmp_path / "update.lock",
    }


def _bootstrap(world: dict) -> None:
    """Real init + initial-evidence apply, so steady applies are unlocked."""
    runtime = world["runtime"]
    assert updater.main([
        "init", "--runtime", str(runtime), "--target", world["t1"],
        "--remote-url", str(world["origin"]),
        "--backup-receipt", str(_backup_receipt(world["scratch"], runtime, "init")),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0
    evidence = world["scratch"] / "evidence.json"
    evidence.write_text(
        updater._canonical_json(updater._build_audit(runtime, world["t1"]))
    )
    evidence.chmod(0o600)
    assert updater.main([
        "apply", "--runtime", str(runtime), "--target", world["t1"],
        "--initial-evidence", str(evidence),
        "--backup-receipt", str(_backup_receipt(world["scratch"], runtime, "a2")),
        "--receipt-dir", str(world["receipts"]),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0


def _target_vendoring_the_interpreter(world: dict) -> str:
    """An ordinary-looking commit that also tracks `venv/bin/python`.

    `venv/` stays gitignored, so the clean-side exclusion is fully intact — the
    only thing this changes is what `reset --hard` will WRITE.
    """
    source = world["source"]
    (source / "pkg" / "live.py").write_text("VERSION_B\n")
    (source / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (source / "venv" / "bin" / "python").write_text("#!/bin/sh\necho HIJACKED\n")
    _git(source, "add", "-f", "venv/bin/python")
    head = _commit(source, "fix + vendors a file under venv/")
    _git(source, "push", "-q", "origin", "main")
    return head


def _plain_target(world: dict) -> str:
    """The control: the same fix, tracking nothing under venv/."""
    source = world["source"]
    (source / "pkg" / "live.py").write_text("VERSION_B\n")
    head = _commit(source, "fix, nothing under venv/")
    _git(source, "push", "-q", "origin", "main")
    return head


def _steady_apply(world: dict, target: str, *extra: str, tag: str) -> int:
    return updater.main([
        "apply", "--runtime", str(world["runtime"]), "--target", target,
        "--fetch", "--remote-url", str(world["origin"]),
        "--backup-receipt",
        str(_backup_receipt(world["scratch"], world["runtime"], tag)),
        "--receipt-dir", str(world["receipts"]),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
        *extra,
    ])


# ═══════════ the `_apply` refusal for a target that TRACKS venv paths ═════════


def test_apply_refuses_a_target_that_tracks_a_path_under_venv(
    world, capsys: pytest.CaptureFixture[str]
):
    """The verb an operator actually invokes must refuse, and say why.

    Deleting the refusal from `_apply` leaves the whole tests/scripts suite green,
    which is why this exists. Driven through `main()`, not through the helper.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    capsys.readouterr()

    target = _target_vendoring_the_interpreter(world)
    rc = _steady_apply(world, target, tag="veg1")

    err = capsys.readouterr().err
    assert rc == updater.UNMEASURED_EXIT, (
        f"apply returned {rc} for a target that tracks venv/bin/python; the reset "
        f"would have written the target's copy over the live interpreter. stderr={err!r}"
    )
    assert "tracks 1 path(s) under venv/" in err, err
    assert "venv/bin/python" in err, err


def test_the_venv_tracking_refusal_lands_before_any_mutation(world):
    """A refusal after the reset would be a fleet outage with an error message.

    Pins the ORDER: HEAD, the tracked tree and the live interpreter must all be
    exactly as they were, and the interpreter must still be the untracked symlink
    rather than the target's committed file.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    head_before = updater._head(runtime)
    tree_before = updater._tree_fingerprint(runtime)
    venv_before = updater._venv_guard(runtime)

    target = _target_vendoring_the_interpreter(world)
    assert _steady_apply(world, target, tag="veg2") == updater.UNMEASURED_EXIT

    interpreter = runtime / "venv" / "bin" / "python"
    assert interpreter.is_symlink(), (
        "the refusal came too late: the advance had already written the target's "
        f"committed file over the live interpreter ({interpreter.read_text()!r})"
    )
    assert (runtime / "venv" / "CANARY").exists()
    assert updater._head(runtime) == head_before
    assert updater._tree_fingerprint(runtime) == tree_before
    assert updater._venv_guard(runtime) == venv_before
    assert (runtime / "pkg" / "live.py").read_text() == "VERSION_A\n"


def test_the_dry_run_rehearsal_refuses_the_same_target(
    world, capsys: pytest.CaptureFixture[str]
):
    """`--dry-run` is what a careful operator types first; it must show the hazard.

    If the check sat after the dry-run early-return, the rehearsal would report a
    clean plan and the real apply would then destroy the interpreter.
    """
    _bootstrap(world)
    capsys.readouterr()

    target = _target_vendoring_the_interpreter(world)
    rc = _steady_apply(world, target, "--dry-run", tag="veg3")

    out, err = capsys.readouterr()
    assert rc == updater.UNMEASURED_EXIT, (
        f"a rehearsal over a venv-vendoring target reported success. stdout={out!r}"
    )
    assert "under venv/" in err, err


def test_a_target_tracking_nothing_under_venv_is_accepted(world):
    """Control. Proves the three tests above fail for the RIGHT reason.

    Without this, `return UNMEASURED_EXIT` unconditionally would satisfy them.
    """
    _bootstrap(world)
    runtime = world["runtime"]

    target = _plain_target(world)
    assert _steady_apply(world, target, tag="veg4") == 0

    assert updater._head(runtime) == target
    assert (runtime / "pkg" / "live.py").read_text() == "VERSION_B\n"
    assert (runtime / "venv" / "CANARY").exists()


def test_the_venv_tracking_probe_fails_closed_when_git_cannot_answer(world):
    """An unmeasurable probe must raise, not return an empty list.

    The caller treats a non-empty list as the refusal, so `return []` on a failed
    `ls-tree` would be a silent PASS — the worst possible failure direction for a
    guard whose whole job is to stop a write over the fleet's interpreter.
    """
    _bootstrap(world)
    absent = "0" * 40  # well-formed, and not an object in this repository

    with pytest.raises(updater.UpdateError) as excinfo:
        updater._target_tracks_under_venv(world["runtime"], absent)

    assert "could not determine" in str(excinfo.value), str(excinfo.value)
    assert absent in str(excinfo.value)


# ══════════════ `_init` must refuse a seed step that moved the tree ═══════════


def test_init_refuses_when_the_seed_step_changed_a_worktree_path(
    world, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Pins the REFUSAL, which nothing else does.

    `init` seeds HEAD+index with `reset --mixed` precisely so no worktree byte
    moves; the fingerprint comparison is the fail-closed that catches it if that
    ever stops being true. Removing the comparison alone leaves the suite green,
    because the happy path never trips it. This forces the seed to `--hard` — the
    single most likely way a future edit reintroduces the hazard — and asserts the
    tool stops and names it.
    """
    runtime = world["runtime"]
    # Drift a tracked file so a `--hard` seed would actually rewrite a worktree byte.
    (runtime / "pkg" / "live.py").write_text("LOCAL_HOTFIX\n")
    receipt = _backup_receipt(world["scratch"], runtime, "init-hard")
    capsys.readouterr()

    real_git = updater._git

    def _hard_seed(rt, *args, **kwargs):
        if args[:2] == ("reset", "--mixed"):
            args = ("reset", "--hard") + args[2:]
        return real_git(rt, *args, **kwargs)

    monkeypatch.setattr(updater, "_git", _hard_seed)

    rc = updater.main([
        "init", "--runtime", str(runtime), "--target", world["t1"],
        "--remote-url", str(world["origin"]),
        "--backup-receipt", str(receipt),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ])

    out, err = capsys.readouterr()
    assert rc == updater.UNMEASURED_EXIT, (
        f"init reported success after its seed step rewrote a worktree file. "
        f"stdout={out!r}"
    )
    assert "init changed a non-.git runtime path" in err, err


def test_init_with_the_real_mixed_seed_is_accepted(
    world, capsys: pytest.CaptureFixture[str]
):
    """Control for the test above: the unmonkeypatched seed must still succeed.

    Otherwise `raise` at the top of `_init` would satisfy it.
    """
    runtime = world["runtime"]
    (runtime / "pkg" / "live.py").write_text("LOCAL_HOTFIX\n")
    before = updater._tree_fingerprint(runtime)
    receipt = _backup_receipt(world["scratch"], runtime, "init-mixed")
    capsys.readouterr()

    assert updater.main([
        "init", "--runtime", str(runtime), "--target", world["t1"],
        "--remote-url", str(world["origin"]),
        "--backup-receipt", str(receipt),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0

    assert updater._tree_fingerprint(runtime) == before
    assert (runtime / "pkg" / "live.py").read_text() == "LOCAL_HOTFIX\n"
