#!/usr/bin/env python3
"""Regression test for CLAWD-1673 — bounded /stop responsiveness.

The parallel-children batch loop in delegate_task() used to run its
ThreadPoolExecutor under a ``with`` block.  On a parent interrupt the loop's
``break`` fell through to the context-manager exit, which calls
``shutdown(wait=True)`` — blocking the parent until every stuck child reached
its next interrupt boundary (up to child_timeout).  That defeated /stop
responsiveness.

The fix (commit 9e10a630) switches to an explicit executor + try/finally:
on the interrupt break it calls ``shutdown(wait=False, cancel_futures=True)``
and bails with the already-built results, and the ``finally`` tears the
executor down with ``wait=False`` so it can never re-block.

This test proves the behavioral guarantee mechanically — no live LLM:

  * ``_run_single_child`` is monkeypatched to a child that sleeps ~5s and
    deliberately ignores the interrupt signal (simulating a stuck child).
  * Two children are submitted with ``max_concurrent_children == 2`` so BOTH
    futures actually start running on worker threads — they therefore CANNOT
    be cancelled by ``cancel_futures=True`` (only not-yet-started futures
    cancel).  This is the worst case: the only way to return fast is to NOT
    wait on the running futures.
  * A side thread sets ``parent._interrupt_requested = True`` ~0.2s in.
  * Assert delegate_task returns in well under the stuck-child duration
    (< 6.0s vs the child's 8s) and that the result set carries the fabricated
    "interrupted" entries with ``_child_role`` preserved.

BEFORE THE FIX this would have taken ~8s (the implicit ``shutdown(wait=True)``
at the ``with`` exit blocks on both running 8s children).  We do not revert
the fix here; the timing assertion (< 6.0s) is itself the discriminator —
the pre-fix code physically cannot satisfy it because two 8s daemon workers
are still running when the loop breaks (and running futures are not
cancellable), so the only way under the deadline is to NOT wait on them.
"""

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task

# Reuse the canonical mock-parent helper from the main delegate suite.
from tests.tools.test_delegate import _make_mock_parent


# How long the stuck child "runs" for, ignoring interruption.  Must be well
# above the return-deadline we assert, so a wait-on-children path is clearly
# distinguishable from a bail-without-waiting path.
STUCK_CHILD_SECONDS = 8.0

# delegate_task must return within this wall-clock budget once interrupted.
# The bail-without-waiting path costs: real _build_child_agent construction of
# two AIAgent instances on the main thread (~1s, observed), + the 0.2s
# interrupt delay + up to one 0.5s poll tick.  STUCK_CHILD_SECONDS * 0.75
# provides load headroom while staying well below the 8s a wait-on-children
# (pre-fix shutdown(wait=True)) path would take.
RETURN_DEADLINE_SECONDS = STUCK_CHILD_SECONDS * 0.75


