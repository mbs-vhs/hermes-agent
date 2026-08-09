"""CLAWD-3507 round 2 — adversarial coverage for the `/opt` runtime updater.

Written by the independent tester, not the implementer. Every test here exists
because a revert-validation pass showed the property it names could be broken with
the 139-test suite staying fully green, or because an input shape nobody had
supplied produces the wrong answer.

Three groups, kept visually separate because they mean different things:

* ``PINS`` — behaviour that is correct today and previously had no falsifiable
  test. Each one was confirmed RED against a one-line reversion of the production
  behaviour it names (see the session's revert-validation table).
* ``DEFECT`` — reproductions of wrong behaviour, marked ``xfail(strict=True)`` so
  the gate stays honest without going red on somebody else's fix. ``strict`` means
  the marker becomes a FAILURE the moment the defect is fixed, so it cannot rot
  into a permanent excuse.
* ``OBSERVED`` — input shapes probed adversarially that turned out correct.
  Recorded so the next reviewer does not have to re-derive them.

Nothing here greps the module's source text. Round 1 and round 2 both found
source-signature assertions standing in for properties they cannot establish
(``source.count('"clean", "-fd"') == 1`` is defeated by writing ``"clean", "-f",
"-d"``; ``'"dependency_skew": skew' in source`` is defeated by leaving the value
permanently empty). Everything below drives the real code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from scripts import update_opt_hermes_runtime as updater

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"

IGNORES_VENV = "venv/\n__pycache__/\nlogs/\n"
DROPS_VENV = "__pycache__/\nlogs/\n"


# ─────────────────────────────── fixtures ────────────────────────────────────



def _venvify(runtime, installed=None, with_packaging=True):
    """Make `<runtime>/venv` resolve like a REAL venv for the sanitised probe.

    A bare `venv/bin/python -> sys.executable` symlink is not a venv: python derives
    sys.prefix from the symlink's own location, so there is no site-packages behind it.
    That only ever worked because the probe inherited the caller's environment. The
    probe is now sanitised on purpose — a leaked PYTHONPATH could report a dependency
    SATISFIED that the gateway, under a clean systemd environment, will not find.
    """
    import sys as _s
    from pathlib import Path as _P
    (runtime / "venv" / "pyvenv.cfg").write_text(
        f"home = {_P(_s.base_prefix) / 'bin'}\n"
        f"include-system-site-packages = false\n"
        f"version = {_s.version.split()[0]}\n"
    )
    site = runtime / "venv" / "lib" / f"python{_s.version_info.major}.{_s.version_info.minor}" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, ver in (installed or {}).items():
        info = site / f"{name.replace('-', '_')}-{ver}.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n")
        (info / "RECORD").write_text("")
    if with_packaging:
        import packaging as _pkg
        (site / "packaging").symlink_to(_P(_pkg.__file__).parent, target_is_directory=True)

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


def _backup_receipt(scratch: Path, runtime: Path, tag: str = "b") -> Path:
    """A receipt the tool will accept: fresh, byte-identical round-trip, on disk."""
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
    """A bootstrapped runtime plus two candidate targets.

    ``t_keeps`` still ignores ``venv/``; ``t_drops`` is the ordinary tidy-up commit
    that stops ignoring it. The runtime carries a CANARY inside the venv so a test
    can tell "the interpreter survived" from "the test happened not to look".
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
    (runtime / "pkg" / "orphan.py").write_text("ORPHAN = True\n")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    (runtime / "venv" / "CANARY").write_text("the interpreter all 11 gateways run\n")
    runtime.chmod(0o755)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    common = {
        "runtime": runtime,
        "source": source,
        "origin": origin,
        "scratch": scratch,
        "t1": t1,
        "receipts": tmp_path / "receipts",
        "transactions": tmp_path / "transactions",
        "lock": tmp_path / "update.lock",
    }
    return common


def _bootstrap(world: dict) -> None:
    """Run the real init + initial-evidence apply so steady applies are unlocked."""
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


def _new_target(world: dict, *, gitignore: str, body: str = "VERSION_B\n",
                deps: str = "[]") -> str:
    source = world["source"]
    (source / ".gitignore").write_text(gitignore)
    (source / "pkg" / "live.py").write_text(body)
    (source / "pyproject.toml").write_text(
        f"[project]\nname='hermes-agent'\nversion='0'\ndependencies = {deps}\n"
    )
    head = _commit(source, "next")
    _git(source, "push", "-q", "origin", "main")
    return head


def _steady_apply(world: dict, target: str, *extra: str) -> int:
    return updater.main([
        "apply", "--runtime", str(world["runtime"]), "--target", target,
        "--fetch", "--remote-url", str(world["origin"]),
        "--backup-receipt",
        str(_backup_receipt(world["scratch"], world["runtime"], f"s{time.time_ns()}")),
        "--receipt-dir", str(world["receipts"]),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
        *extra,
    ])


# ═══════════════════════ PINS — real end-to-end coverage ═════════════════════


