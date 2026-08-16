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
#   2  refused BEFORE mutating (preflight says unsafe/unmeasured, bad state, an
#      unrecognised argument, or a DANGER latch a human has not cleared)
#   5  DANGER, two shapes: (a) the revert ran and the anchor did NOT come back,
#      or (b) the anchor came back but the WORKING TREE did not, so the
#      consumers still load the advanced code. Both need a human — and both LATCH
#      (see the DANGER latch below), because rc 5 that a later run can launder
#      into rc 0 is not a signal, it is a delay.

set -uo pipefail

RUNTIME="${HERMES_RUNTIME:-$HOME/.hermes/hermes-agent}"
PREFLIGHT="${HERMES_PREFLIGHT:-$RUNTIME/scripts/preflight_hermes_runtime_advance.py}"
VENV_PY="$RUNTIME/venv/bin/python"
DANGER_MARK="$RUNTIME/.hermes-advance-DANGER"

log()  { printf '[hermes-advance] %s\n' "$*"; }
die2() { printf '[hermes-advance] REFUSED: %s\n' "$*" >&2; exit 2; }

# ---- arguments ----------------------------------------------------------------------
# BL-7. This was `[ "${1:-}" = "--dry-run" ] && DRY_RUN=1` with no else, so EVERY
# unrecognised argument fell through to a LIVE ADVANCE: `--dryrun`, `--dry_run` and
# `-n` each measured rc 0 having advanced the fleet's OAuth path for real. A near-miss
# of the one flag whose entire purpose is "do not mutate" is exactly the input a human
# types, and the header two dozen lines up promises this script "refuses rather than
# advancing on anything it could not measure". An unparsed argument IS something it
# could not measure. Refuse the whole argv rather than the first token — a trailing
# `--dry-run` after a typo'd first flag must not rescue the run either.
DRY_RUN=0
case "${1:-}" in
  "")        [ "$#" -eq 0 ] || die2 "unrecognised argument: '$1' (only --dry-run is accepted)" ;;
  --dry-run) DRY_RUN=1 ;;
  *)         die2 "unrecognised argument: '$1' (only --dry-run is accepted). Refusing rather than guessing — this script advances the fleet's OAuth path." ;;
esac
[ "$#" -le 1 ] || die2 "too many arguments (got $#). Only an optional --dry-run is accepted; refusing rather than ignoring the rest."

# ---- preconditions -----------------------------------------------------------------
[ -d "$RUNTIME/.git" ] || die2 "no git checkout at $RUNTIME"
[ -x "$VENV_PY" ]      || die2 "no venv interpreter at $VENV_PY"

# BL-5. THE DANGER LATCH, and it is checked HERE — above the already-current no-op —
# because that no-op is precisely how the laundering happened. Measured:
#
#   RUN 1  rc=5  "DANGER: revert did NOT restore …"   HEAD left at TARGET
#   RUN 2  rc=0  "already current — no-op"
#
# After any rc 5 the fleet's OAuth path is sitting on a tree that FAILED consumer
# verification, and because HEAD equals origin/main the next run took the early exit
# below — which runs neither the preflight nor the consumer verification — and reported
# success. Every run after that reported success too. `OnFailure=` fires once and is
# then cleared, so the single rc 5 was the only signal that ever existed and it was
# overwritten within the hour.
#
# The latch is an UNTRACKED file inside the checkout, which is load-bearing three ways:
# `status --porcelain --untracked-files=no` cannot see it (so it does not trip the
# working-tree guard and read as local work), `git checkout -- .` does not touch it, and
# `git reset --hard` does not remove it. It therefore survives exactly the operations a
# recovery attempt performs, which is the point — only a human clears it.
if [ -e "$DANGER_MARK" ]; then
  printf '[hermes-advance] REFUSED: a previous run left this runtime in a DANGER state and no human has cleared it.\n%s\n' \
    "$(cat "$DANGER_MARK" 2>/dev/null || printf '  (the marker exists but could not be read)')" >&2
  printf '[hermes-advance] The OAuth path may be executing code that FAILED consumer verification. Inspect the checkout, then remove %s to re-arm this timer.\n' \
    "$DANGER_MARK" >&2
  exit 2
fi

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

