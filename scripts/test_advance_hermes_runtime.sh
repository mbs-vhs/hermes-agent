#!/usr/bin/env bash
# Suite for advance_hermes_runtime.sh.
#
# WHAT IS UNDER TEST is not "does it pull". It is:
#
#     DOES IT REFUSE, and does it refuse BEFORE mutating?
#
# The subject advances the checkout the fleet's OAuth refresh imports from. The
# expensive failure is not "it did not advance" — it is "it advanced past something it
# could not measure", so nearly every assertion below drives a refusal path and then
# checks that HEAD DID NOT MOVE. An exit code alone cannot tell "refused" from
# "mutated, then failed"; every assertion checks both.
#
# NO LIVE RUNTIME IS TOUCHED. Each case builds a synthetic git checkout with a stub
# venv and a stub preflight under $TMPDIR, and points the subject at it via
# HERMES_RUNTIME / HERMES_PREFLIGHT.
#
# VERIFY THAT BY THE RIGHT PROBE. This paragraph used to claim `grep -c '\.hermes'`
# matches only itself. It measured 6 — and THREE of those were added by the commit that
# repeated the claim, because the DANGER marker is named `.hermes-advance-DANGER` and
# shares the prefix. The probe was matching a string the suite legitimately contains, so
# it could never have been evidence. The discriminating probe is the LIVE RUNTIME PATH,
# which is `~/.hermes/hermes-agent`:
#
#     S=$(sed 's/^[[:space:]]*#.*//' scripts/test_advance_hermes_runtime.sh)
#     printf '%s' "$S" | grep -c 'bash "$SUT"'                     -> 26 invocations
#     printf '%s' "$S" | grep -n 'bash "$SUT"' | grep -vc 'HERMES_RUNTIME=' -> 0 unguarded
#
# COMMENTS ARE STRIPPED FIRST and that is not tidiness: the un-stripped form reads 30 and 3.
# RE-MEASURED 2026-08-16 (round 9, retroactive tester gate). It read `25 / 0` and `28 / 2`
# for the merged tip fb6737022d, and BOTH were re-derived there and reproduced exactly —
# the numbers were honest. Round 9 adds ONE drive line (case 4octies), so the stripped form
# is 26 / 0. The un-stripped form went to 30 / 3 rather than 29 / 2, because THIS PARAGRAPH
# grew two more comment lines carrying the pattern — I wrote `29 and 2`, then my own next
# edit invalidated it, and only re-running the count caught it. That is the fifth
# consecutive round in which the self-counting form drifted inside the commit that
# published it, and it is why the STRIPPED figure is the one to read. USE THE REAL BINARY:
# `grep` in an agent
# session here is a harness function routing to `ugrep -G`, under which the mid-pattern
# `$` is an anchor and `grep -c 'bash "$SUT"'` reads 0 — the sentence below already says
# so, and the figures above were taken with /usr/bin/grep.
# RE-MEASURED 2026-08-16 (round 8). It read `26 and 1` for one commit, and the REASON given
# was false as well as the figures: it said "THIS PARAGRAPH contains the pattern twice".
# The drive pattern occurs in THREE comment lines across TWO paragraphs — this recipe's own
# two lines above, plus the ugrep sentence added further down, which is itself the third
# occurrence and the second unguarded one. A probe that counts its own text was the defect
# being fixed here; the fix reproduced it, and then the fix FOR the fix reproduced it again
# by adding a fourth counted line without re-running the count. Comment-stripping is what
# makes the number stable; publishing both forms is what makes the drift legible.
#
# NOT a string count over the whole file. `grep -c 'hermes/hermes-agent'` returns 2,
# not 0 — both matches are in THIS PARAGRAPH, so the published value was unattainable
# while the recipe existed: the identical self-matching-probe defect the paragraph
# replaced, committed inside the replacement. And `grep -c 'HERMES_RUNTIME='` cannot
# read ZERO — strip the override from every drive line and it still reports nonzero
# from this text, so it could not discriminate the condition it was offered to exclude.
# Counting UNGUARDED INVOCATIONS can: the subject defaults RUNTIME to the live path, so
# a drive that OMITS the override is the reachable hazard, and that is what is counted.
#
# THE CONTROL FOR THE ZERO IS THE DENOMINATOR, NOT `grep -c 'HERMES_RUNTIME='`.
# A sentence naming the latter stood here for one commit — SIX LINES under the paragraph
# proving that exact grep cannot read zero. It was orphaned by the commit that replaced
# the recipe and left standing as this header's LAST WORD on how to believe the number,
# which is the worse position: a reader who stops there is told to trust the refuted one.
# Measured: with the override stripped from EVERY drive line it still reads 3 — nonzero,
# therefore "passes" — while 26 invocations sit unguarded.
# The 26 IS the control. An instrument reporting `unguarded=0` over `invocations=0` would
# be matching nothing at all, so both figures are published together and the degenerate
# state is VISIBLE rather than inferred. That is also not hypothetical here: under the
# `grep` an agent in this repo actually gets (a harness function routing to ugrep -G,
# which treats a mid-pattern `$` as an anchor) `grep -c 'bash "$SUT"'` reads 0 while the
# refuted control reads 29 — the failure text containing the success token, live.
set -uo pipefail

SUT="${SUT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/advance_hermes_runtime.sh}"
# Hand-set, and it must be bumped in the same commit that adds an assertion. A count
# derived from the file it guards cannot catch an assertion VANISHING — e.g. into a loop
# that stopped iterating, which case 7 below is (three `mk_runtime` calls in a `for`).
# It pins cardinality, never identity: delete one and add another and this stays green.
EXPECTED_ASSERTIONS=91

PASS=0; FAIL=0; SKIP=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n       %s\n' "$1" "${2:-}"; }
# BL-3. A SKIP IS NOT A PASS. The root branch below emitted unconditional `ok` lines,
# so on any host running as uid 0 — the default in most container CI — this round's
# headline live-code fix had ZERO coverage while the suite reported full green at
# exit 0. Measured with `id -u` shimmed: deleting the DANGER latch CHECK went 69/3
# non-root and 72/0 root; never ENGAGING it went 68/4 and 72/0. A could-not-measure
# rendered as a measured zero, in the suite for the fix that exists to stop exactly
# that. The counter is separate now, so a machine consumer reading the summary line
# can see the difference a human reading stdout could always have seen.
skip() { SKIP=$((SKIP+1)); printf '  SKIP %s\n' "$1"; }
rc_is(){ [ "$1" = "$2" ] && ok "$3" || bad "$3" "rc=$1 want=$2"; }
head_is(){ # head_is <dir> <want-sha> <case>
  local got; got="$(git -C "$1" rev-parse HEAD 2>/dev/null)"
  [ "$got" = "$2" ] && ok "$3" || bad "$3" "HEAD moved: want ${2:0:9} got ${got:0:9}"
}
# HEAD IS NOT THE REVERT. `head_is` alone is why three mutations stayed green at
# 28/0: `--hard` -> `--soft` (HEAD returns, BROKEN FILES STAY ON DISK), deleting
# the readback, and `exit 5` -> `exit 0`. A revert that restores the pointer and
# leaves the tree modified is not a revert -- it is the shape that made B1 a
# data-loss bug. These two assert the DISK.
tree_clean(){ # tree_clean <dir> <case>
  local dirty; dirty="$(git -C "$1" status --porcelain 2>/dev/null)"
  [ -z "$dirty" ] && ok "$2" \
    || bad "$2" "working TREE dirty after revert (HEAD alone would pass): $dirty"
}
file_has(){ # file_has <dir> <relpath> <want-substring> <case>
  local got; got="$(cat "$1/$2" 2>/dev/null || true)"
  case "$got" in
    *"$3"*) ok "$4" ;;
    *) bad "$4" "content NOT restored in $2 (wanted substring: $3)" ;;
  esac
}

WORK="$(mktemp -d "${TMPDIR:-/var/tmp}/advtest.XXXXXX")" || { echo "mktemp failed"; exit 2; }
trap 'rm -rf "$WORK"' EXIT

# ── build a synthetic runtime: an origin, a checkout behind it, a stub venv ─────────
mk_runtime() { # mk_runtime <name> <commits-behind>
  local behind="$2" up="$WORK/$1.origin" rt="$WORK/$1"
  rm -rf "$up" "$rt"
  git init -q --bare "$up"
  git clone -q "$up" "$rt" 2>/dev/null
  git -C "$rt" config user.email t@t; git -C "$rt" config user.name t
  mkdir -p "$rt/agent" "$rt/hermes_cli"
  printf 'venv/\n' > "$rt/.gitignore"
  # Stub the two modules the real consumers import, with the real symbol names.
  cat > "$rt/agent/anthropic_adapter.py" <<'PY'
def read_claude_code_credentials(): ...
def is_claude_code_token_valid(creds): ...
def refresh_anthropic_oauth_pure(refresh_token, use_json=False): ...
def _write_claude_code_credentials(access_token, refresh_token, expires_at_ms, scopes): ...
PY
  cat > "$rt/hermes_cli/auth.py" <<'PY'
class AuthError(Exception): ...
def _save_codex_tokens(tokens=None, last_refresh=None, label=None): ...
def refresh_codex_oauth_pure(access_token, refresh_token, timeout_seconds=30): ...
PY
  git -C "$rt" add -A >/dev/null; git -C "$rt" commit -qm base
  git -C "$rt" push -q origin HEAD:main 2>/dev/null
  git -C "$rt" branch -q --set-upstream-to=origin/main 2>/dev/null || true
  # Advance the ORIGIN by N commits so the checkout is behind.
  local i=0
  while [ "$i" -lt "$behind" ]; do
    echo "$i" > "$rt/f$i"; git -C "$rt" add -A >/dev/null
    git -C "$rt" commit -qm "up$i"; i=$((i+1))
  done
  [ "$behind" -gt 0 ] && git -C "$rt" push -q origin HEAD:main 2>/dev/null
  [ "$behind" -gt 0 ] && git -C "$rt" reset -q --hard "HEAD~$behind"
  git -C "$rt" fetch -q origin 2>/dev/null
  # Stub venv: a python that really runs, so the verify step is real.
  mkdir -p "$rt/venv/bin"; ln -sf "$(command -v python3)" "$rt/venv/bin/python"
  printf '%s' "$rt"
}
mk_preflight() { # mk_preflight <path> <exit-code>
  cat > "$1" <<PY
import sys
print("stub preflight rc=$2")
sys.exit($2)
PY
}