def test_PIN_the_apply_call_sites_clean_spares_the_venv(world):
    """The `_apply` mutating clean, driven through `main()`, not through the helper.

    The existing venv suite calls `_clean_runtime` directly, so it proves the HELPER
    is safe and says nothing about whether `_apply` still calls it. Replacing that
    call site with `_git(runtime, "clean", "-f", "-d")` — a bare clean spelled so the
    source-count guard cannot see it — leaves all 139 pre-existing tests green while
    the fleet's interpreter is deleted. This drives the real verb.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    canary = runtime / "venv" / "CANARY"
    assert canary.exists()

    target = _new_target(world, gitignore=DROPS_VENV)
    _steady_apply(world, target)

    assert canary.exists(), (
        "an `apply` whose target stopped ignoring venv/ deleted the live interpreter. "
        "git cannot restore it: it was never tracked."
    )
    assert (runtime / "venv" / "bin" / "python").exists()


def test_UNPINNABLE_the_recovery_clean_restores_the_tree_and_keeps_the_venv(world):
    """UNPINNABLE GUARD — read this before treating it as venv coverage.

    `_clean_runtime`'s `-e /venv` inside `_restore_steady_transaction` is correct
    defensive code whose failure mode I could not reach. Measured: replacing that
    call site with a bare `git clean -f -d` leaves this test — and all 167 others —
    GREEN. The reason is ordering. `_restore_steady_transaction` does
    `reset --hard before_head` BEFORE it cleans, so the .gitignore in force during
    the clean is always `before_head`'s, never the failed target's. And `before_head`
    always ignores `venv/`, because `_apply` re-runs `_venv_guard` after every
    advance and refuses any target under which `check-ignore venv` fails — so the
    runtime can never come to REST at a commit that stops ignoring it.

    The production comment above that call ("before_head's .gitignore may differ
    from the tree's current one") is true of the tree, but the tree's copy has
    already been overwritten by the time the clean runs. Do not delete the exclusion
    on the strength of this note: it costs nothing and the reachability argument
    depends on a guard in a different function.

    What this test DOES pin — and what does go red — is that recovery restores the
    exact before-tree and leaves the interpreter in place.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    # Advance the WORKTREE to a commit that does not ignore venv/, then ask the
    # recovery path to put the tree back — exactly the mid-incident sequence.
    target = _new_target(world, gitignore=DROPS_VENV)
    _git(runtime, "fetch", "-q", "origin")
    before_head = updater._head(runtime)
    before_tree = updater._tree_fingerprint(runtime)
    before_venv = updater._venv_guard(runtime)
    _git(runtime, "reset", "--hard", "-q", target)
    (runtime / "half-swept-residue.py").write_text("left behind by the failed run\n")

    updater._restore_steady_transaction(
        runtime,
        {
            "before_head": before_head,
            "before_tree": before_tree,
            "before_venv": before_venv,
            "previous_ref": None,
        },
    )

    assert (runtime / "venv" / "CANARY").exists(), (
        "recovery deleted the interpreter it was invoked to protect"
    )
    assert updater._head(runtime) == before_head
    assert not (runtime / "half-swept-residue.py").exists(), (
        "recovery left the failed run's untracked residue behind, so the runtime "
        "never returns to the exact before-tree and every later apply refuses"
    )
    assert updater._tree_fingerprint(runtime) == before_tree
    assert updater._venv_guard(runtime) == before_venv


def test_PIN_apply_receipt_and_journal_state_remote_verified_false(world):
    """The receipt must not let a consumer read byte-identity as R2 durability.

    `remote_verified: false` is the entire content of commit 98171299f and had zero
    coverage: it could be flipped to `true`, or deleted outright, with the suite
    green.
    """
    _bootstrap(world)
    target = _new_target(world, gitignore=IGNORES_VENV)
    assert _steady_apply(world, target) == 0

    receipts = sorted(Path(world["receipts"]).glob("*.json"))
    assert receipts, "apply produced no receipt"
    payload = json.loads(receipts[-1].read_text())
    assert "remote_verified" in payload["backup"], (
        "the receipt dropped remote_verified — a consumer now cannot tell that "
        "nothing checked R2"
    )
    assert payload["backup"]["remote_verified"] is False, (
        "the receipt CLAIMS the backup is verified in R2. This tool performs no "
        "network I/O; the claim is false and the next operator will act on it."
    )

    journal = json.loads(Path(payload["transaction_journal"]).read_text())
    assert journal["backup"]["remote_verified"] is False
    assert "not checked here" in journal["backup"]["remote_verified_by"]


def test_PIN_backup_receipt_validation_opens_no_socket(world):
    """"This tool performs NO NETWORK I/O" — asserted, not asserted-in-a-docstring."""
    runtime = world["runtime"]
    receipt = _backup_receipt(world["scratch"], runtime, "nonet")
    real_socket = socket.socket

    class _Refuse(real_socket):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **k):
            raise AssertionError(
                "the backup-receipt path opened a socket; the module's central "
                "honesty claim is that it never contacts R2"
            )

    socket.socket = _Refuse  # type: ignore[assignment]
    try:
        validated = updater._validate_backup_receipt(receipt, runtime)
    finally:
        socket.socket = real_socket  # type: ignore[assignment]
    assert validated["remote_uri"].startswith("r2:")


def test_PIN_backup_verification_extracts_beside_the_archive_not_into_tmpdir(world):
    """The helper was tested; the CALL SITE was not.

    Deleting `dir=_verify_scratch_dir(path)` from `_archive_restored_fingerprint`
    puts a full runtime extraction — venv included — back into `/tmp`, which is
    tmpfs on this host. `_verify_scratch_dir` keeps returning the right answer and
    the whole suite stays green, because nothing observed where the extraction
    actually happened.
    """
    runtime = world["runtime"]
    receipt_path = _backup_receipt(world["scratch"], runtime, "scr")
    archive = Path(json.loads(receipt_path.read_text())["roundtrip_path"])

    seen: list[str | None] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        # TemporaryDirectory calls mkdtemp(suffix, prefix, dir) POSITIONALLY, so a
        # kwargs-only spy would report None for a correctly-directed extraction and
        # fail for the wrong reason. Read both forms.
        if "dir" in kwargs:
            seen.append(kwargs["dir"])
        else:
            seen.append(args[2] if len(args) > 2 else None)
        return real_mkdtemp(*args, **kwargs)

    tempfile.mkdtemp = spy  # type: ignore[assignment]
    try:
        updater._archive_restored_fingerprint(archive)
    finally:
        tempfile.mkdtemp = real_mkdtemp  # type: ignore[assignment]

    assert seen, "no temporary directory was created — the extraction did not happen"
    assert seen[0] is not None, (
        "the whole-runtime extraction used the default temp root. On this host that "
        "is tmpfs, so verifying a backup copies the venv into RAM on the box running "
        "the fleet."
    )
    assert Path(seen[0]).resolve() == archive.resolve().parent, (
        f"extraction scratch was {seen[0]}, not the archive's own (disk-backed) dir"
    )


