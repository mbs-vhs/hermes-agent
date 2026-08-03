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
GUARDED_DIRS = (
    REPO_ROOT / "gateway",
    # CLAWD-2378 put the shared fleet OAuth store behind this package; a
    # mis-decoded credential file is a fleet-wide outage, not a cosmetic bug.
    REPO_ROOT / "hermes_cli",
)
UPDATE_RESPONSE_FILES = (
    REPO_ROOT / "plugins/platforms/discord/adapter.py",
    REPO_ROOT / "plugins/platforms/telegram/adapter.py",
    REPO_ROOT / "plugins/platforms/feishu/adapter.py",
    REPO_ROOT / "plugins/platforms/whatsapp/adapter.py",
    REPO_ROOT / "plugins/platforms/google_chat/adapter.py",
    REPO_ROOT / "plugins/platforms/google_chat/oauth.py",
)
METHODS = {"read_text", "write_text"}
SUPPRESSION = "# gateway-utf8: ok"


def _find_violations():
    violations = []
    py_files = [f for d in GUARDED_DIRS for f in d.rglob("*.py")]
    py_files += list(UPDATE_RESPONSE_FILES)
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
