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

# Sanitize PATH before the first external command. A caller's active venv must
# not supply dirname/env/mktemp/ps/sleep or become the child runner's fallback.
SAFE_SYSTEM_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for system_bin in /opt/homebrew/bin /opt/homebrew/sbin; do
  if [ -d "$system_bin" ]; then
    SAFE_SYSTEM_PATH="$SAFE_SYSTEM_PATH:$system_bin"
  fi
done
PATH="$SAFE_SYSTEM_PATH"
export PATH
unset VIRTUAL_ENV

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
VENV=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv"; do
  if [ -L "$candidate" ]; then
    echo "error: refusing symlinked virtualenv: $candidate" >&2
    echo "       test worktrees must own a real .venv/venv directory; do not" >&2
    echo "       link a shared environment from another checkout" >&2
    exit 1
  fi

  if [ -f "$candidate/bin/activate" ]; then
    repo_root_real="$(cd -P "$REPO_ROOT" && pwd -P)"
    candidate_real="$(cd -P "$candidate" && pwd -P)"
    candidate_bin_real="$(cd -P "$candidate/bin" && pwd -P)"

    case "$candidate_real" in
      "$repo_root_real"/*) ;;
      *)
        echo "error: virtualenv resolves outside the current worktree: $candidate" >&2
        echo "       resolved path: $candidate_real" >&2
        exit 1
        ;;
    esac

    case "$candidate_bin_real" in
      "$candidate_real"/*) ;;
      *)
        echo "error: virtualenv bin directory resolves outside its environment: $candidate/bin" >&2
        echo "       resolved path: $candidate_bin_real" >&2
        exit 1
        ;;
    esac

    VENV="$candidate"
    break
  fi
done

if [ -z "$VENV" ]; then
  echo "error: no virtualenv found in $REPO_ROOT/.venv or $REPO_ROOT/venv" >&2
  echo "       (fresh checkout or worktree? create one — the runner deliberately" >&2
  echo "        will NOT borrow an interpreter from outside this repo:)" >&2
  echo "  python -m venv '$REPO_ROOT/.venv' && '$REPO_ROOT/.venv/bin/pip' install -e '.[dev,acp,wecom]'" >&2
  exit 1
fi

PYTHON="$VENV/bin/python"

# A venv's interpreter is commonly a symlink to its base Python.  That is safe
# only when Python still reports the selected venv as sys.prefix; a symlink to
# another checkout's venv would instead execute with that foreign prefix.
if ! python_prefix_real="$(
  env -i "$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.prefix))' 2>/dev/null
)"; then
  echo "error: unable to verify virtualenv interpreter: $PYTHON" >&2
  exit 1
fi

if [ "$python_prefix_real" != "$candidate_real" ]; then
  echo "error: virtualenv interpreter reports a foreign sys.prefix: $PYTHON" >&2
  echo "       selected environment: $candidate_real" >&2
  echo "       reported sys.prefix: $python_prefix_real" >&2
  exit 1
fi


# ── Live-gateway plugin source (materialized by Python runner) ──────────────
LIVE_GUARD_SOURCE=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  LIVE_GUARD_SOURCE="$HOME/.hermes/pytest_live_guard.py"
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

# Do not preserve caller PATH: its VIRTUAL_ENV/bin (or another checkout's bin)
# would remain an executable fallback after the selected venv.
SAFE_PATH="$candidate_bin_real:$SAFE_SYSTEM_PATH"

exec env -i \
  PATH="$SAFE_PATH" \
  VIRTUAL_ENV="$candidate_real" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONDONTWRITEBYTECODE=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${LIVE_GUARD_SOURCE:+HERMES_PYTEST_LIVE_GUARD_SOURCE="$LIVE_GUARD_SOURCE"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