def test_PIN_dry_run_plan_carries_the_real_dependency_skew(world):
    """The plan field, read out of real JSON rather than grepped for in the source.

    `test_skew_is_surfaced_in_the_plan_and_the_flag_exists` asserts the literal text
    `'"dependency_skew": skew'` appears in the file. The value can be made
    permanently empty with that assertion still passing.

    Note the shape the module actually has, which its own comment gets wrong: the
    skew REFUSAL is evaluated before the `--dry-run` early return, so a plain
    `--dry-run` over a skewing target prints no plan at all — it refuses. The plan
    carrying `dependency_skew` is only observable with `--accept-dependency-skew`.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    target = _new_target(
        world, gitignore=IGNORES_VENV,
        deps='["definitely-not-a-real-package-xyz>=1.0"]',
    )
    _git(runtime, "fetch", "-q", "origin")

    def rehearse(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(SUBJECT), "apply",
                "--runtime", str(runtime), "--target", target,
                "--backup-receipt",
                str(_backup_receipt(world["scratch"], runtime, f"p{time.time_ns()}")),
                "--receipt-dir", str(world["receipts"]),
                "--transaction-dir", str(world["transactions"]),
                "--lock-file", str(world["lock"]),
                "--dry-run", *extra,
            ],
            capture_output=True, text=True,
        )

    refused = rehearse()
    assert refused.returncode == updater.UNMEASURED_EXIT, (
        "a rehearsal over a target the live venv cannot satisfy reported success"
    )
    assert "definitely-not-a-real-package-xyz" in refused.stderr, (
        f"the refusal does not name the unsatisfied package: {refused.stderr!r}"
    )
    # THE FIX THIS TEST ASKED FOR. It used to observe that the refusal ran BEFORE the
    # --dry-run early return, so a plain rehearsal "prints no plan at all — it
    # refuses", and the plan was only visible via --accept-dependency-skew. The
    # refusal now lands BELOW the early return: the rehearsal prints the plan, THEN
    # fails. So the operator sees exactly what is wrong without needing an override —
    # and the override is gone, deliberately.
    plan = json.loads(refused.stdout)
    assert plan["dependency_skew"], (
        "the plan shows no dependency skew although the target declares a package "
        "the live venv does not have — an operator rehearsing this advance sees a "
        "clean plan for something that fails at import across all 11 gateways"
    )
    assert any(
        "definitely-not-a-real-package-xyz" in s for s in plan["dependency_skew"]
    )
    assert plan["restart_performed"] is False
    assert plan["dry_run"] is True


def test_PIN_the_conversion_plan_names_the_importable_orphans_it_will_delete(world):
    """`importable_orphans_to_remove` is only ever non-empty at the CONVERSION.

    Every steady apply's preflight requires an empty `clean -nd` preview, so the
    field is structurally always `[]` there. The one-shot initial apply — where the
    real /opt carries 251 orphans, 33 of them importable — is the only run where it
    can say anything, and it is the run whose review the runbook gates on.
    """
    runtime = world["runtime"]
    # A non-importable orphan alongside the importable one, so "the list is just the
    # preview echoed back" and "the list is filtered to modules" are distinguishable.
    (runtime / "dead.txt").write_text("old copy residue\n")
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

    proc = subprocess.run(
        [
            sys.executable, str(SUBJECT), "apply",
            "--runtime", str(runtime), "--target", world["t1"],
            "--initial-evidence", str(evidence),
            "--backup-receipt", str(_backup_receipt(world["scratch"], runtime, "pl")),
            "--receipt-dir", str(world["receipts"]),
            "--transaction-dir", str(world["transactions"]),
            "--lock-file", str(world["lock"]),
            "--dry-run",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["importable_orphans_to_remove"] == ["pkg/orphan.py"], (
        "the conversion plan does not name the importable file this sweep will "
        "delete. That list is the only thing making the runbook's 'review the "
        f"orphans' step real; it said {plan['importable_orphans_to_remove']!r} while "
        f"the preview said {plan['clean_preview']!r}"
    )
    assert "Would remove dead.txt" in plan["clean_preview"], (
        "the preview stopped modelling the real sweep"
    )
    assert "dead.txt" not in plan["importable_orphans_to_remove"], (
        "the list is the raw preview echoed back, not the importable subset"
    )


def test_PIN_dependency_skew_refuses_the_apply_and_there_is_NO_override(world):
    """The refusal itself — the point of the commit — had no test at all.

    CONTRACT CHANGED DELIBERATELY. This previously also asserted that
    `--accept-dependency-skew` lets an operator proceed, on the reasoning that a
    refusal without an override "is a wall rather than a gate". The flag is gone and
    the wall is intended: this branch shipped two advertised-but-inert `--accept-*`
    flags already, and the honest remedy for skew is to resync the runtime venv to the
    target, not to wave through a finding on the way to breaking eleven gateways.
    An override can come back when something actually rebuilds the venv — it needs
    wiring AND a test that it changes behaviour, which is exactly what the two dead
    flags lacked.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    target = _new_target(
        world, gitignore=IGNORES_VENV,
        deps='["definitely-not-a-real-package-xyz>=1.0"]',
    )
    head_before = updater._head(runtime)

    assert _steady_apply(world, target) == updater.UNMEASURED_EXIT, (
        "an apply proceeded although the live venv cannot satisfy the target's "
        "declared main dependencies"
    )
    assert updater._head(runtime) == head_before, "the refusal still moved HEAD"

    # No override exists, and passing one is an argparse error rather than a silent
    # no-op — the failure mode of the two dead flags this branch already shipped.
    with pytest.raises(SystemExit) as exit_info:
        _steady_apply(world, target, "--accept-dependency-skew")
    assert exit_info.value.code == 2, (
        "an --accept-dependency-skew override is live again; argparse should reject "
        "the flag outright rather than the tool silently ignoring it, which is how "
        "the two dead --accept-* flags on this branch failed"
    )
    assert updater._head(runtime) == head_before, "HEAD moved despite the refusal"


