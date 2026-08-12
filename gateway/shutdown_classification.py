"""Decide, once per gateway life, WHY this shutdown is happening (CLAWD-3786).

WHY THIS IS A LATCH AND NOT A FUNCTION

The gateway exits non-zero for an *unexpected* SIGTERM so a service manager with
``Restart=on-failure`` revives it, and exits 0 for a stop somebody asked for.
The evidence that a stop was asked for is a short-lived planned-stop marker file
written before the signal — and consuming it is **destructive** by design: a
marker must not be able to silence a second, genuinely unexpected signal.

``start_gateway``'s shutdown handler can fire more than once for a *single*
shutdown, and the first invocation eats the evidence the second one needs:

  1. ``_run_planned_stop_watcher`` (``gateway/run.py``) polls for the marker
     every 0.5s and, on a self-targeted match, calls the handler with
     ``signal=None``.  That invocation consumes and unlinks the marker.
  2. systemd's ``KillSignal=SIGTERM`` lands ~22ms later (measured 17.7-23.3ms
     over 8 runs: the gap between ``ExecStop=`` writing the marker and the
     ExecStop *process* finishing its own interpreter teardown, before systemd
     signals the main process).  The handler runs again, finds no marker, and
     without a latch would classify a deliberate stop as an unexpected kill —
     ``_signal_initiated_shutdown = True`` and exit 1.

Against a 500ms poll that is >=4.5% of stops on an idle host, and it is a lower
bound: it excludes systemd's own reap->signal latency and interpreter teardown
widens under load.  An intermittent false ``failed`` is worse for an alert rule
than a deterministic one, so the classification is latched here rather than
patched at any one of its triggers.

The race is INHERITED, not new: ``hermes gateway stop`` has always written the
marker and then paid a whole ``systemctl stop`` round trip before the signal —
a much wider window through the same seam.  Latching fixes every writer (CLI
stop, s6, launchd, ``ExecStop=``) and every trigger (watcher tick, signal) at
once, which is why this is the fix rather than a non-consuming watcher (which
would leave a live marker able to silence a later, genuinely unexpected signal)
or skipping the watcher under systemd (which would leave the wider CLI window
open on every platform).

WHAT "ONCE" MEANS

The first classification of a life is final.  A later signal is reported as
``ALREADY_CLASSIFIED`` and, deliberately, does **not** re-run the probes: the
marker consume is destructive, and re-running it after the outcome is settled
can only discard evidence.  A ``--replace`` takeover marker arriving mid-stop is
therefore left on disk; it is PID+start-time guarded, expires on its own 60s
TTL, and the replacer clears it in a ``finally``.

Extracted from the handler closure in ``start_gateway`` so the ladder is
unit-testable against real code rather than a replica — the same reason
``gateway/lifecycle_notifications.py`` exists.
"""

from __future__ import annotations

import logging
import signal
from enum import Enum
from typing import Optional

# Match the logger run.py uses so these records keep their original logger name
# — log-based alerting on the shutdown lines must not break.
logger = logging.getLogger("gateway.run")


class ShutdownCause(Enum):
    """Why a gateway life is ending."""

    #: A sibling gateway starting with ``--replace`` is taking over.
    TAKEOVER = "takeover"
    #: Somebody asked for this stop (service manager, CLI, Ctrl+C).
    PLANNED_STOP = "planned_stop"
    #: A signal nobody announced: an external kill, `hermes update`, a
    #: container runtime. The gateway exits non-zero so it gets revived.
    UNEXPECTED = "unexpected"
    #: A later signal for a shutdown already under way. Carries no verdict of
    #: its own — the first classification stands.
    ALREADY_CLASSIFIED = "already_classified"

    @property
    def is_unexpected(self) -> bool:
        """True only for the one cause that must exit non-zero."""
        return self is ShutdownCause.UNEXPECTED


class ShutdownClassifier:
    """Latches the first answer to "why is this gateway stopping?"."""

    def __init__(self) -> None:
        self._cause: Optional[ShutdownCause] = None

    @property
    def cause(self) -> Optional[ShutdownCause]:
        """The latched cause, or None before the first classification."""
        return self._cause

    def classify(self, received_signal=None) -> ShutdownCause:
        """Classify this shutdown trigger, consuming markers on the first call."""
        if self._cause is not None:
            return ShutdownCause.ALREADY_CLASSIFIED

        # Planned --replace takeover: the replacer wrote a marker naming this
        # PID before signalling, so systemd's Restart=on-failure doesn't revive
        # us and flap-fight the replacer.
        planned_takeover = False
        try:
            from gateway.status import consume_takeover_marker_for_self

            planned_takeover = consume_takeover_marker_for_self()
        except Exception as e:
            logger.debug("Takeover marker check failed: %s", e)

        # Planned stop: service managers and `hermes gateway stop` send the same
        # SIGTERM an external kill does, so the stopper marks it first. SIGINT
        # comes from an interactive Ctrl+C and is likewise an intentional stop.
        planned_stop = False
        if received_signal == signal.SIGINT:
            planned_stop = True
        elif not planned_takeover:
            try:
                from gateway.status import consume_planned_stop_marker_for_self

                planned_stop = consume_planned_stop_marker_for_self()
            except Exception as e:
                logger.debug("Planned stop marker check failed: %s", e)

        if planned_takeover:
            self._cause = ShutdownCause.TAKEOVER
        elif planned_stop:
            self._cause = ShutdownCause.PLANNED_STOP
        else:
            self._cause = ShutdownCause.UNEXPECTED
        return self._cause
