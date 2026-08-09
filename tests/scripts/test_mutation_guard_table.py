"""The mutation battery's table must not rot silently.

WHY THIS FILE IS CHEAP AND THE BATTERY IS NOT
----------------------------------------------
`scripts/mutation_guard_check.py` answers "is each declared guard load-bearing?" by
removing each one and requiring the suite to go red. That costs a suite run per guard —
about six minutes for the current table — which is too slow to put in front of every
commit, and a check that is too slow gets skipped.

So the split is: the EXPENSIVE battery runs before a merge, and this FAST file runs in
the ordinary suite and protects the battery from the one failure mode that would make it
lie — an anchor that no longer matches, so the mutation silently no-ops and the guard
reports as killed when nothing was tested.

That failure mode is not hypothetical. The battery's anchors are literal source strings;
any refactor of the subject moves them. Without this file, the first such refactor turns
a green battery into a green battery that measures nothing, which is strictly worse than
having no battery at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BATTERY = REPO / "scripts" / "mutation_guard_check.py"
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"


def _battery():
    spec = importlib.util.spec_from_file_location("mutation_guard_check", BATTERY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_mutation_anchor_matches_the_subject_exactly_once():
    """A stale anchor makes the battery report 'killed' for a mutation it never applied."""
    source = SUBJECT.read_text()
    stale = []
    for mutation in _battery().MUTATIONS:
        count = source.count(mutation["old"])
        if count != 1:
            stale.append(f"{mutation['id']}: matches {count}x (expected 1)")
    assert not stale, (
        "the mutation battery's table has rotted against the subject:\n  "
        + "\n  ".join(stale)
        + "\n\nFix the anchors. A mutation whose anchor does not match is applied to "
        "nothing, so its guard reports as protected while being untested."
    )


def test_every_mutation_actually_changes_the_source():
    """old != new. A no-op row would report 'killed' for free."""
    for mutation in _battery().MUTATIONS:
        assert mutation["old"] != mutation["new"], (
            f"mutation {mutation['id']} does not change anything, so it can never be "
            "detected and its guard is not really being tested"
        )


def test_every_mutation_declares_why_the_guard_exists():
    """The `why` is what makes a SURVIVED line actionable rather than a puzzle."""
    for mutation in _battery().MUTATIONS:
        assert mutation.get("why", "").strip(), f"mutation {mutation['id']} has no rationale"
        assert len(mutation["why"]) > 20, (
            f"mutation {mutation['id']} rationale is too thin to act on: {mutation['why']!r}"
        )


def test_the_mutated_source_still_parses():
    """A mutation that breaks syntax reddens everything and proves nothing about a guard."""
    import ast

    source = SUBJECT.read_text()
    for mutation in _battery().MUTATIONS:
        mutated = source.replace(mutation["old"], mutation["new"], 1)
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            raise AssertionError(
                f"mutation {mutation['id']} produces invalid Python ({exc}); the suite "
                "would go red on a syntax error rather than on the guard's absence, "
                "which would report the guard as protected when it is not"
            ) from exc
