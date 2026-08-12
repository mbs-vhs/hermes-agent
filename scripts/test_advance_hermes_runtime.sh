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
EXPECTED_ASSERTIONS=28

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n       %s\n' "$1" "${2:-}"; }
rc_is(){ [ "$1" = "$2" ] && ok "$3" || bad "$3" "rc=$1 want=$2"; }
head_is(){ # head_is <dir> <want-sha> <case>
  local got; got="$(git -C "$1" rev-parse HEAD 2>/dev/null)"
  [ "$got" = "$2" ] && ok "$3" || bad "$3" "HEAD moved: want ${2:0:9} got ${got:0:9}"
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
case "$OUT" in *"revert PROVEN"*) ok "4c the revert is proven, not assumed";;
                *) bad "4c revert not proven" "$OUT";; esac

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
# An interpreter that injects the good copy — i.e. the site-packages situation.
cat > "$RT/venv/bin/python" <<PYW
#!/usr/bin/env bash
exec env PYTHONPATH="$GOOD" "$(command -v python3)" "\$@"
PYW
chmod +x "$RT/venv/bin/python"
BEFORE="$(git -C "$RT" rev-parse HEAD)"
OUT="$(HERMES_RUNTIME="$RT" HERMES_PREFLIGHT="$PF" bash "$SUT" 2>&1)"; RC=$?
# DECLARED: these two DO NOT ISOLATE the provenance assert. Measured — with the assert
# reverted the suite stays 28/0, so 6a/6b are satisfied by something else in the chain
# and are not evidence for it. They are kept because they pin a real property (an advance
# whose module is missing from the tree must not stand), and they are labelled rather
# than presented as coverage they do not provide.
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
