"""Static guard: every ``read_text`` / ``write_text`` call in the guarded dirs
must pass an explicit ``encoding=`` keyword argument so non-UTF-8 locales don't
corrupt file IPC.  Mirrors the AST-based guard pattern in
``tests/tools/test_windows_compat.py``.

SCOPE IS THE WHOLE NON-TEST TREE. There is no dir allowlist and adding one back
would be a regression — read this before "simplifying" the scan.

This defect class surfaced FOUR times across the v2026.7.30 merge. Three were
inside ``gateway/``, which an allowlist caught. The fourth was
``hermes_cli/auth.py`` reading the fleet's SHARED Codex OAuth store with a bare
``read_text()`` — fork-local (CLAWD-2378), added after upstream's own
``hermes_cli`` encoding sweep, so invisible to upstream AND to the allowlist.
Independent review found it by hand; nothing mechanical could have.

Naming dirs is what made it unreachable. So the allowlist is gone: everything
outside EXCLUDED_DIRS is scanned, and adding a package no longer requires
remembering to add it here. Do NOT reintroduce a GUARDED_DIRS tuple — an earlier
revision of this file had one built from ``rglob``, and because ``rglob`` on a
missing directory yields nothing and raises nothing, renaming a package made the
whole suite report clean having scanned ZERO files.
"""

import ast
import pathlib
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Scope is the WHOLE non-test tree, not a dir allowlist. Re-review measured the
# allowlist version at 347 guarded files and 753 unguarded ones -- with ZERO
# violations in that unguarded remainder. So guarding everything cost nothing red
# and is the only version of this guard that does not need editing every time a
# violation appears somewhere new. Naming dirs is what let CLAWD-2378 sit in
# hermes_cli/auth.py unseen.
EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "hermes_agent.egg-info", "build", "dist", ".mypy_cache", ".pytest_cache",
    # Tests legitimately read fixtures without an encoding= and a mis-decoded
    # fixture fails the test rather than the fleet. ~1939 hits live here; folding
    # them in would bury the signal this guard exists to carry.
    "tests",
})
METHODS = {"read_text", "write_text"}
SUPPRESSION = "# gateway-utf8: ok"

# The floor is RELATIVE to git's own file list, not an integer.
#
# It was `MIN_EXPECTED_FILES = 900` against 1093 measured — 193 files of slack.
# Review measured what that actually caught: 12 of the 14 top-level packages
# could be added to EXCLUDED_DIRS undetected, INCLUDING ``gateway/`` (89 files),
# the package this guard is named for. Only hermes_cli and plugins were large
# enough to trip it. And it rots OPEN: the tree grows on every upstream merge,
# so the slack widens on its own.
#
# `git ls-files` minus EXCLUDED_DIRS was measured EXACTLY equal to the rglob
# scope (1093 == 1093, zero divergence), so this is self-maintaining and catches
# all 14. It also pins CI parity: a fresh checkout sees the same list, with no
# untracked local padding.
GIT_SCOPE_TOLERANCE = 8


def _scoped_py_files():
    """Every non-test .py under REPO_ROOT, excluding build/vendor noise."""
    out = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if EXCLUDED_DIRS.intersection(rel.parts):
            continue
        out.append(path)
    return out


def _find_violations():
    violations = []
    py_files = _scoped_py_files()
    for py_file in sorted(py_files):
        source = py_file.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in METHODS:
                continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            lineno = node.lineno
            if lineno <= len(source_lines) and SUPPRESSION in source_lines[lineno - 1]:
                continue
            rel = py_file.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{lineno}")
    return violations


def test_all_read_write_text_pass_encoding():
    violations = _find_violations()
    assert not violations, (
        "Bare read_text()/write_text() calls found (missing encoding= kwarg).\n"
        "Add encoding=\"utf-8\" or suppress with '# gateway-utf8: ok':\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_the_guard_actually_scanned_the_tree():
    """A guard that cannot see must FAIL, never report clean.

    THIS IS THE POINT OF THE FILE, and the commit that widened the scope got it
    wrong in exactly the way it was fixing elsewhere: `Path.rglob` on a
    directory that does not exist yields nothing and raises nothing, so a
    renamed package turned this suite green having examined ZERO files -- with a
    real bare read_text() sitting in the tree. Independent re-review reproduced
    it: same violation, same test, green when the guard could not see.

    That is the same "could-not-measure reads as measured-zero" structure the
    same commit had just removed from a `git grep` three hunks earlier. Do not
    reintroduce it by replacing this with a bare rglob.
    """
    import subprocess

    # PIN THE POLICY SET ITSELF. This is the only assertion here that can catch
    # EXCLUDED_DIRS growing, and getting it wrong is instructive: the first
    # attempt compared _scoped_py_files() against `git ls-files` ALSO filtered by
    # EXCLUDED_DIRS. Both sides moved together, so adding "gateway" to the set
    # left it 2/2 green -- a tautology, and the same cannot-fail class this file
    # exists to prevent. Caught by revert-validating it; it looked correct.
    #
    # Growing this set is now a deliberate, reviewable edit rather than a silent
    # loss of coverage.
    assert EXCLUDED_DIRS == frozenset({
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        "hermes_agent.egg-info", "build", "dist", ".mypy_cache", ".pytest_cache",
        "tests",
    }), (
        f"EXCLUDED_DIRS changed to {sorted(EXCLUDED_DIRS)}. Every name added "
        f"here silently removes files from the encoding guard. If the addition "
        f"is intended, update this assertion in the same commit and say why."
    )

    # And pin the SCAN against git's own list using only the STRUCTURAL
    # exclusions -- never the policy set, or it becomes the tautology above.
    STRUCTURAL = frozenset({".git", ".venv", "venv", "node_modules",
                            "__pycache__", "hermes_agent.egg-info",
                            "build", "dist", ".mypy_cache", ".pytest_cache"})
    found = len(_scoped_py_files())
    out = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, (
        f"git ls-files could not run (rc={out.returncode}), so this assertion "
        f"proves NOTHING: {out.stderr.strip()[:200]}"
    )
    tracked_non_test = [
        f for f in out.stdout.split()
        if not STRUCTURAL.intersection(pathlib.PurePosixPath(f).parts)
        and not pathlib.PurePosixPath(f).parts[0] == "tests"
    ]
    assert tracked_non_test, "git ls-files returned no candidate .py files at all"
    missing = len(tracked_non_test) - found
    assert missing <= GIT_SCOPE_TOLERANCE, (
        f"the encoding guard scanned {found} files but git tracks "
        f"{len(tracked_non_test)} non-test .py files -- {missing} are NOT being "
        f"examined, so a clean result here means nothing."
    )
