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
actually about to be created.  They are not redundant — each is the sole
contributor under one of the two shapes this pool is used in.  µs/submit,
median of 7 runs of 20k submits, CPython 3.11:

                        unsaturated          saturated
                        (max_workers=4,      (max_workers=1, worker
                         fast tasks)          permanently busy — the
                                              memory_manager /
                                              delegate_tool shape)
  stdlib                       4.7                 4.6
  both guards (this)           6.7                 7.0
  idle guard only              7.0                41.6
  capacity guard only         15.5                 6.4
  neither                     54.0                40.3

The hop also widens ``submit()``'s hold on the process-global
``_global_shutdown_lock``, from a bare ``Thread.start()`` to a start plus a
join: median 20.2 µs -> 52.4 µs (p95 40.8 -> 119.4) per *spawning* call, so
at most ``max_workers`` times in a pool's life.  Accepted deliberately —
dropping the join would let ``submit()`` return before the worker exists,
lose ``Thread.start()`` errors, and let concurrent submits over-spawn
against a stale ``len(self._threads)``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues

__all__ = ["DaemonThreadPoolExecutor"]

logger = logging.getLogger(__name__)

SPAWN_THREAD_NAME = "daemon-pool-spawn"

# Upper bound on how long an interrupted caller waits for the spawn thread
# before unwinding anyway.  That thread only starts a worker and pops a dict
# key, so this is orders of magnitude more than it can need; it exists so a
# pathological case degrades to the old leak instead of to a hang.
_SPAWN_UNWIND_GRACE_S = 5.0

# The same, for an interrupt that landed in Thread.start().  Deliberately much
# shorter: start() also raises when the OS refuses a new thread, and then no
# runner exists and nothing will EVER set the completion Event, so the full
# grace would be spent holding _global_shutdown_lock for a runner that cannot
# arrive.  This is a CAP CHOSEN TO BOUND THE FAILED-CREATION CASE, not a margin
# a live runner cannot exceed -- the ~1500x figure it was first justified with is
# an IDLE-HOST number.  Measured on this 24-core host under 16 busy threads + 16
# busy processes, start() -> first statement of _target has p95 378ms and exceeds
# this 100ms budget on 26% of spawns (30% at 64+64).  The two effects are
# positively correlated: start() blocks in _started.wait() precisely while the
# child is slow to bootstrap.  So under oversubscription a live runner can be
# mistaken for one that never launched, skipping the grace and leaving the same
# TRANSIENT window this function exists to close -- bounded, never a leak, because
# _spawn_and_detach's finally still runs on the runner.  The invariant above is
# correct; this constant is a supporting trade-off and is declared, not claimed.
_SPAWN_UNLAUNCHED_GRACE_S = 0.1


def _wait_through_signals(event: threading.Event, grace_s: float) -> bool:
    """Wait up to ``grace_s`` for ``event``, ignoring repeat signals.

    Bounded on purpose.  The ``remaining <= 0`` break is the only thing that
    bounds it: ``Event.wait()`` with a non-positive timeout returns
    immediately, so without that line an expired deadline becomes an unbounded
    busy-spin *while holding the process-global* ``_global_shutdown_lock`` —
    every ``submit()`` in the process wedged, which is worse than the transient
    window this wait exists to close.
    """
    deadline = time.monotonic() + grace_s
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event.wait(remaining)
        except BaseException:  # noqa: BLE001,S110 - a repeat signal; only the
            pass  # original is re-raised, by design (see _run_on_daemon_thread)
    return event.is_set()


def _await_spawn_completion(
    done: threading.Event, entered: threading.Event, launched: bool
) -> None:
    """Hold an interrupted caller until the spawn thread has finished."""
    if not launched and not _wait_through_signals(entered, _SPAWN_UNLAUNCHED_GRACE_S):
        # The interrupt landed inside Thread.start(), which is also where
        # start() raises when the OS refuses a new thread — and in that case no
        # runner exists, so nothing will EVER set `done` and spending the full
        # grace on the lock would be pure loss.  `entered` tells the two apart:
        # a runner that exists sets it as its first act.  It did not appear, so
        # there is nothing in flight to wait for.
        return
    _wait_through_signals(done, _SPAWN_UNWIND_GRACE_S)


