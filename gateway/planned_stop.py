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
    # WRONG-HOME CHECK — the one failure mode specific to this deployment gap,
    # and the one a write-failure guard structurally CANNOT see (found in
    # independent review; the comment below used to claim it was covered).
    #
    # A HERMES_HOME that differs between the unit's Environment= and the
    # gateway's own does NOT make the write fail. It makes it SUCCEED into the
    # wrong directory: write_planned_stop_marker returns True, this returns 0,
    # nothing is printed, and the gateway — polling its OWN home — never sees a
    # marker. REPRODUCED: execstop_rc=0, empty stdout AND stderr, marker present
    # in the unit's home and absent from the gateway's, gateway exits 1 and
    # persists state=running. That is the pre-CLAWD-3786 behaviour, silently.
    #
    # Not reachable through the unit GENERATOR (both templates derive
    # Environment="HERMES_HOME=..." from the same value as --profile), but the
    # fleet's 11 units are hand-provisioned and part 1 of this rollout is a
    # hand-written ExecStop= drop-in. A hand-written unit carrying --profile
    # without a matching Environment= is exactly this shape.
    #
    # The gateway writes gateway.pid under ITS OWN home, so that file is the
    # available cross-check: if our home does not hold a PID file naming the pid
    # we were asked to mark, we are looking at the wrong directory. Refuse
    # loudly rather than write a marker nobody will read.
    # THE PID FILE HOLDS A JSON RECORD, NOT A BARE INTEGER. The first version of
    # this guard compared the file's raw text to str(pid), and gateway/status.py
    # writes json.dumps(_build_pid_record()) — so `recorded` was always a JSON
    # blob, never equal to the pid, and the guard REFUSED EVERY STOP ON EVERY
    # GATEWAY. It reinstated the exact defect this card exists to fix, on the one
    # path the card is about (systemd-native stop), while printing a line blaming
    # the operator's unit for a HERMES_HOME mismatch that did not exist — with the
    # SAME path on both sides of the accusation. `hermes gateway stop` writes its
    # own marker first, so the CLI path masked it entirely.
    #
    # The predicate was DEGENERATE: it returned "refuse" for the right home and
    # the wrong home alike, so it could not discriminate the thing it existed to
    # detect. Use the module's own readers — they handle the JSON record and the
    # legacy bare-int form, and they already catch UnicodeDecodeError (a corrupt
    # or binary gateway.pid raised it uncaught here, killing ExecStop with a
    # traceback, because this only caught OSError).
    from gateway.status import _pid_from_record, _read_pid_record

    try:
        recorded_pid = _pid_from_record(_read_pid_record())
    except Exception as exc:  # noqa: BLE001 - a probe must not decide a stop
        print(
            f"hermes: could not read the gateway PID record to confirm HERMES_HOME "
            f"({exc}) — proceeding with the marker write UNVERIFIED (CLAWD-3786)",
            file=sys.stderr,
        )
        recorded_pid = None

    # DECLARED RESIDUAL: a MISSING or unparseable pid file leaves recorded_pid
    # None and is NOT treated as a mismatch. The most likely wrong-home shape — a
    # directory no gateway has ever run in — therefore passes silently. Refusing
    # on absence would refuse every first stop after a pid file is cleaned up,
    # which is worse than the gap; closing it properly needs a second signal.
    if recorded_pid is not None and recorded_pid != pid:
        print(
            f"hermes: HERMES_HOME={os.environ.get('HERMES_HOME') or '<unset>'} holds "
            f"a gateway.pid naming PID {recorded_pid}, but this stop is for PID "
            f"{pid}. The unit's Environment= and the gateway's own HERMES_HOME "
            "disagree, so a marker written here would never be read: this stop "
            "would be classified as an unexpected kill. Fix the unit's "
            "Environment=HERMES_HOME to match its --profile (CLAWD-3786)",
            file=sys.stderr,
        )
        return 1

    if write_planned_stop_marker(pid):
        return 0

    # SAY SO. The unit wires this with a leading '-', so systemd discards the
    # exit code: without this line a FAILED marker write (permissions, a full
    # disk, a read-only home) would silently restore the pre-CLAWD-3786
    # behaviour — the next stop reported as a crash — with nothing anywhere
    # saying why. The wrong-HOME case is NOT in this list: it is a successful
    # write to the wrong place and is caught by the check above instead.
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
