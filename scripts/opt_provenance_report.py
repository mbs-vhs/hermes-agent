#!/usr/bin/env python3
"""Report how a deployed Hermes tree differs from a git ref. READ-ONLY.

WHY THIS EXISTS (CLAWD-2833 / CLAWD-2832)

``/opt/hermes-agent`` serves the whole gateway fleet and is **not a git tree** —
no ``.git``, root-owned, created during the Kudzu Phase-1 rehoming by a ``cp``
without ``--delete``. So ``git log`` and ``git status`` cannot answer the two
questions that matter before any fleet deploy: *what is running?* and *has it
drifted?* Meanwhile ``devops-process/config/repo-ops.tsv`` declares this repo
tier ``runtime`` with ``runtime_path=/opt/hermes-agent``, and that tier means
"deploy from a ref, never leave scratch in the runtime". A tier that demands
deploy-from-a-ref against a target that has no ref is not a documentation nit:
it means there is no rollback path for the fleet.

This tool does not fix that (the deploy substrate is a ratification decision —
see ``devops-process/proposals/2026-07-27-opt-hermes-deploy-substrate.md``). It
makes the gap **measurable**, which is the precondition for fixing it.

THE ORPHANS ARE THE POINT

The interesting output is not "these files differ" but
``only_in_tree_importable``: files present in the deployed tree, absent from the
ref, that Python **still resolves as importable modules**. Concretely,
``gateway/platforms/{telegram,matrix,feishu,dingtalk,email,...}.py`` were
deleted from the repo when those adapters moved to bundled plugins, but the
copy-without-``--delete`` left them in ``/opt`` — where ``find_spec`` finds
them. A naive ref-based deploy with ``--delete`` semantics would remove them
from every live gateway at once. Anything listed there must have a documented
disposition **before** a deploy path is built, not after.

READ-ONLY IS A CONTRACT, NOT AN ACCIDENT

Nothing here writes to ``--tree``. Note that ``importlib.util.find_spec`` is
*not* inherently read-only: resolving ``gateway.platforms.telegram`` imports the
parent packages, executing their ``__init__.py``, which would drop
``__pycache__`` into the tree if it were writable. Today ``/opt`` happens to be
``root:root 755`` so it cannot — but "read-only by filesystem accident" is not a
property to rely on. Resolution therefore runs in a **subprocess** with
``PYTHONDONTWRITEBYTECODE=1`` and ``sys.dont_write_bytecode``, and out-of-process
so a resolved module's import side effects cannot touch this process either.

USAGE

    python scripts/opt_provenance_report.py --tree /opt/hermes-agent --ref HEAD
    python scripts/opt_provenance_report.py --tree /opt/hermes-agent --json
    python scripts/opt_provenance_report.py --tree /opt/hermes-agent --strict

Exit status is ``0`` by default even when drift is found — a tool in ``scripts/``
that is permanently non-zero is a footgun the moment someone wires it to a gate.
Pass ``--strict`` to exit ``2`` while any resolvable orphan remains; that is the
form to use in a CI gate or a deploy preflight. ``--strict`` exits ``3`` instead if
any path could not be READ, because a partially-measured tree must never report
clean — UNMEASURED outranks drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple  # noqa: F401

# Directories that are build/runtime artifacts rather than deployed source.
# ``venv`` matters most: /opt/hermes-agent/venv is an editable install whose
# contents are a function of pip, not of any git ref, so diffing it against a
# ref produces thousands of meaningless "only in tree" rows.
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "test_durations.json",
)

STRICT_EXIT_CODE = 2
# Reused from the probe-failure path (commit 470bdf7d0): 3 means "the tree was not
# fully MEASURED", which is categorically different from "measured and drifted".
# --strict must never return clean when any part of the tree could not be read.
UNMEASURED_EXIT_CODE = 3


def _sha256_file(path: Path) -> Optional[str]:
    """Hash a file, or return None if it cannot be read (permissions, races).

    A None here is NOT benign — see walk_tree: the caller must RECORD it, never
    silently skip the file. An unhashable file is an UNMEASURED file, and a tree
    with unmeasured files cannot be reported as clean.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _is_excluded(rel: Path, excludes: Sequence[str]) -> bool:
    return any(part in excludes for part in rel.parts)