def _run_on_daemon_thread(fn: Callable[[], None]) -> None:
    """Run ``fn`` on a throwaway daemon thread, re-raising on the caller's.

    Any ``threading.Thread`` ``fn`` creates without an explicit ``daemon=``
    argument inherits ``daemon=True`` from this thread.

    ``fn`` must own **every** side effect that needs undoing: nothing sequenced
    after the hop is guaranteed to run.  BOTH halves of the hop are
    signal-interruptible on the main thread — ``Thread.start()`` blocks in
    ``self._started.wait()`` until the child bootstraps, and ``Thread.join()``
    blocks in ``_wait_for_tstate_lock()`` — and a SIGINT in either unwinds the
    caller while the runner keeps going.  Both are therefore inside the ``try``
    and both are covered by the grace wait.

    Guarding only the join would leave the *majority* of the window open:
    ``start()`` measures ~34 µs against a join whose length is whatever is left
    of ``fn``, i.e. roughly two thirds of a real hop.  Measured on 3.11.15 and
    3.14.6 with a delayed bootstrap, an interrupt inside an unguarded
    ``start()`` unwound the caller a full second before the worker was created.

    The wait keys on a completion Event, NOT on ``runner.is_alive()``, and that
    is load-bearing rather than stylistic.  On CPython <= 3.13 an interrupted
    ``join()`` reaches the ``except:`` limb of
    ``Thread._wait_for_tstate_lock()``, and because a *live* thread's
    ``_tstate_lock`` is always held, that limb unconditionally calls
    ``self._stop()`` — marking a still-running thread stopped.  Measured on
    3.11.15: after an interrupted join, ``is_alive()`` is False while the
    thread is still in ``threading.enumerate()`` and its work is unfinished.
    An ``is_alive()``-keyed wait therefore exits at zero iterations on exactly
    the interpreter the gate and both production venvs run (3.14 replaced that
    path with ``_os_thread_handle.is_done()`` and is unaffected).

    NB the caller is unresponsive to further Ctrl-C for the duration of the
    grace wait — repeat KeyboardInterrupts are discarded, and only the first
    is re-raised.  In practice that window is the microseconds ``fn`` needs;
    the grace constants are only the pathological caps.

    A note on how this function got its shape, because the shape is the lesson.
    Rounds of review found, in order: the detach happening on the wrong thread;
    a grace wait that was inert on 3.11; and then this — a guarded ``join()``
    with an unguarded ``start()`` directly above it.  Each fix was scoped to
    where the last defect was found rather than to where the property has to
    hold.  If you change this function, restate the property first (*the caller
    must not leave submit()'s critical section while ``fn`` is mid-flight*) and
    then check every statement between ``fn``'s first side effect and the
    caller's return against it — not just the line the bug report points at.
    """
    raised: list[BaseException] = []
    entered = threading.Event()
    done = threading.Event()

    def _target() -> None:
        entered.set()  # proof-of-life; see _await_spawn_completion
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            raised.append(exc)
        finally:
            done.set()

    runner = threading.Thread(target=_target, name=SPAWN_THREAD_NAME, daemon=True)
    launched = False
    try:
        runner.start()
        # Only reached if start() returned; an interrupt inside it leaves this
        # False, and _await_spawn_completion() then has to work out whether a
        # runner exists at all before committing to the full grace.
        launched = True
        runner.join()
    except BaseException:
        # Signal delivery. Let the runner finish before unwinding, so the
        # caller cannot leave submit()'s critical section (and let
        # _python_exit() in) while _threads_queues is still half-updated.
        _await_spawn_completion(done, entered, launched)
        raise
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
        spawn = super()._adjust_thread_count

        def _spawn_and_detach() -> None:
            # Registration and de-registration must happen on the SAME thread.
            # Splitting them (spawn here, detach back on the caller) meant a
            # KeyboardInterrupt in the caller's join left a worker registered
            # in _threads_queues forever, which is precisely the exit-hang
            # this module exists to prevent (CLAWD-3785 review, B1).
            try:
                spawn()
            finally:
                self._detach_new_workers(known)

        if threading.current_thread().daemon or len(self._threads) >= self._max_workers:
            # Either a new worker would already inherit daemon=True, or the
            # pool is at capacity and CPython will not create one.  This
            # branch still calls super() rather than returning early, and
            # _detach_new_workers() below refuses to detach a worker that
            # came back non-daemon, so a future CPython that spawns here
            # anyway degrades to stdlib behaviour rather than to a hang.
            _spawn_and_detach()
        else:
            _run_on_daemon_thread(_spawn_and_detach)

    def _detach_new_workers(self, known: frozenset[threading.Thread]) -> None:
        """Undo the atexit-join registration CPython just made.

        Safe against ``_python_exit()`` because this runs inside ``submit()``'s
        ``with self._shutdown_lock, _global_shutdown_lock`` block.  Note the
        mechanism is the *lock*, not the snapshot: ``_python_exit()`` takes
        ``_global_shutdown_lock`` only to set the module-global ``_shutdown``
        and reads ``_threads_queues`` after releasing it.  What serialises the
        two is that ``_python_exit()`` cannot acquire that lock until an
        in-flight ``submit()`` has released it — by which point this has run —
        and a ``submit()`` that starts afterwards re-checks ``_shutdown``
        under the same lock and raises instead of reaching here.
        """
        for worker in self._threads - known:
            if not worker.daemon:
                # Unreachable on 3.8-3.14: the branches above only let CPython
                # create a worker from a daemon thread.  If a future version
                # creates one somewhere else, leaving it registered is the
                # safe failure — _python_exit() then sends it the sentinel and
                # joins it, i.e. stdlib behaviour.  Detaching it would be
                # strictly worse: threading._shutdown() joins non-daemon
                # threads regardless, and nothing would ever wake this one.
                logger.error(
                    "daemon_pool: %s was created non-daemon; leaving it "
                    "registered for the interpreter exit hook. Process exit "
                    "can now block on it. This means CPython's thread-spawn "
                    "internals changed shape — see tools/daemon_pool.py.",
                    worker.name,
                )
                continue
            _threads_queues.pop(worker, None)
