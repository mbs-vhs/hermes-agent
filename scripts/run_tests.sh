#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, then venv — REPO-LOCAL ONLY)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Activate venv ───────────────────────────────────────────────────────────
# REPO-LOCAL ONLY.  There used to be a third candidate, $HOME/.hermes/hermes-agent/venv.
# It could never be the right answer (CLAWD-3136):
#
#   * For the checkout it belongs to, it is unreachable — that checkout's REPO_ROOT
#     IS ~/.hermes/hermes-agent, so "$REPO_ROOT/venv" already matches it above.
#     The third candidate therefore only ever fired from a DIFFERENT checkout.
#   * That different checkout is this dev fork (or one of its worktrees), and
#     ~/.hermes/hermes-agent is a LIVE RUNTIME on its own release cadence — it was
#     hermes-agent 0.14.0 while this repo was 0.18.0, so it did not have this repo's
#     declared dependencies installed (e.g. Markdown==3.10.2).  Missing deps that the
#     code import-guards then silently change behaviour instead of erroring, and the
#     gate reports a failure that exists nowhere but the harness.
#
# Selecting an interpreter from outside REPO_ROOT defeats the entire point of this
# wrapper, which is that a local run matches CI.  Fail loudly instead.
#
# v2026.7.20 offered a replacement fallback — "$HERMES_PYTHON if it can import
# pytest" — and it is REFUSED here for the same reason, measured not assumed:
#   * HERMES_PYTHON is not only a Nix devShell variable. hermes_cli/main.py's
#     _apply_tui_python_env() exports it into every TUI/dashboard child process,
#     set to whatever interpreter the running `hermes` is (sys.executable). Any
#     shell descended from a TUI launch inherits it.
#   * On this host that value is $HOME/.hermes/hermes-agent/venv/bin/python —
#     the LIVE runtime venv — and it HAS pytest installed (9.0.2), so upstream's
#     `import pytest` guard passes and the fallback fires. The guard screens for
#     the wrong thing: the hazard is dependency skew against a runtime on its own
#     release cadence, not a missing pytest.
# Retained fork delta (CLAWD-3009); worth upstreaming as a bug report.
VENV=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv"; do
  if [ -f "$candidate/bin/activate" ]; then
    VENV="$candidate"
    break
  fi
done

if [ -z "$VENV" ]; then
  echo "error: no virtualenv found in $REPO_ROOT/.venv or $REPO_ROOT/venv" >&2
  echo "       (fresh checkout or worktree? create one — the runner deliberately" >&2
  echo "        will NOT borrow an interpreter from outside this repo, HERMES_PYTHON" >&2
  echo "        included:)" >&2
  echo "  python -m venv '$REPO_ROOT/.venv' && '$REPO_ROOT/.venv/bin/pip' install -e '.[dev]'" >&2
  exit 1
fi

PYTHON="$VENV/bin/python"


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"
# Which interpreter ran the gate is evidence.  The CLAWD-3136 false RED was
# invisible precisely because this was never stated.
echo "  venv: $VENV"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