echo "=== advance_hermes_runtime suite (NO live runtime touched) ==="

# ── 1. already current -> clean no-op, HEAD unmoved ────────────────────────────────
RT="$(mk_runtime cur 0)"; PF="$WORK/pf-ok.py"; mk_preflight "$PF" 0
BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 0 "1a already current -> rc 0"
head_is "$RT" "$BEFORE" "1b and HEAD did not move"
case "$OUT" in *"already current"*) ok "1c says so";; *) bad "1c says so" "$OUT";; esac

# ── 2. THE HAPPY PATH: behind, preflight ok -> advances AND verifies ───────────────
RT="$(mk_runtime adv 3)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
TARGET="$(git -C "$RT" rev-parse origin/main)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 0 "2a a safe advance -> rc 0"
head_is "$RT" "$TARGET" "2b HEAD reached origin/main"
case "$OUT" in *VERIFIED*) ok "2c the consumer contract was verified, not assumed";;
                *) bad "2c verification did not run" "$OUT";; esac
case "$OUT" in *"ONESHOTS"*|*"oneshots"*) ok "2d states that nothing is restarted";;
                *) bad "2d restart semantics unstated" "";; esac

# ── 3. REFUSALS — every one must leave HEAD untouched ──────────────────────────────
# 3a/b: preflight says the advance would BREAK a consumer.
RT="$(mk_runtime brk 2)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
PF1="$WORK/pf-break.py"; mk_preflight "$PF1" 1
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF1" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3a preflight says WOULD BREAK -> refuse (rc 2)"
head_is "$RT" "$BEFORE" "3b and HEAD did not move"

# 3c/d: preflight COULD NOT MEASURE. Unmeasured is not safe.
RT="$(mk_runtime unm 2)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
PF2="$WORK/pf-unmeasured.py"; mk_preflight "$PF2" 2
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF2" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3c preflight UNMEASURED -> refuse, never advance"
head_is "$RT" "$BEFORE" "3d and HEAD did not move"
case "$OUT" in *"UNMEASURED is not safe"*) ok "3e names the rule";; *) bad "3e names the rule" "";; esac

# 3f/g: the preflight is MISSING. Absent evidence is not evidence of safety — this is
# the fleet's OAuth path, so a missing check refuses rather than degrading to a pull.
RT="$(mk_runtime nopf 2)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$WORK/does-not-exist.py" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3f a MISSING preflight refuses (does not degrade to a blind pull)"
head_is "$RT" "$BEFORE" "3g and HEAD did not move"
# Pin WHICH guard fired. Deleting the explicit missing-file check still exits 2, because
# python returns rc=2 for a missing script and the "*) UNMEASURED" branch catches it —
# so 3f alone is satisfied by a different guard than it names. That is defence in depth,
# not decoration, but an assertion that cannot tell them apart is not evidence for either.
case "$OUT" in *"preflight not found"*) ok "3g2 and it names the MISSING preflight, not a generic unmeasured";;
                *) bad "3g2 wrong guard fired" "$OUT";; esac

# 3h/i: local commits = somebody is holding this deliberately.
RT="$(mk_runtime pinned 2)"
echo local > "$RT/local.txt"; git -C "$RT" add -A >/dev/null; git -C "$RT" commit -qm "local work"
BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3h a local commit reads as a PIN -> refuse"
head_is "$RT" "$BEFORE" "3i and the local commit survives"

# 3j/k: a declared pin file.
RT="$(mk_runtime pinfile 2)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
: > "$RT/.pinned-ref"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3j a declared pin file -> refuse"
head_is "$RT" "$BEFORE" "3k and HEAD did not move"

# 3l/m: the fetch fails. A ref is a CACHED CLAIM (CLAWD-3760) and a stale one makes a
# BEHIND runtime look CURRENT — the error is in the reassuring direction, so an
# unfetchable remote must refuse rather than measure against whatever is cached.
RT="$(mk_runtime nofetch 2)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
git -C "$RT" remote set-url origin "$WORK/no-such-remote"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "3l an unfetchable remote refuses (a stale ref fails GREEN)"
head_is "$RT" "$BEFORE" "3m and HEAD did not move"

# ── 4. VERIFY FAILURE -> revert, and PROVE the revert ──────────────────────────────
# The consumer contract breaks at the target ref: the advance must not stand.
RT="$(mk_runtime vfail 0)"
# Push a target that deletes a symbol the consumers import.
python3 - "$RT" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1], "hermes_cli", "auth.py")
p.write_text("class AuthError(Exception): ...\n", encoding="utf-8")
PY
git -C "$RT" add -A >/dev/null; git -C "$RT" commit -qm "break the contract"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1
git -C "$RT" fetch -q origin 2>/dev/null
BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 1 "4a a broken consumer contract -> rc 1 (applied then reverted)"
head_is "$RT" "$BEFORE" "4b and the anchor came BACK"
tree_clean "$RT" "4b2 and the working TREE came back too, not just HEAD"
file_has "$RT" "hermes_cli/auth.py" "def " \
  "4b3 and the DELETED symbol is back on disk (HEAD alone cannot see this)"
case "$OUT" in *"revert PROVEN"*) ok "4c the revert is proven, not assumed";;
                *) bad "4c revert not proven" "$OUT";; esac

# ── 4bis. B1: UNCOMMITTED WORK IS NOT DISCARDED ───────────────────────────────────
# The regression test for the data-loss defect. `git checkout -- .` destroyed an
# uncommitted edit and the run still exited 0 printing "consumers verified".
# The script's own comment claimed it refused on "a real working-tree change";
# only the local-COMMIT half was implemented. Asserted on the DISK, because the
# whole point is that HEAD never moved and so HEAD could never have caught it.
RT="$(mk_runtime dirty 3)"
BEFORE="$(git -C "$RT" rev-parse HEAD)"
MARK="do-not-destroy-$$"
printf '\n# %s\n' "$MARK" >> "$RT/agent/anthropic_adapter.py"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
# rc 2 SPECIFICALLY, not merely non-zero: the header defines 1 as "advanced then
# reverted" and 2 as "refused before mutating". A refusal returning 1 sends a
# reader hunting a rollback that never happened, and `!= 0` cannot see that.
rc_is "$RC" 2 "4bis-a uncommitted work -> rc 2 (REFUSED before mutating, not 1)"
file_has "$RT" "agent/anthropic_adapter.py" "$MARK" \
  "4bis-b and the uncommitted edit SURVIVED on disk"
head_is "$RT" "$BEFORE" "4bis-c and HEAD did not move"
case "$OUT" in *"consumers verified"*) bad "4bis-d it must NOT claim success" "$OUT";;
                *) ok "4bis-d and it did not report success";; esac

# ── 4ter. B2: IT MERGES THE SHA IT PREFLIGHTED, NOT THE REF ────────────────────────────
# The TOCTOU regression test. `origin/main` used to be resolved THREE times -- the SHA
# for TARGET, the ref again for the preflight, and the ref a THIRD time at the merge --
# and the real preflight fetches by default, so the window opened on EVERY run.
# Reproduced before the fix: preflighted one commit, LANDED another, logged the first,
# rc 0, leaving an un-preflighted commit executing.
#
# The race is simulated where it actually occurs: the preflight stub ADVANCES
# origin/main as a side effect, exactly as a fetch landing mid-run would.
RT="$(mk_runtime toctou 3)"
PREFLIGHTED="$(git -C "$RT" rev-parse origin/main)"
RACE_PF="$WORK/pf-race.py"
{
  printf '%s\n' 'import subprocess, sys, tempfile'
  # BL-2. RECORD THE ARGV THE PREFLIGHT WAS HANDED. Without this the block below
  # derives PREFLIGHTED from its OWN pre-run `rev-parse` and never observes what the
  # subject actually passed — so `--target "$TARGET"` -> `--target origin/main`, the
  # verbatim mirror of the B2 defect, survived at 45/0. Review built the differential
  # externally and measured `handed origin/main -> resolved 6fef7e3a7 ; MERGED
  # daabbe3c2`: an unmeasured commit executing. The observation belongs in the suite.
  printf '%s\n' "open('$WORK/toctou.handed','w').write(sys.argv[sys.argv.index('--target')+1] if '--target' in sys.argv else 'NO-TARGET-FLAG')"
  printf '%s\n' 'tmp = tempfile.mkdtemp()'
  printf '%s\n' "subprocess.run(['git','clone','-q','--branch','main','$WORK/toctou.origin',tmp], check=False)"
  printf '%s\n' "subprocess.run(['git','-C',tmp,'config','user.email','t@t'], check=False)"
  printf '%s\n' "subprocess.run(['git','-C',tmp,'config','user.name','t'], check=False)"
  printf '%s\n' "subprocess.run(['git','-C',tmp,'commit','-q','--allow-empty','-m','raced-in commit'], check=False)"
  printf '%s\n' "subprocess.run(['git','-C',tmp,'push','-q','origin','HEAD:main'], check=False)"
  printf '%s\n' "subprocess.run(['git','-C','$RT','fetch','-q','origin'], check=False)"
  printf '%s\n' 'print("stub preflight rc=0 (advanced origin/main as a side effect)")'
  printf '%s\n' 'sys.exit(0)'
} > "$RACE_PF"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$RACE_PF" bash "$SUT" 2>&1)"; RC=$?
RACED="$(git -C "$RT" rev-parse origin/main)"
LANDED="$(git -C "$RT" rev-parse HEAD)"
# FIXTURE CONTROL FIRST: if the stub did not actually move the ref, every assertion
# below passes vacuously and this test measures nothing.
if [ "$PREFLIGHTED" = "$RACED" ]; then
  bad "4ter-0 fixture control" "the stub did NOT advance origin/main; this test measures nothing"
