"""The runbook's backup-receipt example must be one the code actually accepts.

WHY THIS EXISTS
---------------
`docs/operations/opt-runtime-update.md` documents the backup receipt an operator must
produce before the first `/opt/hermes-agent` conversion. It has now been wrong twice:

1. It listed seven of the ten required fields, so an operator hit
   `FATAL: backup receipt missing fields`.
2. The revision that fixed *that* shipped `"archive_profile": "opt-hermes-agent-full"`
   and `"archive_root": "/opt/hermes-agent"` — values `_validate_backup_receipt`
   refuses outright. An operator following the corrected runbook stopped at the first
   gate of the first conversion with
   `FATAL: backup receipt archive_profile is not approved`.

Both were found by reading, after shipping. The runbook was behaving as a seventh,
untested oracle sitting next to six tested ones: prose that states values the code
rejects, with nothing to contradict it.

So the doc is now on the gate. These tests parse the receipt block out of the shipped
markdown and check it against the subject's own constants — not against a copy of them
— so the two can never drift apart silently again. A doc fix that is itself unverified
is the same defect one layer out.

DELIBERATELY NOT ASSERTED: the value-shaped placeholders (`64-lowercase-hex`, paths,
timestamps). Those are illustrative and cannot be validated without a real archive.
Only the fields whose values the code pins to exact constants are checked here.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"
RUNBOOK = REPO / "docs" / "operations" / "opt-runtime-update.md"


def _subject():
    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _receipt_from_runbook() -> dict:
    """The first ```json block in the runbook that declares a backup receipt."""
    blocks = re.findall(r"```json\n(.*?)\n```", RUNBOOK.read_text(), re.DOTALL)
    for block in blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "archive_profile" in payload:
            return payload
    pytest.fail(
        "no parseable json receipt block containing 'archive_profile' in "
        f"{RUNBOOK} — the documented receipt has moved or stopped being valid JSON"
    )


def test_the_runbook_receipt_declares_every_field_the_code_requires():
    """A missing field is the first failure mode this doc already shipped once."""
    receipt = _receipt_from_runbook()
    required = {
        "runtime",
        "runtime_fingerprint",
        "created_at",
        "archive_profile",
        "archive_root",
        "archive_path",
        "archive_sha256",
        "remote_uri",
        "roundtrip_path",
        "roundtrip_sha256",
    }
    missing = sorted(required - receipt.keys())
    assert not missing, (
        f"the runbook's example receipt omits {missing}; an operator copying it hits "
        "'FATAL: backup receipt missing fields' at the first gate"
    )


def test_the_runbook_archive_profile_is_the_one_the_code_approves():
    """Read from the subject, never restated — a restated constant drifts."""
    mod = _subject()
    receipt = _receipt_from_runbook()
    assert receipt["archive_profile"] == mod.BACKUP_ARCHIVE_PROFILE, (
        f"runbook documents archive_profile={receipt['archive_profile']!r} but the "
        f"code accepts only {mod.BACKUP_ARCHIVE_PROFILE!r}; an operator following the "
        "runbook is refused with 'backup receipt archive_profile is not approved'"
    )


def test_the_runbook_archive_root_is_the_canonical_dot():
    receipt = _receipt_from_runbook()
    assert receipt["archive_root"] == ".", (
        f"runbook documents archive_root={receipt['archive_root']!r}; the code refuses "
        "anything but '.' with 'backup receipt archive_root must be canonical'"
    )


def test_every_updater_verb_the_runbook_tells_an_operator_to_RUN_exists():
    """The runbook must not route an operator to a command the parser rejects.

    Added after an independent reviewer measured that the doc half of the fingerprint
    commit was entirely unverified: reverting the whole documentation hunk to its
    previous revision left this file's gate at 11 passed. This file exists precisely
    because the runbook had been "a seventh, untested oracle" — and it had just gained
    a third operator-facing routing claim without anything checking it.

    A wrong verb here is not cosmetic. `runtime_fingerprint` has exactly one reachable
    producer before `.git` exists, and naming the wrong one puts the operator back in
    the circularity that made the first conversion impossible.
    """
    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    choices = set(mod._parser()._actions[1].choices)

    text = RUNBOOK.read_text()
    # Same line only. An earlier draft allowed the match to cross a newline, which
    # swept up the next entry of the install-assets list ('scripts/...') and reported
    # a phantom unknown verb. A verb is always adjacent to the script name.
    named = set(re.findall(r"update_opt_hermes_runtime\.py[ \t]+([a-z][a-z-]*)", text))
    assert named, "no updater invocations found in the runbook — has it been restructured?"

    unknown = sorted(named - choices)
    assert not unknown, (
        f"the runbook tells an operator to run {unknown}, which the parser rejects. "
        f"Valid verbs: {sorted(choices)}"
    )


def test_the_runbook_routes_the_FIRST_conversion_to_the_pre_git_producer():
    """`audit` requires .git; only `fingerprint` works before the tree is a checkout."""
    text = RUNBOOK.read_text()
    assert "fingerprint" in text, (
        "the runbook never names the `fingerprint` verb, so it routes the first "
        "conversion to `audit`, which requires the .git that `init` has not created "
        "yet — the circularity that made the first conversion unreachable"
    )


def test_the_runbook_gives_a_tar_command_matching_the_profile_it_names():
    """The profile string names an invocation; without it the doc is unexecutable.

    The archive is what `rollback` restores from. `--numeric-owner`/`--acls`/`--xattrs`
    are load-bearing because the tree is root-owned and executed by eleven separate
    per-uid accounts.
    """
    text = RUNBOOK.read_text()
    assert "tar --zstd" in text, "the runbook names an archive profile but no tar command"
    for flag in ("--format=pax", "--numeric-owner", "--acls", "--xattrs", "--directory"):
        assert flag in text, (
            f"the documented tar command omits {flag}, which the profile "
            "'gnu-tar-zstd-pax-numeric-owner-acl-xattr-v1' names"
        )
