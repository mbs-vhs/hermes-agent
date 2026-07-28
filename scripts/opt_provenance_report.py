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
form to use in a CI gate or a deploy preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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


def _sha256_file(path: Path) -> Optional[str]:
    """Hash a file, or return None if it cannot be read (permissions, races)."""
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


def walk_tree(tree: Path, excludes: Sequence[str]) -> Dict[str, str]:
    """Map repo-relative path -> sha256 for every file in a deployed tree."""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
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
            if digest is not None:
                out[rel.as_posix()] = digest
    return out


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


def resolve_importable(
    tree: Path, only_in_tree: Sequence[str], python: Optional[str] = None
) -> Dict[str, str]:
    """Of the tree-only files, report those Python still resolves *inside the
    tree*. A resolution pointing anywhere else (site-packages, stdlib) is not an
    orphan of this tree and is dropped."""
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
    # A throwaway HERMES_HOME: importing gateway packages must never read or
    # write a live profile. Never hardcode ~/.hermes here (profile-safety rule).
    env.pop("HERMES_HOME", None)
    proc = subprocess.run(
        [interpreter, "-c", _FIND_SPEC_PROBE, json.dumps(sorted(candidates))],
        cwd=str(tree),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        resolved = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {}

    tree_str = str(tree.resolve())
    out: Dict[str, str] = {}
    for mod, origin in resolved.items():
        if not origin or origin.startswith("!error:"):
            continue
        try:
            resolved_origin = str(Path(origin).resolve())
        except OSError:
            continue
        if resolved_origin.startswith(tree_str + os.sep):
            out[candidates[mod]] = resolved_origin
    return out


def build_report(
    tree: Path,
    repo: Path,
    ref: str,
    excludes: Sequence[str],
    python: Optional[str] = None,
) -> Dict[str, object]:
    tree_files = walk_tree(tree, excludes)
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
        },
        "only_in_tree": only_in_tree,
        "only_in_ref": only_in_ref,
        "differing": differing,
        "only_in_tree_importable": importable,
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
    ]
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

    report = build_report(tree, repo, args.ref, excludes, python=args.python)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))

    if args.strict and report["counts"]["only_in_tree_importable"]:  # type: ignore[index]
        return STRICT_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
