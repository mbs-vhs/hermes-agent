"""Mark a systemd-initiated stop as intentional before systemd's SIGTERM.

Runs as ``ExecStop=`` so it fires while the gateway's main process is still
alive, immediately before systemd delivers ``KillSignal=SIGTERM`` — CLAWD-3786.

WHY THIS EXISTS

The gateway exits non-zero when an *unexpected* SIGTERM ends it, so a service
manager with ``Restart=on-failure`` revives it after an external kill (upstream
PR #5646).  To keep a deliberate stop distinguishable from that, every stop path
Hermes owns writes a short-lived planned-stop marker naming the target PID
BEFORE signalling: ``hermes gateway stop`` on systemd
(``hermes_cli/gateway.py:systemd_stop``), launchd (``launchd_stop``), s6
(``hermes_cli/service_manager.py:S6ServiceManager.stop``) and Windows
(``hermes_cli/gateway_windows.py``).  The shutdown handler consumes the marker
and exits 0.

``systemctl stop`` does not go through any of them.  systemd sends the unit's
``KillSignal`` directly, no marker is written, and the gateway therefore
classifies an operator-initiated stop as an unexpected kill: it exits 1
(``gateway/run.py`` — the ``_signal_initiated_shutdown`` branch of
``start_gateway``'s tail) and persists ``gateway_state=running``
(``_stop_impl``).  systemd records ``Result=exit-code`` / ``ActiveState=failed``,
so a crashed gateway and a deliberately stopped one are indistinguishable to an
operator, a dashboard probe or an alert rule.

The marker is the mechanism that already tells those two apart; the gap was that
systemd's own stop path had no way to write it.  ``ExecStop=`` is that way, and
it is a property of the *unit's stop job*, so it covers every client that asks
systemd to stop us — ``systemctl stop``/``restart``, a D-Bus/dashboard stop,
``loginctl terminate-user``, host shutdown — not just the ones that route
through the Hermes CLI.  An external kill still writes no marker and still
exits 1, so the crash-vs-stop distinction is preserved rather than flattened.
That is also why this is not fixed with ``SuccessExitStatus=1``: that would mask
a genuine exit-1 fault as success.

CONTRACT

``python -m gateway.planned_stop $MAINPID``.  systemd expands ``$MAINPID`` in
``ExecStop=`` command lines.  Everything here is best-effort — the unit wires it
with a leading ``-`` so a failure here can never fail the stop job — and a
missing/unexpanded/dead PID is a no-op rather than an error: if the main process
is already gone there is nothing to classify, and writing a marker for a PID we
do not own could mis-classify a later shutdown.

Best-effort is not silent, though: because that leading ``-`` also discards the
exit code, a marker write that FAILS says so on stderr (``StandardError=journal``
puts it beside the unit's own stop lines).  Otherwise the one failure mode that
reintroduces the original defect would be the one leaving no evidence.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence


def _parse_main_pid(argv: Sequence[str]) -> Optional[int]:
    """Return the PID named by ``argv``, or None when there is nothing to mark.

    systemd drops the argument entirely when ``$MAINPID`` is unset, and hands
    over the literal, unexpanded text if expansion ever fails — both land here
    as "no PID" rather than as an error.

    A non-positive PID is rejected HERE and not left to the liveness check,
    because that check is not the same guard on every install: with psutil
    present ``_pid_exists(-1)`` is False, but on the documented stdlib fallback
    (``gateway/status.py`` — psutil missing, e.g. a stripped-down install or an
    import error during scaffolding) it is ``os.kill(-1, 0)``, which addresses
    every process the caller can signal and returns True. Measured both ways:
    without the guard, ``main(["-1"])`` writes a marker naming ``target_pid:
    -1`` on the fallback path.
    """
    if not argv:
        return None
    try:
        pid = int(argv[0].strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Mark ``argv[0]``'s pending shutdown as planned. Returns a process code."""
    args = sys.argv[1:] if argv is None else argv
    pid = _parse_main_pid(args)
    if pid is None:
        return 0

    # Imported here, not at module scope: this runs on the stop path of every
    # gateway service, and there is nothing to load when there is no PID to mark.
    from gateway.status import _pid_exists, write_planned_stop_marker

    if not _pid_exists(pid):
        # The main process exited on its own; there is no shutdown left to
        # classify, and the PID could already be recycled.
        return 0
    if write_planned_stop_marker(pid):
        return 0

    # SAY SO. The unit wires this with a leading '-', so systemd discards the
    # exit code: without this line a failed marker write (permissions, a full
    # disk, a HERMES_HOME that differs between the unit's Environment= and the
    # gateway's own) would silently restore the pre-CLAWD-3786 behaviour — the
    # next stop reported as a crash — with nothing anywhere saying why.
    # StandardError=journal, so this lands next to the unit's own stop lines.
    print(
        f"hermes: could not write the planned-stop marker for PID {pid} under "
        f"HERMES_HOME={os.environ.get('HERMES_HOME') or '<unset>'} — this stop "
        "will be classified as an unexpected kill and the unit will report "
        "failed (CLAWD-3786)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