def walk_tree(
    tree: Path, excludes: Sequence[str]
) -> "Tuple[Dict[str, str], List[str]]":
    """Map repo-relative path -> sha256 for every file in a deployed tree.

    Returns ``(hashes, unreadable)``. **Both halves matter.**

    THIS FUNCTION USED TO SILENTLY UNDER-REPORT DRIFT, and it is the same defect
    this whole tool exists to catch — one function over from where it was already
    fixed. Two independent swallows:

      * ``os.walk`` defaults to ``onerror=None``, which DISCARDS permission errors,
        so an unreadable subdirectory simply does not appear in the walk;
      * ``_sha256_file`` returns ``None`` on ``OSError`` and the old loop skipped
        those files with no record.

    Measured on a fixture: ``chmod 000`` on one subdirectory took RESOLVABLE
    ORPHANS from 6 to 4 and ``tree_files`` from 6 to 4, with **no warning and no
    change in exit code**. On the real target that means a root-owned or
    ACL-restricted path under ``/opt/hermes-agent`` makes the fleet look LESS
    drifted than it is — the failure mode most likely to precede a bad deploy.

    Commit 470bdf7d0 established the invariant for the module-resolution probe:
    *a failure to measure must never read as "no drift"*. This applies it here.
    """
    out: Dict[str, str] = {}
    unreadable: List[str] = []

    def _on_walk_error(err: OSError) -> None:
        # os.walk swallows these by default. Record instead.
        try:
            rel = Path(getattr(err, "filename", "") or "").relative_to(tree).as_posix()
        except (ValueError, TypeError):
            rel = str(getattr(err, "filename", "") or "<unknown>")
        unreadable.append(f"{rel} ({type(err).__name__}: {err.strerror or err})")

    for dirpath, dirnames, filenames in os.walk(tree, onerror=_on_walk_error):
        # Prune in place so os.walk never descends into an excluded dir.
        dirnames[:] = [d for d in dirnames if d not in excludes]
        base = Path(dirpath)
        for name in filenames:
            if name in excludes:
                continue
            rel = (base / name).relative_to(tree)
            if _is_excluded(rel, excludes):
                continue
            digest = _sha256_file(base / name)
            if digest is None:
                unreadable.append(f"{rel.as_posix()} (unreadable file)")
                continue
            out[rel.as_posix()] = digest
    return out, unreadable


def read_ref(repo: Path, ref: str, excludes: Sequence[str]) -> Dict[str, str]:
    """Map repo-relative path -> blob sha for every file at a git ref.

    Uses the ref's own blob ids rather than re-hashing: a git blob sha is
    ``sha256(...)`` of neither the content nor the file, so the two sides are
    compared by *content hash* below via ``git cat-file``, not by mixing hash
    families.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--format=%(objectname) %(path)", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"error: git ls-tree {ref} failed: {proc.stderr.strip()}")
    out: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        objectname, _, path = line.partition(" ")
        rel = Path(path)
        if _is_excluded(rel, excludes):
            continue
        out[rel.as_posix()] = objectname
    return out


def ref_content_sha256(repo: Path, ref: str, paths: Sequence[str]) -> Dict[str, str]:
    """sha256 the *content* of specific paths at a ref, for apples-to-apples
    comparison against on-disk hashes."""
    out: Dict[str, str] = {}
    for path in paths:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{path}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        out[path] = hashlib.sha256(proc.stdout).hexdigest()
    return out


# Resolved out-of-process so neither bytecode writes nor import side effects of
# the *deployed* tree can touch this process or that tree. See module docstring.
_FIND_SPEC_PROBE = r"""
import json, sys
sys.dont_write_bytecode = True
import importlib.util
out = {}
for mod in json.loads(sys.argv[1]):
    try:
        spec = importlib.util.find_spec(mod)
        out[mod] = spec.origin if spec is not None else None
    except BaseException as exc:  # a broken __init__ must not kill the probe
        out[mod] = f"!error: {type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


def _module_name(rel: str) -> Optional[str]:
    """Repo-relative .py path -> dotted module name, or None if not a module."""
    if not rel.endswith(".py"):
        return None
    parts = Path(rel).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def origin_is_inside(origin: str, tree: Path) -> bool:
    """True iff ``origin`` resolves to a path inside ``tree``.

    Factored out so it is directly testable. The ``os.sep`` suffix is load-bearing:
    without it a sibling like ``/opt/hermes-agent-evil/x.py`` would pass a bare
    prefix check. ``resolve()`` also normalizes ``..`` and follows symlinks, so a
    link pointing out of the tree resolves out and is (conservatively) dropped.
    """
    if not origin or origin.startswith("!error:"):
        return False
    try:
        resolved = str(Path(origin).resolve())
    except OSError:
        return False
    return resolved.startswith(str(tree.resolve()) + os.sep)