else
  ok "4ter-0 control: origin/main genuinely moved during the run"
fi
rc_is "$RC" 0 "4ter-a the raced advance still exits 0"
head_is "$RT" "$PREFLIGHTED" "4ter-b HEAD is the PREFLIGHTED sha, not the raced ref"
if [ "$LANDED" = "$RACED" ]; then
  bad "4ter-c it landed an UN-PREFLIGHTED commit" "landed=${LANDED:0:9} raced=${RACED:0:9}"
else
  ok "4ter-c no un-preflighted commit was left executing"
fi
# The assertion that closes B2 rather than restating it: what the preflight was HANDED
# must be, exactly, the commit that ended up executing. A ref name here is a finding on
# its own — the preflight resolves it a SECOND time, so "preflighted" and "merged" would
# name two different commits while every rc stayed 0.
HANDED="$(cat "$WORK/toctou.handed" 2>/dev/null || echo 'STUB-DID-NOT-RECORD')"
if [ "$HANDED" = "$LANDED" ]; then
  ok "4ter-d the preflight was handed the EXACT sha that landed (not a ref it re-resolves)"
else
  bad "4ter-d preflight/merge disagree" "handed=$HANDED landed=${LANDED:0:9}"
fi
# ── 4quater. B1 PERMISSIVE DIRECTION: an upstream-deletion tree must STILL ADVANCE ──
# The refusal direction was covered (4bis); the PERMIT direction was not, and review
# measured the consequence: `UPSTREAM_DELETES=""` or breaking the deletion classifier
# turns the advancer into a PERMANENT REFUSAL and this suite stayed 38/0. That is the
# regression this whole PR exists to prevent -- a guard that refuses forever on the
# runtime that reached 10,078 commits behind is worse than the drift it replaced.
#
# The shape is the real one: the "19 dirty files" on 2026-08-12 were deletions that
# UPSTREAM ALSO MAKES. The guard must classify those as NOT-local-work and proceed.
RT="$(mk_runtime updel 0)"
# A NEUTRAL tracked file -- deliberately NOT one the consumer contract imports.
# First attempt deleted hermes_cli/auth.py and the run failed rc=1: the guard
# PERMITTED it correctly (no REFUSING) and the consumer verification then failed
# on the missing module, exactly as designed. The fixture was wrong, not the
# subject, and 4quater-c is what distinguished the two.
printf 'legacy\n' > "$RT/docs_legacy.txt"
git -C "$RT" add docs_legacy.txt >/dev/null
git -C "$RT" commit -qm "add a neutral tracked file"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
# upstream deletes it...
git -C "$RT" rm -q docs_legacy.txt
git -C "$RT" commit -qm "upstream removes the neutral file"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1
git -C "$RT" fetch -q origin 2>/dev/null
# ...and the runtime working tree has the SAME deletion pending, unstaged.
rm -f "$RT/docs_legacy.txt"
TARGET_UD="$(git -C "$RT" rev-parse origin/main)"
# FIXTURE CONTROL: the tree must actually be dirty, or this proves nothing.
if [ -z "$(git -C "$RT" status --porcelain --untracked-files=no)" ]; then
  bad "4quater-0 fixture control" "tree is CLEAN; the permissive path is not being exercised"
else
  ok "4quater-0 control: the tree carries a pending deletion"
fi
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 0 "4quater-a a deletion UPSTREAM ALSO MAKES is not local work -> advance proceeds"
head_is "$RT" "$TARGET_UD" "4quater-b and HEAD reached the target"
case "$OUT" in *REFUSING*) bad "4quater-c it must NOT refuse" "$OUT";;
                *) ok "4quater-c and it did not refuse";; esac
# ── 4quinquies. THE FAILED-REVERT DANGER PATH (rc 5) ───────────────────────────────
# I declared this "unreachable without stubbing git at the harness level". That was
# FALSE and review refuted it by construction, using a technique THIS SUITE ALREADY
# USES (case 6 replaces venv/bin/python with a wrapper). Recording the refutation
# rather than the claim, because a declared residual is the line nobody re-measures.
#
# Mechanism: the anchor contains subdir/keep.txt and the target DELETES it. The
# fixture interpreter, on the VERIFY invocation only, chmods the directory read-only
# and fails. `git reset --hard` then cannot recreate the file, so HEAD does not return
# to the anchor -- a genuinely failed revert, which is the rc 5 DANGER case.
#
# This one fixture kills TWO survivors the previous commit accepted: M24 (the DANGER
# exit 5 -> 0) and M14 (the revert readback -> true, which otherwise prints
# "revert PROVEN" over a runtime still sitting on the ADVANCED sha).
if [ "$(id -u)" = "0" ]; then
  skip "4quinquies-0 as root: chmod cannot constrain uid 0, so the revert cannot be made to fail"
  skip "4quinquies-a as root"
  skip "4quinquies-b as root"
  skip "4quinquies-d as root (the latch ENGAGING needs a real rc 5)"
else
  RT="$(mk_runtime danger 0)"
  # `other.txt` is load-bearing: it keeps subdir/ ALIVE at the target commit. First
  # attempt put only keep.txt there, the target deleted it, git does not track empty
  # directories, so subdir/ did not exist at target and the chmod hit nothing -- the
  # revert then succeeded and the fixture control caught it.
  mkdir -p "$RT/subdir"; printf 'keep\n' > "$RT/subdir/keep.txt"; printf 'stay\n' > "$RT/subdir/other.txt"
  git -C "$RT" add subdir >/dev/null; git -C "$RT" commit -qm "anchor has keep.txt + other.txt"
  git -C "$RT" push -q origin HEAD:main 2>/dev/null
  git -C "$RT" rm -q subdir/keep.txt; git -C "$RT" commit -qm "target deletes keep.txt"
  git -C "$RT" push -q origin HEAD:main 2>/dev/null
  git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
  ANCHOR_D="$(git -C "$RT" rev-parse HEAD)"
  # interpreter: succeeds for the preflight, sabotages the tree then fails on verify
  rm -f "$RT/venv/bin/python"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' "if [ \"\$1\" = \"-c\" ]; then chmod 0555 \"$RT/subdir\"; exit 3; fi"
    printf '%s\n' "exec $(command -v python3) \"\$@\""
  } > "$RT/venv/bin/python"
  chmod +x "$RT/venv/bin/python"
  OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
  chmod 0755 "$RT/subdir" 2>/dev/null || true   # let the trap clean up
  NOW_D="$(git -C "$RT" rev-parse HEAD)"
  # FIXTURE CONTROL: the revert must genuinely have FAILED, or rc 5 proves nothing.
  if [ "$NOW_D" = "$ANCHOR_D" ]; then
    bad "4quinquies-0 fixture control" "the revert SUCCEEDED; this fixture cannot reach the DANGER path"
  else
    ok "4quinquies-0 control: the revert genuinely failed (HEAD is not the anchor)"
  fi
  rc_is "$RC" 5 "4quinquies-a a FAILED revert exits 5 (DANGER), not 0 or 1"
  case "$OUT" in *"revert PROVEN"*) bad "4quinquies-b it must NOT claim PROVEN over a failed revert" "$OUT";;
                  *) ok "4quinquies-b and it did not claim PROVEN";; esac

  # ── BL-5: rc 5 MUST NOT LAUNDER TO rc 0 ON THE NEXT RUN ──────────────────────────
  # The suite built this exact state and then never invoked the script again, so the
  # whole laundering path was unobserved. Measured before the latch:
  #     RUN 1  rc=5  "DANGER: revert did NOT restore …"   HEAD left at TARGET
  #     RUN 2  rc=0  "already current — no-op"
  # HEAD equals origin/main after the failed revert, so the already-current early exit
  # fires — and that exit runs NEITHER the preflight NOR the consumer verification. The
  # fleet's OAuth path sits on a tree that FAILED verification and every later run
  # reports success. `OnFailure=` fires once and is cleared, so the single rc 5 was the
  # only signal that ever existed.
  [ -e "$RT/.hermes-advance-DANGER" ] \
    && ok "4quinquies-d the DANGER state LATCHED (an untracked marker survives reset --hard)" \
    || bad "4quinquies-d the DANGER state did not latch" "no marker at $RT/.hermes-advance-DANGER"
