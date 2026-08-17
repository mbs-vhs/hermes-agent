"""Safety contracts for the root-owned `/opt/hermes-agent` updater (CLAWD-3507)."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import update_opt_hermes_runtime as updater


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def runtime_fixture(tmp_path: Path) -> dict[str, Path | str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.com")
    (source / ".gitignore").write_text("venv/\n__pycache__/\n", encoding="utf-8")
    (source / "pkg").mkdir()
    (source / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (source / "pkg" / "live.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "only_at_ref.txt").write_text("restored by a2\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "target one")
    target1 = _git(source, "rev-parse", "HEAD")

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(origin)],
        check=True,
    )
    _git(source, "remote", "add", "origin", str(origin))

    runtime = tmp_path / "runtime"
    (runtime / "pkg").mkdir(parents=True)
    (runtime / ".gitignore").write_text("venv/\n__pycache__/\n", encoding="utf-8")
    (runtime / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "pkg" / "live.py").write_text("VALUE = 0\n", encoding="utf-8")
    (runtime / "pkg" / "orphan.py").write_text("ORPHAN = True\n", encoding="utf-8")
    (runtime / "dead.txt").write_text("old copy residue\n", encoding="utf-8")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    runtime.chmod(0o755)

    return {
        "source": source,
        "origin": origin,
        "runtime": runtime,
        "target1": target1,
        "lock": tmp_path / "update.lock",
        "receipts": tmp_path / "receipts",
        "transactions": tmp_path / "transactions",
    }


def _backup_receipt(tmp_path: Path, runtime: Path) -> Path:
    archive = tmp_path / "backup.tar.zst"
    roundtrip = tmp_path / "backup.roundtrip.tar.zst"
    subprocess.run(
        [
            "/usr/bin/tar",
            "--zstd",
            "--format=pax",
            "--numeric-owner",
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
            "--file",
            str(archive),
            "--create",
            "--directory",
            str(runtime),
            ".",
        ],
        check=True,
    )
    shutil.copyfile(archive, roundtrip)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = tmp_path / f"backup-{len(list(tmp_path.glob('backup-*.json')))}.json"
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


def _init(fixture: dict[str, Path | str]) -> None:
    runtime = Path(fixture["runtime"])
    backup = _backup_receipt(Path(fixture["lock"]).parent, runtime)
    assert (
        updater.main([
            "init",
            "--runtime",
            str(fixture["runtime"]),
            "--target",
            str(fixture["target1"]),
            "--remote-url",
            str(fixture["origin"]),
            "--backup-receipt",
            str(backup),
            "--transaction-dir",
            str(fixture["transactions"]),
            "--lock-file",
            str(fixture["lock"]),
        ])
        == 0
    )


def _write_initial_evidence(fixture: dict[str, Path | str], tmp_path: Path) -> Path:
    payload = updater._build_audit(Path(fixture["runtime"]), str(fixture["target1"]))
    evidence = tmp_path / "initial-audit.json"
    evidence.write_text(updater._canonical_json(payload), encoding="utf-8")
    evidence.chmod(0o600)
    return evidence


def _initial_apply(
    fixture: dict[str, Path | str], tmp_path: Path, *, dry_run: bool = False
) -> int:
    evidence = _write_initial_evidence(fixture, tmp_path)
    backup = _backup_receipt(tmp_path, Path(fixture["runtime"]))
    argv = [
        "apply",
        "--runtime",
        str(fixture["runtime"]),
        "--target",
        str(fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(backup),
        "--receipt-dir",
        str(fixture["receipts"]),
        "--transaction-dir",
        str(fixture["transactions"]),
        "--lock-file",
        str(fixture["lock"]),
    ]
    if dry_run:
        argv.append("--dry-run")
    return updater.main(argv)


def _make_target_two(fixture: dict[str, Path | str]) -> str:
    source = Path(fixture["source"])
    (source / "pkg" / "live.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(source, "add", "pkg/live.py")
    _git(source, "commit", "-qm", "target two")
    _git(source, "push", "-q", "origin", "main")
    return _git(source, "rev-parse", "HEAD")


def _steady_apply(
    fixture: dict[str, Path | str],
    tmp_path: Path,
    target: str,
    *,
    inject: str | None = None,
) -> int:
    argv = [
        "apply",
        "--runtime",
        str(fixture["runtime"]),
        "--target",
        target,
        "--fetch",
        "--remote-url",
        str(fixture["origin"]),
        "--backup-receipt",
        str(_backup_receipt(tmp_path, Path(fixture["runtime"]))),
        "--receipt-dir",
        str(fixture["receipts"]),
        "--transaction-dir",
        str(fixture["transactions"]),
        "--lock-file",
        str(fixture["lock"]),
    ]
    if inject is not None:
        argv += ["--inject-failure-after", inject]
    return updater.main(argv)


def test_init_changes_only_git_metadata_and_seeds_exact_head(
    runtime_fixture: dict[str, Path | str], capsys: pytest.CaptureFixture[str]
):
    runtime = Path(runtime_fixture["runtime"])
    before = updater._tree_fingerprint(runtime)
    _init(runtime_fixture)
    output = json.loads(capsys.readouterr().out)

    assert output["worktree_unchanged"] is True
    assert updater._tree_fingerprint(runtime) == before
    assert _git(runtime, "rev-parse", "HEAD") == runtime_fixture["target1"]
    audit = updater._build_audit(runtime, str(runtime_fixture["target1"]))
    assert "Would remove dead.txt" in audit["clean_preview"]
    assert "pkg/orphan.py" in audit["provenance"]["only_in_tree"]
    assert "only_at_ref.txt" in audit["provenance"]["only_in_ref"]
    assert "pkg/live.py" in audit["provenance"]["differing"]


def test_initial_apply_requires_exact_evidence_and_preserves_ignored_venv(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    runtime = Path(runtime_fixture["runtime"])
    python = runtime / "venv" / "bin" / "python"
    venv_hash = updater._sha256(python)

    assert _initial_apply(runtime_fixture, tmp_path) == 0

    assert _git(runtime, "status", "--porcelain") == ""
    assert _git(runtime, "rev-parse", "HEAD") == runtime_fixture["target1"]
    assert not (runtime / "dead.txt").exists()
    assert not (runtime / "pkg" / "orphan.py").exists()
    assert (runtime / "only_at_ref.txt").read_text() == "restored by a2\n"
    assert (runtime / "pkg" / "live.py").read_text() == "VALUE = 1\n"
    assert python.exists()
    assert updater._sha256(python) == venv_hash
    assert len(list(Path(runtime_fixture["receipts"]).glob("*.json"))) == 1


def test_initial_evidence_drift_blocks_before_mutation(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    runtime = Path(runtime_fixture["runtime"])
    evidence = _write_initial_evidence(runtime_fixture, tmp_path)
    backup = _backup_receipt(tmp_path, runtime)
    (runtime / "late-arrival.txt").write_text("not reviewed\n", encoding="utf-8")
    before = updater._tree_fingerprint(runtime)

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(backup),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])

    assert rc == updater.UNMEASURED_EXIT
    assert updater._tree_fingerprint(runtime) == before
    assert (runtime / "late-arrival.txt").exists()


@pytest.mark.parametrize(
    ("relative", "replacement"),
    [
        ("pkg/live.py", "VALUE = 9\n"),
        ("pkg/orphan.py", "ORPHAN = None\n"),
    ],
)
def test_initial_evidence_fingerprint_blocks_existing_path_byte_mutation(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    relative: str,
    replacement: str,
):
    _init(runtime_fixture)
    runtime = Path(runtime_fixture["runtime"])
    evidence = _write_initial_evidence(runtime_fixture, tmp_path)
    (runtime / relative).write_text(replacement, encoding="utf-8")
    before = updater._tree_fingerprint(runtime)

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(_backup_receipt(tmp_path, runtime)),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])

    assert rc == updater.UNMEASURED_EXIT
    assert updater._tree_fingerprint(runtime) == before
    assert (runtime / relative).read_text(encoding="utf-8") == replacement


def test_initial_evidence_blocks_ignored_venv_pth_mutation(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    runtime = Path(runtime_fixture["runtime"])
    site = runtime / "venv/lib/python3.11/site-packages"
    site.mkdir(parents=True)
    pth = site / "runtime.pth"
    pth.write_text("/approved/path\n", encoding="utf-8")
    _init(runtime_fixture)
    evidence = _write_initial_evidence(runtime_fixture, tmp_path)
    pth.write_text("/unreviewed/path\n", encoding="utf-8")

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(_backup_receipt(tmp_path, runtime)),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])

    assert rc == updater.UNMEASURED_EXIT
    assert pth.read_text(encoding="utf-8") == "/unreviewed/path\n"


def test_venv_guard_itself_fingerprints_site_packages_pth_files(
    runtime_fixture: dict[str, Path | str],
):
    runtime = Path(runtime_fixture["runtime"])
    site = runtime / "venv/lib/python3.11/site-packages"
    site.mkdir(parents=True)
    pth = site / "runtime.pth"
    pth.write_text("/approved/path\n", encoding="utf-8")
    _init(runtime_fixture)
    before = updater._venv_guard(runtime)
    pth.write_text("/changed/path\n", encoding="utf-8")
    assert updater._venv_guard(runtime) != before


def test_apply_dry_run_is_read_only_and_names_cleanup(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _init(runtime_fixture)
    capsys.readouterr()
    runtime = Path(runtime_fixture["runtime"])
    before = updater._tree_fingerprint(runtime)
    assert _initial_apply(runtime_fixture, tmp_path, dry_run=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["dry_run"] is True
    assert "Would remove dead.txt" in payload["clean_preview"]
    assert updater._tree_fingerprint(runtime) == before
    assert not Path(runtime_fixture["receipts"]).exists()


def test_steady_apply_refuses_untracked_runtime_drift(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    runtime = Path(runtime_fixture["runtime"])
    (runtime / "surprise.py").write_text("DRIFT = True\n", encoding="utf-8")
    backup = _backup_receipt(tmp_path, runtime)

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--backup-receipt",
        str(backup),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])
    assert rc == updater.UNMEASURED_EXIT
    assert (runtime / "surprise.py").exists()

    # Re-freezing the now-dirty tree must not reopen the one-time destructive
    # bootstrap lane after a2 has completed.
    evidence = tmp_path / "forged-second-bootstrap.json"
    evidence.write_text(
        updater._canonical_json(
            updater._build_audit(runtime, str(runtime_fixture["target1"]))
        ),
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    assert (
        updater.main([
            "apply",
            "--runtime",
            str(runtime),
            "--target",
            str(runtime_fixture["target1"]),
            "--initial-evidence",
            str(evidence),
            "--backup-receipt",
            str(backup),
            "--receipt-dir",
            str(runtime_fixture["receipts"]),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == updater.UNMEASURED_EXIT
    )
    assert (runtime / "surprise.py").exists()


def test_backup_must_be_fresh_byte_verified_and_roundtripped(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    evidence = _write_initial_evidence(runtime_fixture, tmp_path)
    runtime = Path(runtime_fixture["runtime"])
    backup = _backup_receipt(tmp_path, runtime)
    payload = json.loads(backup.read_text())
    payload["roundtrip_sha256"] = "0" * 64
    backup.write_text(json.dumps(payload), encoding="utf-8")

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(backup),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])
    assert rc == updater.UNMEASURED_EXIT
    assert (runtime / "dead.txt").exists()


@pytest.mark.parametrize(
    "failure", ["missing", "stale", "hash-mismatch", "same-path", "hardlink", "symlink"]
)
def test_init_requires_fresh_genuinely_separate_backup_before_git_mutation(
    runtime_fixture: dict[str, Path | str], tmp_path: Path, failure: str
):
    runtime = Path(runtime_fixture["runtime"])
    backup = _backup_receipt(tmp_path, runtime)
    payload = json.loads(backup.read_text(encoding="utf-8"))
    if failure == "stale":
        payload["created_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        ).isoformat()
    elif failure == "hash-mismatch":
        payload["archive_sha256"] = "0" * 64
    elif failure == "same-path":
        payload["roundtrip_path"] = payload["archive_path"]
    elif failure == "hardlink":
        roundtrip = Path(payload["roundtrip_path"])
        roundtrip.unlink()
        os.link(payload["archive_path"], roundtrip)
    elif failure == "symlink":
        roundtrip = Path(payload["roundtrip_path"])
        roundtrip.unlink()
        roundtrip.symlink_to(payload["archive_path"])
    backup.write_text(json.dumps(payload), encoding="utf-8")

    argv = [
        "init",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--remote-url",
        str(runtime_fixture["origin"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ]
    if failure != "missing":
        argv += ["--backup-receipt", str(backup)]

    assert updater.main(argv) == updater.UNMEASURED_EXIT
    assert not (runtime / ".git").exists()


def test_clean_to_clean_advance_and_receipted_rollback(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    capsys.readouterr()
    source = Path(runtime_fixture["source"])
    runtime = Path(runtime_fixture["runtime"])
    (source / "pkg" / "live.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(source, "add", "pkg/live.py")
    _git(source, "commit", "-qm", "target two")
    _git(source, "push", "-q", "origin", "main")
    target2 = _git(source, "rev-parse", "HEAD")
    backup2 = _backup_receipt(tmp_path, runtime)

    assert (
        updater.main([
            "apply",
            "--runtime",
            str(runtime),
            "--target",
            target2,
            "--fetch",
            "--remote-url",
            str(runtime_fixture["origin"]),
            "--backup-receipt",
            str(backup2),
            "--receipt-dir",
            str(runtime_fixture["receipts"]),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == 0
    )
    advance = json.loads(capsys.readouterr().out)
    assert (runtime / "pkg" / "live.py").read_text() == "VALUE = 2\n"

    rollback_backup = _backup_receipt(tmp_path, runtime)
    assert (
        updater.main([
            "apply",
            "--runtime",
            str(runtime),
            "--target",
            str(runtime_fixture["target1"]),
            "--backup-receipt",
            str(rollback_backup),
            "--receipt-dir",
            str(runtime_fixture["receipts"]),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == updater.UNMEASURED_EXIT
    ), "a normal apply must not disguise a downgrade as an update"
    assert (
        updater.main([
            "rollback",
            "--runtime",
            str(runtime),
            "--update-receipt",
            advance["receipt"],
            "--backup-receipt",
            str(rollback_backup),
            "--receipt-dir",
            str(runtime_fixture["receipts"]),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == 0
    )
    assert _git(runtime, "rev-parse", "HEAD") == runtime_fixture["target1"]
    assert (runtime / "pkg" / "live.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("stage", ["reset", "clean", "post-audit", "receipt"])
def test_steady_failure_is_journaled_and_exact_before_state_is_restored(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    runtime = Path(runtime_fixture["runtime"])
    before_head = _git(runtime, "rev-parse", "HEAD")
    before_tree = updater._tree_fingerprint(runtime)
    target2 = _make_target_two(runtime_fixture)
    monkeypatch.setenv("HERMES_UPDATER_TESTING", "1")

    assert (
        _steady_apply(runtime_fixture, tmp_path, target2, inject=stage)
        == updater.UNMEASURED_EXIT
    )

    assert _git(runtime, "rev-parse", "HEAD") == before_head
    assert updater._tree_fingerprint(runtime) == before_tree
    assert updater._incomplete_transactions(Path(runtime_fixture["transactions"])) == []
    journals = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in Path(runtime_fixture["transactions"]).glob("*.json")
        if path.name != updater.BOOTSTRAP_STATE
    ]
    assert any(journal["state"] == "recovered_after_failure" for journal in journals)
    if stage == "receipt":
        assert list(Path(runtime_fixture["receipts"]).glob("*.json.aborted"))


def test_interrupted_initial_reconciliation_requires_verified_external_recovery(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _init(runtime_fixture)
    runtime = Path(runtime_fixture["runtime"])
    before_tree = updater._tree_fingerprint(runtime)
    evidence = _write_initial_evidence(runtime_fixture, tmp_path)
    monkeypatch.setenv("HERMES_UPDATER_TESTING", "1")

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--initial-evidence",
        str(evidence),
        "--backup-receipt",
        str(_backup_receipt(tmp_path, runtime)),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
        "--inject-failure-after",
        "bootstrap-ref",
    ])
    assert rc == updater.UNMEASURED_EXIT
    pending = updater._incomplete_transactions(Path(runtime_fixture["transactions"]))
    assert len(pending) == 1
    assert pending[0][1]["state"] == "recovery_required"
    assert not updater._ref_exists(runtime, updater.BOOTSTRAP_REF)
    assert not (
        Path(runtime_fixture["transactions"]) / updater.BOOTSTRAP_STATE
    ).exists()

    # Simulate restoring the verified pre-a2 archive. `recover` consumes no
    # success receipt; it closes only after the complete before fingerprint.
    (runtime / "only_at_ref.txt").unlink()
    (runtime / "pkg/live.py").write_text("VALUE = 0\n", encoding="utf-8")
    (runtime / "pkg/orphan.py").write_text("ORPHAN = True\n", encoding="utf-8")
    (runtime / "dead.txt").write_text("old copy residue\n", encoding="utf-8")
    assert updater._tree_fingerprint(runtime) == before_tree
    assert (
        updater.main([
            "recover",
            "--runtime",
            str(runtime),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == 0
    )
    assert updater._incomplete_transactions(Path(runtime_fixture["transactions"])) == []


def test_normal_apply_and_rollback_require_both_bootstrap_closure_records(
    runtime_fixture: dict[str, Path | str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    capsys.readouterr()
    runtime = Path(runtime_fixture["runtime"])
    target2 = _make_target_two(runtime_fixture)
    assert _steady_apply(runtime_fixture, tmp_path, target2) == 0
    advance = json.loads(capsys.readouterr().out)
    closure = Path(runtime_fixture["transactions"]) / updater.BOOTSTRAP_STATE
    closure.unlink()

    assert _steady_apply(runtime_fixture, tmp_path, target2) == updater.UNMEASURED_EXIT
    assert (
        updater.main([
            "rollback",
            "--runtime",
            str(runtime),
            "--update-receipt",
            advance["receipt"],
            "--backup-receipt",
            str(_backup_receipt(tmp_path, runtime)),
            "--receipt-dir",
            str(runtime_fixture["receipts"]),
            "--transaction-dir",
            str(runtime_fixture["transactions"]),
            "--lock-file",
            str(runtime_fixture["lock"]),
        ])
        == updater.UNMEASURED_EXIT
    )
    assert _git(runtime, "rev-parse", "HEAD") == target2


def test_unreachable_side_commit_is_rejected_without_mutation(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    source = Path(runtime_fixture["source"])
    runtime = Path(runtime_fixture["runtime"])
    _git(source, "checkout", "-qb", "side")
    (source / "side.txt").write_text("sideways\n", encoding="utf-8")
    _git(source, "add", "side.txt")
    _git(source, "commit", "-qm", "side target")
    side = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:refs/heads/side")
    _git(source, "checkout", "-q", "main")
    _git(
        runtime,
        "fetch",
        "-q",
        "origin",
        "refs/heads/side:refs/hermes-runtime/test-side",
    )
    before = updater._tree_fingerprint(runtime)

    rc = updater.main([
        "apply",
        "--runtime",
        str(runtime),
        "--target",
        side,
        "--backup-receipt",
        str(_backup_receipt(tmp_path, runtime)),
        "--receipt-dir",
        str(runtime_fixture["receipts"]),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])
    assert rc == updater.UNMEASURED_EXIT
    assert updater._tree_fingerprint(runtime) == before


def test_git_failure_diagnostics_redact_embedded_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "super-secret-token"

    def failed_run(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 128, "", f"fatal: https://{secret}@github.example/private failed"
        )

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(updater.UpdateError) as caught:
        updater._git(tmp_path, "fetch")
    assert secret not in str(caught.value)
    assert "<redacted>" in str(caught.value)


@pytest.mark.parametrize("bad", ["origin/main", "abcdef1", "A" * 40, "0" * 39])
def test_moving_abbreviated_or_noncanonical_targets_are_rejected(
    runtime_fixture: dict[str, Path | str], bad: str
):
    rc = updater.main([
        "init",
        "--runtime",
        str(runtime_fixture["runtime"]),
        "--target",
        bad,
        "--remote-url",
        str(runtime_fixture["origin"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
        "--dry-run",
    ])
    assert rc == updater.UNMEASURED_EXIT


def test_credential_bearing_remote_is_rejected_before_git_init(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    runtime = Path(runtime_fixture["runtime"])
    rc = updater.main([
        "init",
        "--runtime",
        str(runtime),
        "--target",
        str(runtime_fixture["target1"]),
        "--remote-url",
        "https://embedded-token@github.example/private.git",
        "--backup-receipt",
        str(_backup_receipt(tmp_path, runtime)),
        "--transaction-dir",
        str(runtime_fixture["transactions"]),
        "--lock-file",
        str(runtime_fixture["lock"]),
    ])
    assert rc == updater.UNMEASURED_EXIT
    assert not (runtime / ".git").exists()


def test_tree_fingerprint_measures_mode_ownership_symlinks_and_xattrs(
    runtime_fixture: dict[str, Path | str],
):
    runtime = Path(runtime_fixture["runtime"])
    original = updater._tree_fingerprint(runtime)
    assert {"mode", "uid", "gid", "symlink-target", "xattrs"}.issubset(
        original["contract"]
    )
    live = runtime / "pkg/live.py"
    live.chmod(0o600)
    assert updater._tree_fingerprint(runtime) != original
    live.chmod(0o644)
    assert updater._tree_fingerprint(runtime) == original

    python = runtime / "venv/bin/python"
    python.unlink()
    python.symlink_to("/bin/false")
    assert updater._tree_fingerprint(runtime) != original
    python.unlink()
    python.symlink_to(sys.executable)
    assert updater._tree_fingerprint(runtime) == original

    try:
        os.setxattr(live, "user.clawd3507", b"measured", follow_symlinks=False)
    except OSError as exc:
        if exc.errno in {getattr(os, "ENOTSUP", 95), 95}:
            pytest.skip("test filesystem has no user xattr support")
        raise
    assert updater._tree_fingerprint(runtime) != original


def test_unreadable_path_makes_fingerprint_unmeasured(
    runtime_fixture: dict[str, Path | str],
):
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 fixtures")
    live = Path(runtime_fixture["runtime"]) / "pkg/live.py"
    live.chmod(0)
    try:
        with pytest.raises(updater.UpdateError):
            updater._tree_fingerprint(Path(runtime_fixture["runtime"]))
    finally:
        live.chmod(0o644)


def test_updater_has_no_gateway_restart_and_constructs_only_clean_fd():
    """Scope is the WHOLE MODULE, not just `_apply`.

    This previously grepped `inspect.getsource(updater._apply)`, which made a
    `systemctl` call in `_recover`, `_init` or `main` invisible — and independent
    review demonstrated exactly that by adding a restart to `_recover` while this
    stayed green. It also could not see the clean in `_restore_steady_transaction`.
    A signature check narrowed to one function is not evidence about a module.
    """
    source = inspect.getsource(updater)
    assert "systemctl" not in source, (
        "a gateway restart appeared somewhere in the module; source advance and "
        "restart are deliberately separate operator-visible phases"
    )
    assert '"clean", "-fdx"' not in source
    # Every mutating clean must go through the one helper that spares the venv.
    assert source.count('"clean", "-fd"') == 1, (
        "a bare `clean -fd` was constructed outside `_clean_runtime`. Every mutating "
        "clean must route through that helper, which excludes the live venv; a clean "
        "that skips it deletes the interpreter all 11 gateways run whenever the "
        "target commit stops ignoring venv/."
    )
    assert 'def _clean_runtime(' in source


def test_systemd_templates_keep_mutation_manual_and_timer_audit_only():
    root = Path(__file__).resolve().parents[2]
    update = (root / "systemd/ai.hermes.opt-runtime-update.service").read_text()
    audit = (root / "systemd/ai.hermes.opt-runtime-audit.service").read_text()
    timer = (root / "systemd/ai.hermes.opt-runtime-audit.timer").read_text()

    assert "User=root" in update
    assert "ProtectSystem=strict" in update
    assert "ConditionPath" not in update
    assert "CapabilityBoundingSet=\n" in update
    assert "AmbientCapabilities=\n" in update
    assert "LockPersonality=yes" in update
    assert " --target-file /etc/hermes-agent/runtime-target " in update
    assert " --fetch " in update
    assert not any(line.startswith("ExecStartPost=") for line in update.splitlines())
    assert not any(
        "systemctl" in line for line in update.splitlines() if line.startswith("Exec")
    )
    assert "[Install]" not in {line.strip() for line in update.splitlines()}
    assert not (root / "systemd/ai.hermes.opt-runtime-update.timer").exists()

    assert " status " in audit
    assert "ConditionPath" not in audit
    assert "CapabilityBoundingSet=\n" in audit
    assert "AmbientCapabilities=\n" in audit
    assert "LockPersonality=yes" in audit
    assert "RestrictSUIDSGID=yes" in audit
    assert "RestrictAddressFamilies=AF_UNIX" in audit
    assert "Unit=ai.hermes.opt-runtime-audit.service" in timer
    assert "WantedBy=timers.target" in timer


def test_update_unit_allows_tar_openat2_for_backup_fidelity_extract():
    root = Path(__file__).resolve().parents[2]
    update = (root / "systemd/ai.hermes.opt-runtime-update.service").read_text()
    assert "RestrictSUIDSGID=yes" not in update


def test_update_unit_umask_preserves_git_declared_modes(
    runtime_fixture: dict[str, Path | str], tmp_path: Path
):
    _init(runtime_fixture)
    assert _initial_apply(runtime_fixture, tmp_path) == 0
    source = Path(runtime_fixture["source"])
    runtime = Path(runtime_fixture["runtime"])
    (source / "pkg" / "live.py").write_text("VALUE = 2\n", encoding="utf-8")
    executable = source / "run-target"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    _git(source, "add", "pkg/live.py", "run-target")
    _git(source, "commit", "-qm", "target with declared modes")
    _git(source, "push", "-q", "origin", "main")
    target = _git(source, "rev-parse", "HEAD")

    root = Path(__file__).resolve().parents[2]
    update = (root / "systemd/ai.hermes.opt-runtime-update.service").read_text()
    unit_umask = int(
        next(line.split("=", 1)[1] for line in update.splitlines() if line.startswith("UMask=")),
        8,
    )
    previous_umask = os.umask(unit_umask)
    try:
        assert _steady_apply(runtime_fixture, tmp_path, target) == 0
    finally:
        os.umask(previous_umask)

    assert (runtime / "pkg" / "live.py").stat().st_mode & 0o777 == 0o644
    assert (runtime / "run-target").stat().st_mode & 0o777 == 0o755