# A local commit or a real working-tree change means somebody is holding this checkout
# deliberately. Advancing would discard it, so this refuses instead of guessing — the
# same pin-vs-drift question this whole thing exists to answer, asked every run rather
# than answered once. Deletions upstream ALSO makes are not a local change (that was the
# entire content of the "19 dirty files" on 2026-08-12); the preflight distinguishes them.
#
# BL-3. This ran ABOVE the fetch, so the pin-vs-drift question — the one guard standing
# between a timer and somebody's deliberately held checkout — was answered against a
# CACHED CLAIM about the remote: the identical defect the comment on the fetch above
# names, twenty lines from where it is spelled out. After an upstream force-push the
# stale ref reported AHEAD=0, the fetch then made HEAD genuinely divergent, and
# `merge --ff-only` was the only thing left refusing.
#
# ROUND 4 THEN DECLARED `--ff-only` KNOWINGLY REDUNDANT (§19.2) ON THE STRENGTH OF THIS
# FIX. THAT DECLARATION WAS FALSE AND IS WITHDRAWN. Review refuted it twice, both
# reproduced, and a §19.2 declaration is a LICENCE TO DELETE — so a false one is more
# dangerous than the hole it describes, which is this repo's own
# "the remedy inherits the disease" shape.
#
#   (i) Moving the guard below the fetch NARROWED the window; it did not close it.
#       AHEAD counted against the REF while the merge used the SHA resolved on the next
#       line, so a ref update landing between those two git invocations reproduced the
#       whole fail-open with the "root cause fix" in place: shipped rc 1, `--no-edit`
#       rc 0 with a 2-parent commit and VERIFIED. That is fixed properly below by
#       measuring against $TARGET.
#  (ii) `merge.ff=false` in ANY config scope makes the two forms diverge with NO race at
#       all — `--no-edit` synthesises a merge commit that exists nowhere upstream, and
#       the NEXT run then refuses it as a pin at rc 2, wedging the advancer permanently.
#       `merge.ff` is unset on this host today (global, system and local all rc 1), so
#       this is latent rather than live — and it is exactly why the round-4 measurement
#       read RED=0: every fixture sets only user.email/user.name, i.e. the control was
#       drawn from the one configuration the redundancy depends on (§19.7a).
#
# So `--ff-only` is LOAD-BEARING, not redundant: it is what makes this script immune to a
# repo-, user- or system-level `merge.ff`, which nothing here controls.
# RESOLVE THE SHA FIRST, THEN MEASURE AGAINST IT. Round 4 moved this guard below the
# fetch and declared the window closed. It was NARROWED, not closed: AHEAD counted
# against the REF `origin/main` while everything downstream merges the SHA in $TARGET,
# resolved on the next line — so a ref update landing between these two git invocations
# reproduced the fail-open with the "root cause fix" fully in place. Review injected the
# race at exactly that window and measured `--no-edit` producing rc 0, a 2-parent merge
# commit, and VERIFIED, against a shipped rc 1.
#
# This script already knew the rule and writes it down ~95 lines below, on the deletion
# classifier: "Against $TARGET, not origin/main ... resolving the ref again here would
# classify against a commit we are not merging — the same TOCTOU as B2, reintroduced by
# its own fix." The AHEAD guard was the one guard that did not obey it. Measuring against
# $TARGET makes AHEAD=0 PROVE that HEAD is an ancestor of the commit actually merged,
# which is the property the guard was always claimed to have.
ANCHOR="$(git -C "$RUNTIME" rev-parse HEAD)"
TARGET="$(git -C "$RUNTIME" rev-parse origin/main)"

# A local commit means somebody is holding this checkout deliberately. Advancing would
# discard it, so this refuses instead of guessing — the same pin-vs-drift question this
# whole thing exists to answer, asked every run rather than answered once.
AHEAD="$(git -C "$RUNTIME" rev-list --count "$TARGET"..HEAD 2>/dev/null || echo '?')"
[ "$AHEAD" = "0" ] || die2 "$AHEAD local commit(s) not on the target ${TARGET:0:9} — this looks like a deliberate pin, not drift"
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
# BL-5. Engage the latch checked in the preconditions. A write that FAILS is announced
# rather than swallowed, and the announcement says what the failure costs — the rc is 5
# either way, but without the marker the NEXT run launders it back to 0 and this one was
# the only signal. That is the whole defect, so a silent failure to latch would leave it
# open while looking closed.
latch_danger() { # latch_danger <shape> <detail>
  if ! { printf 'DANGER latched %s\n  shape   : %s\n  runtime : %s\n  anchor  : %s\n  detail  : %s\n' \
           "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo 'unknown-time')" \
           "$1" "$RUNTIME" "$ANCHOR" "$2" > "$DANGER_MARK"; } 2>/dev/null; then
    printf '[hermes-advance] AND THE LATCH DID NOT ENGAGE: could not write %s. This run exits 5, but nothing stops the next run reporting success over the same state — treat this as unlatched DANGER and clear it by hand.\n' \
      "$DANGER_MARK" >&2
  fi
}

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
    latch_danger "tree-not-restored" "HEAD returned to the anchor but the working tree did not; the consumers load the advanced files"
    exit 5
  fi
  # COVERED as of 2026-08-16 by case 4quinquies. My earlier declaration that this
  # branch was unreachable "without stubbing git at the harness level" was FALSE and
  # review refuted it by construction: a fixture interpreter that chmods a directory
  # read-only on the verify invocation makes `git reset --hard` genuinely fail. That
  # one fixture kills BOTH this exit (RED=1) and the readback mutation (RED=3), each of
  # which previously survived at 38/0. The fixture is skipped as root, declared.
  printf '[hermes-advance] DANGER: revert did NOT restore %s (HEAD is now %s). The fleet OAuth path is in an UNKNOWN state; a human must look.\n' \
    "${ANCHOR:0:9}" "${now:0:9}" >&2
  latch_danger "anchor-not-restored" "revert did not return HEAD to the anchor; HEAD is now ${now:0:9}"
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
    # EXIT 2, not 1. The header defines 1 as "advance failed -> REVERTED to the
    # anchor" and 2 as "refused BEFORE mutating". This refusal mutates NOTHING:
    # HEAD is unmoved, no merge was attempted, no revert ran. Returning 1 told a
    # reader (and any log rule keying on rc) to go looking for a rollback that
    # never happened. rc is the only machine-readable signal this script emits.
    printf '[hermes-advance] REFUSING: %s carries local working-tree changes upstream does not make.\n%s\nCommit, stash or discard them deliberately; this script will not do it for you.\n' \
      "$RUNTIME" "$LOCAL_WORK" >&2
    exit 2
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