fi
# ── 4septies. DANGER SHAPE (b): THE ANCHOR CAME BACK AND THE TREE DID NOT ──────────
# THIS CASE EXISTS BECAUSE REVIEW MEASURED THE B3 FIX SILENTLY REMOVABLE IN ONE LINE.
# Deleting `&& [ -z "$dirty" ]` from the PROVEN branch — the exact pre-B3 shape — turned
# ZERO assertions red, non-root AND under a root shim, and so did blanking the `dirty`
# readback and disabling the shape-(b) branch outright. The subject's own comment claims
# "The tree is now part of the proof"; the clause that makes that true was proved by
# nothing, and the header's declared shape (b) was exercised by nothing. That is this
# repo's named remedy-inherits-the-disease shape: the guard added to stop `head_is`-alone
# from proving a revert was itself unpinned.
#
# MECHANISM — a smudge filter that is NOT the inverse of its clean filter. `clean` is
# identity and `smudge` APPENDS, so every checkout writes content the index does not
# hold. `git reset --hard` therefore SUCCEEDS (HEAD returns to the anchor, which is what
# separates this from 4quinquies) while leaving the working tree DIRTY. No `git` stub and
# no harness-level interception: this is git doing exactly what it is configured to do,
# with the same `git -C "$RT" config` device case 12 already uses for `merge.ff`.
#
# NOT ROOT-GATED, deliberately, and that is a strict gain over 4quinquies: this mechanism
# is filter configuration rather than file permissions, so uid 0 cannot walk past it. The
# shape-(b) branch is therefore covered on EVERY host, including the ones where
# 4quinquies degrades to four skips.
RT="$(mk_runtime dangerb 0)"
printf 'seed\n' > "$RT/smudged.txt"
printf 'smudged.txt filter=noisy\n' > "$RT/.gitattributes"
git -C "$RT" add smudged.txt .gitattributes >/dev/null; git -C "$RT" commit -qm "anchor carries a filtered file"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
printf 'seed\nadvanced\n' > "$RT/smudged.txt"
git -C "$RT" add smudged.txt >/dev/null; git -C "$RT" commit -qm "target changes the filtered file"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_B="$(git -C "$RT" rev-parse HEAD)"
# The smudge script lives under venv/, which .gitignore excludes, so it is UNTRACKED —
# `reset --hard` cannot remove it, and `status --untracked-files=no` cannot mistake it
# for the dirt this case is about.
{ printf '%s\n' '#!/usr/bin/env bash'; printf '%s\n' 'cat'; printf '%s\n' 'echo NOISE'; } > "$RT/venv/bin/smudge.sh"
chmod +x "$RT/venv/bin/smudge.sh"
git -C "$RT" config filter.noisy.clean cat
git -C "$RT" config filter.noisy.smudge "$RT/venv/bin/smudge.sh"
# Fail the VERIFY (`-c`) so the revert is triggered at all; the preflight is a separate
# executable and must still succeed, exactly as in 4quinquies.
rm -f "$RT/venv/bin/python"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'if [ "$1" = "-c" ]; then exit 3; fi'
  printf '%s\n' "exec $(command -v python3) \"\$@\""
} > "$RT/venv/bin/python"
chmod +x "$RT/venv/bin/python"
# ── PRE-RUN HALF OF THE CONTROL (round 9). ─────────────────────────────────────────
# -0 below asserts POST-run state only, so its label — "the revert RESTORED the anchor" —
# could not separate that from "HEAD NEVER MOVED". Round 8 declared this as a residual and
# left it open; this is the missing half. Measured: injecting a dirty tree before the drive
# makes the subject refuse at the working-tree guard (rc 2, no revert runs at all) and -0
# still printed `ok`.
#
# TWO assertions, because a clean pre-tree ALONE still cannot show that HEAD moved:
#   -0pre  the dirt measured after the run was produced BY the run (the shape 4quater-0 /
#          10-0 / 11-0 / 12-0 all use, and the one round 8 named as missing).
#   -0move HEAD genuinely REACHED the target during the run and came back — which is what
#          the word "RESTORED" in -0's label actually claims.
#
# THE REFLOG DELTA IS LOAD-BEARING, AND A PLAIN "the reflog contains the target" WOULD BE
# VACUOUS: measured on this fixture, the setup's own `reset -q --hard HEAD~1` leaves the
# target in the reflog (count 1) BEFORE the subject is ever invoked, so that form is
# satisfied by a run that never happened. Only entries the RUN appended are counted.
if [ -z "$(git -C "$RT" status --porcelain --untracked-files=no)" ]; then
  ok "4septies-0pre control: the tree is CLEAN before the drive, so the dirt below is the RUN's"
else
  bad "4septies-0pre control" "tree was ALREADY dirty pre-drive — -0 cannot tell a revert from a refusal"
fi
TARGET_B="$(git -C "$RT" rev-parse origin/main)"
PRE_REFLOG_B="$(git -C "$RT" reflog --format=%H 2>/dev/null | wc -l)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
NOW_B="$(git -C "$RT" rev-parse HEAD)"
DIRTY_B="$(git -C "$RT" status --porcelain --untracked-files=no 2>/dev/null)"
POST_REFLOG_B="$(git -C "$RT" reflog --format=%H 2>/dev/null | wc -l)"
NEW_N_B=$((POST_REFLOG_B - PRE_REFLOG_B))
NEW_REFLOG_B="$(git -C "$RT" reflog --format=%H 2>/dev/null | head -n "$NEW_N_B")"
if [ "$NEW_N_B" -ge 2 ] && printf '%s\n' "$NEW_REFLOG_B" | grep -qxF "$TARGET_B"; then
  ok "4septies-0move control: HEAD genuinely REACHED the target during the run and came back"
else
  bad "4septies-0move control" \
      "the run appended $NEW_N_B reflog entries and none was the target ${TARGET_B:0:9} — HEAD may never have moved, so -0 is measuring a REFUSAL, not a revert"
fi
# FIXTURE CONTROL, and it must assert BOTH halves. HEAD back at the anchor is what makes
# this shape (b) rather than 4quinquies' shape (a); a dirty tree is what makes it a
# finding at all. Either half alone would let this case pass over the wrong state.
if [ "$NOW_B" = "$ANCHOR_B" ] && [ -n "$DIRTY_B" ]; then
  ok "4septies-0 control: the revert RESTORED the anchor and the tree is genuinely dirty"
else
  bad "4septies-0 fixture control" "need HEAD==anchor AND a dirty tree; HEAD=$NOW_B anchor=$ANCHOR_B dirty=[$DIRTY_B]"
fi
rc_is "$RC" 5 "4septies-a anchor restored + DIRTY tree exits 5 (DANGER), not 1"
case "$OUT" in *"revert PROVEN"*) bad "4septies-b it must NOT claim PROVEN over a dirty tree" "$OUT";;
                *) ok "4septies-b and it did not claim the working tree was clean";; esac
case "$OUT" in *"WORKING TREE IS DIRTY"*) ok "4septies-c it names the tree as the reason, not the pointer";;
                *) bad "4septies-c the shape-(b) message did not print" "$OUT";; esac
[ -f "$RT/.hermes-advance-DANGER" ] \
  && ok "4septies-d shape (b) LATCHES, so the next run cannot launder it" \
  || bad "4septies-d shape (b) did not latch" "no marker at $RT/.hermes-advance-DANGER"
# THE MARKER'S MACHINE-READABLE `shape:` FIELD (round 9). Round 8 declared this unpinned in
# BOTH branches and measured it: `latch_danger "tree-not-restored"` -> `"anchor-not-restored"`
# reddened ZERO, non-root and under a root shim. -d asserts only that the marker EXISTS, so a
# marker that misattributes the failure to the pointer while the tree is the actual cause reads
# as full coverage. This is the field a triaging human reads out of .hermes-advance-DANGER;
# 4octies-c pins the same field for shape (a).
file_has "$RT" ".hermes-advance-DANGER" "shape   : tree-not-restored" \
  "4septies-e and the marker attributes it to the TREE (shape: tree-not-restored)"

