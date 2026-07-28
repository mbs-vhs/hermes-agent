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


def test_does_not_write_to_inspected_tree(tree: Path, repo: Path, monkeypatch):
    """Read-only is a contract, not a filesystem accident. find_spec imports
    parent packages; without bytecode suppression that drops __pycache__ into the
    tree.

    THE delenv IS LOAD-BEARING — without it this test passes vacuously.
    `scripts/run_tests.sh` exports PYTHONDONTWRITEBYTECODE=1 itself, and
    `resolve_importable` builds the probe env with `dict(os.environ)`, so the
    harness silently supplies a THIRD guard from outside the code under test.
    Measured: with the env var inherited, deleting BOTH in-code guards
    (env["PYTHONDONTWRITEBYTECODE"] and the probe's sys.dont_write_bytecode) still
    passed 12/12. An operator running the tool from a normal shell has no such
    harness, so the in-code guards are exactly what protects them — and this test
    could not see their loss. Clearing the inherited var makes the guards
    observable.
    """
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    before = {p.relative_to(tree) for p in tree.rglob("*")}
    _report(tree, repo)
    after = {p.relative_to(tree) for p in tree.rglob("*")}
    assert after == before, f"tool wrote into the inspected tree: {after - before}"


def test_origin_containment_filter_rejects_outside_and_sibling_prefix(tmp_path: Path):
    """Directly guard the tree-containment predicate. A review mutation that
    replaced this filter with `if True:` survived every other test in this file —
    without a direct test, the tool would happily report stdlib and
    site-packages modules as orphans of the inspected tree."""
    tree = tmp_path / "opt" / "hermes-agent"
    (tree / "gateway").mkdir(parents=True)
    inside = tree / "gateway" / "x.py"
    inside.write_text("", encoding="utf-8")

    assert prov.origin_is_inside(str(inside), tree) is True

    # A SIBLING sharing the tree's path prefix must be rejected — this is what
    # the explicit os.sep in the predicate buys.
    sibling_root = tmp_path / "opt" / "hermes-agent-evil"
    sibling_root.mkdir(parents=True)
    sibling = sibling_root / "x.py"
    sibling.write_text("", encoding="utf-8")
    assert prov.origin_is_inside(str(sibling), tree) is False

    # Wholly outside, plus the sentinel/empty cases the probe can emit.
    outside = tmp_path / "elsewhere.py"
    outside.write_text("", encoding="utf-8")
    assert prov.origin_is_inside(str(outside), tree) is False
    assert prov.origin_is_inside("", tree) is False
    assert prov.origin_is_inside(None, tree) is False
    assert prov.origin_is_inside("!error: ImportError: boom", tree) is False

    # `..` must be NORMALIZED, not taken literally. tree/gateway/../x.py
    # normalizes to tree/x.py, which IS inside; tree/../elsewhere.py escapes.
    (tree / "x.py").write_text("", encoding="utf-8")
    assert prov.origin_is_inside(str(tree / "gateway" / ".." / "x.py"), tree) is True
    assert prov.origin_is_inside(str(tree / ".." / "elsewhere.py"), tree) is False

    # The tree root itself is not "inside" the tree — only paths beneath it.
    assert prov.origin_is_inside(str(tree), tree) is False


def test_probe_failure_is_loud_not_a_clean_report(tmp_path: Path, tree: Path, repo: Path):
    """THE headline regression for this tool. If the resolution subprocess cannot
    run, the orphan set is UNMEASURED — which must never be reported as "no
    orphans". The default interpreter is the root-owned fleet venv, the thing
    most likely to be broken or relocated, and deploy-to-runtime.sh points
    operators straight at --strict. A false clean there reads as "safe to deploy"."""
    broken = tmp_path / "broken-python"
    broken.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)

    with pytest.raises(prov.ProbeFailure) as excinfo:
        prov.build_report(tree, repo, "HEAD", prov.DEFAULT_EXCLUDES, python=str(broken))
    assert "boom" in str(excinfo.value), "probe stderr must be surfaced, not swallowed"

    # And the CLI must exit non-zero rather than print a clean report.
    rc = prov.main(["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
                    "--python", str(broken), "--strict"])
    assert rc == 3
    rc_nonstrict = prov.main(["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
                              "--python", str(broken)])
    assert rc_nonstrict == 3, "even without --strict, an unmeasured probe is not success"


