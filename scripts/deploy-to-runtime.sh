#!/usr/bin/env bash
# deploy-to-runtime.sh — deploy this fork's `main` to the LIVE Hermes runtime.
#
# The 10 Hermes gateways run from the RUNTIME checkout (~/.hermes/hermes-agent),
# NOT this dev fork. Editing the fork does nothing until the runtime is advanced
# and the gateways reload. This script makes that the ONLY supported path, so the
# runtime can never silently drift from `main` (the CLAWD-1008 failure mode).
#
# It advances the runtime to origin/<branch> by a strict fast-forward and then
# (optionally, gated) restarts the gateways.
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
#   --no-restart         Fast-forward the runtime but do NOT restart gateways
#                        (note: file changes do not take effect until reload).
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
    -h|--help)          sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
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
  log "--no-restart: gateways NOT restarted. On-disk changes take effect only on reload."
  exit 0
fi

mapfile -t units < <(systemctl --user list-unit-files 'ai.hermes.gateway-*.service' --no-legend 2>/dev/null | awk '{print $1}' | sort)
[ "${#units[@]}" -gt 0 ] || die "no ai.hermes.gateway-*.service units found (is this the right host?)"

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