# ── 4octies. DANGER SHAPE (a) — ON EVERY HOST, AND ITS ATTRIBUTION PINNED ──────────
# ROUND 8 DECLARED TWO RESIDUALS HERE AND CLOSED NEITHER; this case closes both. Measured
# on the merged tip before this case existed, one mutation at a time, non-root AND under an
# `id -u`->0 shim:
#
#   shape (a)'s message text rewritten to shape (b)'s wording   -> 0 red
#   shape (a)'s `latch_danger` shape argument swapped            -> 0 red
#
# The first is the dangerous one. Shape (a) is HEAD NOT BACK AT THE ANCHOR — what the exit-code
# table calls an UNKNOWN state, strictly worse than a dirty tree — and once both shapes emit the
# same string, the discriminator 4septies-c depends on has stopped existing. 4quinquies is the
# shape-(a) case and asserts only rc 5, the ABSENCE of "revert PROVEN", and marker existence;
# nothing pins its wording, and it is ROOT-GATED, so on a uid-0 host shape (a) had NO coverage
# whatsoever — including the M14/M24 killers 4quinquies is credited with.
#
# MECHANISM, AND WHY IT SURVIVES uid 0 — MEASURED, NOT ASSERTED. 4quinquies fails the revert by
# `chmod 0555` on a directory, and DAC is exactly what uid 0 walks past, which is why it is
# gated. This case plants `.git/index.lock` instead: git takes that lock with O_CREAT|O_EXCL and
# EEXIST is not a permission, so CAP_DAC_OVERRIDE buys nothing. `git reset --hard` then fails
# BEFORE it moves HEAD, so HEAD is stranded on the advanced target — a genuinely failed revert.
# Measured at BOTH uids in a user namespace (`unshare -r`), which is the both-answers control
# for the claim, since a probe that reported "the revert failed" unconditionally would prove
# nothing:
#
#   uid=1000  chmod 0555   reset rc=128   REVERT FAILED   (the mechanism 4quinquies uses)
#   uid=0     chmod 0555   reset rc=0     REVERT SUCCEEDED  <- the gate is real, and this is why
#   uid=1000  index.lock   reset rc=128   REVERT FAILED
#   uid=0     index.lock   reset rc=128   REVERT FAILED     <- uid 0 does not walk past it
#
# So this case is NOT root-gated, and shape (a) is covered on every host 4quinquies degrades on.
RT="$(mk_runtime dangera 0)"
printf 'anchor\n' > "$RT/keep.txt"
git -C "$RT" add keep.txt >/dev/null; git -C "$RT" commit -qm "anchor content"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
printf 'advanced\n' > "$RT/keep.txt"
git -C "$RT" add keep.txt >/dev/null; git -C "$RT" commit -qm "target changes it"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_A="$(git -C "$RT" rev-parse HEAD)"
TARGET_A="$(git -C "$RT" rev-parse origin/main)"
# B4 AGAIN: `rm -f` FIRST. `{ ... } >` FOLLOWS the symlink mk_runtime created, so without this
# the write truncates the HOST interpreter and builds a wrapper that execs itself.
rm -f "$RT/venv/bin/python"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' "if [ \"\$1\" = \"-c\" ]; then : > \"$RT/.git/index.lock\"; exit 3; fi"
  printf '%s\n' "exec $(command -v python3) \"\$@\""
} > "$RT/venv/bin/python"
chmod +x "$RT/venv/bin/python"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rm -f "$RT/.git/index.lock"       # release it so the assertions below and the trap can work
NOW_A="$(git -C "$RT" rev-parse HEAD)"
# FIXTURE CONTROL, BOTH HALVES. "HEAD is not the anchor" alone is also true of a fixture that
# simply never advanced; requiring HEAD == the TARGET is what says the advance LANDED and the
# revert then failed, which is the only state shape (a) is about.
if [ "$NOW_A" != "$ANCHOR_A" ] && [ "$NOW_A" = "$TARGET_A" ]; then
  ok "4octies-0 control: the revert genuinely FAILED — HEAD is stranded on the advanced target"
else
  bad "4octies-0 fixture control" \
      "need HEAD==target!=anchor; HEAD=${NOW_A:0:9} anchor=${ANCHOR_A:0:9} target=${TARGET_A:0:9}"
fi
rc_is "$RC" 5 "4octies-a a FAILED revert exits 5 (DANGER) on EVERY host, uid 0 included"
# THE ATTRIBUTION ASSERTION, and the forbidden token is tested FIRST deliberately: a message
# carrying both wordings must fail, not pass on the strength of the half that survived.
case "$OUT" in
  *"WORKING TREE IS DIRTY"*)
    bad "4octies-b it must name the POINTER, not the tree" \
        "shape (a) printed shape (b)'s wording — an UNKNOWN-state failure reported as a dirty tree" ;;
  *"revert did NOT restore"*)
    ok "4octies-b it names the POINTER as the cause (revert did NOT restore), not the tree" ;;
  *)
    bad "4octies-b the shape-(a) message did not print" "$OUT" ;;
esac
file_has "$RT" ".hermes-advance-DANGER" "shape   : anchor-not-restored" \
  "4octies-c and the marker attributes it to the POINTER (shape: anchor-not-restored)"
# HOST-INDEPENDENT M14/M24 COVER. 4quinquies-b is the only other assertion that kills
# "readback -> true" and "exit 5 -> exit 0", and it is skipped as root.
case "$OUT" in *"revert PROVEN"*) bad "4octies-d it must NOT claim PROVEN over a failed revert" "$OUT";;
                *) ok "4octies-d and it did not claim PROVEN, on a host where 4quinquies-b is skipped";; esac

# MEASURED (round 9), one mutation at a time against a same-pass PASS=91 FAIL=0 SKIP=0
# control at the same scratch path, each verified APPLIED by sha256 and `bash -n`'d, and the
# unmutated subject first run through the identical scratch path to prove the harness is
# neutral (assertion lines byte-identical to the in-repo run). Non-root / under an
# `id -u`->0 shim:
#
#   shape (a) message text -> shape (b)'s wording          -> 1 / 1 red  (4octies-b)
#   shape (a) message text -> BOTH wordings                -> 1 / 1 red  (4octies-b)
#   latch_danger "anchor-not-restored" -> "tree-not-..."   -> 1 / 1 red  (4octies-c)
#   latch_danger "tree-not-restored" -> "anchor-not-..."   -> 1 / 1 red  (4septies-e)
#   shape (a) `exit 5` -> `exit 0`                         -> 2 / 1 red  (4octies-a)
#   revert readback -> `now="$ANCHOR"`                     -> 7 / 4 red  (4octies a/b/c/d)
#
# Every one of those six read 0 red UNDER THE ROOT SHIM before this case existed; the last
# two read 0 red under the shim while the suite exited 0.
#
# DECLARED RESIDUAL (§19.5) — WHAT IS STILL SILENTLY REMOVABLE HERE, MEASURED, WITH COUNTS:
#   * 4octies-b tests the FORBIDDEN token FIRST so a message carrying BOTH wordings fails
#     rather than passing on the surviving half. Swapping the two case arms reds ZERO
#     (91/0/0 both uids), because no fixture makes the subject emit both — only the
#     deliberate mutation above does, and a suite does not carry its own mutants. The
#     ordering is load-bearing against that mutant and unpinned without it. Not layered
#     further: the pin for the ordering would need its own pin.
#   * `rm -f "$RT/.git/index.lock"` after the drive reds ZERO if deleted (91/0/0). It is
#     hygiene, not coverage — nothing below it needs the index — and is kept so a future
#     assertion in this case cannot be poisoned by a lock the fixture planted. §19.2.
#   * The mechanism claim ("uid 0 does not walk past `index.lock`") is pinned by 4octies-0,
#     which reds if the revert ever succeeds. What is NOT pinned is that this suite was
#     actually RUN as uid 0: the root shim fakes `id -u` only. The uid-0 rows in the table
#     above were measured under `unshare -r`, out of band, and nothing in this file re-runs
#     them.

