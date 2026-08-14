# DELETE AT MERGE (CLAWD-2837)
#
# ADOPT UPSTREAM'S MODULE INSTEAD. Upstream shipped gateway/shutdown_watchdog.py
# after our merge-base (#66892 / #69089) and we do not have it. At the
# v2026.7.20 merge: delete THIS file, adopt theirs, and contribute only the
# residual gap upstream. Verify their module exists at merge time with
#   git ls-tree upstream/main -- gateway/shutdown_watchdog.py
"""Post-stop hard-exit watchdog, parked out of gateway/run.py (CLAWD-1023).

WHY THIS FILE IS TEMPORARY AND SAYS SO IN LINE 1

This is a *parking space*, not a home. Its only job is to stop our CLAWD-1023
watchdog from being ~40 lines scattered inside a 20k-line file that upstream is
rewriting, so the v2026.7.20 merge has nothing to reconcile here. It is tagged
for deletion because upstream now ships a better-placed equivalent for most of
what it does.

WHAT UPSTREAM COVERS, AND THE GAP THAT REMAINS (CLAWD-2841, re-derived
merge-base-relative — an earlier, stronger claim that "the post-stop window is
entirely uncovered" was measured and RETRACTED):

  * Upstream's ``gateway/shutdown_watchdog.py`` arms an OS-thread watchdog at the
    TOP of ``stop()`` and disarms it via ``_watchdog_done.set()`` in a ``finally``
    when ``_stop_impl_body`` returns. That covers the **drain** window.
  * Upstream ALSO added ``_hard_exit_after_gateway_teardown`` in the CLI wrapper
    (``hermes_cli/gateway.py``), routing all four exit paths of
    ``hermes gateway run`` through ``_exit_after_graceful_shutdown`` →
    ``os._exit``. The fork has none of that: our four paths are
    ``return`` / ``raise`` / ``sys.exit(1)`` / fall-through, i.e. a full
    ``Py_FinalizeEx`` that joins non-daemon threads and can therefore hang. Since
    the fleet enters through that wrapper, **upstream covers a final-exit hang we
    do not.**
  * Upstream's loop-liveness watchdog cannot help either way: it is stopped at the
    very top of ``stop()``, and it fires on *missed probes* (a frozen loop) —
    whereas CLAWD-1023's symptom is a loop still **spinning** after ``stop()``
    completed, which passes every probe. Wrong detector, not merely mis-sequenced.

  The genuine residual gap is therefore the **middle stretch**: after
  ``_shutdown_event.set()`` releases ``wait_for_shutdown()``, through
  ``start_gateway``'s cron / housekeeping / MCP teardown and ``asyncio.run()``'s
  own teardown. That is the window this watchdog actually covers, and it is what
  should be offered upstream.

  Note our own coverage is narrower than "post-stop" suggests: the watchdog is an
  on-loop ``asyncio`` task, so ``asyncio.run``'s ``_cancel_all_tasks`` kills it
  once ``start_gateway`` returns. Its window is bounded to time spent *inside*
  ``start_gateway`` after ``wait_for_shutdown()``.

DO **NOT** UPSTREAM THE EXIT-CODE LADDER. Its docstring used to warn it may drift
from ``start_gateway``; it was measured and it does **not** — the fork ladder and
upstream's ``start_gateway`` tail decision are semantically identical (same four
conditions in the same order, same ``GATEWAY_SERVICE_RESTART_EXIT_CODE``). An
earlier claim that ours is "already stale vs upstream" was unsupported and is
retracted. The upstream contribution is the ~15-line post-stop *arm*, and it must
engage upstream's existing ``os._exit`` wiring or it will be read as redundant.

THE LATE-BINDING TRAP (why ``signal_initiated`` is a CALLABLE)

In ``run.py`` the watchdog was a closure nested inside ``shutdown_signal_handler``,
reading the ``start_gateway`` local ``_signal_initiated_shutdown`` at **fire**
time, not arm time. The handler can fire more than once — a planned stop first
(flag ``False``), then a later unexpected signal (flag ``True``). Passing a plain
``bool`` would snapshot the value at arm time and silently change shutdown
semantics. So this takes a **zero-argument callable**, evaluated after the grace
sleep. ``run.py`` passes ``lambda: _signal_initiated_shutdown``.

**THAT ORIGINAL JUSTIFICATION NO LONGER HOLDS, and saying so is the honest form**
(found in independent review of CLAWD-3786, not by the author).
``ShutdownClassifier`` now latches the first verdict for the gateway's life, and
``run.py`` sets the flag BEFORE arming this watchdog, so the False -> True
transition described above is unreachable on every path that exists today. Late
binding is inert here, not load-bearing.

The callable shape is kept anyway: it is the right shape for a value read after a
grace sleep, and narrowing it to a ``bool`` would bake in an assumption that holds
only while the latch does. This is a knowingly-redundant guard, recorded rather
than deleted — do not "simplify" it back on the strength of the paragraph above.

WHERE THE GRACE CONSTANT LIVES, AND WHY IT STAYED IN run.py

``_HARD_EXIT_GRACE_SEC = _hard_exit_grace_seconds()`` is deliberately **not**
evaluated here. In ``run.py`` that assignment executes *before*
``load_hermes_dotenv(...)``, which loads ``~/.hermes/.env`` with
``override=True``. So today only the process/shell/systemd environment is
honoured, and a ``HERMES_HARD_EXIT_GRACE_SEC`` in a ``.env`` file is ignored. Had
the constant moved here — with the import landing in run.py's normal gateway
import block, i.e. *after* the dotenv load — the ``.env`` value would silently
start winning. That is a behaviour change, and this card is a pure move. ``run.py``
therefore keeps the assignment and imports only the function.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE

# Match the logger run.py uses so the forensic warning keeps its original logger
# name — log-based alerting on this line must not break.
logger = logging.getLogger("gateway.run")

DEFAULT_HARD_EXIT_GRACE_SEC = 20.0


def _hard_exit_grace_seconds() -> float:
    """Grace (seconds) after stop() before the watchdog force-exits. A malformed
    env value must NOT crash module import (that would fail all gateways at
    once), so fall back to the 20s default."""
    try:
        return float(os.environ.get("HERMES_HARD_EXIT_GRACE_SEC", "20") or "20")
    except (TypeError, ValueError):
        return DEFAULT_HARD_EXIT_GRACE_SEC


def _resolve_hung_shutdown_exit_code(runner, signal_initiated: bool) -> int:
    """Exit code the hard-exit watchdog uses when a shutdown hangs.

    Mirrors ``start_gateway()``'s post-``wait_for_shutdown()`` exit decision
    (the ``should_exit_with_failure`` / ``exit_code`` / signal-initiated /
    ``_restart_via_service`` ladder) so the watchdog never changes shutdown
    semantics. Keep in sync if that decision changes. (CLAWD-1023)

    Measured 2026-07-27: this ladder is semantically IDENTICAL to upstream's
    ``start_gateway`` tail decision — do not "fix" a drift that does not exist.
    """
    if getattr(runner, "should_exit_with_failure", False):
        return 1
    if runner.exit_code is not None:
        return runner.exit_code
    if signal_initiated and not runner._restart_requested:
        return 1
    if runner._restart_via_service:
        return GATEWAY_SERVICE_RESTART_EXIT_CODE
    return 0


def arm_post_stop_exit_watchdog(
    runner,
    signal_initiated: Callable[[], bool],
    *,
    grace_seconds: Optional[float] = None,
    logger_override: Optional[logging.Logger] = None,
) -> "asyncio.Task":
    """Arm the post-stop hard-exit watchdog. Returns the created task.

    Awaits ``runner.wait_for_shutdown()``, sleeps the grace, and if the process is
    still alive resolves an exit code and calls ``os._exit``. The 180s agent-turn
    drain lives inside ``stop()``, so awaiting ``wait_for_shutdown()`` keeps the
    watchdog clear of it.

    ``signal_initiated`` MUST be a zero-argument callable, evaluated after the
    grace sleep — see the module docstring on late binding. Passing a bare bool
    would snapshot it at arm time and change shutdown semantics.
    """
    grace = _hard_exit_grace_seconds() if grace_seconds is None else float(grace_seconds)
    log = logger_override if logger_override is not None else logger

    async def _hard_exit_watchdog() -> None:
        try:
            await runner.wait_for_shutdown()
        except Exception:
            pass
        await asyncio.sleep(grace)
        # Still alive → exit is hung. Resolve the code via the shared helper
        # that mirrors start_gateway()'s decision (CLAWD-1023).
        code = _resolve_hung_shutdown_exit_code(runner, signal_initiated())
        # Format the EFFECTIVE grace, not a module constant: once grace_seconds is
        # an override parameter, logging the constant would make this forensic
        # line lie during any override.
        log.warning(
            "Hard-exit watchdog: process still alive %.0fs after stop() "
            "completed — a lingering subprocess/transport is blocking exit; "
            "forcing os._exit(%d). (CLAWD-1023)",
            grace,
            code,
        )
        os._exit(code)

    return asyncio.create_task(_hard_exit_watchdog())