def test_probe_does_not_use_the_live_hermes_home(tree: Path, repo: Path, monkeypatch):
    """Popping HERMES_HOME is NOT enough: unset, hermes_constants falls back to
    Path.home()/'.hermes' — the operator's LIVE profile tree. The probe must be
    handed a throwaway dir instead."""
    captured = {}
    real_run = prov.subprocess.run

    def _spy(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(prov.subprocess, "run", _spy)
    prov.build_report(tree, repo, "HEAD", prov.DEFAULT_EXCLUDES, python=sys.executable)

    assert "HERMES_HOME" in captured, "probe env must SET HERMES_HOME, not just unset it"
    home = Path(captured["HERMES_HOME"])
    assert home != Path.home() / ".hermes"
    assert str(home) not in str(Path.home() / ".hermes")


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


# ── unreadable paths must never read as "no drift" ──────────────────────────
# Same invariant as test_probe_failure_is_loud_not_a_clean_report, one function
# over. Before this, os.walk's default onerror=None discarded permission errors and
# _sha256_file's None returns were skipped without a trace: chmod 000 on ONE
# subdirectory took RESOLVABLE ORPHANS from 6 to 4 and tree_files from 6 to 4, with
# no warning and no change in exit code. On /opt/hermes-agent that makes the fleet
# look LESS drifted than it is — the failure most likely to precede a bad deploy.

def _chmod_000(path: Path):
    path.chmod(0o000)
    return path


def test_unreadable_directory_is_counted_not_swallowed(tmp_path, repo):
    tree = tmp_path / "deployed2"
    (tree / "pkg" / "sub").mkdir(parents=True)
    (tree / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "pkg" / "orphan.py").write_text("A = 1\n", encoding="utf-8")
    (tree / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "pkg" / "sub" / "hidden.py").write_text("B = 1\n", encoding="utf-8")

    before = prov.build_report(tree, repo, "HEAD", prov.DEFAULT_EXCLUDES,
                               python=sys.executable)
    assert before["counts"]["unreadable"] == 0
    baseline_files = before["counts"]["tree_files"]

    blocked = _chmod_000(tree / "pkg" / "sub")
    try:
        after = prov.build_report(tree, repo, "HEAD", prov.DEFAULT_EXCLUDES,
                                  python=sys.executable)
        assert after["counts"]["unreadable"] >= 1, (
            "an unreadable subdirectory was SWALLOWED — os.walk's default "
            "onerror=None discards permission errors, so the tree silently "
            "measures smaller and looks less drifted than it is"
        )
        assert after["counts"]["tree_files"] < baseline_files, (
            "sanity: the unreadable dir should reduce the measured file count; if it "
            "does not, this fixture is not exercising the path"
        )
        assert after["unreadable"], "the unreadable paths must be NAMED, not just counted"
    finally:
        blocked.chmod(0o755)


def test_strict_returns_UNMEASURED_not_clean_when_a_path_is_unreadable(tmp_path, repo):
    """UNMEASURED outranks drift. A partially-read tree must not exit 0 just because
    the readable part looked clean, and must not exit 2 either — 2 means 'measured
    and drifted', which would understate the situation."""
    tree = tmp_path / "deployed3"
    (tree / "sub").mkdir(parents=True)
    (tree / "shared.txt").write_text("same\n", encoding="utf-8")
    (tree / "sub" / "x.txt").write_text("y\n", encoding="utf-8")

    blocked = _chmod_000(tree / "sub")
    try:
        rc = prov.main(["--tree", str(tree), "--repo", str(repo), "--ref", "HEAD",
                        "--python", sys.executable, "--strict"])
        assert rc == prov.UNMEASURED_EXIT_CODE, (
            f"--strict returned {rc}; expected {prov.UNMEASURED_EXIT_CODE} "
            f"(UNMEASURED). Returning 0 here is the silent-clean failure this tool "
            f"exists to prevent; returning 2 would misreport it as merely drifted."
        )
    finally:
        blocked.chmod(0o755)


def test_strict_still_distinguishes_clean_from_drifted_when_fully_readable(tmp_path, repo):
    """The new UNMEASURED path must not swallow the existing contract."""
    clean = tmp_path / "clean2"
    (clean / "pkg").mkdir(parents=True)
    (clean / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (clean / "pkg" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    argv = ["--tree", str(clean), "--repo", str(repo), "--ref", "HEAD",
            "--python", sys.executable, "--strict"]
    assert prov.main(argv) == 0, "a fully-readable, orphan-free tree must exit 0"


def test_script_is_executable_because_operators_are_told_to_invoke_it_directly(tmp_path):
    """scripts/deploy-to-runtime.sh instructs the operator to run this script by
    path. It carries a shebang, so if the committed mode lacks +x the invocation
    exits 126 — which, piped without capturing PIPESTATUS, reads as a PASS.

    That is the CLAWD-3078 shape (check-windows-footguns.py, mode 0644), reproduced
    in this file's own tooling. Guarded here so it cannot regress.
    """
    import stat
    import subprocess as sp

    script = Path(prov.__file__)
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"{script.name} is not executable (mode {oct(mode & 0o777)}) but has a "
        f"shebang and is documented as directly invokable — direct invocation would "
        f"exit 126, which reads as a pass through a pipe"
    )
    proc = sp.run([str(script), "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, (
        f"direct invocation exited {proc.returncode} (126 = not executable)"
    )
