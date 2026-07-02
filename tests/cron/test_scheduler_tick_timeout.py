"""Independent verification for CLAWD-1674.

tick() runs parallel cron jobs through a ThreadPoolExecutor and drains their
futures with ``concurrent.futures.as_completed(_futures, timeout=600)``. The
*iterator itself* raises ``concurrent.futures.TimeoutError`` at the 600s
wall-clock cap when one or more jobs are still running. Before the fix only the
per-future ``f.result()`` exceptions were caught, so the iterator timeout
propagated out of tick() and killed the whole cron cycle.

These tests exercise that new path by substituting a fake ``as_completed`` that
yields the already-completed futures and then raises the timeout — with one job
deliberately still running at the moment the timeout fires. We assert tick():
  * RETURNS (does not propagate the TimeoutError),
  * logs the timeout at ERROR with the correct pending/total count,
  * counts the completed jobs (not lost) and the still-running job as failed
    (not double-counted).
"""

import concurrent.futures
import logging
import threading
import time

import pytest


def _run_with_watchdog(fn, timeout=15.0):
    """Run ``fn`` in a thread; fail loudly instead of hanging the suite."""
    box = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raise on the main thread
            box["error"] = exc

    t = threading.Thread(target=_target, name="tick-under-test", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(
            f"tick() did not return within {timeout}s — TimeoutError propagated "
            "or the pool join hung."
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.mark.timeout(30)
class TestTickAsCompletedTimeout:
    def _install_common_stubs(self, monkeypatch, sched, release_event, ran):
        """Stub the scheduler helpers tick() touches so no real work happens."""

        def fake_run_job(job):
            if job["id"] == "slow":
                # Block until the fake as_completed releases us — this keeps the
                # future un-done at the instant the timeout is raised.
                assert release_event.wait(10), "slow job was never released"
            ran.append(job["id"])
            return True, "output", "response", None

        monkeypatch.setattr(sched, "advance_next_run", lambda *_a, **_k: None)
        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_k: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_k: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_k: None)

    def _fake_as_completed_factory(self, release_event, n_expected_done):
        """Return a fake as_completed that yields done futures, then times out.

        It waits until ``n_expected_done`` futures have completed, yields those,
        schedules the slow job's release (so the pool __exit__ join can finish),
        and finally raises the iterator-level TimeoutError — exactly what the
        real ``as_completed`` does at its wall-clock cap.
        """

        def fake_as_completed(fs, timeout=None):
            fs = list(fs)
            deadline = time.monotonic() + 8
            while (
                sum(1 for f in fs if f.done()) < n_expected_done
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            done = [f for f in fs if f.done()]
            # Release the slow job AFTER the timeout is handled so the pool's
            # __exit__ join doesn't block forever. The except handler runs
            # synchronously right after the raise (well under 0.25s), so the
            # slow future is still un-done when _pending is computed.
            threading.Timer(0.25, release_event.set).start()
            for f in done:
                yield f
            raise concurrent.futures.TimeoutError()

        return fake_as_completed

    def test_iterator_timeout_does_not_propagate_and_counts_pending_failed(
        self, monkeypatch, caplog
    ):
        import cron.scheduler as sched

        jobs = [
            {"id": "fast1", "name": "F1", "workdir": None, "profile": None},
            {"id": "fast2", "name": "F2", "workdir": None, "profile": None},
            {"id": "slow", "name": "S", "workdir": None, "profile": None},
        ]
        monkeypatch.setattr(sched, "get_due_jobs", lambda: list(jobs))

        release_event = threading.Event()
        ran: list[str] = []
        self._install_common_stubs(monkeypatch, sched, release_event, ran)

        monkeypatch.setattr(
            concurrent.futures,
            "as_completed",
            self._fake_as_completed_factory(release_event, n_expected_done=2),
        )

        with caplog.at_level(logging.ERROR, logger="cron.scheduler"):
            n = _run_with_watchdog(lambda: sched.tick(verbose=False))

        # 1) tick RETURNED (did not propagate TimeoutError) and counted only the
        #    two completed jobs. The still-running job is counted failed (False),
        #    so it neither inflates (double-count) nor is silently dropped.
        assert n == 2, f"expected 2 completed jobs counted, got {n}"

        # 2) The timeout was logged at ERROR with the correct pending/total split
        #    (1 of 3 still running) — proving done futures were excluded from the
        #    pending set (no double-count) and the pending one was accounted for.
        timeout_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "Parallel cron tick timed out" in r.getMessage()
        ]
        assert timeout_records, "expected a timeout ERROR log record"
        assert "1/3 jobs still running" in timeout_records[0].getMessage()

    def test_all_futures_pending_returns_zero(self, monkeypatch, caplog):
        """If the timeout fires before ANY future completes, tick still returns
        (0 jobs counted) rather than propagating."""
        import cron.scheduler as sched

        jobs = [
            {"id": "slow", "name": "S", "workdir": None, "profile": None},
        ]
        monkeypatch.setattr(sched, "get_due_jobs", lambda: list(jobs))

        release_event = threading.Event()
        ran: list[str] = []
        self._install_common_stubs(monkeypatch, sched, release_event, ran)

        monkeypatch.setattr(
            concurrent.futures,
            "as_completed",
            self._fake_as_completed_factory(release_event, n_expected_done=0),
        )

        with caplog.at_level(logging.ERROR, logger="cron.scheduler"):
            n = _run_with_watchdog(lambda: sched.tick(verbose=False))

        assert n == 0, f"expected 0 jobs counted when all still running, got {n}"
        assert any(
            "1/1 jobs still running" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
        ), "expected a 1/1 timeout ERROR log record"