# MEASURED, one mutation at a time against a same-pass control at the same path, each
# verified APPLIED by hash and `bash -n`'d, subject restored byte-identical under a sha256
# pin. NON-ROOT and under a root shim (`id -u` -> 0) the figures are IDENTICAL, which is the
# claim above about host-independence, measured rather than asserted.
#
# ROUND 8 SHIPPED 4 / 4 / 1 AGAINST A PASS=83 CONTROL. Round 9's retroactive tester gate
# re-derived all three at that tip and reproduced them exactly — and then INVALIDATED them
# by adding 4septies-e, which reds on all three. Against the PASS=91 control they are:
#
#   delete `&& [ -z "$dirty" ]` from the PROVEN branch (the pre-B3 shape)  -> 5 red (was 4)
#   `dirty="$(git … status …)"` -> `dirty=""`                              -> 5 red (was 4)
#   the shape-(b) `if` -> `if false`                                       -> 2 red (was 1)
#
# All three read ZERO before this case existed. This is the file's own rule applied to
# itself — "adding an assertion invalidates every sibling figure exactly as changing a
# fixture does" — and it is the third round in which that rule was broken by the commit
# that restated it, so the figures are re-derived here rather than left to be inferred.
#
# THE 2 IS DECLARED RATHER THAN LEFT TO LOOK WEAK. Disabling the shape-(b) branch does
# not fall through to success — it falls through to shape (a), which ALSO exits 5 and
# ALSO latches. So rc, the absence of "revert PROVEN" and the marker are all RESCUED BY
# ACCIDENT, and the only thing that discriminates is WHICH CAUSE IS NAMED: the tree, or
# the pointer. That is the repo's own rule — when an rc is rescued downstream by
# accident, pin the ATTRIBUTION — and 4septies-c and -e are the two assertions that do it
# (the human-readable message and the marker's machine-readable field). A reader who
# "strengthens" either into an rc check would delete the only coverage this mutation has.
#
# ROUND-8 RESIDUAL — CLOSED BY ROUND 9 (retroactive tester gate on the merged tip). Kept as
# a record rather than deleted, because the figures are the evidence that 4septies-e and
# 4octies below are not decoration. Round 8's declaration was accurate: independently
# re-measured at fb6737022d, one mutation at a time, non-root AND under an `id -u`->0 shim,
# each verified APPLIED by sha256 and `bash -n`:
#
#   shape (a)'s message text rewritten to shape (b)'s wording  -> 0 red   (now 4octies-b: 1)
#   latch_danger "tree-not-restored" -> "anchor-not-restored"   -> 0 red   (now 4septies-e: 1)
#   latch_danger "anchor-not-restored" -> "tree-not-restored"   -> 0 red   (now 4octies-c: 1)
#
# The first is the worse one. A genuinely FAILED revert — HEAD not back at the anchor, which
# the exit-code table calls an UNKNOWN state and is strictly more dangerous than a dirty
# tree — would tell the operator the working tree is dirty, and once both shapes emit the
# same string the discriminator 4septies-c relies on has stopped existing. The second leaves
# the marker's machine-readable `shape:` field — the thing a triaging human reads out of
# .hermes-advance-DANGER — unpinned in both branches.
#
# Round 8 named the remedy exactly right — "closing (a) means giving 4quinquies a message
# assertion plus a non-permission-based fixture so it survives root" — and round 9 did that
# as case 4octies rather than by editing 4quinquies, so the chmod path keeps its own
# coverage where it works. The cost of leaving it open was measured on the merged tip, and
# it is larger than "wording": on a uid-0 host BOTH mutations 4quinquies is credited with
# killing scored RED=0 and the suite exited 0 —
#
#   shape (a) `exit 5` -> `exit 0`   (M24)   root RED=0 rc=0   (now 4octies-a: RED=1 rc=1)
#   revert readback -> `now="$ANCHOR"` (M14) root RED=0 rc=0   (now 4octies a/b/c/d: RED=4)
#
# — i.e. a subject that prints "revert PROVEN" over a runtime still sitting on the ADVANCED
# sha passed the whole suite at rc 0 on any uid-0 host. The `UNMEASURED:` line was printed,
# but rc is what a timer or a CI gate reads.
#
# THREE FURTHER RESIDUALS from the same round, declared rather than silently carried:
#   * 4septies-0's LABEL says "the revert RESTORED the anchor" and it measures POST-run
#     state, which cannot separate that from "HEAD never moved". CLOSED round 9 by
#     4septies-0pre + 4septies-0move. Reproduced first: injecting a dirty tree before the
#     drive makes the subject refuse at the working-tree guard (rc 2, no revert runs) and
#     -0 STILL PRINTS `ok`. It fails loudly elsewhere (-a/-c/-d/-e red), so it was a label
#     overstating its assertion rather than a false green — and -0 remains `ok` under that
#     fixture even now, which is why the two new assertions carry the claim instead of it.
#   * `{ ... } > "$RT/venv/bin/python"` FOLLOWS the symlink mk_runtime created. Deleting the
#     `rm -f` above it truncates the host interpreter and builds a wrapper that execs
#     itself — an exec loop, not a failing assertion. Same shape as the B4 incident this
#     file already records; the `rm -f` is present and correct, and now says why.
#   * Two added fixture lines are INERT: the first `push` (the later one already carries the
#     anchor commit) and `filter.noisy.clean cat` (git already uses identity with no clean
#     filter). Blanking either leaves 91/0/0. Kept as statements of intent per 19.2, and
#     named here so neither reads as coverage.

# ── 5. --dry-run mutates nothing on the path that WOULD mutate ─────────────────────
RT="$(mk_runtime dry 3)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" --dry-run 2>&1)"; RC=$?
rc_is "$RC" 0 "5a --dry-run on a behind runtime -> rc 0"
head_is "$RT" "$BEFORE" "5b and HEAD did not move"


# ── 6. THE PROVENANCE ASSERT: imports must come FROM THE RUNTIME ───────────────────
# `cd /` closes the CWD path. It does NOT close site-packages: the real venv has
# hermes-agent installed, so an import can succeed from THERE while the tree being
# advanced is broken — a verified-looking advance over a broken runtime. Measured live:
# before `cd /`, the check passed against a deliberately broken tree because it had
# loaded a sibling worktree's copy. This case reproduces the same shape via the
# interpreter rather than the CWD.
RT="$(mk_runtime prov 0)"
GOOD="$WORK/goodpkgs"; mkdir -p "$GOOD/hermes_cli" "$GOOD/agent"
cp "$RT/hermes_cli/auth.py" "$GOOD/hermes_cli/auth.py"
cp "$RT/agent/anthropic_adapter.py" "$GOOD/agent/anthropic_adapter.py"
# DELETE the module from the tree at the target, rather than breaking it. This is the
# case the assert exists for: `sys.path.insert(0, RUNTIME)` puts the tree FIRST, so a
# BROKEN module in the tree still wins and the import fails anyway — measured, that
# version of this test went green with the assert removed, i.e. it was pinning sys.path
# ordering rather than provenance. When the module is ABSENT from the tree, the import
# silently resolves from site-packages and succeeds over a runtime that does not contain
# the code at all.
rm -f "$RT/hermes_cli/auth.py"
git -C "$RT" add -A >/dev/null; git -C "$RT" commit -qm "break tree only"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
# B4. `rm -f` FIRST. mk_runtime creates venv/bin/python as a SYMLINK to the real
# interpreter (line ~86), and `cat >` FOLLOWS a symlink — so this write did not
# replace the fixture's stub, it truncated and overwrote the HOST's python3
# (readlink -f resolves it to /usr/bin/python3.14). A test suite that can destroy
# the system interpreter is not a declared design limit; it is one line.
rm -f "$RT/venv/bin/python"
# An interpreter that injects the good copy — i.e. the site-packages situation.
cat > "$RT/venv/bin/python" <<PYW
#!/usr/bin/env bash
exec env PYTHONPATH="$GOOD" "$(command -v python3)" "\$@"
PYW
chmod +x "$RT/venv/bin/python"
BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
# CORRECTED 2026-08-16. The paragraph that stood here declared these two as NOT
# isolating the provenance assert, citing "the suite stays 28/0". That number is
# from a SUPERSEDED commit and the conclusion is now INVERTED — measured on this
# tree: deleting the provenance assert gives 89/2 (6a and 6b red — it read 36/2, a
# count from a 38-assertion tip), and weakening
# it to `.startswith('/')` also gives 89/2.
# RE-CORRECTED 2026-08-16 by review. The figures above read 75/2 for one commit, which
# is ARITHMETICALLY IMPOSSIBLE beside EXPECTED_ASSERTIONS=78 — 75+2=77 — and that is a
# check needing no measurement at all. `git log -S` places the paragraph in the SAME
# commit that bumped the pin 77 -> 78, so the number was taken pre-bump and republished
# under an explicit date-stamped "measured on this tree" label. The paragraph exists to
# retire a stale number and shipped one. RE-MEASURE AFTER AN ASSERTION IS ADDED, not
# only after a code change: a count is meaningless without the control it was taken
# against, and adding an assertion invalidates every sibling figure exactly as changing
# a fixture does.
#
# AND THAT SENTENCE WAS BROKEN BY THE COMMIT THAT WROTE IT — round 8, measured. It shipped
# `76/2` while adding five assertions (78 -> 83), so `76+2=78` disagreed with its own pin
# by five: the identical arithmetic self-refutation the paragraph above exists to retire
# (`75+2=77` against a pin of 78), one layer out, six lines under the rule forbidding it.
# `76/2` was CORRECT for the 78-assertion parent and was invalidated by its own commit.
# ROUND 9 IS THE FOURTH INSTANCE OF THE SAME SHAPE AND IT WAS CAUGHT BY RE-RUNNING, NOT BY
# READING: the merged tip's `81/2` was re-derived at fb6737022d and reproduced EXACTLY, and
# was then invalidated by round 9's own eight added assertions. FAIL stays 2 (6a, 6b — the
# provenance assert's blast radius did not change); PASS moves 81 -> 89. Publishing the pair
# rather than the FAIL alone is what makes that legible.
# The figure for this tip is 89/2, re-derived here rather than adjusted. THE LESSON IS NOT
# THE NUMBER: a rule written into a file does not apply itself, and the author who writes
# it is the one least likely to re-read it in the same sitting. Repairing the case-6 fixture (the
# `cat >` that followed a symlink and truncated the host interpreter) is what
# restored real coverage here; the fixture was writing over /usr/bin/python3.14
# instead of the stub, so case 6 had been exercising nothing.
#
# The prior review named that paragraph as a durable false explanation that
# "froze the bug in place". The one-line bug was fixed and the explanation it
# justified was left behind verbatim, which is the same defect one layer out.
#
# The assert IS load-bearing; that is measured directly rather than through this suite:
#     $ cd / && PYTHONPATH=<good> python3 -c "import sys; sys.path.insert(0,'<rt>');
#                 import hermes_cli.auth as m; print(m.__file__)"
#     -> <good>/hermes_cli/auth.py        i.e. resolved from OUTSIDE the runtime
# `sys.path.insert(0, RUNTIME)` puts the tree first, so a BROKEN module in the tree still
# wins and fails the import on its own. The assert only bites when the module is ABSENT
# from the tree, where the import silently succeeds from site-packages over a runtime
# that does not contain the code at all — which is the real shape, since the live venv
# has hermes-agent installed.
rc_is "$RC" 1 "6a an advance whose module is missing from the tree does not stand"
head_is "$RT" "$BEFORE" "6b and it was reverted"