class ProbeFailure(RuntimeError):
    """The module-resolution subprocess could not be run or did not answer.

    This must never be reported as "no orphans found". The whole point of this
    tool is to make a runtime's drift *visible*; a probe that silently returns
    an empty set turns a broken interpreter into a clean bill of health — and the
    default interpreter is the root-owned fleet venv, exactly the thing most
    likely to be relocated or broken. ``deploy-to-runtime.sh`` points operators at
    ``--strict``, so a false clean here would read as "safe to deploy".
    """


def resolve_importable(
    tree: Path, only_in_tree: Sequence[str], python: Optional[str] = None
) -> Dict[str, str]:
    """Of the tree-only files, report those Python still resolves *inside the
    tree*. A resolution pointing anywhere else (site-packages, stdlib) is not an
    orphan of this tree and is dropped.

    Raises ``ProbeFailure`` if the probe could not run — never returns ``{}`` to
    mean "the probe broke".
    """
    candidates = {}
    for rel in only_in_tree:
        mod = _module_name(rel)
        if mod is not None:
            candidates[mod] = rel
    if not candidates:
        return {}

    interpreter = python or (str(tree / "venv" / "bin" / "python"))
    if not Path(interpreter).exists():
        interpreter = sys.executable

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # A REAL throwaway HERMES_HOME. Popping the var is not enough: with it unset,
    # hermes_constants falls back to Path.home()/".hermes" — the operator's LIVE
    # profile tree (the chat.vhs.box population). Importing gateway packages must
    # never be able to read or write that. Point it at a temp dir instead, and
    # never hardcode ~/.hermes here (fork profile-safety rule).
    probe_home = tempfile.mkdtemp(prefix="opt-provenance-hermes-home-")
    env["HERMES_HOME"] = probe_home
    try:
        proc = subprocess.run(
            [interpreter, "-c", _FIND_SPEC_PROBE, json.dumps(sorted(candidates))],
            cwd=str(tree),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise ProbeFailure(f"could not run the probe interpreter {interpreter}: {exc}") from exc
    finally:
        shutil.rmtree(probe_home, ignore_errors=True)

    if proc.returncode != 0:
        raise ProbeFailure(
            f"probe interpreter {interpreter} exited {proc.returncode}; "
            f"stderr: {proc.stderr.strip()[:2000] or '(empty)'}"
        )
    if not proc.stdout.strip():
        raise ProbeFailure(
            f"probe interpreter {interpreter} produced no output; "
            f"stderr: {proc.stderr.strip()[:2000] or '(empty)'}"
        )
    try:
        resolved = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise ProbeFailure(
            f"probe output was not JSON: {exc}; stdout tail: {proc.stdout.strip()[-500:]!r}"
        ) from exc

    return {
        candidates[mod]: str(Path(origin).resolve())
        for mod, origin in resolved.items()
        if origin_is_inside(origin, tree)
    }


def build_report(
    tree: Path,
    repo: Path,
    ref: str,
    excludes: Sequence[str],
    python: Optional[str] = None,
) -> Dict[str, object]:
    tree_files, unreadable = walk_tree(tree, excludes)
    ref_files = read_ref(repo, ref, excludes)

    only_in_tree = sorted(set(tree_files) - set(ref_files))
    only_in_ref = sorted(set(ref_files) - set(tree_files))
    shared = sorted(set(tree_files) & set(ref_files))

    ref_hashes = ref_content_sha256(repo, ref, shared)
    differing = sorted(
        path for path in shared
        if path in ref_hashes and ref_hashes[path] != tree_files[path]
    )

    importable = resolve_importable(tree, only_in_tree, python=python)

    return {
        "tree": str(tree),
        "repo": str(repo),
        "ref": ref,
        "ref_commit": subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref],
            capture_output=True, text=True, check=False,
        ).stdout.strip() or None,
        "counts": {
            "tree_files": len(tree_files),
            "ref_files": len(ref_files),
            "only_in_tree": len(only_in_tree),
            "only_in_ref": len(only_in_ref),
            "differing": len(differing),
            "only_in_tree_importable": len(importable),
            # Non-zero means the tree was only PARTIALLY measured. Any drift number
            # above is a FLOOR, not a count.
            "unreadable": len(unreadable),
        },
        "only_in_tree": only_in_tree,
        "only_in_ref": only_in_ref,
        "differing": differing,
        "only_in_tree_importable": importable,
        "unreadable": unreadable,
    }


