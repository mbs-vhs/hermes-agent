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

        # Every call site, tagged with the chain of function scopes enclosing it.
        # COUNTING SITES IS NOT ENOUGH, and that was a real hole: the whole fix is
        # that the classifier is built ONCE PER GATEWAY LIFE, and moving the
        # constructor from start_gateway's body into shutdown_signal_handler makes
        # it once per INVOCATION -- the pre-CLAWD-3786 race verbatim -- while the
        # site count stays at exactly 1. Measured: that one-line move left
        # tests/gateway/ + tests/hermes_cli/ at 8638 passed with a failure set
        # IDENTICAL to the control, and the PR's own three test files at 93/93.
        # An assertion whose message says "per life" must be able to see placement.
        def call_sites(name: str) -> list:
            found = []

            def walk(node, scope):
                for child in ast.iter_child_nodes(node):
                    inner = scope
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inner = scope + (child.name,)
                    elif isinstance(child, ast.Lambda):
                        # A LAMBDA IS A NESTED CLOSURE and was invisible: the
                        # comment below claimed "or any other nested closure"
                        # while the walk tagged FunctionDef only, so
                        # `_make = lambda: ShutdownClassifier()` in start_gateway's
                        # body, called from the handler, defeated the guard
                        # completely at 9 passed / 0 failed. Measured.
                        inner = scope + ("<lambda>",)
                    if isinstance(child, ast.Call):
                        func = child.func
                        if (isinstance(func, ast.Name) and func.id == name) or (
                            isinstance(func, ast.Attribute) and func.attr == name
                        ):
                            found.append(scope)
                    walk(child, inner)

            walk(tree, ())
            return found

        def calls_to(name: str) -> list:
            return call_sites(name)

        sites = call_sites("ShutdownClassifier")
        assert len(sites) == 1, f"one classifier per life, found {len(sites)}: {sites}"
        # ...and it is constructed in start_gateway's OWN body, not inside the
        # handler (or any other nested closure), which is what makes it per-life.
        assert sites[0] == ("start_gateway",), (
            "the classifier must be built in start_gateway's body -- one per gateway "
            f"LIFE. Found it at scope {sites[0]}, which rebuilds it per call and "
            "restores the watcher race this card exists to close."
        )
        # Scoped to start_gateway deliberately (N1): an unscoped walk matches ANY
        # attribute named `classify` anywhere in gateway/run.py's ~26k lines, so a
        # future intent/router `.classify(...)` would redden this with the message
        # "one place decides" while nothing about shutdown had changed.
        decide = [s for s in call_sites("classify") if s and s[0] == "start_gateway"]
        assert len(decide) == 1, f"one place decides, found {len(decide)}: {decide}"
        # The marker consumes are destructive, so a second caller anywhere is a
        # second verdict: whoever loses the race gets no evidence.
        assert calls_to("consume_planned_stop_marker_for_self") == []
        assert calls_to("consume_takeover_marker_for_self") == []
