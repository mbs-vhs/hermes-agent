#!/usr/bin/env bash
# advance_hermes_runtime.sh — advance ~/.hermes/hermes-agent to origin/main, but ONLY
# when a preflight has PROVED the advance is safe for the things that execute from it.
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-12 that checkout measured 10,078 commits behind origin/main. Every other
# pinned runtime in this mesh has a mechanism and sits <=2 commits behind; this one's
# only mechanism was `deploy-to-runtime.sh`, run by hand, and nothing scheduled it. So
# it drifted for five weeks and nobody could say whether that was drift or a pin.
#
# The measured answer was DRIFT (no pin file, 0 local commits, and the 19 "dirty" files
# were deletions upstream had already made). It was advanced, and both consumers were
# verified by running them for real. This script is the part that stops it recurring.
#
# WHAT MAKES THIS DIFFERENT FROM `git pull` ON A TIMER
# ----------------------------------------------------
# The two consumers are oneshot OAuth-refresh timers whose scripts live OUTSIDE the
# checkout but do `sys.path.insert(0, ~/.hermes/hermes-agent)` and import from the tree.
# The tree shadows site-packages, so advancing it changes what they execute — and they
# are the credential path for the whole fleet. A blind pull on a timer would be a
# scheduled, unattended, unverified mutation of the fleet's OAuth path.
#
# So the order is: PREFLIGHT -> advance -> VERIFY THE CONSUMER CONTRACT -> revert on
# failure. The preflight (`preflight_hermes_runtime_advance.py`) resolves every symbol
# the consumers import at the target ref, binds them against the real call sites, and
# executes the imports under the runtime's own venv — with a negative control proving it
# can fail. Exit 2 from it means UNMEASURED, and UNMEASURED is never treated as safe.
#
# FAIL-CLOSED, DELIBERATELY. The sibling devops runtime advance is fail-soft (a network
# blip must not wedge it). This one is not: it refuses rather than advancing on anything
# it could not measure, because the failure it is guarding is "the fleet cannot refresh
# its credentials", not "a scan runs on slightly stale code".
#
# EXIT CODES
#   0  advanced and verified, or already current (no-op)
#   1  advance failed or verification failed -> REVERTED to the recorded anchor
#   2  refused BEFORE mutating (preflight says unsafe/unmeasured, or bad state)
#   5  DANGER: reverted but the anchor did not come back. A human must look.

set -uo pipefail

RUNTIME="${HERMES_RUNTIME:-$HOME/.hermes/hermes-agent}"
PREFLIGHT="${HERMES_PREFLIGHT:-$RUNTIME/scripts/preflight_hermes_runtime_advance.py}"
VENV_PY="$RUNTIME/venv/bin/python"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log()  { printf '[hermes-advance] %s\n' "$*"; }
die2() { printf '[hermes-advance] REFUSED: %s\n' "$*" >&2; exit 2; }

# ---- preconditions -----------------------------------------------------------------
[ -d "$RUNTIME/.git" ] || die2 "no git checkout at $RUNTIME"
[ -x "$VENV_PY" ]      || die2 "no venv interpreter at $VENV_PY"

# A local commit or a real working-tree change means somebody is holding this checkout
# deliberately. Advancing would discard it, so this refuses instead of guessing — the
# same pin-vs-drift question this whole thing exists to answer, asked every run rather
# than answered once. Deletions upstream ALSO makes are not a local change (that was the
# entire content of the "19 dirty files" on 2026-08-12); the preflight distinguishes them.
AHEAD="$(git -C "$RUNTIME" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
[ "$AHEAD" = "0" ] || die2 "$AHEAD local commit(s) not on origin/main — this looks like a deliberate pin, not drift"
for f in .pinned-ref .deployed-ref PIN; do
  [ -e "$RUNTIME/$f" ] && die2 "pin file $f present — refusing to advance a declared pin"
done

# ---- fetch BEFORE measuring ---------------------------------------------------------
# A ref is a CACHED CLAIM about a remote and its freshness is measured by nothing
# (CLAWD-3760). Skipping this is how a runtime reports "behind: 0" from a ref nothing
# refreshed — and the error is in the reassuring direction, so it fails green.
if ! git -C "$RUNTIME" fetch --quiet origin 2>/dev/null; then
  die2 "could not fetch origin — every distance below would be against a stale ref, and a stale ref makes a BEHIND runtime look CURRENT"
fi

ANCHOR="$(git -C "$RUNTIME" rev-parse HEAD)"
TARGET="$(git -C "$RUNTIME" rev-parse origin/main)"
BEHIND="$(git -C "$RUNTIME" rev-list --count HEAD.."$TARGET" 2>/dev/null || echo '?')"