def render_text(report: Dict[str, object]) -> str:
    counts = report["counts"]  # type: ignore[index]
    importable = report["only_in_tree_importable"]  # type: ignore[index]
    lines: List[str] = [
        f"tree: {report['tree']}",
        f"ref:  {report['ref']} ({report['ref_commit']})",
        "",
        f"  files in tree           {counts['tree_files']}",
        f"  files at ref            {counts['ref_files']}",
        f"  only in tree            {counts['only_in_tree']}",
        f"  only at ref             {counts['only_in_ref']}",
        f"  differing content       {counts['differing']}",
        f"  RESOLVABLE ORPHANS      {counts['only_in_tree_importable']}",
        f"  UNREADABLE (unmeasured) {counts['unreadable']}",
    ]
    if counts["unreadable"]:
        lines += [
            "",
            "!! PARTIAL MEASUREMENT — every count above is a FLOOR, not a total.",
            "!! Unreadable paths are skipped by the walk, so the tree can look LESS",
            "!! drifted than it is. Re-run with permission to read these paths",
            "!! (on /opt/hermes-agent that means sudo):",
        ]
        for entry in report["unreadable"][:20]:  # type: ignore[index]
            lines.append(f"     {entry}")
        if len(report["unreadable"]) > 20:  # type: ignore[arg-type]
            lines.append(f"     ... and {len(report['unreadable']) - 20} more")
    if importable:
        lines += [
            "",
            "RESOLVABLE ORPHANS — present in the tree, absent from the ref, and",
            "still importable. A ref-based deploy with --delete semantics removes",
            "these from every live gateway. Each needs a documented disposition",
            "BEFORE a deploy path is built:",
        ]
        for rel, origin in sorted(importable.items()):  # type: ignore[union-attr]
            lines.append(f"  {rel}  ->  {origin}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report how a deployed Hermes tree differs from a git ref (read-only).",
    )
    parser.add_argument("--tree", required=True, help="deployed tree to inspect, e.g. /opt/hermes-agent")
    parser.add_argument("--ref", default="HEAD", help="git ref to compare against (default: HEAD)")
    parser.add_argument("--repo", default=None, help="git repo providing --ref (default: this checkout)")
    parser.add_argument("--python", default=None, help="interpreter for module resolution (default: <tree>/venv/bin/python)")
    parser.add_argument("--exclude", action="append", default=[], help="extra path component to exclude (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=f"exit {STRICT_EXIT_CODE} while any resolvable orphan remains (for gates)",
    )
    args = parser.parse_args(argv)

    tree = Path(args.tree)
    if not tree.is_dir():
        print(f"error: --tree {tree} is not a directory", file=sys.stderr)
        return 1

    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    excludes = list(DEFAULT_EXCLUDES) + list(args.exclude)

    try:
        report = build_report(tree, repo, args.ref, excludes, python=args.python)
    except ProbeFailure as exc:
        # Loud failure, never a clean report. A broken probe means the orphan set
        # is UNMEASURED, which is not the same as empty.
        print(f"error: module-resolution probe failed: {exc}", file=sys.stderr)
        print("error: orphan set is UNMEASURED — do not treat this as 'no drift'", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))

    if args.strict:
        # Order matters: UNMEASURED outranks drift. A partially-read tree must not
        # exit 0 just because the readable part happened to look clean — that is
        # exactly the silent-clean failure this tool exists to prevent, and it is
        # the same invariant commit 470bdf7d0 applied to the resolution probe.
        if report["counts"]["unreadable"]:  # type: ignore[index]
            print(
                f"error: {report['counts']['unreadable']} path(s) could not be read — "
                f"the tree is only PARTIALLY measured",
                file=sys.stderr,
            )
            print(
                "error: drift counts are a FLOOR, not a total — do not treat this as "
                "'no drift'",
                file=sys.stderr,
            )
            return UNMEASURED_EXIT_CODE
        if report["counts"]["only_in_tree_importable"]:  # type: ignore[index]
            return STRICT_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