def test_PIN_dependency_skew_asks_the_RUNTIME_venv_not_the_updater_interpreter(
    tmp_path: Path,
):
    """The existing depskew fixture symlinks venv/bin/python -> sys.executable.

    That makes "asked the fleet's interpreter" and "asked its own" indistinguishable,
    so pointing the probe at `sys.executable` keeps every depskew test green. The
    whole premise of the check is that /opt's venv holds `nemo-relay==0.3` while the
    updater's python3.11 does not — two different environments. Here the runtime's
    interpreter can see a package the updater's cannot.
    """
    site = tmp_path / "runtime-site"
    (site / "clawd3507_fake_pkg-9.9.9.dist-info").mkdir(parents=True)
    (site / "clawd3507_fake_pkg-9.9.9.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: clawd3507-fake-pkg\nVersion: 9.9.9\n"
    )

    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\n"
        "dependencies = [\"clawd3507-fake-pkg>=9\"]\n"
    )
    head = _commit(runtime, "target declares a package only the RUNTIME venv has")

    (runtime / "venv" / "bin").mkdir(parents=True)
    shim = runtime / "venv" / "bin" / "python"
    shim.write_text(
        f"#!/bin/sh\nPYTHONPATH={site} exec {sys.executable} \"$@\"\n"
    )
    shim.chmod(0o755)

    # Precondition: the updater's own interpreter genuinely cannot see it.
    probe = subprocess.run(
        [sys.executable, "-c",
         "from importlib.metadata import version, PackageNotFoundError\n"
         "try: version('clawd3507-fake-pkg'); print('FOUND')\n"
         "except PackageNotFoundError: print('ABSENT')\n"],
        capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "ABSENT", "fixture precondition broken"

    assert updater._dependency_skew(runtime, head) == [], (
        "skew was computed against the UPDATER's interpreter, not the runtime venv. "
        "On the real fleet those are different environments and this check exists "
        "precisely because of that difference."
    )


def test_PIN_the_dependency_probe_writes_no_bytecode_into_the_venv(
    tmp_path, monkeypatch
):
    """Commit 503867819 shipped this fix with NO test. Here is one.

    The probe runs the runtime's own interpreter. If CPython is allowed to write
    bytecode, importing `packaging` drops `.pyc` files into the venv's site-packages
    — the exact tree `_venv_guard` fingerprints before and after the advance — so a
    successful apply aborts with "live venv changed during source advance" and the
    auto-recovery fails too, leaving the fleet on new source at recovery_required.

    HARNESS NEUTRALITY, and it is the whole reason the defect survived: the commit
    message says the suite could not see this because the runner exports
    PYTHONDONTWRITEBYTECODE globally. This test therefore DELETES that variable from
    its own environment first. Without that line the test passes against the
    unfixed code — measured — and is pure decoration.
    """
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)

    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / ".gitignore").write_text("venv/\n")
    (runtime / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\ndependencies = [\"clawd3507-fake-pkg\"]\n"
    )
    head = _commit(runtime, "t")

    # A venv whose site-packages holds `packaging` with NO precompiled bytecode —
    # the uv-built / pip --no-compile / bumped-interpreter-tag case the commit names.
    site = runtime / "venv" / "lib" / "python3" / "site-packages"
    (site / "packaging").mkdir(parents=True)
    (site / "packaging" / "__init__.py").write_text("")
    (site / "packaging" / "requirements.py").write_text(
        "import re\n"
        "class _Spec:\n"
        "    def __str__(self): return ''\n"
        "    def contains(self, v, prereleases=False): return True\n"
        "class Requirement:\n"
        "    def __init__(self, raw):\n"
        # NOTE: two backslashes here, not four. With four, the file received
        # r'^\\s*(...)' — literal-backslash + zero-or-more 's' — so this fake
        # Requirement raised ValueError on EVERY well-formed package name. The probe's
        # old `except Exception: continue` swallowed that, so nothing was ever found and
        # the precondition below ("the probe must actually run and find the package")
        # was silently false, making the bytecode assertion vacuous. Surfaced only when
        # the probe stopped failing open.
        "        m = re.match(r'^\\s*([A-Za-z0-9._-]+)', raw)\n"
        "        if not m: raise ValueError(raw)\n"
        "        self.name = m.group(1); self.marker = None\n"
        "        self.specifier = _Spec()\n"
    )
    (site / "clawd3507_fake_pkg-9.9.9.dist-info").mkdir()
    (site / "clawd3507_fake_pkg-9.9.9.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: clawd3507-fake-pkg\nVersion: 9.9.9\n"
    )

    (runtime / "venv" / "bin").mkdir(parents=True)
    shim = runtime / "venv" / "bin" / "python"
    shim.write_text(f"#!/bin/sh\nPYTHONPATH={site} exec {sys.executable} \"$@\"\n")
    shim.chmod(0o755)

    assert not list(site.rglob("*.pyc")), "fixture must start with no bytecode"
    before = updater._venv_guard(runtime)

    assert updater._dependency_skew(runtime, head) == [], (
        "fixture precondition: the probe must actually run and find the package, "
        "otherwise 'no bytecode written' is true because nothing was imported"
    )

    written = [str(p) for p in site.rglob("*.pyc")]
    assert not written, (
        f"the probe wrote bytecode into the runtime venv: {written}"
    )
    assert updater._venv_guard(runtime) == before, (
        "the dependency probe MUTATED the venv it fingerprints. On a real apply "
        "`venv_before` is captured before this call and compared after the advance, "
        "so the write turns a successful advance into 'live venv changed during "
        "source advance' and the automatic recovery fails too."
    )


def test_PIN_a_non_40_hex_target_is_refused_BY_THE_SHAPE_CHECK(world):
    """The existing parametrised test cannot fail.

    `test_moving_abbreviated_or_noncanonical_targets_are_rejected` runs `init
    --dry-run` with no `--backup-receipt`, so `main()` returns UNMEASURED_EXIT for
    EVERY target — valid or not — via "init requires --backup-receipt". Removing the
    40-hex check entirely leaves it green. This one supplies a complete, otherwise
    valid invocation and asserts on the refusal REASON.
    """
    runtime = world["runtime"]
    receipt = _backup_receipt(world["scratch"], runtime, "shape")
    for bad in ("origin/main", "main", "abcdef1", "A" * 40, "0" * 39, world["t1"][:12]):
        proc = subprocess.run(
            [
                sys.executable, str(SUBJECT), "init",
                "--runtime", str(runtime), "--target", bad,
                "--remote-url", str(world["origin"]),
                "--backup-receipt", str(receipt),
                "--transaction-dir", str(world["transactions"]),
                "--lock-file", str(world["lock"]),
                "--dry-run",
            ],
            capture_output=True, text=True,
        )
        assert proc.returncode == updater.UNMEASURED_EXIT, f"{bad!r} was accepted"
        assert "target must be one lowercase 40-hex commit id" in proc.stderr, (
            f"{bad!r} was refused, but not by the target-shape check — it got "
            f"{proc.stderr.strip()!r}. A refusal for an unrelated reason is not "
            f"evidence that moving or abbreviated targets are rejected."
        )
    assert not (runtime / ".git").exists(), "a refused target still initialised git"


def test_PIN_recover_dry_run_is_inert_WITH_A_REAL_PENDING_TRANSACTION(world):
    """The existing dry-run test leaves the transaction dir EMPTY.

    So `recover` has nothing to recover, its "HEAD did not move" and "SENTINEL
    survived" assertions cannot fire, and only the exit code distinguishes the two
    code paths. This builds the journal a SIGKILL mid-clean actually leaves behind —
    state `clean_done`, a real `before_head` — so the mutating path has something to
    mutate.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    before_head = updater._head(runtime)
    before_tree = updater._tree_fingerprint(runtime)
    before_venv = updater._venv_guard(runtime)

    target = _new_target(world, gitignore=IGNORES_VENV)
    _git(runtime, "fetch", "-q", "origin")
    _git(runtime, "reset", "--hard", "-q", target)          # the half-done advance
    (runtime / "SENTINEL").write_text("untracked; a real clean would remove me\n")

    txn_id = "11111111-2222-3333-4444-555555555555"
    journal = Path(world["transactions"]) / f"{txn_id}.json"
    journal.write_text(updater._canonical_json({
        "schema": 1, "transaction_id": txn_id, "state": "clean_done",
        "action": "apply", "runtime": str(runtime.resolve()),
        "before_head": before_head, "before_tree": before_tree,
        "before_venv": before_venv, "previous_ref": None,
        "target": target, "initial": False, "clean_preview": [],
        "backup": {}, "created_at": "2026-08-04T00:00:00Z",
    }))
    journal.chmod(0o600)

    proc = subprocess.run(
        [
            sys.executable, str(SUBJECT), "recover",
            "--runtime", str(runtime), "--dry-run",
            "--transaction-dir", str(world["transactions"]),
            "--lock-file", str(world["lock"]),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    # Inertness FIRST, so a failure names the mutation rather than a missing key.
    assert updater._head(runtime) == target, (
        "recover --dry-run performed a real reset --hard and rolled a live runtime "
        "back without announcing it"
    )
    assert (runtime / "SENTINEL").exists(), (
        "recover --dry-run ran a real `git clean` and deleted untracked files"
    )
    assert json.loads(journal.read_text())["state"] == "clean_done", (
        "recover --dry-run CONSUMED the transaction, so the operator's real recover "
        "now has nothing to act on"
    )

    payload = json.loads(proc.stdout)
    assert payload.get("dry_run") is True, payload
    assert payload.get("would_recover") == [txn_id], (
        f"the preview does not name the transaction it would act on: {payload}"
    )

    # And the real verb, on the same journal, still does the work.
    assert updater.main([
        "recover", "--runtime", str(runtime),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0
    assert updater._head(runtime) == before_head
    assert not (runtime / "SENTINEL").exists()
    assert json.loads(journal.read_text())["state"] == "recovered"


def test_PIN_sigterm_during_the_clean_unwinds_the_transaction(world, monkeypatch):
    """The signal tests prove a bare process raises. They do not prove it UNWINDS.

    The docstring's claim is that the raise makes "the existing transaction
    machinery unwind and record a recoverable state". That is the part that matters
    and the part nothing exercised. Here a REAL SIGTERM arrives inside the real
    clean, during a real apply.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    before_head = updater._head(runtime)
    target = _new_target(world, gitignore=IGNORES_VENV)

    real_clean = updater._clean_runtime
    fired: list[int] = []

    def clean_then_stop(rt: Path) -> None:
        real_clean(rt)
        # ONCE. `_restore_steady_transaction` cleans too, and re-signalling there
        # would test the recovery path failing rather than the recovery path
        # running — a red for the wrong reason.
        if not fired:
            fired.append(1)
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(updater, "_clean_runtime", clean_then_stop)
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = _steady_apply(world, target)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    assert rc == updater.UNMEASURED_EXIT, "a stop signal was reported as success"
    assert updater._head(runtime) == before_head, (
        "SIGTERM left the runtime advanced with no completed transaction"
    )
    assert (runtime / "venv" / "CANARY").exists()

    journals = [
        json.loads(p.read_text())
        for p in sorted(Path(world["transactions"]).glob("*.json"))
        if p.name != updater.BOOTSTRAP_STATE
    ]
    unwound = [j for j in journals if j.get("state") == "recovered_after_failure"]
    assert unwound, (
        "no transaction reached a recovered state — the SIGTERM did not ride the "
        "failure machinery, which is the whole claim: journals are "
        f"{[j.get('state') for j in journals]}"
    )
    assert "interrupted by signal 15" in unwound[-1].get("failure", ""), (
        f"the journal does not record WHY: {unwound[-1].get('failure')!r}"
    )
    assert not any(
        j.get("state") in updater.INCOMPLETE_TRANSACTION_STATES for j in journals
    ), "an incomplete transaction was left behind, which blocks every later apply"


def test_PIN_a_second_updater_is_refused_while_the_lock_is_held(world, tmp_path):
    """Two concurrent invocations against one root-owned tree.

    `flock` is the only thing between two operators (or a timer and an operator)
    running `reset --hard` on the live fleet tree at once. It had no test.
    """
    # Bootstrap first: `status` on a ready runtime exits 0, so if the lock stops
    # refusing, the second invocation SUCCEEDS and the returncode assertion below
    # names the concurrency — not some unrelated "runtime is not a git checkout".
    _bootstrap(world)
    lock = tmp_path / "concurrent.lock"
    holder_src = tmp_path / "holder.py"
    holder_src.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from pathlib import Path\n"
        "from scripts import update_opt_hermes_runtime as u\n"
        f"with u._exclusive_lock(Path({str(lock)!r})):\n"
        "    print('HELD', flush=True)\n"
        "    time.sleep(20)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, str(holder_src)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD", "holder never took the lock"
        started = time.monotonic()
        try:
            second = subprocess.run(
                [
                    sys.executable, str(SUBJECT), "status",
                    "--runtime", str(world["runtime"]), "--target", world["t1"],
                    "--transaction-dir", str(world["transactions"]),
                    "--lock-file", str(lock),
                ],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "the second invocation BLOCKED on the lock instead of failing "
                "closed. A timer-launched run would then queue behind an operator's "
                "and fire unannounced on the live fleet tree once they finished."
            )
    finally:
        holder.kill()
        holder.wait()

    assert second.returncode == updater.UNMEASURED_EXIT, (
        f"a second updater ran concurrently against the same runtime "
        f"(exit {second.returncode}); flock is the only thing between two "
        f"`reset --hard` runs on the live fleet tree"
    )
    assert "another updater holds" in second.stderr, second.stderr
    assert time.monotonic() - started < 10


# ═══════════════════════════════ DEFECTS ═════════════════════════════════════
#
# xfail(strict=True): these FAIL today. strict means each one becomes a hard
# failure the moment the defect is fixed, so the marker cannot silently outlive
# the bug.


def test_readiness_does_not_wedge_on_a_non_ascii_gitignored_orphan(world):
    """REGRESSION GUARD. This was an xfail(strict=True) pinning a live defect.

    The defect: `git check-ignore --stdin` quotes non-ASCII paths (core.quotePath
    defaults on) while the provenance walk returned them raw, so the set subtraction
    in `_provenance_is_exact` never matched and readiness wedged. A gateway fleet
    handling arbitrary chat content writes gitignored logs, session files and
    attachments named after user input, so one non-ASCII character re-created exactly
    the wedge commit 6d969f790 was written to remove: every later `apply` and
    `rollback` refused, and `-x` is banned so nothing could clear it.

    The CLAWD-3655 narrowing removed the defect by construction rather than by patch —
    `_ready` no longer consults the gitignore-blind provenance walk at all, so there is
    no set subtraction left to disagree about quoting. The marker is therefore dropped
    and this now asserts the fix positively: the wedge must stay gone.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    (runtime / "logs").mkdir(exist_ok=True)
    (runtime / "logs" / "agent.log").write_text("ascii, matches fine\n")
    (runtime / "logs" / "agent-δοκιμή.log").write_text("non-ascii, does not\n")

    audit = updater._build_audit(runtime, updater._head(runtime))
    assert updater._ready(audit), (
        "readiness is False because of a gitignored file `clean -fd` will never "
        "remove — the non-ASCII wedge is back. only_in_tree was "
        f"{sorted(audit['provenance']['only_in_tree'])!r}; git status was "
        f"{audit['status']!r} and clean_preview was {audit['clean_preview']!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: _git_ignored joins paths with \\n on check-ignore's stdin, so a "
           "path containing a newline is read as two paths. Fix: -z.",
)
def test_DEFECT_git_ignored_splits_a_path_that_contains_a_newline(world):
    _bootstrap(world)
    runtime = world["runtime"]
    (runtime / "logs").mkdir(exist_ok=True)
    weird = "logs/two\nlines.log"
    (runtime / "logs" / "two\nlines.log").write_text("x\n")
    assert updater._git_ignored(runtime, [weird]) == {weird}, (
        f"got {updater._git_ignored(runtime, [weird])!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: importable_orphans_to_remove is parsed out of `clean -nd` text "
           "with endswith('.py'); git quotes non-ASCII paths so the line ends '.py\"' "
           "and the module is silently omitted from the plan while still being swept.",
)
def test_DEFECT_importable_orphan_list_omits_a_non_ascii_module(world):
    """The list exists so an operator can review what a sweep will delete.

    A module it cannot name is a module the operator cannot review, and it is deleted
    anyway.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    (runtime / "pkg" / "shadow-δ.py").write_text("x = 1\n")
    preview = updater._clean_preview(runtime)
    listed = sorted(
        line.split("Would remove ", 1)[1]
        for line in preview
        if "Would remove " in line and line.rstrip().endswith(".py")
    )
    assert listed, f"clean_preview was {preview!r} but nothing was listed as importable"


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: `-e /venv` constrains `git clean` only. `git reset --hard <target>` "
           "writes TRACKED files unconditionally, so a target that tracks anything "
           "under venv/ overwrites the fleet's interpreter; the automatic recovery "
           "then resets back to a commit that does not track it and DELETES it.",
)
def test_DEFECT_a_target_tracking_a_path_under_venv_destroys_the_interpreter(world):
    """The venv exclusion protects against what a target IGNORES, not what it TRACKS.

    Reproduced end to end: after the advance, `venv/bin/python` is the target's
    tracked file; after `_restore_steady_transaction`, it does not exist at all.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    source = world["source"]

    (source / ".gitignore").write_text(IGNORES_VENV)
    (source / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (source / "venv" / "bin" / "python").write_text("#!/bin/sh\necho HIJACKED\n")
    _git(source, "add", "-f", "venv/bin/python")
    target = _commit(source, "vendors a file under venv/")
    _git(source, "push", "-q", "origin", "main")

    before_head = updater._head(runtime)
    before_tree = updater._tree_fingerprint(runtime)
    before_venv = updater._venv_guard(runtime)
    _git(runtime, "fetch", "-q", "origin")
    _git(runtime, "reset", "--hard", "-q", target)
    updater._clean_runtime(runtime)

    interpreter = runtime / "venv" / "bin" / "python"
    assert interpreter.is_symlink(), (
        "the advance overwrote the live interpreter with a file tracked by the "
        f"target commit: {interpreter.read_text()!r}"
    )

    updater._restore_steady_transaction(
        runtime,
        {"before_head": before_head, "before_tree": before_tree,
         "before_venv": before_venv, "previous_ref": None},
    )
    assert interpreter.exists(), "recovery deleted the interpreter outright"


# ══════════════════ OBSERVED — adversarial inputs, correct today ═════════════


@pytest.mark.parametrize(
    "dirname",
    ["has spaces", "has\nnewline", "wéîrd-δοκιμή-🚀", "it's \"quoted\"", "--dash-lead"],
)
def test_OBSERVED_runtime_paths_with_hostile_names_are_handled(tmp_path, dirname):
    """No shell interpolation anywhere: every git call is an argv list.

    Fingerprinting, clean preview and the mutating clean all behave. Recorded so a
    later refactor to `shell=True` or an f-string command has something to break.
    """
    source = tmp_path / "src"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / ".gitignore").write_text(IGNORES_VENV)
    (source / "pkg").mkdir()
    (source / "pkg" / "a.py").write_text("A\n")
    head = _commit(source, "base")

    runtime = tmp_path / dirname
    (runtime / "pkg").mkdir(parents=True)
    (runtime / ".gitignore").write_text(IGNORES_VENV)
    (runtime / "pkg" / "a.py").write_text("A\n")
    (runtime / "pkg" / "orphan.py").write_text("O\n")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    _git(runtime, "init", "-q", "-b", "main")
    _git(runtime, "fetch", "-q", str(source), "main")
    _git(runtime, "reset", "--mixed", "-q", head)

    assert updater._tree_fingerprint(runtime)["entry_count"] > 0
    assert updater._clean_preview(runtime) == ["Would remove pkg/orphan.py"]
    updater._venv_guard(runtime)
    updater._clean_runtime(runtime)
    assert not (runtime / "pkg" / "orphan.py").exists()
    assert (runtime / "venv" / "bin" / "python").exists()


def test_OBSERVED_check_ignore_settles_ten_thousand_paths_in_one_process(world):
    """The batch stdin form does not deadlock or truncate at fleet scale.

    /opt currently carries 251 orphans; a wedged conversion could carry orders of
    magnitude more. subprocess.run(input=...) uses communicate(), so there is no
    pipe-buffer deadlock, and nothing is passed on argv so ARG_MAX is not in play.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    (runtime / "logs").mkdir(exist_ok=True)
    paths = [f"logs/f{i}.log" for i in range(5000)] + [
        f"pkg/keep{i}.py" for i in range(5000)
    ]
    ignored = updater._git_ignored(runtime, paths)
    assert len(ignored) == 5000
    assert all(p.startswith("logs/") for p in ignored)
    assert updater._git_ignored(runtime, []) == set()