if [ "$ANCHOR" = "$TARGET" ]; then
  log "already current at ${ANCHOR:0:9} — no-op"
  exit 0
fi
log "anchor=${ANCHOR:0:9}  target=${TARGET:0:9}  behind=$BEHIND"

# ---- preflight: would this break a consumer? ----------------------------------------
if [ ! -f "$PREFLIGHT" ]; then
  die2 "preflight not found at $PREFLIGHT — refusing to advance the fleet's OAuth path unverified"
fi
log "preflight …"
PF_OUT="$("$VENV_PY" "$PREFLIGHT" --target "$TARGET" 2>&1)"; PF_RC=$?
printf '%s\n' "$PF_OUT" | sed 's/^/[preflight] /'
case "$PF_RC" in
  0) log "preflight: advance is safe for the measured consumers" ;;
  1) die2 "preflight says this advance WOULD BREAK a consumer — see above" ;;
  *) die2 "preflight could not measure (rc=$PF_RC). UNMEASURED is not safe." ;;
esac

if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would: git checkout -- . && git merge --ff-only ${TARGET:0:9} (the preflighted SHA)"
  log "[dry-run] would then verify the consumer imports and revert on failure"
  exit 0
fi

# ---- apply ---------------------------------------------------------------------------
revert_and_prove() {
  local why="$1"
  log "$why — reverting to ${ANCHOR:0:9}"
  git -C "$RUNTIME" reset --hard "$ANCHOR" >/dev/null 2>&1
  # B3. "revert PROVEN" used to read HEAD ALONE, which is not a revert: measured,
  # `reset --hard` -> `--soft` returns the pointer and leaves the BROKEN FILES ON
  # DISK, and the run still printed PROVEN. Mutations that stayed green on the old
  # proof: --hard -> --soft, deleting this readback entirely, and exit 5 -> exit 0.
  # The tree is now part of the proof, because the tree is what the consumers load.
  local now dirty
  now="$(git -C "$RUNTIME" rev-parse HEAD 2>/dev/null || echo none)"
  dirty="$(git -C "$RUNTIME" status --porcelain --untracked-files=no 2>/dev/null)"
  if [ "$now" = "$ANCHOR" ] && [ -z "$dirty" ]; then
    log "revert PROVEN (anchor restored, working tree clean)"
    exit 1
  fi
  if [ "$now" = "$ANCHOR" ] && [ -n "$dirty" ]; then
    printf '[hermes-advance] DANGER: HEAD is back at %s but the WORKING TREE IS DIRTY — the revert restored the pointer and not the files, so the consumers still load the advanced code:\n%s\n' \
      "${ANCHOR:0:9}" "$dirty" >&2
    exit 5
  fi
  # DECLARED RESIDUAL (uncovered): no test drives this branch. Measured — mutating
  # this `exit 5` to `exit 0` leaves the suite GREEN at 38/0, so a revert that
  # FAILED would report success and nothing would catch the regression. Reaching it
  # needs `git reset --hard` itself to fail, which a fixture cannot arrange without
  # stubbing git at the harness level; a fragile test here would be worse than an
  # honest gap. The DIRTY-TREE danger path above IS covered (M22 reddens 5).
  printf '[hermes-advance] DANGER: revert did NOT restore %s (HEAD is now %s). The fleet OAuth path is in an UNKNOWN state; a human must look.\n' \
    "${ANCHOR:0:9}" "${now:0:9}" >&2
  exit 5
}

# WORKING-TREE GUARD (B1). The comment at the top of this script has ALWAYS
# claimed this refusal — "a real working-tree change means somebody is holding
# this checkout deliberately ... this refuses instead of guessing". It was never
# implemented: only the local-commit (AHEAD) half existed, and the
# `git checkout -- .` below then DESTROYED the work while the run reported
# success (rc 0, "consumers verified"). Reproduced twice, independently.
#
# The distinction the comment already draws is the right one and is kept:
# deletions that UPSTREAM ALSO MAKES are not local work (that was the entire
# content of the "19 dirty files" on 2026-08-12). Everything else is, and is
# fatal. Untracked files are ignored deliberately — `checkout -- .` does not
# touch them, so they are not at risk here.
DIRTY="$(git -C "$RUNTIME" status --porcelain --untracked-files=no 2>/dev/null)"
if [ -n "$DIRTY" ]; then
  # Against $TARGET, not origin/main: this guard decides which deletions are "not
  # local work", so resolving the ref again here would classify against a commit we
  # are not merging — the same TOCTOU as B2, reintroduced by its own fix.
  UPSTREAM_DELETES="$(git -C "$RUNTIME" diff --name-only --diff-filter=D HEAD "$TARGET" 2>/dev/null || true)"
  LOCAL_WORK=""
  while IFS= read -r _line; do
    [ -n "$_line" ] || continue
    _st="${_line:0:2}"; _f="${_line:3}"
    if [ "$_st" = " D" ] || [ "$_st" = "D " ]; then
      printf '%s\n' "$UPSTREAM_DELETES" | grep -qxF -- "$_f" && continue
    fi
    LOCAL_WORK="${LOCAL_WORK}${_line}"$'\n'
  done <<< "$DIRTY"
  if [ -n "$LOCAL_WORK" ]; then
    printf '[hermes-advance] REFUSING: %s carries local working-tree changes upstream does not make.\n%s\nCommit, stash or discard them deliberately; this script will not do it for you.\n' \
      "$RUNTIME" "$LOCAL_WORK" >&2
    exit 1
  fi
