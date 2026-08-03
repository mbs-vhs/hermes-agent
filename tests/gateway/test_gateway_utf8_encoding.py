"""Static guard: every ``read_text`` / ``write_text`` call in the guarded dirs
must pass an explicit ``encoding=`` keyword argument so non-UTF-8 locales don't
corrupt file IPC.  Mirrors the AST-based guard pattern in
``tests/tools/test_windows_compat.py``.

SCOPE IS THE WHOLE POINT — widen it rather than patch instances (CLAWD-3388).
This defect class surfaced FOUR times across the v2026.7.30 merge. Three were
inside ``gateway/``, so this guard caught them. The fourth was
``hermes_cli/auth.py`` reading the fleet's SHARED Codex OAuth store with a bare
``read_text()`` — a fork-local file (CLAWD-2378) added after upstream's own
``hermes_cli`` encoding sweep, and therefore invisible to both. Independent
review found it by hand; nothing mechanical could have.

Patching that one line would have left the next one equally unreachable, so
``hermes_cli/`` is now guarded too. If you find a violation outside these dirs,
ADD THE DIR — do not fix only the instance.
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

# Floor, not a guess: 1,100 .py files were measured in scope. A run that finds
# far fewer has lost its subject, and "found no violations" would then mean
# "looked at almost nothing". See test_the_guard_actually_scanned_the_tree.
MIN_EXPECTED_FILES = 900


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
    found = len(_scoped_py_files())
    assert found >= MIN_EXPECTED_FILES, (
        f"the encoding guard scanned only {found} files (floor "
        f"{MIN_EXPECTED_FILES}) -- it has lost its subject, so a clean result "
        f"here means nothing. Did a package move, or did EXCLUDED_DIRS grow?"
    )