# ── 7. BL-7: AN UNRECOGNISED ARGUMENT IS NOT A LIVE RUN ────────────────────────────
# `[ "${1:-}" = "--dry-run" ] && DRY_RUN=1` had no else, so everything that was not
# exactly that string fell through and ADVANCED FOR REAL. Measured: `--dryrun`,
# `--dry_run` and `-n` each rc 0 having mutated the fleet's OAuth path — near-misses of
# the one flag whose entire purpose is "do not mutate", i.e. the typo a human makes at
# the moment they are being careful. `5a` cannot see this: it passes the flag correctly.
for _arg in --dryrun -n --dry_run; do
  RT="$(mk_runtime "arg$(printf '%s' "$_arg" | tr -dc 'a-z')" 3)"
  BEFORE="$(git -C "$RT" rev-parse HEAD)"
  OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" "$_arg" 2>&1)"; RC=$?
  if [ "$RC" = "2" ] && [ "$(git -C "$RT" rev-parse HEAD)" = "$BEFORE" ]; then
    ok "7 '$_arg' is REFUSED (rc 2) and HEAD did not move"
  else
    bad "7 '$_arg' is REFUSED (rc 2) and HEAD did not move" \
        "rc=$RC head=$(git -C "$RT" rev-parse --short HEAD) before=${BEFORE:0:9}"
  fi
done
# A trailing correct flag must not rescue a typo'd first one, and vice versa: the whole
# argv is refused, not the first token.
RT="$(mk_runtime argextra 3)"; BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" --dry-run --and-also 2>&1)"; RC=$?
rc_is "$RC" 2 "7d a trailing unrecognised argument is refused, not ignored"

# ── 8. BL-3 ROOT CAUSE: THE PIN GUARD IS MEASURED AGAINST A FRESH REF ──────────────
# The AHEAD (pin-vs-drift) guard ran ABOVE the fetch, so the one check standing between
# a timer and somebody's deliberately-held checkout was answered from a CACHED CLAIM —
# the identical defect the fetch's own comment names, twenty lines away. The reachable
# consequence is not a stale count: after an upstream force-push to a divergent history
# the stale ref reports AHEAD=0, the fetch then makes HEAD genuinely divergent, and only
# `merge --ff-only` is left refusing. Review measured `--ff-only` -> `--no-edit` at 45/0,
# producing rc 0, "VERIFIED", and a MERGE COMMIT THAT EXISTS NOWHERE UPSTREAM.
#
# THE FIXTURE IS A REWRITE, NOT AN ORPHAN, AND THAT IS LOAD-BEARING. The first version
# force-pushed an UNRELATED history, which `git merge` refuses on its own ("refusing to
# merge unrelated histories") — so it exercised the pin guard but could never reproduce
# the shape the downstream guard fails open on, and measured rc 1 for BOTH forms of the
# merge. A realistic force-push SHARES an ancestor. Measured across the 2x2:
#
#   shipped                     rc 2  HEAD unmoved   1 parent
#   --ff-only -> --no-edit      rc 2  HEAD unmoved   1 parent
#   AHEAD above the fetch       rc 1  HEAD unmoved   1 parent    (--ff-only holds it shut)
#   BOTH                        rc 0  HEAD MOVED     2 PARENTS   VERIFIED, and the commit
#                                                                exists NOWHERE upstream
#
# THE ROUND-4 CONCLUSION DRAWN FROM THAT TABLE — "`--ff-only` is KNOWINGLY REDUNDANT
# (§19.2)" — WAS FALSE AND IS WITHDRAWN. It was inferred from `--no-edit` reading RED=0
# in the row above, and that 0 is an artefact of the fixture: every fixture here sets
# only `user.email`/`user.name`, so the whole table was measured under DEFAULT `merge.ff`
# — the one configuration the redundancy depends on. §19.7(a), committed by the author
# in the act of declaring coverage. Review refuted it twice (a race at the ref-vs-sha
# window, and `merge.ff=false` with no race at all), and a §19.2 declaration is a licence
# to DELETE, so publishing a false one is worse than leaving the guard unexplained.
#
# `--ff-only` is LOAD-BEARING. Case 12 below pins the half a fixture can reach.
RT="$(mk_runtime forced 0)"
echo a > "$RT/a.txt"; git -C "$RT" add -A >/dev/null; git -C "$RT" commit -qm "A (the commit upstream will rewrite away)"
git -C "$RT" push -q origin HEAD:main 2>/dev/null; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_F="$(git -C "$RT" rev-parse HEAD)"
FDIR="$WORK/forced.push"
# `--branch main` is load-bearing and its absence made this whole case VACUOUS on its
# first run: the bare origin's HEAD names a branch that does not exist there, so a plain
# clone checks nothing out, `--orphan` has an empty index, the commit fails "nothing to
# commit", the push fails, and the remote never diverges. Both assertions below then
# measured a runtime that was simply current. The suite already records this trap for
# the 4ter stub; it was rebuilt here anyway.
git clone -q --branch main "$WORK/forced.origin" "$FDIR" 2>/dev/null
git -C "$FDIR" config user.email t@t; git -C "$FDIR" config user.name t
git -C "$FDIR" reset -q --hard HEAD~1                    # back to the shared ancestor
echo b > "$FDIR/b.txt"; git -C "$FDIR" add -A >/dev/null
git -C "$FDIR" commit -qm "A-prime — the rewrite that drops A"
git -C "$FDIR" push -q -f origin HEAD:main 2>/dev/null
# FIXTURE CONTROL, BOTH HALVES. The first version checked only that the CACHED ref reads
# AHEAD=0 — which is also true when nothing happened at all, so it passed green over an
# inert fixture. A control must distinguish "the window is open" from "the setup did
# nothing", and only the second half does that.
CACHED_AHEAD="$(git -C "$RT" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
REMOTE_TIP="$(git -C "$WORK/forced.origin" rev-parse main 2>/dev/null || echo none)"
if [ "$CACHED_AHEAD" = "0" ] && [ "$REMOTE_TIP" != "$ANCHOR_F" ] && [ "$REMOTE_TIP" != "none" ]; then
  ok "8-0 control: the remote genuinely diverged AND the cached ref still reads AHEAD=0"
else
  bad "8-0 control" "window not open: cached_ahead=$CACHED_AHEAD remote_tip=${REMOTE_TIP:0:9} anchor=${ANCHOR_F:0:9}"
fi
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "8a a force-push that DIVERGES is caught by the pin guard (rc 2), not by --ff-only"
head_is "$RT" "$ANCHOR_F" "8b and HEAD did not move"
case "$OUT" in *"deliberate pin"*) ok "8c and it refused as a PIN, naming the measurement that fired";;
                *) bad "8c wrong guard fired" "$OUT";; esac
# The OUTCOME assertion, not the rc one: what the 2x2 above produces when both guards
# are defeated is a MERGE COMMIT that exists nowhere upstream and was never preflighted.
# rc and HEAD-position assertions are satisfied by a revert; this one is not.
_P=$(( $(git -C "$RT" rev-list --parents -n1 HEAD 2>/dev/null | wc -w) - 1 ))
[ "$_P" -le 1 ] && ok "8d and HEAD is not a MERGE COMMIT synthesised from a divergent remote" \
                || bad "8d HEAD is a merge commit" "parents=$_P — this commit exists nowhere upstream"

# ── 9. BL-3b: A FAILED MERGE MUST REVERT, NOT REPORT SUCCESS ───────────────────────
# Dropping the revert on merge failure measured 45/0, rc 0, "consumers verified" — with
# HEAD NEVER MOVED. A permanent green over a runtime that never advanced is the worst
# shape available: the drift this whole script exists to end, reported as health.
# Reached without the stale-ref window: an UNTRACKED file colliding with a path the
# merge must create. `checkout -- .` deliberately does not remove untracked files, so
# the collision survives to the merge.
RT="$(mk_runtime collide 0)"
printf 'upstream\n' > "$RT/collide.txt"
git -C "$RT" add collide.txt >/dev/null; git -C "$RT" commit -qm "upstream adds collide.txt"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_C="$(git -C "$RT" rev-parse HEAD)"
printf 'local untracked\n' > "$RT/collide.txt"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
case "$OUT" in *"fast-forward failed"*) ok "9-0 control: the merge genuinely failed (not a vacuous pass)";;
                *) bad "9-0 control" "the merge did not fail; this case measures nothing: $OUT";; esac
rc_is "$RC" 1 "9a a failed merge exits 1 (applied-then-reverted), never 0"
head_is "$RT" "$ANCHOR_C" "9b and HEAD is back at the anchor"
case "$OUT" in *"revert PROVEN"*) ok "9c and the revert was proven";;
                *) bad "9c the revert did not run or was not proven" "$OUT";; esac
case "$OUT" in *"consumers verified"*) bad "9d it must NOT report success over a merge that failed" "$OUT";;
                *) ok "9d and it claimed no success";; esac

# ── 10. BL-4a: THE DELETION CLASSIFIER, PINNED IN THE PERMISSIVE DIRECTION ─────────
# §19.7(a): every existing fixture was written in the shape the guard already matches,
# so `if [ "$_st" = " D" ] || [ "$_st" = "D " ]` -> `if true` survived at 45/0 — the
# suite pinned only over-REFUSING. With the classifier defeated, a MODIFIED file whose
# path merely appears in the upstream-deletion list takes the exemption, and the
# operator's edit is destroyed under a clean rc 0. That is B1 restored.
RT="$(mk_runtime moddel 0)"
printf 'legacy\n' > "$RT/docs_legacy.txt"
git -C "$RT" add docs_legacy.txt >/dev/null; git -C "$RT" commit -qm "add a file upstream will delete"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" rm -q docs_legacy.txt; git -C "$RT" commit -qm "upstream deletes it"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_MD="$(git -C "$RT" rev-parse HEAD)"
MARK2="operator-edit-$$"
printf '# %s\n' "$MARK2" >> "$RT/docs_legacy.txt"     # MODIFIED — deliberately not deleted
if [ -n "$(git -C "$RT" status --porcelain --untracked-files=no)" ]; then
  ok "10-0 control: the tree carries a MODIFICATION to a file upstream deletes"
