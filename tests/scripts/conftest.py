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

# Each entry: the subject attribute whose ABSENCE means the feature is deferred,
# and the substrings in a test's name that indicate it exercises that feature.
# Whole FILES that exist only to cover deferred machinery. Keyed the same way — the
# file's tests return the moment the named attribute does. Cheaper and more honest
# than name-matching for a file that is 100% about one removed feature.
_DEFERRED_FILES = {
    "_dependency_skew": ("test_update_opt_hermes_runtime_depskew.py",),
}

_DEFERRED = {
    "_dependency_skew": ("dependency_skew", "depskew", "dependency_probe", "requirement_shapes",
                         "target_pyproject"),
    "_git_ignored": ("git_ignored", "check_ignore", "gitignored_orphan"),
    "_provenance_is_exact": ("importable_orphan_still_refuses",),
}


def _subject_has(name: str) -> bool:
    spec = importlib.util.spec_from_file_location("_subject_probe", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return hasattr(mod, name)


def pytest_collection_modifyitems(config, items):
    absent = {attr for attr in _DEFERRED if not _subject_has(attr)}
    if not absent:
        return  # assurance half is back; every test runs again automatically
    for item in items:
        fname = Path(str(item.fspath)).name
        for attr in absent:
            if fname in _DEFERRED_FILES.get(attr, ()) or any(
                token in item.name for token in _DEFERRED[attr]
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
