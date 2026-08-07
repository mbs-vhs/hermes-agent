"""Skip assurance-half tests while the assurance half is deferred (CLAWD-3655).

WHY THIS EXISTS
---------------
`update_opt_hermes_runtime.py` was narrowed to its deploy half. Measured before the
cut: 34% of the module performed the advance, 66% proved the advance was safe — and
12 of 16 adversarial findings across two rounds lived in that 66%. Round 2 produced
8 blocking findings, every one introduced by the round-1 fixes to that same half.
So it is deferred, not abandoned: see CLAWD-3655 for the decision and the three
structural rules any return has to satisfy.

The tests for the deferred machinery are KEPT, not deleted. They are the most
expensive artifact in this directory — several were written by an independent
tester and pin real defects — and deleting them would mean rediscovering those
defects when the machinery returns.

HOW IT WORKS, AND WHY IT IS CONDITIONAL RATHER THAN A HARDCODED SKIPLIST
------------------------------------------------------------------------
The skip is keyed on whether the *subject* still defines the function under test.
When `_dependency_skew` or `_git_ignored` comes back, these tests start running
again on their own — nobody has to remember to unskip them, and nobody can quietly
leave them skipped after the code returns. A hardcoded list would rot in exactly
the direction that hides coverage.

DELIBERATELY NOT SKIPPED: anything covering the deploy half — the venv exclusion,
`_clean_runtime`, `recover --dry-run`, the signal handling, target validation,
transaction/rollback. Those held across both review rounds and are the reason this
narrowing is worth doing. If a skip below ever swallows one of them, that is a
defect in this file, not a deferral.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SUBJECT = Path(__file__).resolve().parents[2] / "scripts" / "update_opt_hermes_runtime.py"

# Whole FILES that exist only to cover deferred machinery. The file's tests return
# the moment the named attribute does.
_DEFERRED_FILES = {
    "_dependency_skew": ("test_update_opt_hermes_runtime_depskew.py",),
}

# EXACT test names, scoped by file, NOT name substrings.
#
# This was substring matching until an independent tester demonstrated the hole: the
# token `git_ignored` also matches a plausible DEPLOY-half name such as
# `test_venv_must_be_git_ignored_before_any_clean` — the tester dropped exactly that
# test, containing a bare `assert False`, into a deploy-half file and the gate still
# reported green. A skiplist that can silently swallow a failing deploy-half test is
# worse than no skiplist, because it reports as coverage.
#
# Exactness costs nothing here and cannot collide. The failure mode it introduces is
# the safe one: a deferred test nobody listed does not get skipped, so it fails loudly
# with `AttributeError: module has no attribute '_dependency_skew'`. Rot is visible.
# Parametrised ids are matched on the name before '['.
_DEFERRED_TESTS = {
    "_dependency_skew": {
        "test_update_opt_hermes_runtime_adversarial.py": (
            "test_PIN_dry_run_plan_carries_the_real_dependency_skew",
            "test_PIN_dependency_skew_refuses_the_apply_and_the_flag_overrides",
            "test_PIN_dependency_skew_asks_the_RUNTIME_venv_not_the_updater_interpreter",
            "test_PIN_the_dependency_probe_writes_no_bytecode_into_the_venv",
            "test_OBSERVED_requirement_shapes_are_parsed_by_packaging",
            "test_OBSERVED_an_unreadable_target_pyproject_FAILS_OPEN",
        ),
    },
    "_git_ignored": {
        "test_update_opt_hermes_runtime_adversarial.py": (
            "test_DEFECT_git_ignored_splits_a_path_that_contains_a_newline",
            "test_OBSERVED_check_ignore_settles_ten_thousand_paths_in_one_process",
        ),
        "test_update_opt_hermes_runtime_readiness.py": (
            "test_git_ignored_helper_is_exact",
        ),
    },
    "_provenance_is_exact": {
        "test_update_opt_hermes_runtime_readiness.py": (
            "test_an_ignored_but_importable_orphan_still_refuses",
        ),
    },
}
#
# DELIBERATELY NOT LISTED: test_DEFECT_readiness_wedges_on_a_non_ascii_gitignored_orphan.
# It is an xfail(strict=True) pinning a wedge the narrowing REMOVED — `_ready` no longer
# consults the provenance walk, so the test now passes and strict-xfail turns that into
# the hard failure it is designed to be ("your defect is fixed, update me"). Skipping it
# suppressed exactly the signal the marker exists to raise. Its marker is dropped and it
# now runs as a positive regression guard that the non-ASCII wedge stays gone.


def _subject_has(name: str) -> bool:
    spec = importlib.util.spec_from_file_location("_subject_probe", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return hasattr(mod, name)


def pytest_collection_modifyitems(config, items):
    absent = {attr for attr in _DEFERRED_TESTS if not _subject_has(attr)}
    if not absent:
        return  # assurance half is back; every test runs again automatically
    for item in items:
        fname = Path(str(item.fspath)).name
        base = item.name.split("[", 1)[0]  # parametrised ids share one base name
        for attr in absent:
            if fname in _DEFERRED_FILES.get(attr, ()) or base in _DEFERRED_TESTS[attr].get(
                fname, ()
            ):
                item.add_marker(
                    pytest.mark.skip(
                        reason=(
                            f"deferred with the assurance half: subject no longer defines "
                            f"{attr} (CLAWD-3655). This test un-skips itself the moment it "
                            f"comes back."
                        )
                    )
                )
                break
