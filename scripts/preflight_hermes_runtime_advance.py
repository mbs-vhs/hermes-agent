#!/usr/bin/env python3
"""Answer ONE question about ~/.hermes/hermes-agent before anyone advances it:

    would advancing this checkout break the things that actually execute from it?

WHY THIS EXISTS
---------------
On 2026-08-12 that runtime measured 10,078 commits behind origin/main with "19 dirty
files", and NOBODY COULD SAY WHETHER THAT WAS DRIFT OR A DELIBERATE PIN. It is the
OAuth credential path for the whole fleet, so the cost of guessing wrong in either
direction is high: advance a deliberate pin and you break credential refresh; leave
drift alone and it compounds. The question sat open because answering it by reading
was not possible and answering it by trying was not safe.

It is answerable by measurement, and this script is that measurement. Everything here
is READ-ONLY: it exports origin/main to a scratch directory and probes it with the
runtime's OWN venv interpreter. It never writes inside HERMES_RUNTIME, never touches
credentials (it runs probes under a throwaway HOME), and never restarts anything.

WHAT IT CHECKS, AND WHY EACH ONE IS NECESSARY
---------------------------------------------
The consumers are two oneshot systemd timers whose scripts live OUTSIDE the checkout
(`~/.hermes/bin/refresh-*-oauth.py`) but which do

    sys.path.insert(0, ~/.hermes/hermes-agent)

and import from the tree. So the tree shadows site-packages and advancing the checkout
DOES change what they execute — which is why "the venv is unchanged" is not an answer.

  1. PIN-OR-DRIFT   local commits, a pin file, and whether the tree is strictly behind
                    or diverged. A pin has evidence; drift is the absence of it.
  2. SYMBOLS        every name the consumers import, resolved by AST at the target ref.
                    An import that vanished is a break with no runtime to catch it.
  3. SIGNATURES     bound against the ACTUAL call site. A symbol can survive and its
                    signature change underneath it — measured on the first run:
                    `_save_codex_tokens` gained a third parameter. It has a default and
                    the live call passes one argument, so it binds. Existence alone
                    would have said "fine"; existence alone was not enough to know that.
  4. IMPORTABILITY  the imports executed against the target tree using the CURRENT venv,
                    which is the only check that covers a transitive dependency the venv
                    cannot satisfy.
  5. NEGATIVE CONTROL  a deliberately renamed symbol MUST make check 4 fail. Without it,
                    "all imports OK" is indistinguishable from a probe that cannot fail.
                    This repo has measured that exact false green more than once.

EXIT CODES
    0  the advance is safe for the measured consumers
    1  the advance WOULD BREAK a consumer  (a named symbol/signature/import)
    2  UNMEASURED — a check could not run. Never treated as safe.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(os.environ.get("HERMES_RUNTIME", Path.home() / ".hermes" / "hermes-agent"))
CONSUMER_DIR = Path(os.environ.get("HERMES_BIN", Path.home() / ".hermes" / "bin"))

# Modules that are stdlib for our purposes — an import of these tells us nothing about
# the tree. Everything else a consumer imports is assumed to come from the checkout.
_STDLIB = {"json", "sys", "time", "pathlib", "os", "base64", "argparse", "subprocess",
           "urllib", "datetime", "typing", "re", "shutil", "hashlib", "textwrap"}


def run(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", **kw)


def git(*args) -> str:
    r = run("git", "-C", str(RUNTIME), *args)
    return r.stdout.strip() if r.returncode == 0 else ""


class Result:
    def __init__(self):
        self.breaks: list[str] = []      # -> exit 1
        self.unmeasured: list[str] = []  # -> exit 2 (dominates)

    def broke(self, msg): self.breaks.append(msg)
    def cannot(self, msg): self.unmeasured.append(msg)


def discover_consumers() -> list[tuple[Path, str, list[str]]]:
    """Find every script that sys.path-inserts the runtime, and what it imports from it.

    Discovered rather than hardcoded: a hardcoded list silently stops covering a
    consumer added later, and the failure mode is a green preflight over an untested
    importer — which is the shape this whole file exists to refuse.
    """
    found = []
    if not CONSUMER_DIR.is_dir():
        return found
    for path in sorted(CONSUMER_DIR.glob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "hermes-agent" not in src or "sys.path" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in _STDLIB:
                    continue
                found.append((path, node.module, [a.name for a in node.names]))
    return found


def top_level_defs(src: str) -> dict[str, ast.AST]:
    out = {}
    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n
    return out


def call_arity(consumer_src: str, func: str) -> int | None:
    """How many positional args does the live call site actually pass?"""
    try:
        tree = ast.parse(consumer_src)
    except SyntaxError:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == func:
            return len(n.args)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="origin/main",
                    help="ref to test advancing TO (default: origin/main)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the fetch. A ref is a CACHED claim about a remote and its "
                         "freshness is not measured by anything (CLAWD-3760) — so without "
                         "a fetch the distance reported here may be stale in the "
                         "reassuring direction.")
    args = ap.parse_args(argv)
    res = Result()

    if not (RUNTIME / ".git").is_dir():
        print(f"UNMEASURED: no git checkout at {RUNTIME}", file=sys.stderr)
        return 2
    venv_py = RUNTIME / "venv" / "bin" / "python"
    if not venv_py.exists():
        print(f"UNMEASURED: no venv interpreter at {venv_py}", file=sys.stderr)
        return 2

    if not args.no_fetch:
        # Fetch BEFORE measuring distance. Skipping this is how a runtime reports
        # "behind: 0" from a ref nothing refreshed (CLAWD-3760).
        if run("git", "-C", str(RUNTIME), "fetch", "--quiet", "origin").returncode != 0:
            res.cannot("could not fetch — every distance below is against a cached ref")

    head = git("rev-parse", "--short", "HEAD")
    behind = git("rev-list", "--count", f"HEAD..{args.target}") or "?"
    ahead = git("rev-list", "--count", f"{args.target}..HEAD") or "?"

    print(f"RUNTIME   {RUNTIME}")
    print(f"HEAD      {head}   behind {args.target}: {behind}   ahead: {ahead}")
    print()

    # ---- 1. PIN OR DRIFT ---------------------------------------------------------
    print("1. PIN OR DRIFT")
    pin_files = [p for p in (".pinned-ref", ".deployed-ref", "PIN") if (RUNTIME / p).exists()]
    porcelain = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    # X=index, Y=worktree. A deletion is D in either column.
    deletions = [l for l in porcelain if len(l) > 1 and "D" in l[:2]]
    others = [l for l in porcelain if l not in deletions]
    # Anything Python writes while probing lands here and would flip the verdict, so
    # the pin/drift read is taken BEFORE any probe runs and __pycache__ is excluded:
    # an earlier ad-hoc probe of mine polluted this very tree and made one run report
    # PIN-SHAPED off a file the measurement itself created.
    others = [l for l in others if "__pycache__" not in l and not l.endswith(".pyc")]
    evidence = []
    if pin_files:
        evidence.append(f"pin file present: {pin_files}")
    if ahead not in ("0", "?"):
        evidence.append(f"{ahead} local commit(s) not upstream")
    if others:
        evidence.append(f"{len(others)} non-deletion working-tree change(s)")
    if evidence:
        print("   PIN-SHAPED — advancing would discard something:")
        for e in evidence:
            print(f"     - {e}")
    else:
        print("   DRIFT — no pin file, no local commits, no non-deletion changes.")
        print(f"   ({len(deletions)} deletion(s) in the tree; checked against the target below)")
    # A deletion the target ALSO makes is not a local change to preserve.
    for line in deletions[:200]:
        p = line[3:].strip()
        if git("ls-tree", "-r", "--name-only", args.target, "--", p):
            res.cannot(f"deleted locally but PRESENT at {args.target}: {p}")
    if deletions and not res.unmeasured:
        print(f"   all {len(deletions)} deletion(s) are already made at {args.target} "
              f"— they vanish on advance rather than conflicting")
    print()

    # ---- 2/3/4. CONSUMERS --------------------------------------------------------
    consumers = discover_consumers()
    print(f"2. CONSUMERS (discovered, not hardcoded): {len(consumers)} import site(s)")
    if not consumers:
        res.cannot(f"no consumer found under {CONSUMER_DIR} — cannot show the advance is safe "
                   f"for anything. An empty consumer set is not a safe advance.")
        print("   NONE FOUND — this is UNMEASURED, not safe.")

    tmp = Path(tempfile.mkdtemp(prefix="hermes-preflight-",
                                dir=os.environ.get("TMPDIR") or None))
    fake_home = tmp / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    tree = tmp / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    try:
        # git archive emits a BINARY tarball. The first version of this ran it through
        # the text-mode helper and died on a PNG byte at offset 76800 — a crash, which
        # is at least loud. Piped straight through tar, never decoded.
        p1 = subprocess.Popen(["git", "-C", str(RUNTIME), "archive", args.target],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", str(tree)], stdin=p1.stdout,
                             capture_output=True)
        p1.stdout.close()
        if p1.wait() != 0 or tar.returncode != 0:
            res.cannot(f"could not export {args.target} to a scratch tree")

        for path, module, names in consumers:
            rel = module.replace(".", "/") + ".py"
            target_src_p = tree / rel
            print(f"   {path.name}: from {module} import {', '.join(names)}")
            if not target_src_p.exists():
                res.broke(f"{module} does not exist at {args.target} "
                          f"(imported by {path.name})")
                print(f"     MODULE GONE at {args.target}")
                continue
            src = target_src_p.read_text(encoding="utf-8", errors="replace")
            defs = top_level_defs(src)
            consumer_src = path.read_text(encoding="utf-8", errors="replace")
            cur_p = RUNTIME / rel
            cur_defs = top_level_defs(cur_p.read_text(encoding="utf-8", errors="replace")) \
                if cur_p.exists() else {}
            for n in names:
                if n not in defs:
                    res.broke(f"{module}.{n} is GONE at {args.target} "
                              f"(imported by {path.name})")
                    print(f"     {n:34} *** GONE ***")
                    continue
                note = "ok"
                node, cur = defs[n], cur_defs.get(n)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = node.args
                    params = [x.arg for x in a.posonlyargs + a.args]
                    required = len(params) - len(a.defaults)
                    passed = call_arity(consumer_src, n)
                    if passed is not None and passed < required:
                        res.broke(f"{module}.{n} at {args.target} needs {required} "
                                  f"positional arg(s); {path.name} passes {passed}")
                        note = f"*** CALL SITE PASSES {passed}, NEEDS {required} ***"
                    elif isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        cur_params = [x.arg for x in cur.args.posonlyargs + cur.args.args]
                        if cur_params != params:
                            note = (f"signature changed {cur_params} -> {params}, "
                                    f"call site passes {passed} — still binds")
                print(f"     {n:34} {note}")

        # ---- 4 + 5. IMPORTABILITY, WITH ITS NEGATIVE CONTROL ---------------------
        print()
        print("3. IMPORTABILITY under the CURRENT venv, with a negative control")

        def probe(root: Path) -> tuple[int, str]:
            lines = ["import sys", f"sys.path.insert(0, {str(root)!r})"]
            for _, module, names in consumers:
                lines.append(f"from {module} import {', '.join(names)}")
            lines.append("print('IMPORTS-OK')")
            r = subprocess.run([str(venv_py), "-c", "\n".join(lines)],
                               capture_output=True, text=True, encoding="utf-8",
                               env={**os.environ, "HOME": str(fake_home),
                                    "PYTHONDONTWRITEBYTECODE": "1"})
            return r.returncode, (r.stdout + r.stderr).strip()

        if consumers:
            rc_cur, _ = probe(RUNTIME)
            print(f"   control  (current tree)      : "
                  f"{'imports OK' if rc_cur == 0 else 'FAILS — probe is unusable'}")
            if rc_cur != 0:
                res.cannot("the probe cannot even import the CURRENT tree, so a pass "
                           "against the target would mean nothing")

            rc_new, out_new = probe(tree)
            print(f"   subject  ({args.target})"
                  f"{' ' * max(1, 14 - len(args.target))}: "
                  f"{'imports OK' if rc_new == 0 else 'FAILS'}")
            if rc_new != 0:
                res.broke(f"imports fail against {args.target} with the current venv: "
                          f"{out_new.splitlines()[-1] if out_new else 'unknown'}")

            # NEGATIVE CONTROL. Break one symbol in the exported tree; the probe MUST
            # fail. A probe that passes here proves nothing when it passes anywhere.
            broke_one = False
            for _, module, names in consumers:
                f = tree / (module.replace(".", "/") + ".py")
                if not f.exists():
                    continue
                src = f.read_text(encoding="utf-8", errors="replace")
                for pat in (f"def {names[0]}", f"class {names[0]}", f"{names[0]} ="):
                    if pat in src:
                        f.write_text(src.replace(pat, pat.replace(names[0],
                                     "__PREFLIGHT_RENAMED__"), 1), encoding="utf-8")
                        broke_one = True
                        break
                if broke_one:
                    rc_neg, _ = probe(tree)
                    f.write_text(src, encoding="utf-8")
                    print(f"   negative control (renamed one symbol): "
                          f"{'probe FAILED as required' if rc_neg != 0 else '*** PROBE STILL PASSED ***'}")
                    if rc_neg == 0:
                        res.cannot("the negative control did not fail — this probe cannot "
                                   "detect a break, so its pass is not evidence")
                    break
            if not broke_one:
                res.cannot("could not construct a negative control — probe unvalidated")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- VERDICT -----------------------------------------------------------------
    print()
    if res.unmeasured:
        print("UNMEASURED — this is NOT a safe advance, and NOT a broken one:")
        for m in res.unmeasured:
            print(f"  {m}")
        return 2
    if res.breaks:
        print("WOULD BREAK — do not advance until these are resolved:")
        for m in res.breaks:
            print(f"  {m}")
        return 1
    print(f"ok — advancing {RUNTIME} to {args.target} is safe for "
          f"{len(consumers)} measured import site(s):")
    print( "     every imported symbol exists, every call site binds, every import")
    print( "     executes under the current venv, and the probe was shown able to fail.")
    print( "     NOT covered: runtime behaviour beyond import, and any consumer that")
    print(f"     does not live in {CONSUMER_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