def test_OBSERVED_a_symlinked_venv_is_refused_before_any_mutation(tmp_path):
    """Fail-closed, but the DIAGNOSTIC is wrong, and that is worth knowing.

    `.gitignore` says `venv/`; git's trailing-slash patterns match directories only,
    so a symlinked venv is "not ignored" and `_venv_guard` refuses. The refusal is
    the right outcome — but it reports "runtime/venv is not ignored; clean -fd is
    unsafe" to an operator looking at a `.gitignore` that plainly lists it.
    """
    source = tmp_path / "src"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / ".gitignore").write_text(IGNORES_VENV)
    (source / "pkg").mkdir()
    (source / "pkg" / "a.py").write_text("A\n")
    head = _commit(source, "base")

    elsewhere = tmp_path / "real-venv"
    (elsewhere / "bin").mkdir(parents=True)
    (elsewhere / "bin" / "python").symlink_to(sys.executable)

    runtime = tmp_path / "rt"
    (runtime / "pkg").mkdir(parents=True)
    (runtime / ".gitignore").write_text(IGNORES_VENV)
    (runtime / "pkg" / "a.py").write_text("A\n")
    (runtime / "venv").symlink_to(elsewhere, target_is_directory=True)
    _git(runtime, "init", "-q", "-b", "main")
    _git(runtime, "fetch", "-q", str(source), "main")
    _git(runtime, "reset", "--hard", "-q", head)

    with pytest.raises(updater.UpdateError) as caught:
        updater._venv_guard(runtime)
    assert "not ignored" in str(caught.value)
    assert (elsewhere / "bin" / "python").exists()


