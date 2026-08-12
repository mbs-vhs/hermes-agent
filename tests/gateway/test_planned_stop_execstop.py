"""Tests for the ExecStop planned-stop marker writer (CLAWD-3786).

``systemctl stop`` sends the unit's ``KillSignal`` directly, so none of the
Hermes stop paths that write a planned-stop marker ever run.  The gateway then
classifies an operator-initiated stop as an unexpected kill and exits 1, which
systemd reports as ``Result=exit-code`` / ``ActiveState=failed`` — a deliberate
stop becomes indistinguishable from a crash.  ``gateway.planned_stop`` is the
``ExecStop=`` hook that closes that gap by writing the same marker
``hermes gateway stop`` writes.

The load-bearing test here is the round trip: what this module writes must be
what the gateway's shutdown handler consumes.  Asserting only that some file
appeared would pass even if the two sides drifted apart.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import gateway
from gateway import planned_stop
from gateway.status import (
    _get_planned_stop_marker_path,
    consume_planned_stop_marker_for_self,
)

_REPO_ROOT = Path(gateway.__file__).resolve().parent.parent


def _unusable_pid() -> int:
    """A PID that cannot be alive: one past the kernel's maximum."""
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_max = 4194304
    return pid_max + 1


class TestExecStopMarkerRoundTrip:
    def test_marker_written_for_live_pid_is_consumed_by_that_process(self):
        """The end-to-end contract: ExecStop writes it, the shutdown handler
        consumes it and classifies the incoming SIGTERM as a planned stop.

        Uses this process' own PID as ``$MAINPID`` so the consumer's PID +
        start-time identity check runs for real."""
        assert planned_stop.main([str(os.getpid())]) == 0
        assert _get_planned_stop_marker_path().exists()

        assert consume_planned_stop_marker_for_self() is True
        # The consume is destructive by design — a marker must not be able to
        # silence a second, genuinely unexpected signal.
        assert consume_planned_stop_marker_for_self() is False

    def test_the_exact_command_line_in_the_unit_marks_the_stop(self):
        """systemd runs `<python> -m gateway.planned_stop $MAINPID`, not
        ``main()``. Exercise that literal command line so a broken module
        entry point cannot pass as green here and fail only in the journal."""
        result = subprocess.run(
            [sys.executable, "-m", "gateway.planned_stop", str(os.getpid())],
            # HERMES_HOME (the per-test sandbox) is inherited — the marker is
            # written into the home the unit pins, not the caller's.
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert consume_planned_stop_marker_for_self() is True

    def test_without_the_hook_the_shutdown_is_classified_unexpected(self):
        """The defect this fixes: no marker means the gateway takes the
        signal-initiated (exit 1) branch, whatever asked it to stop."""
        assert not _get_planned_stop_marker_path().exists()
        assert consume_planned_stop_marker_for_self() is False


class TestExecStopNoOpCases:
    """systemd runs ``ExecStop=`` only while the main process lives, but the
    hook must stay harmless when that assumption does not hold — writing a
    marker for a PID we do not own could silence a later unexpected signal."""

    def test_no_marker_when_mainpid_is_unexpanded(self):
        assert planned_stop.main(["$MAINPID"]) == 0
        assert not _get_planned_stop_marker_path().exists()

    def test_no_marker_without_an_argument(self):
        assert planned_stop.main([]) == 0
        assert not _get_planned_stop_marker_path().exists()

    def test_no_marker_for_a_non_numeric_argument(self):
        assert planned_stop.main(["not-a-pid"]) == 0
        assert not _get_planned_stop_marker_path().exists()

    def test_a_pid_that_names_nothing_is_rejected_before_the_liveness_check(self):
        """Asserted at the parse, not end-to-end, because end-to-end this is
        psutil's behaviour and not ours: with psutil present `_pid_exists(-1)`
        is False, but on the documented stdlib fallback it is `os.kill(-1, 0)`
        — every process the caller can signal — and returns True. Measured
        there: without this guard `main(['-1'])` writes `target_pid: -1`."""
        assert planned_stop._parse_main_pid(["-1"]) is None
        assert planned_stop._parse_main_pid(["0"]) is None

    def test_no_marker_for_a_dead_pid(self):
        assert planned_stop.main([str(_unusable_pid())]) == 0
        assert not _get_planned_stop_marker_path().exists()

    def test_reports_failure_when_the_marker_cannot_be_written(self, monkeypatch):
        """The unit wires this with a leading ``-`` so systemd ignores the
        code, but a failed write must not be reported as a successful one."""
        monkeypatch.setattr(
            "gateway.status.write_planned_stop_marker", lambda pid: False
        )
        assert planned_stop.main([str(os.getpid())]) == 1

    def test_a_failed_write_says_so_on_stderr(self, monkeypatch, capsys):
        """systemd discards the exit code (leading ``-``), so stderr is the only
        channel left. Without it, the single failure mode that reinstates the
        original defect is also the one that leaves no evidence."""
        monkeypatch.setattr(
            "gateway.status.write_planned_stop_marker", lambda pid: False
        )

        planned_stop.main([str(os.getpid())])

        captured = capsys.readouterr()
        assert str(os.getpid()) in captured.err
        assert "HERMES_HOME" in captured.err

    def test_a_successful_write_is_quiet(self, capsys):
        """The stop path runs this on every gateway; a healthy stop must not
        put a line in the journal."""
        assert planned_stop.main([str(os.getpid())]) == 0

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
