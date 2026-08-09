#!/usr/bin/env python3
"""Prove every declared guard in the /opt runtime updater is load-bearing.

WHY THIS EXISTS
---------------
CLAWD-3655 and CLAWD-3678 are the same finding twice: on this file, roughly three of
every five fixes have introduced a defect of the class they closed, across four rounds
of independent review. Every round the reviewer ran a mutation battery by hand and found
guards that could be deleted with the suite fully green — six of them in round 4 alone.
Reading caught none of those. Mutation caught all of them.

That is a TOOLING gap, not an attention gap. The gate could not answer "is this guard
load-bearing?", so the only thing standing between an unpinned guard and main was whether
a human remembered to dispatch a reviewer. This script makes the answer mechanical.

WHAT IT DOES
------------
For each declared guard: copy the tree, delete or invert exactly that guard, run the
suite, and require it to go RED. A guard whose removal leaves the suite green is
reported as SURVIVED and the script exits non-zero. Survived == the guard is decoration:
it may well be correct, but nothing would tell you if it stopped being correct.

WHAT IT IS NOT
--------------
Not a general mutation tester. The mutation table is deliberately hand-written and
explicit — each row names a guard someone argued for, with the reason it exists. A
generated-mutant tool would drown this in equivalent mutants and get switched off, which
is the failure mode that matters for a check nobody is forced to read.

COST
----
Each surviving mutant costs a full suite run; each killed mutant exits at the first
failure (`-x`). So the healthy case is fast and the unhealthy case is the one that pays,
which is the right way round.

USAGE
    scripts/mutation_guard_check.py            # all guards
    scripts/mutation_guard_check.py --list     # names only
    scripts/mutation_guard_check.py -k venv    # substring filter
Exit 0 = every guard killed its mutant. Exit 1 = at least one SURVIVED. Exit 2 = the
table itself is stale (an anchor no longer matches the source).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBJECT_REL = "scripts/update_opt_hermes_runtime.py"
SUITE = "tests/scripts"

# Scratch lives beside the repo, never under /tmp: /tmp is tmpfs on the reference host
# and a 60-way fixture matrix there took the machine down mid-session.
SCRATCH_ROOT = REPO.parent


# Each row: a guard someone argued for, and the single edit that removes it.
# `old` must appear EXACTLY ONCE in the subject, or the table is stale and we exit 2 —
# a silently-skipped mutation is worse than no mutation.
MUTATIONS: list[dict[str, str]] = [
    {
        "id": "venv-exclusion",
        "why": "clean -fd would delete the live interpreter all 11 gateways execute",
        "old": '_git(runtime, "clean", "-fd", "-e", VENV_EXCLUDE)',
        "new": '_git(runtime, "clean", "-fd")',
    },
    {
        "id": "target-tracks-venv",
        "why": "a target tracking venv/** lets reset --hard overwrite the interpreter",
        "old": "    if tracked_under_venv:",
        "new": "    if False:",
    },
    {
        "id": "skew-refusal-real",
        "why": "advancing to a target the venv cannot satisfy breaks every gateway at start",
        "old": "    if skew:\n        raise UpdateError(_skew_refusal(target, skew))",
        "new": "    if False:\n        raise UpdateError(_skew_refusal(target, skew))",
    },
    {
        "id": "skew-direction-not-verb",
        "why": "replaying a rollback's own receipt is a FORWARD advance with the refusal skipped",
        "old": "moving_backward = rollback_from is not None and _is_ancestor(runtime, target, before_head)",
        "new": "moving_backward = rollback_from is not None",
    },
    {
        "id": "probe-cwd-pinned",
        "why": "python -c puts cwd on sys.path; a stray dist-info reports a missing dep as satisfied",
        "old": '        cwd="/",\n',
        "new": "",
    },
    {
        "id": "probe-env-sanitised",
        "why": "a leaked PYTHONPATH reports a dependency satisfied that the gateway will not find",
        "old": 'env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "PYTHONDONTWRITEBYTECODE": "1"},',
        "new": "env=dict(os.environ),",
    },
    {
        "id": "finding-sanitised",
        "why": "a raw requirement string forges journal lines and echoes credentials",
        "old": '+ "; ".join(_safe_finding(s) for s in skew[:10])',
        "new": '+ "; ".join(skew[:10])',
    },
    {
        "id": "unparseable-requirement",
        "why": "a dropped requirement returns [] which reads as a measured clean",
        "old": "\"            out.append(raw+': unparseable requirement — the runtime cannot evaluate it'); continue\\n\"\n"
               '        "        if req.marker is not None',
        "new": '        "        if req.marker is not None',
    },
    {
        "id": "packaging-absent",
        "why": "without packaging, version skew and markers go unevaluated and report CLEAN",
        "old": '        "if Requirement is None:\\n"',
        "new": '        "if False:\\n"',
    },
    {
        "id": "empty-probe-stdout",
        "why": "empty stdout with rc 0 read as a measured clean",
        "old": '    if not proc.stdout.strip():',
        "new": '    if False:',
    },
    {
        "id": "project-not-a-table",
        "why": "unhandled AttributeError escapes the UpdateError contract (exit 1, not 3)",
        "old": "    if not isinstance(project, dict):",
        "new": "    if False:",
    },
    {
        "id": "non-string-deps",
        "why": "silently dropping entries under-counts the dependency set being certified",
        "old": "    if bad:",
        "new": "    if False:",
    },
    {
        "id": "deps-not-a-list",
        "why": "a non-list dependencies value would be iterated as characters",
        "old": "    if not isinstance(deps, list):",
        "new": "    if False:",
    },
    {
        "id": "init-worktree-unchanged",
        "why": "init must not change a single non-.git path in the live runtime",
        "old": "    if before != after:",
        "new": "    if False:",
    },
    {
        "id": "audit-tree-stable",
        "why": "a tree changing mid-audit makes the whole measurement meaningless",
        "old": "    if tree_before != tree_after:",
        "new": "    if False:",
    },
    {
        "id": "venv-unchanged-by-apply",
        "why": "the venv is the interpreter; an advance must not touch it",
        "old": '        if venv_after != venv_before:',
        "new": "        if False:",
    },
    {
        "id": "signal-handlers",
        "why": "a stop signal must unwind the transaction, not kill the process mid-clean",
        "old": "        signal.signal(sig, _raise)",
        "new": "        pass",
    },
]


def _apply_mutation(tree: Path, mutation: dict[str, str]) -> None:
    subject = tree / SUBJECT_REL
    text = subject.read_text()
    count = text.count(mutation["old"])
    if count != 1:
        raise SystemExit(
            f"STALE TABLE: mutation {mutation['id']!r} anchor matches {count} times "
            f"(expected exactly 1). The guard moved or was renamed — fix the table "
            f"rather than letting a mutation silently no-op."
        )
    subject.write_text(text.replace(mutation["old"], mutation["new"], 1))


def _run_suite(tree: Path, stop_early: bool) -> int:
    env = dict(os.environ)
    # Match the wrapper's CI-parity variables. Bare pytest is used deliberately here:
    # this battery measures RED-vs-GREEN on a throwaway copy, not absolute pass counts,
    # and the wrapper's per-file subprocess isolation would multiply the cost by ~15.
    env.update(TZ="UTC", LANG="C.UTF-8", PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    cmd = [str(REPO / ".venv" / "bin" / "python"), "-m", "pytest", SUITE, "-q",
           "-p", "no:cacheprovider"]
    if stop_early:
        cmd.append("-x")
    # encoding= explicit: `text=True` alone decodes with the locale encoding, which the
    # repo's footgun lint rejects (and which this file tripped on its first run).
    return subprocess.run(
        cmd, cwd=tree, env=env, capture_output=True, text=True, encoding="utf-8"
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print guard names and exit")
    parser.add_argument("-k", dest="filter", default="", help="substring filter on guard id")
    args = parser.parse_args(argv)

    selected = [m for m in MUTATIONS if args.filter in m["id"]]
    if args.list:
        for m in selected:
            print(f"{m['id']:28} {m['why']}")
        return 0
    if not selected:
        print(f"no guard matches {args.filter!r}", file=sys.stderr)
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="mutguard-", dir=SCRATCH_ROOT))
    survived: list[dict[str, str]] = []
    try:
        print(f"baseline: verifying the unmutated copy is green ({len(selected)} guards queued)")
        base = scratch / "base"
        _copy_tree(base)
        if _run_suite(base, stop_early=False) != 0:
            print("BASELINE IS RED — the battery cannot distinguish a killed mutant "
                  "from a pre-existing failure. Fix the suite first.", file=sys.stderr)
            return 2
        print("baseline green\n")

        for mutation in selected:
            tree = scratch / mutation["id"]
            _copy_tree(tree)
            _apply_mutation(tree, mutation)
            rc = _run_suite(tree, stop_early=True)
            if rc == 0:
                survived.append(mutation)
                print(f"  SURVIVED  {mutation['id']:28} {mutation['why']}")
            else:
                print(f"  killed    {mutation['id']}")
            shutil.rmtree(tree, ignore_errors=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    if survived:
        print(f"{len(survived)} of {len(selected)} guard(s) SURVIVED — no test detects their removal:")
        for m in survived:
            print(f"  {m['id']:28} {m['why']}")
        print("\nA surviving guard may still be correct. What it is not is PROTECTED: "
              "nothing would tell you if it stopped being correct.")
        return 1
    print(f"all {len(selected)} guard(s) killed their mutant — every declared guard is load-bearing")
    return 0


# Copy everything EXCEPT these. Enumerating what to INCLUDE was tried first and the
# baseline came back red: the suite also reads systemd/ units, docs/ runbooks and
# repo-root config. A denylist fails safe here — a missed include silently reddens the
# baseline and disables the whole battery, while a missed exclude only costs disk.
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
         ".ruff_cache", ".mypy_cache", "build", "dist", ".worktrees"}


def _copy_tree(dest: Path) -> None:
    shutil.copytree(
        REPO, dest,
        ignore=lambda d, names: [n for n in names if n in _SKIP or n.endswith(".pyc")],
        symlinks=True, dirs_exist_ok=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