def test_OBSERVED_a_venv_at_a_non_default_path_is_refused_not_silently_advanced(
    tmp_path,
):
    """`_venv_guard` hard-codes `<runtime>/venv`.

    A runtime whose interpreter lives at `.venv` can never be advanced. That is
    fail-closed and correct for /opt, which does use `venv`. Recorded because the
    refusal is the ONLY thing standing between a `.venv` layout and a clean that
    would delete it — `_clean_runtime` alone does not spare `.venv` (verified: it
    removes `.venv/bin/python` once the target stops ignoring it). The guard is
    therefore load-bearing rather than defensive.
    """
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / ".gitignore").write_text("venv/\n.venv/\n")
    (runtime / "a.py").write_text("A\n")
    _commit(runtime, "base")
    (runtime / ".venv" / "bin").mkdir(parents=True)
    (runtime / ".venv" / "bin" / "python").symlink_to(sys.executable)

    with pytest.raises(updater.UpdateError) as caught:
        updater._venv_guard(runtime)
    assert "missing or incomplete" in str(caught.value)


def test_OBSERVED_a_broken_symlink_interpreter_is_refused(tmp_path):
    """`_venv_guard` uses `.exists()`, which DEREFERENCES.

    That is the safe direction here: a dangling `venv/bin/python` means the fleet
    already has no interpreter, and the guard refuses rather than proceeding. The
    same `.exists()` in `_dependency_skew` is a REDUNDANT GUARD — `_apply` calls
    `_venv_guard` first and never reaches it — recorded rather than removed.
    """
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / ".gitignore").write_text("venv/\n")
    (runtime / "a.py").write_text("A\n")
    _commit(runtime, "base")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to("/nonexistent/python")

    with pytest.raises(updater.UpdateError) as caught:
        updater._venv_guard(runtime)
    assert "missing or incomplete" in str(caught.value)