fi

# The working tree carries deletions that upstream also makes; restore them so the
# fast-forward can apply cleanly, then let the merge delete them properly.
git -C "$RUNTIME" checkout -- . 2>/dev/null
# B2 (TOCTOU). `origin/main` used to be resolved THREE separate times — the SHA at
# TARGET, the ref again for the preflight, and the ref a THIRD time here — and the
# preflight fetches by default, so the window opened on EVERY run, not rarely.
# Reproduced: preflighted bcff885f2, LANDED fe8c39385, logged "advanced to bcff885f2",
# rc 0. An un-preflighted commit left executing under a log line naming a different
# one. Everything downstream now merges the SHA that was actually measured.
if ! git -C "$RUNTIME" merge --ff-only "$TARGET" >/dev/null 2>&1; then
  revert_and_prove "fast-forward failed"
fi
log "advanced to ${TARGET:0:9}"

# ---- verify THE CONSUMER CONTRACT ----------------------------------------------------
# Not a liveness ping. These are the exact symbols the two OAuth refresh scripts import
# from the tree; if they do not resolve, credential refresh is dead and the fleet finds
# out at the next timer fire rather than now.
#
# HOME is redirected so nothing here can touch the real credential pool. mktemp -d is
# checked: the first version of this verification let mktemp fail, ran with an EMPTY
# HOME, and still reported PASS — a verify hook that degrades silently is worse than
# none, because it launders an unverified advance into a verified one.
cd / || die2 "cannot cd /"
VHOME="$(mktemp -d "${TMPDIR:-/var/tmp}/hermes-verify.XXXXXX")" \
  || revert_and_prove "could not create a scratch HOME for verification"

# `cd /` is load-bearing: `python -c` puts the CURRENT WORKING DIRECTORY on sys.path
# ahead of everything, so running this from any hermes checkout resolved the modules
# from THAT tree instead of the runtime. Measured: the check passed against a
# deliberately broken runtime because it had silently loaded a sibling worktree's copy.
# A verification that can be satisfied by a different tree is not a verification.
#
# The provenance assert is the belt to that brace: it is not enough to import the
# symbols, they must have come FROM THE RUNTIME. site-packages, a stale editable
# install, or a namespace-package collision could each satisfy the import while the
# tree being advanced is broken.
if HOME="$VHOME" PYTHONDONTWRITEBYTECODE=1 "$VENV_PY" -c "
import sys
sys.path.insert(0, '$RUNTIME')
from agent.anthropic_adapter import (read_claude_code_credentials,
                                     is_claude_code_token_valid,
                                     refresh_anthropic_oauth_pure,
                                     _write_claude_code_credentials)
from hermes_cli.auth import _save_codex_tokens, refresh_codex_oauth_pure, AuthError
import agent.anthropic_adapter as _a, hermes_cli.auth as _b
for _m in (_a, _b):
    if not (getattr(_m, '__file__', '') or '').startswith('$RUNTIME'):
        raise SystemExit('RESOLVED FROM THE WRONG TREE: %s came from %s, not $RUNTIME'
                         % (_m.__name__, getattr(_m, '__file__', '?')))
" >/dev/null 2>&1; then
  rm -rf "$VHOME"
  log "VERIFIED: both OAuth consumers' imports resolve against the advanced tree"
else
  rm -rf "$VHOME"
  revert_and_prove "consumer imports FAILED against the advanced tree"
fi

log "ok — ${ANCHOR:0:9} -> ${TARGET:0:9} ($BEHIND commits), consumers verified"
log "the two refresh timers are ONESHOTS; they pick up this code on their next firing, so nothing is restarted here"
exit 0
