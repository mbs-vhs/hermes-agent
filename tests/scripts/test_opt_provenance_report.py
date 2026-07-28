"""Invariants for scripts/opt_provenance_report.py.

Context (CLAWD-2833): ``/opt/hermes-agent`` serves the whole gateway fleet and
has no ``.git``, so nothing could answer "what is running?" or "has it drifted?".
This tool answers both against a git ref. Two properties are load-bearing and
both are easy to regress silently:

  1. **Resolvable orphans are found.** A file present in the deployed tree,
     absent from the ref, that Python still resolves as an importable module
     inside that tree, is the dangerous case: a ref-based deploy with
     ``--delete`` semantics removes it from every live gateway at once. If the
     detector stops finding these, the report reads clean and the deploy looks
     safe.
  2. **The tool never writes to the tree it inspects.** Module resolution runs
     ``find_spec``, which imports parent packages and would drop ``__pycache__``
     into the tree if it were writable. Today ``/opt`` is ``root:root 755`` so it
     cannot — but a test that relies on that accident tests nothing.

These tests build throwaway trees + a throwaway git repo in ``tmp_path``. They
must never require ``/opt/hermes-agent`` to exist (CI runners do not have it).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.opt_provenance_report as prov


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit."""
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (r / "pkg" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    (r / "shared.txt").write_text("same\n", encoding="utf-8")
    (r / "only_at_ref.txt").write_text("ref\n", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A deployed tree: shares some files with the ref, drops one, adds an
    importable orphan plus a non-module orphan, and carries excluded noise."""
    t = tmp_path / "deployed"
    (t / "pkg").mkdir(parents=True)
    (t / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (t / "pkg" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    # An importable orphan: real module path, absent from the ref.
    (t / "pkg" / "orphan.py").write_text("ORPHANED = True\n", encoding="utf-8")
    # A non-module orphan: must be reported as only-in-tree but NOT importable.
    (t / "notes.md").write_text("# notes\n", encoding="utf-8")
    # Shared but with different content -> "differing".
    (t / "shared.txt").write_text("DIFFERENT\n", encoding="utf-8")
    # only_at_ref.txt deliberately absent -> "only at ref".
    # Excluded noise that must never appear in any bucket.
    (t / "venv" / "lib").mkdir(parents=True)
    (t / "venv" / "lib" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (t / "__pycache__").mkdir()
    (t / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
    return t


def _report(tree: Path, repo: Path):
    return prov.build_report(
        tree, repo, "HEAD", prov.DEFAULT_EXCLUDES, python=sys.executable
    )


def test_finds_resolvable_orphan(tree: Path, repo: Path):
    """The headline contract: an importable tree-only module is flagged, and the
    origin it reports points INSIDE the inspected tree."""
    report = _report(tree, repo)
    importable = report["only_in_tree_importable"]
    assert "pkg/orphan.py" in importable, (
        f"resolvable orphan not detected; only_in_tree={report['only_in_tree']}"
    )
    assert importable["pkg/orphan.py"].startswith(str(tree.resolve()))
    assert report["counts"]["only_in_tree_importable"] == 1


def test_non_module_orphan_is_listed_but_not_importable(tree: Path, repo: Path):
    report = _report(tree, repo)
    assert "notes.md" in report["only_in_tree"]
    assert "notes.md" not in report["only_in_tree_importable"]


def test_classifies_only_at_ref_and_differing(tree: Path, repo: Path):
    report = _report(tree, repo)
    assert report["only_in_ref"] == ["only_at_ref.txt"]
    assert report["differing"] == ["shared.txt"]
    # A byte-identical shared file must appear in no bucket at all.
    for bucket in ("only_in_tree", "only_in_ref", "differing"):
        assert "pkg/kept.py" not in report[bucket]


def test_excluded_paths_never_appear(tree: Path, repo: Path):
    report = _report(tree, repo)
    joined = json.dumps(report)
    assert "venv/lib/junk.py" not in joined
    assert "stale.pyc" not in joined


def test_does_not_write_to_inspected_tree(tree: Path, repo: Path):
    """Read-only is a contract, not a filesystem accident. find_spec imports
    parent packages; without PYTHONDONTWRITEBYTECODE that drops __pycache__
    into the tree."""
    before = {p.relative_to(tree) for p in tree.rglob("*")}
    _report(tree, repo)
    after = {p.relative_to(tree) for p in tree.rglob("*")}
    assert after == before, f"tool wrote into the inspected tree: {after - before}"


def test_strict_exit_code_only_with_flag(tree: Path, repo: Path, capsys):
    """Default exit is 0 even with drift — a permanently non-zero tool in
    scripts/ is a footgun once someone wires it to a gate. --strict opts in."""
    argv = ["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
            "--python", sys.executable]
    assert prov.main(argv) == 0
    assert prov.main(argv + ["--strict"]) == prov.STRICT_EXIT_CODE


def test_strict_exits_zero_when_no_orphans(tmp_path: Path, repo: Path):
    """--strict must gate on resolvable orphans specifically, not on any drift:
    a tree that differs but has no importable orphan is not a deploy hazard."""
    clean = tmp_path / "clean"
    (clean / "pkg").mkdir(parents=True)
    (clean / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (clean / "pkg" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    (clean / "shared.txt").write_text("DIFFERENT\n", encoding="utf-8")
    (clean / "notes.md").write_text("# not a module\n", encoding="utf-8")
    argv = ["--tree", str(clean), "--repo", str(repo), "--ref", "HEAD",
            "--python", sys.executable]
    report = _report(clean, repo)
    assert report["differing"] == ["shared.txt"]
    assert report["counts"]["only_in_tree_importable"] == 0
    assert prov.main(argv + ["--strict"]) == 0


def test_missing_tree_is_an_error_not_a_clean_report(tmp_path: Path, repo: Path):
    """A nonexistent --tree must fail loudly. Reporting "no drift" for a tree
    that isn't there is the silent-clean failure this whole card is about."""
    rc = prov.main(["--tree", str(tmp_path / "nope"), "--repo", str(repo)])
    assert rc == 1


def test_json_output_is_parseable(tree: Path, repo: Path, capsys):
    prov.main(["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
               "--python", sys.executable, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["only_in_tree_importable"] == 1
    assert payload["ref_commit"]


def test_text_output_names_the_orphans(tree: Path, repo: Path, capsys):
    prov.main(["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
               "--python", sys.executable])
    out = capsys.readouterr().out
    assert "RESOLVABLE ORPHANS" in out
    assert "pkg/orphan.py" in out


# ── deploy-to-runtime.sh --help range guard ─────────────────────────────────
# Not strictly about the provenance report, but it guards the SAME card's other
# edit and there is no other test of that script. The `-h|--help` branch uses a
# hardcoded `sed -n '2,NNp'` range over its own header; growing the header
# without bumping NN silently truncates --help. That has already happened once —
# it cut off every flag, including the --no-restart the preflight die() tells
# operators to use. A one-shot manual check does not hold a known-recurring
# regression, so assert it here.

DEPLOY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-to-runtime.sh"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script; POSIX only")
def test_deploy_to_runtime_help_is_not_truncated():
    proc = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"--help exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout
    # Every flag the script accepts must survive the sed range...
    for flag in ("--dry-run", "--no-restart", "--parallel-restart", "--yes"):
        assert flag in out, f"{flag} missing from --help (stale sed range?)"
    # ...and so must the final section, which is what a stale range eats first.
    for tail_token in ("Env overrides", "HERMES_RUNTIME_CHECKOUT", "HERMES_DEPLOY_BRANCH"):
        assert tail_token in out, f"{tail_token} missing — --help truncated early"


@pytest.mark.skipif(sys.platform == "win32", reason="bash script; POSIX only")
def test_deploy_to_runtime_help_range_matches_header():
    """Pin the invariant directly: the sed range must end at the last comment
    line before `set -euo pipefail`. This fails on a header edit even if every
    token above happens to still fit inside a stale range."""
    lines = DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()
    set_line = next(i for i, ln in enumerate(lines, 1)
                    if ln.startswith("set -euo pipefail"))
    header_end = set_line - 1
    sed_ranges = [ln for ln in lines if "-h|--help)" in ln]
    assert len(sed_ranges) == 1, "expected exactly one -h|--help branch"
    match = re.search(r"sed -n '2,(\d+)p'", sed_ranges[0])
    assert match, f"could not parse the sed range from: {sed_ranges[0]}"
    assert int(match.group(1)) == header_end, (
        f"--help sed range ends at {match.group(1)} but the header ends at "
        f"{header_end}; bump the range (see the NOTE above that line)"
    )
