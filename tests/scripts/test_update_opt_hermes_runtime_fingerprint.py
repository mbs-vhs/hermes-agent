"""CLAWD-3507 — the first conversion must be reachable without already being converted.

THE DEFECT THIS PINS
--------------------
`init` requires `--backup-receipt`; a valid receipt must carry a `runtime_fingerprint`
that `_valid_tree_fingerprint` accepts. The only producer of that object was `audit`,
and `_audit` opens with `_runtime_safety(runtime, require_git=True)`.

So: receipt needs audit -> audit needs .git -> .git is what init creates.

The tool could not perform its own FIRST conversion. That is not a corner case; it is
the entire purpose of the tool, because `/opt/hermes-agent` is a non-git tree. Measured
before the fix: `require_git=False` appeared exactly once in the whole module, inside
`_init` itself.

The negative controls below matter as much as the positive one. A `fingerprint` verb
that quietly accepted an unsafe runtime, or that emitted an object `_validate_backup_receipt`
then rejects, would look like a fix and leave the circularity in place one layer down.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"


def _subject():
    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def nongit_runtime(tmp_path: Path) -> Path:
    """A runtime shaped like /opt/hermes-agent BEFORE conversion: no .git at all."""
    rt = tmp_path / "opt-hermes-agent"
    (rt / "gateway").mkdir(parents=True)
    (rt / "gateway" / "run.py").write_text("A\n")
    (rt / "venv" / "bin").mkdir(parents=True)
    (rt / "venv" / "bin" / "python").symlink_to(sys.executable)
    return rt


def _run(runtime: Path, *args: str, lock: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SUBJECT),
            *args,
            "--runtime",
            str(runtime),
            "--lock-file",
            str(lock),
        ],
        capture_output=True,
        text=True,
    )


def test_fingerprint_runs_on_a_runtime_with_no_git(nongit_runtime: Path, tmp_path: Path):
    """THE defect: this is the state every first conversion starts from."""
    proc = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode == 0, (
        "fingerprint refused a non-git runtime, so the first conversion is still "
        f"unreachable: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["entry_count"] >= 3
    assert payload["algorithm"]


def test_a_receipt_BUILT_FROM_THE_VERB_OUTPUT_passes_the_real_validator(
    nongit_runtime: Path, tmp_path: Path
):
    """The commit's central claim, exercised end to end through the real validator.

    THIS TEST WAS DECORATIVE ON FIRST WRITE and an independent reviewer proved it.
    It called `_valid_tree_fingerprint(_tree_fingerprint(runtime))` — two helpers that
    both PREDATE the fix — so it never invoked the verb and never reached
    `_validate_backup_receipt`. Two measurements: reverting the entire fix (parent
    commit, no `fingerprint` verb at all) left this one test GREEN while the other six
    went red; and a one-line mutant emitting `sha256-canonical-manifest-v1` kept all
    seven green while the operator's real first command died with
    `FATAL: backup receipt runtime_fingerprint is invalid`.

    That is the first gate of the first conversion failing with a green suite — the
    third time this runbook has shipped broken that way. So this now does what it
    always claimed: takes the verb's ACTUAL stdout, builds a real receipt around a real
    archive, and hands it to the real validator.
    """
    mod = _subject()
    proc = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode == 0, proc.stderr
    emitted = json.loads(proc.stdout)

    archive = tmp_path / "backup.tar.zst"
    subprocess.run(
        [
            "tar", "--zstd", "--format=pax", "--numeric-owner", "--acls",
            "--xattrs", "--xattrs-include=*",
            "--directory", str(nongit_runtime), "-cf", str(archive), ".",
        ],
        check=True,
        capture_output=True,
    )
    roundtrip = tmp_path / "roundtrip.tar.zst"
    shutil.copy2(archive, roundtrip)
    digest = mod._sha256(archive)

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "runtime": str(nongit_runtime),
                "runtime_fingerprint": emitted,
                "created_at": dt.datetime.now(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "archive_profile": mod.BACKUP_ARCHIVE_PROFILE,
                "archive_root": ".",
                "archive_path": str(archive),
                "archive_sha256": digest,
                "remote_uri": "r2:example-bucket/opt-hermes-agent/object.tar.zst",
                "roundtrip_path": str(roundtrip),
                "roundtrip_sha256": digest,
            }
        )
    )

    # Must not raise. This is the gate the first conversion actually hits.
    mod._validate_backup_receipt(receipt_path, nongit_runtime)


def test_fingerprint_needs_no_target(nongit_runtime: Path, tmp_path: Path):
    """It dispatches before _target_value; requiring a target would re-block init."""
    proc = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode == 0, proc.stderr
    assert "target" not in proc.stderr.lower()


def test_fingerprint_does_not_create_a_git_dir_or_mutate_the_tree(
    nongit_runtime: Path, tmp_path: Path
):
    """Read-only. If it converted anything it would be doing init's job unaudited."""
    mod = _subject()
    before = mod._tree_fingerprint(nongit_runtime)
    proc = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode == 0, proc.stderr
    assert not (nongit_runtime / ".git").exists(), "fingerprint created a .git"
    assert mod._tree_fingerprint(nongit_runtime) == before, "fingerprint mutated the tree"


def test_fingerprint_is_deterministic_across_runs(nongit_runtime: Path, tmp_path: Path):
    """A receipt is validated later against a re-walk; drift would fail every init."""
    a = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    b = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout


def test_fingerprint_still_refuses_a_group_or_other_writable_runtime(
    nongit_runtime: Path, tmp_path: Path
):
    """NEGATIVE CONTROL. require_git=False must not mean 'skip every safety check'.

    Deliberately NOT a missing directory: the fingerprint walk fails on that by itself,
    so such a test passes with `_runtime_safety` deleted entirely and pins nothing. It
    was written that way first and measured green under exactly that reversion.

    A world-writable runtime is the discriminating case — the walk fingerprints it
    happily, and only `_runtime_safety` objects. It is also the case that matters: a
    group-writable `/opt/hermes-agent` means any member of that group can plant code
    the eleven gateways execute, and a fingerprint taken over it would certify it.
    """
    nongit_runtime.chmod(0o777)
    proc = _run(nongit_runtime, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode != 0, (
        "fingerprint certified a group/other-writable runtime; relaxing the git "
        "requirement must not relax _runtime_safety as a whole"
    )
    assert "group/other-writable" in proc.stderr, proc.stderr


def test_fingerprint_still_refuses_a_symlinked_runtime(nongit_runtime: Path, tmp_path: Path):
    """NEGATIVE CONTROL. A symlink lets the fingerprinted tree differ from the applied one."""
    link = tmp_path / "runtime-link"
    link.symlink_to(nongit_runtime)
    proc = _run(link, "fingerprint", lock=tmp_path / "lk")
    assert proc.returncode != 0
    assert "must not be a symlink" in proc.stderr, proc.stderr
