"""CLAWD-3678 — the post-advance `venv_after != venv_before` refusal.

WHY THIS FILE EXISTS
--------------------
`scripts/mutation_guard_check.py` declares this guard as row `venv-unchanged-by-apply`
("the venv is the interpreter; an advance must not touch it") and reports it KILLED.
It was not killed by anything behavioural. Measured by an independent tester against
the real gate on `7f139d9e3`:

    reversion: `if venv_after != venv_before:` -> `if False:`   (in `_apply`)
    gate:      scripts/run_tests.sh tests/scripts/
    result:    16 files, 203 tests passed, 1 failed
    the ONE failure: tests/scripts/test_mutation_guard_table.py::
      test_every_mutation_anchor_matches_the_subject_exactly_once
      "venv-unchanged-by-apply: matches 0x (expected 1)"

That is the battery's own table-integrity test noticing its anchor string vanished.
It fires for EVERY row in the table and says nothing about the guard's behaviour, so
before this file the refusal that stops an advance from mutating the interpreter all
11 gateways execute could be deleted with every behavioural assertion still green.

HOW THE VENV IS MADE TO CHANGE — and why that is not a contrived condition
--------------------------------------------------------------------------
On the real fleet the advance runs against a venv that is IN USE: eleven gateway
processes execute out of `/opt/hermes-agent/venv` while the reset and clean happen.
The updater itself runs that interpreter mid-apply — `_dependency_skew` invokes
`venv/bin/python -c <probe>` between `venv_before` and `venv_after` — and it passes
`PYTHONDONTWRITEBYTECODE=1` precisely because otherwise its own probe would leave
`__pycache__` behind inside the venv. So "something wrote into the venv during the
window" is the ordinary case this guard is for, not an exotic one.

The fixture models it in the only place a test controls: `venv/bin/python` is a real
script (a venv's `bin/python` is a real file, not a fiction) that drops one byte
inside the venv the first time it runs, then `exec`s a genuine interpreter. Nothing
in the subject is monkeypatched; the probe, the reset, the clean and the recovery all
run for real.

TWO ROUTES WERE TRIED. THE OTHER ONE IS A REDUNDANT GUARD, RECORDED BELOW
-------------------------------------------------------------------------
The first attempt drove the change through git: park HEAD on a commit that TRACKS a
path under `venv/`, then advance to one that does not, so `reset --hard` deletes it
out of the live venv. `_target_tracks_under_venv` inspects the TARGET only and is
silent about HEAD, so that side is genuinely open — but the apply never gets there.
`_venv_guard` runs `git check-ignore -q -- venv`, which consults the index, and a
tracked path under `venv/` makes it fail: "runtime/venv is not ignored; clean -fd is
unsafe". A lower layer already refuses the state. That is recorded as its own test
below rather than deleted, because the reason the HEAD-side asymmetry is safe is not
obvious from reading `_apply`.
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

# Dropped inside the venv by the instrumented interpreter. Named so a failure
# message reads as what it is.
MARKER = "left-behind-by-something-running-out-of-the-venv"


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
    """A source repo plus a non-git runtime holding the gitignored live venv."""
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
    # The initial-evidence apply refuses an already-clean runtime, so the
    # conversion needs something real to sweep.
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


def _steady_apply(world: dict, target: str, *, tag: str) -> int:
    return updater.main([
        "apply", "--runtime", str(world["runtime"]), "--target", target,
        "--fetch", "--remote-url", str(world["origin"]),
        "--backup-receipt",
        str(_backup_receipt(world["scratch"], world["runtime"], tag)),
        "--receipt-dir", str(world["receipts"]),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ])


def _target_declaring(world: dict, dependency: str) -> str:
    """A target whose main dependency the exec'd interpreter really satisfies.

    It has to be SATISFIED, or `_dependency_skew` refuses the advance first and the
    test would be green against a subject with no venv guard at all.
    """
    source = world["source"]
    (source / "pkg" / "live.py").write_text("VERSION_B\n")
    (source / "pyproject.toml").write_text(
        f"[project]\nname='hermes-agent'\nversion='0'\ndependencies = ['{dependency}']\n"
    )
    head = _commit(source, "ordinary advance that declares a satisfied dependency")
    _git(source, "push", "-q", "origin", "main")
    return head


def _interpreter_that_leaves_a_byte_behind(runtime: Path) -> None:
    """Replace `venv/bin/python` with a real script that writes inside the venv.

    Installed AFTER the bootstrap on purpose. During the initial-evidence apply the
    tree still has orphans, so `opt_provenance_report.resolve_importable` also runs
    this interpreter — and it does so from inside `_build_audit`, between that
    function's two tree fingerprints. A write there would trip
    "runtime tree changed while the audit was being measured" instead, which is a
    different guard.

    In the steady state exercised below there are no orphans, `resolve_importable`
    returns early without spawning anything, and the ONLY invocation of this
    interpreter during `_apply` is the dependency-skew probe — which sits between
    `venv_before` and `venv_after`.
    """
    python = runtime / "venv" / "bin" / "python"
    real = sys.executable
    python.unlink()
    python.write_text(
        "#!/bin/sh\n"
        f"[ -e '{runtime}/venv/{MARKER}' ] || printf x > '{runtime}/venv/{MARKER}'\n"
        f"exec '{real}' \"$@\"\n"
    )
    python.chmod(0o755)


def _journal_failures(world: dict) -> list[str]:
    return [
        str(json.loads(p.read_text(encoding="utf-8")).get("failure", ""))
        for p in sorted(world["transactions"].glob("*.json"))
        if p.name != updater.BOOTSTRAP_STATE
    ]


# ═══════════════════════════ the guard under test ════════════════════════════


def test_apply_REFUSES_when_the_live_venv_changed_during_the_advance(
    world, capsys: pytest.CaptureFixture[str]
):
    """`venv_after != venv_before` is the last thing between a mutated interpreter
    and a success receipt.

    Nothing else in `_apply` can notice. `_ready` asks only whether the tree is clean
    and exactly at the target, and the venv is IGNORED — so a byte added or removed
    inside it leaves the tree clean, at the target, and looking like a good advance.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    _interpreter_that_leaves_a_byte_behind(runtime)

    marker = runtime / "venv" / MARKER
    assert not marker.exists(), "fixture precondition: nothing has run the venv yet"
    before_head = updater._head(runtime)

    target = _target_declaring(world, "pytest")
    capsys.readouterr()
    rc = _steady_apply(world, target, tag="vu1")
    err = capsys.readouterr().err

    assert marker.exists(), (
        "fixture precondition failed: the dependency probe never ran the runtime's "
        "own interpreter, so nothing changed the venv and this test proves nothing"
    )
    assert rc == updater.UNMEASURED_EXIT, (
        f"apply returned {rc} after the live venv changed mid-advance — the "
        f"interpreter every gateway runs was mutated and the tool reported success. "
        f"stderr={err!r}"
    )
    # The refusal names the guard. It reaches the operator through the transaction
    # journal rather than stderr, because a venv change is not something the tool can
    # undo: `_restore_steady_transaction` resets and cleans, neither of which touches
    # an ignored venv, so recovery legitimately fails its own venv check and stderr
    # carries the outer "run recover" wording.
    assert any(
        "live venv changed during source advance" in failure
        for failure in _journal_failures(world)
    ), (
        "the apply failed, but not because of the venv guard; journal failures were "
        f"{_journal_failures(world)!r} and stderr was {err!r}"
    )
    assert updater._head(runtime) == before_head, (
        "HEAD was left on the target although the advance was refused"
    )