@pytest.mark.parametrize(
    ("deps", "expected_skew"),
    [
        # An environment marker that does not apply must not be reported.
        ('["nonexistent-pkg-abc; python_version < \'3.0\'"]', False),
        # Extras are stripped: the base package is what the venv must supply.
        ('["pytest[an-extra-that-does-not-exist]>=1"]', False),
        # A direct-URL requirement still resolves to a name that must be installed.
        ('["definitely-not-real-xyz @ https://example.invalid/x.whl"]', True),
    ],
)
def test_OBSERVED_requirement_shapes_are_parsed_by_packaging(
    tmp_path, deps, expected_skew
):
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / "pyproject.toml").write_text(
        f"[project]\nname='x'\nversion='0'\ndependencies = {deps}\n"
    )
    head = _commit(runtime, "t")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    # pytest present so the "installed, extra does not exist" case is really satisfied,
    # and packaging present so markers are evaluated rather than ignored.
    _venvify(runtime, installed={"pytest": "9.0.2"})
    assert bool(updater._dependency_skew(runtime, head)) is expected_skew


def test_a_target_whose_pyproject_is_UNPARSEABLE_now_RAISES(tmp_path):
    """The case removed from the parametrize above, kept as its own assertion.

    It used to be one of three "FAILS_OPEN" cases returning `[]` — indistinguishable
    from "measured, no skew". That test's own docstring said it pinned the behaviour
    "so a change is deliberate" and was "NOT an endorsement". This is the deliberate
    change: unreadable is not clean, so it raises.

    The other two cases stay empty on purpose. A target with `dynamic = ['dependencies']`
    or no [project] table declares no STATIC main dependencies, which is a measured
    answer, not a failure to measure. hermes-agent does not use dynamic deps today.
    """
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / "pyproject.toml").write_text("[project\nname='x'\n")   # malformed TOML
    (runtime / "keep.txt").write_text("so the commit is never empty\n")
    head = _commit(runtime, "t")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)
    _venvify(runtime)

    with pytest.raises(updater.UpdateError) as err:
        updater._target_main_dependencies(runtime, head)
    assert "not parseable" in str(err.value)

    with pytest.raises(updater.UpdateError):
        updater._dependency_skew(runtime, head)


