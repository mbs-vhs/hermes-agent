"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so:

  - ``_python_exit`` never joins them, and
  - the interpreter's non-daemon thread join at shutdown skips them.

Semantics are otherwise identical (initializer/initargs, work queue,
idle-thread reuse).  Use it for any pool whose work is best-effort or
independently interruptible and must never hold the process open:
concurrent tool execution, background memory sync, catalog fan-out,
subagent timeout wrappers.  Do NOT use it for work that must complete
before exit (durable writes) — those belong on foreground threads with
explicit bounded joins.

Implementation note — why this does NOT copy CPython's spawn logic.  It
used to: ``_adjust_thread_count`` was a verbatim copy of the 3.8–3.13 body
with ``daemon=True`` added, which meant it called the private ``_worker``
with a hardcoded positional signature.  3.14 changed that signature
(``_worker(ref, ctx, queue)``; ``_initializer``/``_initargs`` were replaced
by a worker-context factory), so every ``submit()`` raised
``AttributeError: no attribute '_initializer'`` — a total breakage of the
class on that interpreter (CLAWD-3785).  Both required properties are
reachable without owning that code:

  - ``daemon``: ``threading.Thread`` inherits the daemon flag from the
    creating thread when ``daemon=`` is omitted (documented behaviour), and
    CPython's ``_adjust_thread_count`` has omitted it since 3.9.  Spawning
    from a daemon thread therefore yields daemon workers.
  - registration: ``_threads_queues`` entries are removed after the fact.

What is left is the two no-op guards (idle worker available / pool already
at capacity), which exist only so the daemon hop is paid when a worker is
actually about to be created.  Measured on 3.11, median of 9 runs of 20k
submits into a 4-worker pool: 7.8 µs/submit stdlib, 7.7 µs/submit here
(4 hops in total); with the guards removed it is 52.6 µs/submit.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues

__all__ = ["DaemonThreadPoolExecutor"]


def _run_on_daemon_thread(fn: Callable[[], None]) -> None:
    """Run ``fn`` on a throwaway daemon thread, re-raising on the caller's.

    Any ``threading.Thread`` ``fn`` creates without an explicit ``daemon=``
    argument inherits ``daemon=True`` from this thread.
    """
    raised: list[BaseException] = []

    def _target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            raised.append(exc)

    runner = threading.Thread(target=_target, name="daemon-pool-spawn", daemon=True)
    runner.start()
    runner.join()
    if raised:
        raise raised[0]


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def _adjust_thread_count(self) -> None:
        # CPython's own first line: an idle worker is parked, so no thread
        # will be created and there is nothing for us to do differently.
        if self._idle_semaphore.acquire(timeout=0):
            return

        known = frozenset(self._threads)
        if threading.current_thread().daemon or len(self._threads) >= self._max_workers:
            # Either a new worker would already inherit daemon=True, or the
            # pool is at capacity and CPython will not create one.  Note this
            # branch still calls super() rather than returning early, so if
            # that capacity guard ever changes the work still happens.
            super()._adjust_thread_count()
        else:
            _run_on_daemon_thread(super()._adjust_thread_count)

        # Undo the atexit-join registration super() just made.  This runs
        # inside submit()'s `with self._shutdown_lock, _global_shutdown_lock`
        # block, and _python_exit() takes _global_shutdown_lock before it
        # snapshots _threads_queues, so the two cannot interleave.
        for worker in self._threads - known:
            _threads_queues.pop(worker, None)
