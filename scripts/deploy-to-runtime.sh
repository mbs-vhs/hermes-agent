#!/usr/bin/env bash
# deploy-to-runtime.sh — deploy this fork's `main` to the LIVE Hermes runtime.
#
# TOPOLOGY (re-baselined 2026-07-25 — the old comment here was STALE and the
# staleness made this script dangerous; see the preflight in step 1b):
# There are now TWO live Hermes populations, and this script only serves (b):
#   (a) THE FLEET — 12 per-user accounts (/home/hermes-<profile>), 11 of which
#       run ai.hermes.gateway-<profile>.service under THEIR OWN systemd --user
#       manager (hermes-cc has no gateway unit), executing from
#       /opt/hermes-agent/venv — an EDITABLE install rooted at the (non-git)
#       /opt/hermes-agent source tree. Editing that tree IS a live fleet
#       mutation. This script does NOT reach that population at all.
#   (b) THE ~/.hermes RUNTIME CHECKOUT — serves chat.vhs.box (hermes-webui) +
#       research. hermes-agent is installed EDITABLE into
#       ~/.hermes/hermes-agent/venv, and hermes-webui SPAWNS SUBPROCESSES with
#       that venv's python (it does not import hermes_agent into the webui
#       parent). So advancing this checkout takes effect on the NEXT SPAWNED
#       AGENT — live, unstaged, without any unit restart. Restarting
#       ai.minerva.chat-ui.service does NOT gate that; it only recycles the
#       parent. That is exactly why a half-applied deploy here is unsafe.
# The 11 ai.hermes.gateway-*.service units under the operator's own manager are
# LEGACY LEFTOVERS and are all MASKED (the fleet relocated to per-user accounts).
#
# WHY THE FLEET HALF IS NOT JUST UNIMPLEMENTED (CLAWD-2833): /opt/hermes-agent
# has no .git, so there is no ref to deploy FROM and no `git status` to detect
# drift with. It is also not a clean copy of any ref — measured 2026-07-27, 251
# files exist in it that HEAD does not have, 33 of which Python still resolves as
# importable modules. A ref-based deploy with --delete semantics would remove
# those from 11 live gateways at once. Every one of the 251 now has a documented
# provenance (see scripts/opt_provenance_report.py), but the deploy substrate
# itself is a ratification decision, not something this script should grow
# quietly:  devops-process/proposals/2026-07-27-opt-hermes-deploy-substrate.md
#
# It advances the runtime to origin/<branch> by a strict fast-forward and then
# (optionally, gated) restarts the gateway units it validated up front.
#
# DRIFT GUARD (the important part): if the runtime working tree is DIRTY, this
# refuses to run. A dirty runtime means someone hot-fixed in place and never
# committed — reconcile that work to the fork first (commit + push), then deploy.
# This is what prevents the silent accumulation of uncommitted live changes.
#
# Usage:
#   scripts/deploy-to-runtime.sh [--dry-run] [--no-restart] [--parallel-restart] [--yes]
#
# Flags:
#   --dry-run            Show what would happen; make no mutating changes.
#   --no-restart         Fast-forward the runtime but do NOT restart gateways.
#                        (Skips the step-1b preflight. Because the runtime venv
#                        is an EDITABLE install that hermes-webui spawns agents
#                        from, the new code still takes effect on the NEXT
#                        SPAWNED AGENT — this does not stage the change.)
#   --parallel-restart   Restart all gateways at once (brief full-fleet blip).
#                        Default is a rolling restart (<=1 gateway down at a time).
#   --yes, -y            Skip the interactive restart confirmation.
#
# Env overrides:
#   HERMES_RUNTIME_CHECKOUT  (default: ~/.hermes/hermes-agent)
#   HERMES_DEPLOY_BRANCH     (default: main)
set -euo pipefail

RUNTIME="${HERMES_RUNTIME_CHECKOUT:-$HOME/.hermes/hermes-agent}"
BRANCH="${HERMES_DEPLOY_BRANCH:-main}"
DRY_RUN=0; NO_RESTART=0; PARALLEL=0; ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)          DRY_RUN=1 ;;
    --no-restart)       NO_RESTART=1 ;;
    --parallel-restart) PARALLEL=1 ;;
    --yes|-y)           ASSUME_YES=1 ;;
    # NOTE: keep this range in sync with the header block above — it ends at the
    # last "#" line before `set -euo pipefail`. A stale range silently truncates
    # --help (it once cut off every flag, including the --no-restart that the
    # step-1b die() tells operators to use).
    -h|--help)          sed -n '2,58p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "deploy-to-runtime: unknown arg: $arg (try --help)" >&2; exit 2 ;;
  esac