@pytest.mark.parametrize(
    "pyproject",
    [
        "[project]\nname='x'\nversion='0'\ndynamic=['dependencies']\n",
        "",                                                       # empty
    ],
)
def test_OBSERVED_a_target_declaring_no_STATIC_deps_measures_as_empty(tmp_path, pyproject):
    """A target declaring no STATIC main deps measures as empty — and that is correct.

    DOCSTRING REWRITTEN. It previously described this as a fail-OPEN path, listing
    "cannot be parsed, cannot be read from git" alongside these cases and citing "four
    other silent-empty returns". All of that is now false: unparseable raises, unreadable
    raises, an unparseable requirement is reported, an absent packaging is reported, and
    empty probe output raises. Only these two remain empty, and they are MEASURED empties
    — `dynamic = ['dependencies']` and a pyproject with no [project] table both declare
    no static main dependencies. hermes-agent does not use dynamic deps today.

    The old text was the contract documentation rotting inside the test that documents
    the contract, which review caught.
    """
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / "pyproject.toml").write_text(pyproject)
    (runtime / "keep.txt").write_text("so the commit is never empty\n")
    head = _commit(runtime, "t")
    (runtime / "venv" / "bin").mkdir(parents=True)
    (runtime / "venv" / "bin" / "python").symlink_to(sys.executable)

    assert updater._target_main_dependencies(runtime, head) == []
    assert updater._dependency_skew(runtime, head) == []


def test_ROLLBACK_is_never_refused_for_dependency_skew(world):
    """B1 from independent review — the severest defect in this branch, end to end.

    `rollback` routes through `_apply`, so the skew check was evaluated against the
    ROLLBACK target and refused the incident verb. The sequence is exactly the one the
    venv-resync packet creates: resync the venv forward, advance, hit a regression, then
    find rollback refused because the OLDER target pins the OLDER dependency — with the
    refusal telling the operator to resync the venv mid-incident while 11 gateways sit on
    the bad commit.

    Going back to a commit the venv over-satisfies is the recovery path, not a hazard:
    that code ran against these same packages until minutes ago.

    This is deliberately end-to-end rather than a grep of `_apply`'s source, which is how
    it was first pinned. A one-line short-circuit protecting the incident verb deserves a
    test that actually rolls back.
    """
    _bootstrap(world)
    runtime = world["runtime"]

    # t_old declares a package the venv HAS; advancing to it succeeds.
    # packaging present too, or the B3 guard refuses the FORWARD advance before we ever
    # reach the rollback this test is about.
    _venvify(runtime, installed={"rollbackpkg": "1.0"})
    import sys as _s
    site = runtime / "venv" / "lib" / f"python{_s.version_info.major}.{_s.version_info.minor}" / "site-packages"
    info = site / "rollbackpkg-1.0.dist-info"

    t_old = _new_target(world, gitignore=IGNORES_VENV, deps='["rollbackpkg==1.0"]')
    _git(runtime, "fetch", "-q", "origin")
    assert _steady_apply(world, t_old) == 0, "advancing to a satisfied target failed"

    # t_new declares nothing; advance succeeds. Its receipt is the one rollback consumes:
    # target == current HEAD (t_new), before_head == t_old.
    t_new = _new_target(world, gitignore=IGNORES_VENV, deps="[]", body="VERSION_C\n")
    _git(runtime, "fetch", "-q", "origin")
    assert _steady_apply(world, t_new) == 0
    receipts = sorted(world["receipts"].glob("*.json"))
    assert receipts, "no update receipt was written for the advance"
    rollback_receipt = receipts[-1]

    # The operator "resyncs" the venv forward, dropping the package t_old required.
    # This is the step that arms the trap.
    shutil.rmtree(info)

    # Now roll back. Under the defect this returned UNMEASURED_EXIT with HEAD unmoved.
    rc = updater.main([
        "rollback", "--runtime", str(runtime),
        "--update-receipt", str(rollback_receipt),
        "--backup-receipt",
        str(_backup_receipt(world["scratch"], runtime, f"rb{time.time_ns()}")),
        "--receipt-dir", str(world["receipts"]),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ])
    assert rc == 0, (
        "rollback was refused for dependency skew against the rollback target — the "
        "incident verb is disabled in exactly the situation it exists for"
    )
