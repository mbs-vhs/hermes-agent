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

# Keep native Windows system tools (notably taskkill) reachable without
# preserving the caller's possibly venv-contaminated PATH.
if [ -n "${SYSTEMROOT:-}" ] && command -v cygpath >/dev/null 2>&1; then
  windows_system_bin="$(cygpath -u "$SYSTEMROOT/System32")"
  if [ -d "$windows_system_bin" ]; then
    SAFE_SYSTEM_PATH="$SAFE_SYSTEM_PATH:$windows_system_bin"
    PATH="$SAFE_SYSTEM_PATH"
    export PATH
  fi
fi

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
VENV_PYTHON=""
SKIPPED_VENVS=""
repo_root_real="$(cd -P "$REPO_ROOT" && pwd -P)"
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv"; do
  if [ -L "$candidate" ]; then
    echo "error: refusing symlinked virtualenv: $candidate" >&2
    echo "       test worktrees must own a real .venv/venv directory; do not" >&2
    echo "       link a shared environment from another checkout" >&2
    exit 1
  fi

  candidate_bin=""
  candidate_python=""
  candidate_layout=""
  if [ -f "$candidate/bin/activate" ] && [ -x "$candidate/bin/python" ]; then
    candidate_bin="$candidate/bin"
    candidate_python="$candidate/bin/python"
    candidate_layout="bin"
  elif [ -f "$candidate/Scripts/activate" ] && [ -x "$candidate/Scripts/python.exe" ]; then
    # Native Windows venv layout under Git Bash / MSYS.
    candidate_bin="$candidate/Scripts"
    candidate_python="$candidate/Scripts/python.exe"
    candidate_layout="Scripts"
  fi

  if [ -n "$candidate_python" ]; then
    candidate_real="$(cd -P "$candidate" && pwd -P)"
    candidate_bin_real="$(cd -P "$candidate_bin" && pwd -P)"

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
        echo "error: virtualenv $candidate_layout directory resolves outside its environment: $candidate_bin" >&2
        echo "       resolved path: $candidate_bin_real" >&2
        exit 1
        ;;
    esac

    if ! env -i \
        PATH="$candidate_bin_real:$SAFE_SYSTEM_PATH" \
        VIRTUAL_ENV="$candidate_real" \
        "$candidate_python" -c 'import pytest' 2>/dev/null; then
      SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
      continue
    fi

    VENV="$candidate"
    VENV_PYTHON="$candidate_python"
    break
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping repo-local venv without pytest: $skipped" >&2
  done
fi

if [ -z "$VENV" ]; then
  echo "error: no virtualenv found in $REPO_ROOT/.venv or $REPO_ROOT/venv" >&2
  echo "       (fresh checkout or worktree? create one — the runner deliberately" >&2
  echo "        will NOT borrow an interpreter from outside this repo, HERMES_PYTHON" >&2
  echo "        included:)" >&2
  echo "  python -m venv '$REPO_ROOT/.venv' && '$REPO_ROOT/.venv/bin/pip' install -e '.[dev,acp,wecom]'" >&2
  exit 1
fi

PYTHON="$VENV_PYTHON"

# A venv's interpreter is commonly a symlink to its base Python.  That is safe
# only when Python still reports the selected venv as sys.prefix; a symlink to
# another checkout's venv would instead execute with that foreign prefix.
if ! python_prefix_real="$(
  env -i "$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.prefix))' 2>/dev/null
)"; then
  echo "error: unable to verify virtualenv interpreter: $PYTHON" >&2
  exit 1
fi

prefix_matches=false
if [ "$python_prefix_real" = "$candidate_real" ]; then
  prefix_matches=true
elif command -v cygpath >/dev/null 2>&1 \
    && [ "$(cygpath -u "$python_prefix_real")" = "$candidate_real" ]; then
  # Native Windows Python reports C:\\... while Git Bash reports /c/....
  prefix_matches=true
fi

if [ "$prefix_matches" != true ]; then
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

# Native Windows CPython resolves home and platform paths from these rather
# than HOME. They are locations, not credentials, and are forwarded only when
# present.
#
# GATED ON $SYSTEMROOT, same predicate as the cygpath block above, and the gate
# is load-bearing rather than tidiness. TEMP and TMP are ordinary POSIX-settable
# variables: ungated, this loop collects them on Linux and re-injects them PAST
# `env -i`, which is the exact boundary the surrounding hardening exists to
# enforce. Python's tempfile honours TMPDIR, then TEMP, then TMP -- so a caller
# running `TEMP=/their/dir ./scripts/run_tests.sh` relocates the live-gateway
# guard module under a caller-chosen root and puts that root on PYTHONPATH for
# every pytest child. Measured, with a control:
#     control   PYTHONPATH=/tmp/hermes-pytest-live-guard.XXXX
#     TEMP set  PYTHONPATH=<caller dir>/hermes-pytest-live-guard.XXXX
# On native Windows SYSTEMROOT is always present, so the intent is unchanged.
WIN_ENV=()
if [ -n "${SYSTEMROOT:-}" ]; then
  for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
    if [ -n "${!_win_var:-}" ]; then
      WIN_ENV+=("$_win_var=${!_win_var}")
    fi
  done
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

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
env -i \
  PATH="$SAFE_PATH" \
  VIRTUAL_ENV="$candidate_real" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  "$PYTHON" -S -m compileall -q -j 4 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
# NOTE on PYTHONDONTWRITEBYTECODE below: it is DELIBERATE and it reverses a
# deletion the v7.20 ledger made -- recorded here so it reads as a decision
# rather than a merge accident (review NB1). The ledger dropped it together with
# the pre-compile block; that block is back, and it runs under `env -i` WITHOUT
# the flag, so it populates the bytecode cache and these children then read it.
# Keeping it on the children is also the safer direction on this host: a stale
# .pyc has previously made a reverted edit re-run the OLD bytecode and silently
# invalidate a revert-validation result. No test pins this either way.
#
# Do NOT put comments inside the continuation below -- a `#` line after a
# trailing `\` terminates the command, and the runner then exits 0 having run
# NOTHING. That was measured here, not theorised.
exec env -i \
  PATH="$SAFE_PATH" \
  VIRTUAL_ENV="$candidate_real" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUTF8=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${LIVE_GUARD_SOURCE:+HERMES_PYTEST_LIVE_GUARD_SOURCE="$LIVE_GUARD_SOURCE"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
