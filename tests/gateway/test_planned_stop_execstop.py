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

import json
import os

import pytest
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
        # The consume is destructive by design — a MARKER must not be able to
        # silence a second, genuinely unexpected signal. That property is about
        # the marker and does NOT extend to the ShutdownClassifier latch, which
        # keeps a PLANNED_STOP verdict for the process's life; see that module's
        # declared residual. This is the fourth site of that sentence and the one
        # the first sweep missed, because it wraps across two comment lines and a
        # single-line grep cannot see it.
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

    def test_a_hermes_home_that_disagrees_with_the_unit_is_REFUSED(
        self, monkeypatch, tmp_path, capsys
    ):
        """The deployment gap this rollout will actually hit, and the one a
        write-failure guard structurally cannot see.

        A HERMES_HOME mismatch between the unit's Environment= and the gateway's
        own does not make the write FAIL — it makes it SUCCEED into the wrong
        directory. Pre-fix: rc=0, stdout and stderr both EMPTY, marker in the
        unit's home, absent from the gateway's, gateway exits 1 and persists
        state=running. Silently the pre-CLAWD-3786 behaviour.

        The gateway writes gateway.pid under its OWN home, so a PID file naming
        somebody else is the tell that we are in the wrong directory."""
        wrong_home = tmp_path / "wrong-home"
        wrong_home.mkdir()
        # Somebody else's gateway lives here — the hallmark of the wrong home.
        # WRITTEN IN THE PRODUCT'S OWN FORMAT. The first version of this fixture
        # wrote "999999\n", a bare int the product NEVER writes, which is why it
        # passed over a guard that refused every real stop: the fixture and the
        # subject disagreed about the file, and only the fixture was consulted.
        (wrong_home / "gateway.pid").write_text(
            json.dumps({"pid": 999999, "kind": "hermes-gateway"}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(wrong_home))
        monkeypatch.setattr("gateway.status._get_pid_path", lambda: wrong_home / "gateway.pid")

        wrote = []
        monkeypatch.setattr(
            "gateway.status.write_planned_stop_marker",
            lambda pid: (wrote.append(pid), True)[1],
        )

        rc = planned_stop.main([str(os.getpid())])

        captured = capsys.readouterr()
        assert rc == 1, "a marker nobody will read is a refusal, not a success"
        assert wrote == [], "it must REFUSE before writing into the wrong home"
        assert "999999" in captured.err, "name the PID actually recorded there"
        assert str(os.getpid()) in captured.err, "and the PID we were asked to mark"
        assert "Environment=" in captured.err, "point at the unit directive to fix"

    def test_the_REAL_producer_round_trips_into_this_guard(self, monkeypatch, tmp_path):
        """THE MISSING CONTROL, and the one that would have caught a total
        regression: drive gateway.status.write_pid_file() — the actual producer —
        and then this consumer, instead of hand-writing a fixture.

        The guard shipped comparing the pid file's RAW TEXT to str(pid). The
        product writes json.dumps(_build_pid_record()), so the comparison was
        never equal and ExecStop refused EVERY stop on EVERY gateway, silently
        reinstating the pre-CLAWD-3786 defect on the systemd-native path this
        card exists to fix. Both hand-written fixtures passed over it because
        both used a format the product does not produce.

        This module's own docstring already declares the round trip as the
        load-bearing property — for the marker. The guard reads what the GATEWAY
        writes, and that direction had no round-trip test at all."""
        from gateway import status as gw_status

        home = tmp_path / "real-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        gw_status.write_pid_file()          # the real producer, real format

        wrote = []
        monkeypatch.setattr(
            "gateway.status.write_planned_stop_marker",
            lambda pid: (wrote.append(pid), True)[1],
        )

        rc = planned_stop.main([str(os.getpid())])

        assert rc == 0, "a HEALTHY stop in the CORRECT home must not be refused"
        assert wrote == [os.getpid()], "and the marker must actually be written"

    def test_the_real_producer_format_still_catches_a_genuine_mismatch(
        self, monkeypatch, tmp_path, capsys
    ):
        """The negative twin. Same real format, a record naming somebody else."""
        home = tmp_path / "other-home"
        home.mkdir()
        (home / "gateway.pid").write_text(
            json.dumps({"pid": 999999, "kind": "hermes-gateway"}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(
            "gateway.status.write_planned_stop_marker",
            lambda pid: pytest.fail("must refuse BEFORE writing"),
        )

        rc = planned_stop.main([str(os.getpid())])

        assert rc == 1
        assert "999999" in capsys.readouterr().err

    def test_a_corrupt_pid_file_does_not_crash_ExecStop(
        self, monkeypatch, tmp_path, capsys
    ):
        """read_text() on non-UTF-8 raises UnicodeDecodeError, which is NOT an
        OSError — the first version caught only OSError, so a corrupt gateway.pid
        killed ExecStop with a traceback and wrote no marker. gateway/status.py
        catches (OSError, UnicodeDecodeError) twenty lines away, for this."""
        home = tmp_path / "corrupt-home"
        home.mkdir()
        (home / "gateway.pid").write_bytes(b"\xff\xfe\x00binary garbage")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("gateway.status.write_planned_stop_marker", lambda pid: True)

        rc = planned_stop.main([str(os.getpid())])

        assert rc == 0, "a corrupt probe must not refuse a legitimate stop"
        assert capsys.readouterr().err == "" or True  # message optional, crash is not

    def test_an_unreadable_pid_file_does_not_refuse_the_stop(
        self, monkeypatch, tmp_path, capsys
    ):
        """Could-not-measure is not a finding. If the cross-check itself cannot
        run, say so and proceed — refusing every stop on an unreadable probe is a
        worse failure than the one being guarded."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        def _boom():
            raise OSError("probe unavailable")

        monkeypatch.setattr("gateway.status._get_pid_path", _boom)
        monkeypatch.setattr("gateway.status.write_planned_stop_marker", lambda pid: True)

        rc = planned_stop.main([str(os.getpid())])

        captured = capsys.readouterr()
        assert rc == 0, "an unreadable probe must not block a legitimate stop"
        assert "UNVERIFIED" in captured.err, "but it must not pass as verified either"

    def test_a_successful_write_is_quiet(self, capsys):
        """The stop path runs this on every gateway; a healthy stop must not
        put a line in the journal."""
        assert planned_stop.main([str(os.getpid())]) == 0

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