else
  bad "10-0 control" "tree is clean; this case measures nothing"
fi
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "10a a MODIFIED file is local work even where upstream deletes that path"
file_has "$RT" "docs_legacy.txt" "$MARK2" "10b and the operator's edit SURVIVED on disk"
head_is "$RT" "$ANCHOR_MD" "10c and HEAD did not move"

# ── 11. BL-4b: THE EXEMPTION IS WHOLE-LINE, AND THE `-x` IS LOAD-BEARING ───────────
# `grep -qxF` -> `grep -qF` survived at 45/0. Substring matching exempts a deletion the
# operator made because some OTHER path upstream deletes CONTAINS it: the operator
# deletes `notes.txt`, upstream deletes `old/notes.txt.bak`, and `checkout -- .` then
# silently restores the operator's deletion under a clean rc 0.
RT="$(mk_runtime substr 0)"
mkdir -p "$RT/old"; printf 'notes\n' > "$RT/notes.txt"; printf 'bak\n' > "$RT/old/notes.txt.bak"
git -C "$RT" add -A >/dev/null; git -C "$RT" commit -qm "add notes.txt and old/notes.txt.bak"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" rm -q old/notes.txt.bak; git -C "$RT" commit -qm "upstream deletes ONLY the .bak"
git -C "$RT" push -q origin HEAD:main 2>/dev/null
git -C "$RT" reset -q --hard HEAD~1; git -C "$RT" fetch -q origin 2>/dev/null
ANCHOR_SS="$(git -C "$RT" rev-parse HEAD)"
rm -f "$RT/notes.txt"     # the OPERATOR deletes a file upstream KEEPS
if [ -n "$(git -C "$RT" status --porcelain --untracked-files=no)" ]; then
  ok "11-0 control: the tree carries the operator's own deletion"
else
  bad "11-0 control" "tree is clean; this case measures nothing"
fi
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "11a a deletion upstream does NOT make is local work, even as a substring of one it does"
if [ -e "$RT/notes.txt" ]; then
  bad "11b the operator's deletion SURVIVED" "notes.txt was silently restored"
else
  ok "11b and the operator's deletion survived (the file is still absent)"
fi
head_is "$RT" "$ANCHOR_SS" "11c and HEAD did not move"

# ── 4sexies. THE LATCH'S REFUSAL, PINNED ON EVERY HOST ─────────────────────────────
# 4quinquies-e/f/g used to live inside the root-gated block, so on a root host the
# round's headline fix was covered by nothing while the suite read full green. Only
# ENGAGING the latch needs a genuinely failed revert (hence -d stays gated); the
# REFUSAL it produces needs only the marker to exist, and that is host-independent.
RT="$(mk_runtime latched 0)"
BEFORE="$(git -C "$RT" rev-parse HEAD)"
printf 'DANGER latched by the suite\n' > "$RT/.hermes-advance-DANGER"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 2 "4sexies-a a latched DANGER refuses (rc 2) instead of laundering to rc 0"
case "$OUT" in *"already current"*) bad "4sexies-b it took the no-op early exit over a DANGER state" "$OUT";;
                *) ok "4sexies-b and it did not report the runtime as a clean no-op";; esac
# A refusal whose remedy is unstated loops the caller forever.
case "$OUT" in *".hermes-advance-DANGER"*) ok "4sexies-c and the refusal names the marker to remove";;
                *) bad "4sexies-c refusal does not say how to clear it" "$OUT";; esac
# NEGATIVE CONTROL: clearing the marker re-arms. Without this, every assertion above is
# satisfied by a subject that refuses unconditionally WITH THIS MESSAGE — review built the
# counterexample: a `die2` inserted above the pin-file loop refuses unconditionally with a
# DIFFERENT message and passes all four 4sexies assertions including this one (50/27
# overall). What kills that shape is 4quater-a/-b, not this control. §19.7(c): the clause
# is true of the same-message mutant (M16 `if true` reds -d) and false in general.
rm -f "$RT/.hermes-advance-DANGER"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
case "$OUT" in *"DANGER state and no human"*) bad "4sexies-d clearing the marker RE-ARMS" "still refusing after the marker was cleared";;
                *) ok "4sexies-d clearing the marker RE-ARMS the timer";; esac

# ── 12. `--ff-only` IS LOAD-BEARING: `merge.ff=false` MAKES THE FORMS DIVERGE ───────
# Round 4 declared `--ff-only` knowingly redundant (§19.2) because `--no-edit` reddened
# ZERO. That 0 was an artefact of THIS FILE: every fixture sets only user.email/user.name,
# so the whole 2x2 was measured under DEFAULT `merge.ff` — the one configuration the
# redundancy depends on. §19.7(a), committed while declaring coverage. A §19.2 note is a
# licence to DELETE, so the false declaration was more dangerous than the unexplained
# guard. `merge.ff` is unset on this host today, which is why this is a latent config
# vector rather than a live break — and exactly why a fixture, not a measurement of the
# current host, is what pins it.
RT="$(mk_runtime mergeff 1)"
git -C "$RT" config merge.ff false            # repo-local; user- and system-level do the same
TARGET_FF="$(git -C "$RT" rev-parse origin/main)"
if [ "$(git -C "$RT" config --get merge.ff)" = "false" ]; then
  ok "12-0 control: merge.ff=false is actually set in the runtime config"
else
  bad "12-0 control" "merge.ff was not set; this case measures nothing"
fi
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
rc_is "$RC" 0 "12a it still advances under merge.ff=false"
head_is "$RT" "$TARGET_FF" "12b and HEAD is EXACTLY the target, not a merge commit on top of it"
# The discriminating assertion. rc and HEAD-equality are both satisfiable by a
# fast-forward; parent count is what separates a fast-forward from a synthesised merge.
_PF=$(( $(git -C "$RT" rev-list --parents -n1 HEAD 2>/dev/null | wc -w) - 1 ))
[ "$_PF" -le 1 ] && ok "12c and it is a FAST-FORWARD, not a synthesised merge commit" \
                 || bad "12c HEAD is a merge commit" "parents=$_PF — merge.ff=false was honoured, so --ff-only is gone"

# ── 11bis. THE SKIP COUNTER ITSELF — BL-3's fix was removable in ONE LINE ──────────
# Reverting `skip()` to `PASS=$((PASS+1))` restores the exact pre-fix false green: under a
# root shim the suite prints SKIP=0 with NO `UNMEASURED` line and exits 0, and does so WITH
# the never-engage-the-latch mutant applied. Nothing asserted it — the fix for a false-green
# defect was one line away from being undone silently.
#
# STRUCTURAL, and declared as such: it reads the FUNCTION rather than driving a skip, because
# the only skip this suite can produce is the root-gated one and a non-root run would have to
# manufacture a fake to observe it. It catches the reversion; it does not prove the counter
# is correct on a host that takes the branch.
if declare -f skip | grep -q 'SKIP=$((SKIP+1))' && ! declare -f skip | grep -q 'PASS='; then
  ok "11bis skip() increments SKIP and never PASS (a skip folded into PASS is the false green)"
else
  bad "11bis skip() increments SKIP and never PASS" "skip() body: $(declare -f skip | tr '\n' ' ')"
fi

# DECLARED RESIDUAL (§19.5) — THE TRAILER CANNOT ASSERT ITSELF, and review measured what
# that costs. `EXPECTED_ASSERTIONS` is not a counted assertion and the `FAIL -eq 0` gate
# lives in the same block: delete the block and the suite exits 0 over any number of
# failures; delete it AND assertion 12c and it prints `PASS=90 ... (expected: 91)` at rc 0.
# (Round 8 published `PASS=77 ... (expected: 78)`, correct for its own 78-assertion pin and
# invalidated by round 9's eight additions. RE-DERIVED at this tip, not adjusted.)
# Control: deleting 12c with the ratchet intact correctly reds at rc 1 with
# `FAIL count drift: ran 90, expected 91` — regenerated in the same run, not read back from
# a stored baseline. The `UNMEASURED:`
# line below is likewise emitted after the last assertion, so nothing can observe it.
# 11bis pins the one line whose reversion restores the pre-fix false green; the rest of
# the trailer is stated here rather than wrapped in another layer that would need its own.
printf '\n=== PASS=%d FAIL=%d SKIP=%d (expected assertions: %d) ===\n' "$PASS" "$FAIL" "$SKIP" "$EXPECTED_ASSERTIONS"
[ "$SKIP" -eq 0 ] || printf 'UNMEASURED: %d assertion(s) SKIPPED on this host — the run is NOT full coverage.\n' "$SKIP"
if [ $((PASS+FAIL+SKIP)) -ne "$EXPECTED_ASSERTIONS" ]; then
  printf 'FAIL count drift: ran %d, expected %d\n' $((PASS+FAIL+SKIP)) "$EXPECTED_ASSERTIONS"; exit 1
fi
[ "$FAIL" -eq 0 ] || exit 1