done

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
git_rt() { git -C "$RUNTIME" "$@"; }

[ -d "$RUNTIME/.git" ] || die "runtime checkout not found at $RUNTIME (set HERMES_RUNTIME_CHECKOUT)"

# 1) DRIFT GUARD — refuse to deploy over an uncommitted runtime tree.
dirty="$(git_rt status --porcelain)"
if [ -n "$dirty" ]; then
  log "runtime working tree is DIRTY — refusing to deploy over uncommitted work:"
  printf '%s\n' "$dirty" | sed 's/^/    /'
  die "reconcile this to the fork first (commit + push to origin/$BRANCH), then re-run. See CLAWD-1008 for the pattern."
fi

# 1b) RESTART-TARGET PREFLIGHT — validate BEFORE mutating anything.
#
# ORDERING IS LOAD-BEARING. This script used to fast-forward the runtime FIRST
# and only discover at the restart step that it had nothing valid to restart.
# Because the runtime venv installs hermes-agent EDITABLE, that left the live
# chat runtime advanced by thousands of commits on disk while the script exited
# with a message about gateways that reads like "nothing was deployed". Mutate
# only after we know the restart half can actually succeed.
units=()
if [ "$NO_RESTART" = "0" ]; then
  mapfile -t units < <(systemctl --user list-unit-files 'ai.hermes.gateway-*.service' --no-legend 2>/dev/null | awk '{print $1}' | sort)
  # NOTE (CLAWD-2833): on the current host this branch is DEAD CODE — 11 masked
  # legacy units still exist under the operator's manager, so units[] is never
  # empty and control always reaches the loadability check below. Kept for the
  # host where those leftovers have been removed. Re-derive which branch fires:
  #   systemctl --user list-unit-files 'ai.hermes.gateway-*.service' --no-legend
  [ "${#units[@]}" -gt 0 ] || die "no ai.hermes.gateway-*.service units found under this user manager. If the fleet has moved (it now runs per-user from /opt/hermes-agent), this script cannot deploy it — use --no-restart to advance ONLY the ~/.hermes checkout, and restart its consumers (chat-ui) yourself. The fleet has no deploy path yet, by design: /opt/hermes-agent has no .git to deploy from — run scripts/opt_provenance_report.py --tree /opt/hermes-agent --strict to see what diverges, and see devops-process/proposals/2026-07-27-opt-hermes-deploy-substrate.md (CLAWD-2833)."
  # Reject anything not cleanly loadable, not just `masked`. LoadState collapses
  # masked/masked-runtime, but `error` / `bad-setting` (unparseable unit) would
  # also sail past a masked-only check and then fail at restart — reaching the
  # same half-deployed state through a different door.
  bad=()
  for u in "${units[@]}"; do
    state="$(systemctl --user show "$u" -p LoadState --value 2>/dev/null || true)"
    [ "$state" = "loaded" ] || bad+=("$u ($state)")
  done
  if [ "${#bad[@]}" -gt 0 ]; then
    log "${#bad[@]}/${#units[@]} gateway unit(s) are not loadable and cannot be restarted:"
    printf '%s\n' "${bad[@]}" | sed 's/^/    /'
    log "masked ones are legacy leftovers; the live fleet runs per-user from /opt/hermes-agent."
    log "the fleet has no deploy path yet BY DESIGN: /opt/hermes-agent has no .git,"
    log "so there is no ref to deploy from and no git status to detect drift with."
    log "  measure the divergence: scripts/opt_provenance_report.py --tree /opt/hermes-agent --strict"
    log "  the substrate decision:  devops-process/proposals/2026-07-27-opt-hermes-deploy-substrate.md (CLAWD-2833)"
    die "refusing to deploy: nothing was mutated. Restarting a masked/unloadable unit always fails, and advancing the runtime first would leave the live chat runtime half-deployed. To advance ONLY the ~/.hermes checkout (chat.vhs.box / research), re-run with --no-restart — and note that takes effect on the NEXT SPAWNED AGENT, so there is no restart that stages it."
  fi