class TestDelegateInterruptBounded(unittest.TestCase):
    def test_interrupt_returns_without_waiting_on_stuck_children(self):
        """/stop on a stuck parallel batch returns in bounded wall-clock."""

        start_barrier = threading.Event()

        def _stuck_child(task_index, goal, child=None, parent_agent=None, **_kw):
            # Signal that at least one worker thread has actually started so
            # the test can be sure the futures are RUNNING (not cancellable).
            start_barrier.set()
            # Ignore the interrupt entirely — a genuinely stuck child.
            time.sleep(STUCK_CHILD_SECONDS)
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "should never be observed by the parent",
                "api_calls": 0,
                "duration_seconds": STUCK_CHILD_SECONDS,
            }

        parent = _make_mock_parent()
        # Roles let us verify the fabricated entries carry _child_role.
        parent._interrupt_requested = False

        tasks = [
            {"goal": "stuck task A", "role": "leaf"},
            {"goal": "stuck task B", "role": "leaf"},
        ]

        def _interrupt_after_delay():
            # Wait until a worker has actually begun, then fire the interrupt
            # ~0.2s in so the poll loop observes it on a subsequent tick.
            start_barrier.wait(timeout=2.0)
            time.sleep(0.2)
            parent._interrupt_requested = True

        # Spy on the subagent_stop hook — that is where the fabricated entry's
        # internal ``_child_role`` is consumed (and then popped before the
        # results are serialized to JSON), so the role is only observable here.
        hook_spy = MagicMock()

        # Force max_concurrent_children == 2 so both children start running
        # concurrently (worst case: neither future is cancellable).
        with patch("tools.delegate_tool._run_single_child", side_effect=_stuck_child), \
             patch("tools.delegate_tool._get_max_concurrent_children", return_value=2), \
             patch("hermes_cli.plugins.invoke_hook", hook_spy):
            interrupter = threading.Thread(target=_interrupt_after_delay, daemon=True)
            interrupter.start()

            t0 = time.monotonic()
            raw = delegate_task(tasks=tasks, parent_agent=parent)
            elapsed = time.monotonic() - t0

        result = json.loads(raw)

        # 1) Bounded return: must NOT have waited on the 5s stuck children.
        self.assertLess(
            elapsed,
            RETURN_DEADLINE_SECONDS,
            f"delegate_task blocked {elapsed:.2f}s after interrupt — expected "
            f"< {RETURN_DEADLINE_SECONDS}s (stuck children run {STUCK_CHILD_SECONDS}s). "
            f"The pre-fix `with ThreadPoolExecutor` shutdown(wait=True) would block here.",
        )

        # 2) The interrupt was actually observed (the test isn't passing by
        #    finishing before the interrupt fired).
        self.assertTrue(parent._interrupt_requested, "interrupt flag was never set")

        # 3) Results carry the fabricated 'interrupted' entries.
        self.assertIn("results", result)
        entries = result["results"]
        self.assertEqual(len(entries), 2, f"expected 2 result entries, got {entries!r}")
        interrupted = [e for e in entries if e.get("status") == "interrupted"]
        self.assertGreaterEqual(
            len(interrupted),
            1,
            f"expected >=1 status=='interrupted' entry, got statuses "
            f"{[e.get('status') for e in entries]}",
        )

        # The fabricated entry should explain why it didn't finish.
        for e in interrupted:
            self.assertIn("interrupted", (e.get("error") or "").lower())

        # 4) _child_role is preserved on the fabricated entries.  The field is
        #    popped off the entry and passed to the subagent_stop hook before
        #    JSON serialization, so we verify it at that seam: at least one
        #    interrupted child fired subagent_stop with child_role='leaf' (the
        #    role the abandoned child was built with) and child_status carrying
        #    through the interrupted disposition.
        interrupted_hook_calls = [
            c
            for c in hook_spy.call_args_list
            if c.args and c.args[0] == "subagent_stop"
            and c.kwargs.get("child_status") == "interrupted"
        ]
        self.assertGreaterEqual(
            len(interrupted_hook_calls),
            1,
            f"expected >=1 subagent_stop hook with status 'interrupted'; "
            f"got calls {hook_spy.call_args_list!r}",
        )
        for c in interrupted_hook_calls:
            self.assertEqual(
                c.kwargs.get("child_role"),
                "leaf",
                f"interrupted child's subagent_stop fired with wrong "
                f"child_role: {c.kwargs!r}",
            )

    def test_normal_batch_still_collects_all_results(self):
        """No-interrupt regression guard: the try/finally rewrite must not
        change the happy path — every fast child is collected in order."""

        def _fast_child(task_index, goal, child=None, parent_agent=None, **_kw):
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": f"done {task_index}",
                "api_calls": 1,
                "duration_seconds": 0.0,
            }

        parent = _make_mock_parent()
        parent._interrupt_requested = False
        tasks = [{"goal": "A"}, {"goal": "B"}]

        with patch("tools.delegate_tool._run_single_child", side_effect=_fast_child), \
             patch("tools.delegate_tool._get_max_concurrent_children", return_value=2):
            result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

        entries = result["results"]
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["status"] for e in entries], ["completed", "completed"])
        # Sorted by task_index so output order matches input order.
        self.assertEqual(entries[0]["task_index"], 0)
        self.assertEqual(entries[1]["task_index"], 1)


if __name__ == "__main__":
    unittest.main()
