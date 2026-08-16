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
# HERMES_RUNTIME / HERMES_PREFLIGHT. `grep -c '\.hermes' ` over this file matches only
# this paragraph.
set -uo pipefail

SUT="${SUT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/advance_hermes_runtime.sh}"
EXPECTED_ASSERTIONS=45

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n       %s\n' "$1" "${2:-}"; }
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
  local n="$1" behind="$2" up="$WORK/$1.origin" rt="$WORK/$1"
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
  ok "4quinquies SKIPPED as root (chmod does not constrain uid 0) — declared, not silent"
  ok "4quinquies-b skipped"
  ok "4quinquies-c skipped"
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
fi
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
# tree: deleting the provenance assert gives 36/2 (6a and 6b red), and weakening
# it to `.startswith('/')` also gives 36/2. Repairing the case-6 fixture (the
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

printf '\n=== PASS=%d FAIL=%d (expected assertions: %d) ===\n' "$PASS" "$FAIL" "$EXPECTED_ASSERTIONS"
if [ $((PASS+FAIL)) -ne "$EXPECTED_ASSERTIONS" ]; then
  printf 'FAIL count drift: ran %d, expected %d\n' $((PASS+FAIL)) "$EXPECTED_ASSERTIONS"; exit 1
fi
[ "$FAIL" -eq 0 ] || exit 1
