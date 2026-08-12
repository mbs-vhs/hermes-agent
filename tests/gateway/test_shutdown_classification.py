"""Tests for the once-per-life shutdown classification (CLAWD-3786).

The defect these pin is a race, so they drive the interleaving directly rather
than racing a 500ms poll: the planned-stop watcher tick and the SIGTERM behind
it are two calls into the same classifier, ~22ms apart in production, and the
first one destroys the marker the second would need. A test that reproduced it
by sleeping would pass on a fast host for the wrong reason.
"""

from __future__ import annotations

import os
import signal

from gateway.shutdown_classification import ShutdownCause, ShutdownClassifier
from gateway.status import (
    _get_planned_stop_marker_path,
    write_planned_stop_marker,
    write_takeover_marker,
)


class TestTheWatcherRace:
    def test_a_signal_behind_the_watcher_tick_stays_a_planned_stop(self):
        """THE REGRESSION. Watcher tick consumes the marker and classifies the
        stop; the SIGTERM that follows must not re-classify the life as an
        unexpected kill, which is what makes systemd report `failed`."""
        write_planned_stop_marker(os.getpid())
        classifier = ShutdownClassifier()

        # 1. `_run_planned_stop_watcher` -> `shutdown_handler(None)`.
        first = classifier.classify(None)
        assert first is ShutdownCause.PLANNED_STOP
        # The consume is destructive — that is the whole race.
        assert not _get_planned_stop_marker_path().exists()

        # 2. systemd's KillSignal, ~22ms later, with the evidence already gone.
        second = classifier.classify(signal.SIGTERM)

        assert second is ShutdownCause.ALREADY_CLASSIFIED
        assert second.is_unexpected is False
        assert classifier.cause is ShutdownCause.PLANNED_STOP

    def test_repeated_signals_do_not_reclassify_either(self):
        """`marker + two SIGTERMs` fired the handler twice and exited 1."""
        write_planned_stop_marker(os.getpid())
        classifier = ShutdownClassifier()

        assert classifier.classify(signal.SIGTERM) is ShutdownCause.PLANNED_STOP
        for _ in range(3):
            assert classifier.classify(signal.SIGTERM) is ShutdownCause.ALREADY_CLASSIFIED
        assert classifier.cause is ShutdownCause.PLANNED_STOP

    def test_a_marker_arriving_after_the_verdict_is_not_consumed(self):
        """Once settled, the probes do not run again: re-running a destructive
        consume can only discard evidence a later life might need."""
        classifier = ShutdownClassifier()
        assert classifier.classify(signal.SIGTERM) is ShutdownCause.UNEXPECTED

        write_planned_stop_marker(os.getpid())
        assert classifier.classify(signal.SIGTERM) is ShutdownCause.ALREADY_CLASSIFIED

        assert _get_planned_stop_marker_path().exists()
        assert classifier.cause is ShutdownCause.UNEXPECTED


class TestClassificationLadder:
    def test_an_unannounced_signal_is_unexpected(self):
        """The negative control: with no marker, a signal must still be a
        crash. Exiting 0 for everything would 'fix' the card by deleting the
        distinction it exists to protect."""
        classifier = ShutdownClassifier()

        cause = classifier.classify(signal.SIGTERM)

        assert cause is ShutdownCause.UNEXPECTED
        assert cause.is_unexpected is True

    def test_sigint_is_a_planned_stop_without_a_marker(self):
        """Ctrl+C is an intentional foreground stop; nobody writes a marker."""
        assert ShutdownClassifier().classify(signal.SIGINT) is ShutdownCause.PLANNED_STOP

    def test_a_takeover_marker_outranks_a_planned_stop_marker(self):
        """`--replace` is reported as a takeover even when both markers exist,
        and the planned-stop marker is left for whoever wrote it."""
        write_takeover_marker(os.getpid())
        write_planned_stop_marker(os.getpid())

        assert ShutdownClassifier().classify(signal.SIGTERM) is ShutdownCause.TAKEOVER
        assert _get_planned_stop_marker_path().exists()

    def test_only_unexpected_reports_itself_as_unexpected(self):
        """`is_unexpected` is the single input to the exit-code decision, so no
        other cause may answer True to it."""
        unexpected = [c for c in ShutdownCause if c.is_unexpected]

        assert unexpected == [ShutdownCause.UNEXPECTED]

    def test_no_verdict_before_the_first_signal(self):
        assert ShutdownClassifier().cause is None


class TestRunPyWiring:
    """Pin the wiring at the only place it can regress.

    `start_gateway`'s shutdown handler is a closure no test can invoke, so
    everything above would stay green if run.py stopped using the classifier
    and re-derived the answer itself — and the race would come back silently.
    Same technique, and the same reason, as
    `test_hard_exit_watchdog_arm.py::test_arm_call_site_passes_a_callable_not_a_bare_name`.
    """

    def test_the_handler_has_exactly_one_classification_point(self):
        # Parsed, not grepped: `_run_planned_stop_watcher`'s docstring *names*
        # the consume it delegates, and a substring check reads that prose as a
        # call. (It did — this assertion was red before its own mutation until
        # the AST replaced it.)
        import ast
        import inspect

        import gateway.run as gateway_run

        tree = ast.parse(inspect.getsource(gateway_run))

        def calls_to(name: str) -> list:
            found = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id == name:
                    found.append(node)
                elif isinstance(func, ast.Attribute) and func.attr == name:
                    found.append(node)
            return found

        assert len(calls_to("ShutdownClassifier")) == 1, "one classifier per life"
        assert len(calls_to("classify")) == 1, "one place decides"
        # The marker consumes are destructive, so a second caller anywhere is a
        # second verdict: whoever loses the race gets no evidence.
        assert calls_to("consume_planned_stop_marker_for_self") == []
        assert calls_to("consume_takeover_marker_for_self") == []