fi

# 2) Fetch the target (read-only to the working tree).
log "fetching origin/$BRANCH ..."
git_rt fetch origin "$BRANCH" --quiet

before="$(git_rt rev-parse --short HEAD)"
target="$(git_rt rev-parse --short "origin/$BRANCH")"

# 3) Fast-forward (only if there is something to do, and only if it is clean).
if [ "$before" = "$target" ]; then
  log "runtime already at origin/$BRANCH ($target) — no fast-forward needed."
else
  if ! git_rt merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    die "HEAD ($before) is NOT an ancestor of origin/$BRANCH ($target) — not a clean fast-forward. Manual reconcile required (history diverged)."
  fi
  log "fast-forward $before -> $target. Incoming:"
  git_rt --no-pager log --oneline "HEAD..origin/$BRANCH" | sed 's/^/    /'
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] would run: git -C $RUNTIME merge --ff-only origin/$BRANCH"
  else
    git_rt merge --ff-only "origin/$BRANCH"
    log "runtime advanced to $(git_rt rev-parse --short HEAD)"
  fi
fi

# 4) Gateway restart (gated).
if [ "$NO_RESTART" = "1" ]; then
  log "--no-restart: gateways NOT restarted. NOTE the runtime venv is an EDITABLE install and hermes-webui SPAWNS agents from it, so the ~/.hermes population picks this up on its NEXT SPAWNED AGENT — already-running gateway processes keep their loaded modules until restarted."
  exit 0
fi

# units[] was collected AND validated (non-empty, none masked) by the step-1b
# preflight, before any mutation. Do not re-derive it here.
mode=$([ "$PARALLEL" = "1" ] && echo "parallel" || echo "rolling")
log "${#units[@]} gateways to restart ($mode): ${units[*]}"

if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would restart the ${#units[@]} gateways ($mode) and health-check each."
  exit 0
fi

if [ "$ASSUME_YES" = "0" ]; then
  [ -t 0 ] || die "non-interactive shell and --yes not given; refusing to restart the live fleet unattended."
  read -r -p "[deploy] restart ${#units[@]} LIVE gateways now? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) die "aborted by operator (fast-forward applied to disk; gateways NOT restarted — run again with --yes or restart manually)." ;;
  esac
fi

# Restart one unit and confirm it returns to active (systemctl restart blocks
# until the job completes; the short poll covers a brief 'activating' tail).
restart_one() {
  local u="$1" s
  if ! systemctl --user restart "$u"; then
    log "  $u: restart command FAILED"; return 1
  fi
  # systemctl restart is synchronous, but a gateway can sit in 'activating'
  # while it cold-loads models (see the VRAM/keep-warm history) — poll generously.
  for _ in $(seq 1 30); do
    s="$(systemctl --user is-active "$u" 2>/dev/null || true)"
    case "$s" in
      active)  log "  $u: active"; return 0 ;;
      failed)  log "  $u: FAILED"; return 1 ;;
      *)       sleep 3 ;;
    esac
  done
  log "  $u: not active after restart (state=$s)"; return 1
}

failed=0
if [ "$PARALLEL" = "1" ]; then
  systemctl --user restart "${units[@]}" || failed=1
  for u in "${units[@]}"; do
    s="$(systemctl --user is-active "$u" 2>/dev/null || true)"
    log "  $u: $s"; [ "$s" = "active" ] || failed=1
  done
else
  # Fail-fast: if a gateway doesn't come back, stop — don't keep restarting the
  # rest (limits blast radius if the freshly-deployed code is broken; the
  # un-restarted gateways stay up on what they were already running).
  for u in "${units[@]}"; do
    if ! restart_one "$u"; then
      failed=1
      log "halting rolling restart after failure — remaining gateways left untouched."
      break
    fi
  done
fi

[ "$failed" = "0" ] || die "one or more gateways did not return to active — investigate: systemctl --user status ai.hermes.gateway-<profile>.service"

log "deploy complete: runtime at $(git_rt rev-parse --short HEAD), all ${#units[@]} gateways active."