def test_no_success_receipt_is_published_when_the_venv_changed(world):
    """A runbook reads receipts, not exit codes.

    Pinned separately: an operator who only checks `runtime-receipts/` would see a
    clean advance with no indication the interpreter moved underneath the fleet.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    _interpreter_that_leaves_a_byte_behind(runtime)
    before = sorted(p.name for p in world["receipts"].glob("*.json"))

    target = _target_declaring(world, "pytest")
    assert _steady_apply(world, target, tag="vu2") == updater.UNMEASURED_EXIT

    after = sorted(p.name for p in world["receipts"].glob("*.json"))
    assert after == before, (
        "an advance that mutated the live venv still published a success receipt: "
        f"{sorted(set(after) - set(before))}"
    )


def test_CONTROL_the_same_advance_SUCCEEDS_when_the_venv_is_left_alone(world):
    """The control that makes the two tests above mean something.

    Same fixture, same instrumented interpreter, same target — the ONLY difference is
    that the byte is already there before the advance starts, so running the venv
    changes nothing. If this went red the assertions above would be measuring "apply
    refuses" rather than "apply refuses because the venv changed".
    """
    _bootstrap(world)
    runtime = world["runtime"]
    _interpreter_that_leaves_a_byte_behind(runtime)
    (runtime / "venv" / MARKER).write_text("x")
    venv_before = updater._venv_guard(runtime)

    target = _target_declaring(world, "pytest")
    assert _steady_apply(world, target, tag="vu3") == 0, (
        "the instrumented interpreter alone blocks the advance, so the refusal above "
        "is not evidence about the venv guard"
    )
    assert updater._head(runtime) == target
    assert updater._venv_guard(runtime) == venv_before


def test_CONTROL_a_plain_advance_with_no_instrumentation_succeeds(world):
    """Second control, one layer simpler: the untouched fixture advances cleanly."""
    _bootstrap(world)
    runtime = world["runtime"]
    venv_before = updater._venv_guard(runtime)

    source = world["source"]
    (source / "pkg" / "live.py").write_text("VERSION_B\n")
    target = _commit(source, "ordinary advance")
    _git(source, "push", "-q", "origin", "main")

    assert _steady_apply(world, target, tag="vu4") == 0
    assert updater._head(runtime) == target
    assert updater._venv_guard(runtime) == venv_before


# ═════════ the REDUNDANT half of the hazard, recorded rather than dropped ═════


def test_a_runtime_whose_HEAD_tracks_a_venv_path_is_refused_at_the_LOWER_layer(world):
    """REDUNDANT GUARD, stated rather than left as an apparent gap.

    `_target_tracks_under_venv` checks the TARGET and says nothing about HEAD, so
    "HEAD tracks venv/x, target does not, `reset --hard` deletes it out of the live
    venv" looks like an open hole in `_apply`. It is closed one layer down:
    `_venv_guard` asks `git check-ignore -q -- venv`, which consults the index, and a
    tracked path under `venv/` makes that fail.

    Do not delete `_venv_guard`'s check-ignore refusal on the strength of the
    target-side one: they answer different questions, and this is the only thing
    covering the HEAD side.
    """
    _bootstrap(world)
    runtime = world["runtime"]
    source = world["source"]

    (source / "venv" / "lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (source / "venv" / "lib" / "site-packages" / "vendored.pth").write_text("/opt\n")
    _git(source, "add", "-f", "venv/lib/site-packages/vendored.pth")
    tracks = _commit(source, "vendors a path under venv/")
    _git(source, "push", "-q", "origin", "main")

    _git(runtime, "fetch", "-q", "origin")
    _git(runtime, "reset", "--hard", "-q", tracks)
    assert (runtime / "venv" / "lib" / "site-packages" / "vendored.pth").exists()

    with pytest.raises(updater.UpdateError) as err:
        updater._venv_guard(runtime)
    assert "not ignored" in str(err.value), str(err.value)


# ══════════ the battery's third blind row: audit-tree-stable ════════════════


def test_an_audit_whose_tree_MOVED_underneath_it_is_refused_not_reported(world):
    """`audit-tree-stable` was the last declared guard with no behavioural cover.

    Measured against the real gate on `7f139d9e3`: reverting
    `if tree_before != tree_after:` to `if False:` left `17 files, 209 passed,
    1 failed`, and that one failure was again the battery's own anchor test.

    The trigger is the ordinary fleet condition, not an exotic one. `_build_audit`
    fingerprints the tree, then runs `opt_provenance_report.resolve_importable`,
    which spawns `venv/bin/python` with `cwd=<runtime>`, then fingerprints again.
    Eleven gateways are executing out of that tree the whole time and they write
    into it — `logs/` is in the fork's own .gitignore, and `_tree_fingerprint` walks
    IGNORED paths too. A single log line landing in that window makes the audit a
    measurement of two different trees, and every later decision (`ready`, the
    initial-evidence byte-comparison, the receipt's `post_tree_fingerprint`) is then
    derived from a tree that never existed.

    The fixture makes the write happen deterministically at the one moment the
    subject hands control to the runtime's interpreter.
    """
    runtime = world["runtime"]
    # `init` only: the initial-evidence apply would sweep `pkg/orphan.py`, and the
    # orphan is what gives `resolve_importable` a candidate to spawn for.
    assert updater.main([
        "init", "--runtime", str(runtime), "--target", world["t1"],
        "--remote-url", str(world["origin"]),
        "--backup-receipt", str(_backup_receipt(world["scratch"], runtime, "init")),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0

    python = runtime / "venv" / "bin" / "python"
    python.unlink()
    python.write_text(
        "#!/bin/sh\n"
        f"mkdir -p '{runtime}/logs'\n"
        f"[ -e '{runtime}/logs/gateway.log' ] || "
        f"printf 'a gateway logged while the audit ran\\n' > '{runtime}/logs/gateway.log'\n"
        f"exec '{sys.executable}' \"$@\"\n"
    )
    python.chmod(0o755)

    with pytest.raises(updater.UpdateError) as err:
        updater._build_audit(runtime, world["t1"])
    assert "changed while the audit was being measured" in str(err.value), str(err.value)
    assert (runtime / "logs" / "gateway.log").exists(), (
        "fixture precondition failed: the provenance probe never spawned the "
        "runtime interpreter, so nothing moved the tree and this test proves nothing"
    )


def test_CONTROL_the_same_audit_SUCCEEDS_when_nothing_writes_to_the_tree(world):
    """Control for the above: same fixture, same `init`, interpreter left alone."""
    runtime = world["runtime"]
    assert updater.main([
        "init", "--runtime", str(runtime), "--target", world["t1"],
        "--remote-url", str(world["origin"]),
        "--backup-receipt", str(_backup_receipt(world["scratch"], runtime, "init")),
        "--transaction-dir", str(world["transactions"]),
        "--lock-file", str(world["lock"]),
    ]) == 0

    audit = updater._build_audit(runtime, world["t1"])
    assert audit["target"] == world["t1"]
    assert audit["tree_fingerprint"]["entry_count"] > 0


# ══════════ the battery's other blind row: deps-not-a-list ═══════════════════


def test_a_non_list_dependencies_value_is_REFUSED_not_iterated_as_characters(
    tmp_path: Path,
):
    """`deps-not-a-list` is the battery's other row with no behavioural coverage.

    Measured the same way: reverting `if not isinstance(deps, list):` to `if False:`
    left the gate at `203 passed, 1 failed`, and the one failure was the battery's
    anchor test.

    Without the guard the value is not rejected, it is ITERATED. A TOML string flows
    into the `isinstance(d, str)` filter where every CHARACTER is a str, so the
    non-string check waves it through too, and the probe is handed one bogus
    requirement per letter — a wall of invented findings instead of "your target's
    dependency list is malformed".
    """
    runtime = tmp_path / "rt"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    (runtime / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\ndependencies = 'requests'\n"
    )
    head = _commit(runtime, "malformed dependency list")

    with pytest.raises(updater.UpdateError) as err:
        updater._target_main_dependencies(runtime, head)
    assert "not a list" in str(err.value), str(err.value)
