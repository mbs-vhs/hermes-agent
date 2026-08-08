"""CLAWD-3507 — a stopped apply must unwind, and verification must not run in RAM.

THREE DEFECTS, all in the "the tool survives its own environment" class.

1. **No `TimeoutStartSec`.** The unit is `Type=oneshot` with no timeout, so the host
   default applies — 45s on the reference host. One apply performs 8 full-runtime
   manifest walks plus a whole-archive extract-and-rehash, which exceeds that on any
   real runtime. systemd then SIGKILLs it, and with the default
   `KillMode=control-group` a running `git clean` dies too. Reproduced by SIGKILL
   mid-clean: 11,400 of 20,000 orphans removed, journal stuck at `cleaning`, and both
   `recover` and `apply` refusing afterwards — a half-swept live tree with no way
   forward but a manual restore.

2. **`import signal` with zero uses.** No handler meant a stop signal killed the
   process outright: no `except`, no `_handle_failed_transaction`, no rollback.

3. **Whole-tree extraction into tmpfs.** Backup verification extracts a FULL copy of
   the runtime — venv included — to re-hash it, using the default temp root. `/tmp`
   is tmpfs on this host, and `PrivateTmp=yes` does not change where it lives. That
   is a memory event on the box running the fleet. It is not hypothetical: a 60-way
   revert-validation matrix under the same tmpfs took this machine down during the
   work that produced these fixes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"
UNIT = REPO / "systemd" / "ai.hermes.opt-runtime-update.service"


def _subject():
    import importlib.util

    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_mutating_unit_sets_an_explicit_start_timeout():
    """Absent, the host default (45s) SIGKILLs a normal apply mid-clean."""
    unit = UNIT.read_text()
    line = [l for l in unit.splitlines() if l.startswith("TimeoutStartSec=")]
    assert line, (
        "no TimeoutStartSec — the host default applies and a real apply exceeds it, "
        "so systemd SIGKILLs the process and KillMode=control-group takes the running "
        "git clean with it, leaving a half-swept live runtime"
    )
    seconds = int(line[0].split("=", 1)[1])
    assert seconds >= 600, f"TimeoutStartSec={seconds} is too tight for a full runtime walk"


def test_the_unit_allows_the_handler_to_run_before_sigkill():
    """TimeoutStopSec gives the SIGTERM handler a window to unwind the transaction."""
    unit = UNIT.read_text()
    line = [l for l in unit.splitlines() if l.startswith("TimeoutStopSec=")]
    assert line, "no TimeoutStopSec — the handler can be SIGKILLed before it unwinds"
    assert int(line[0].split("=", 1)[1]) >= 30


def test_signal_handlers_are_actually_installed():
    """`import signal` was present and unused. Prove a handler now exists."""
    mod = _subject()
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        mod._install_signal_handlers()
        for sig in (signal.SIGTERM, signal.SIGINT):
            handler = signal.getsignal(sig)
            assert callable(handler), f"{sig} has no callable handler"
            assert handler not in (signal.SIG_DFL, signal.SIG_IGN)
    finally:
        for sig, h in previous.items():
            signal.signal(sig, h)


def test_sigterm_raises_instead_of_killing_the_process():
    """The handler must raise an UpdateError so the failure path runs.

    Dying silently is the defect: no rollback, no journal transition, a half-swept
    tree. This sends a real SIGTERM to a real child and asserts it unwound.
    """
    program = (
        "import importlib.util, os, signal, sys\n"
        f"spec = importlib.util.spec_from_file_location('s', {str(SUBJECT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "m._install_signal_handlers()\n"
        "try:\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "except m.UpdateError as exc:\n"
        "    print('UNWOUND:' + type(exc).__name__); sys.exit(0)\n"
        "print('NOT-CAUGHT'); sys.exit(1)\n"
    )
    proc = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert proc.returncode == 0, f"SIGTERM was not converted to an exception: {proc.stdout}{proc.stderr}"
    assert "UNWOUND:_Interrupted" in proc.stdout, proc.stdout


def test_interrupted_is_an_update_error_so_existing_handlers_catch_it():
    """It must ride the transaction machinery already in place, not bypass it."""
    mod = _subject()
    assert issubclass(mod._Interrupted, mod.UpdateError)


def test_archive_verification_scratch_is_disk_backed_not_tmpfs(tmp_path: Path):
    """Whole-tree extraction must not happen in RAM on the box running the fleet."""
    mod = _subject()
    archive = tmp_path / "backup.tar.zst"
    archive.write_text("x")
    chosen = mod._verify_scratch_dir(archive)
    assert chosen == str(tmp_path), (
        "verification scratch did not follow the archive onto disk; extracting a full "
        "runtime copy into the default temp root puts the venv in tmpfs"
    )


def test_verify_scratch_falls_back_rather_than_refusing():
    """A missing state dir must not block verifying a backup.

    Refusing to verify is worse than verifying somewhere less ideal — this path is
    what stands between an operator and an unverified restore.
    """
    mod = _subject()
    assert mod._verify_scratch_dir(Path("/no/such/dir/backup.tar.zst")) == tempfile.gettempdir()
