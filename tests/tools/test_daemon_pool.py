"""Tests for tools.daemon_pool.DaemonThreadPoolExecutor.

The daemon pool exists so abandoned workers (interrupted/timed-out tool
batches, wedged memory-provider syncs) can never block interpreter exit:
stdlib ThreadPoolExecutor workers are non-daemon AND registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every
worker unconditionally — even after shutdown(wait=False).

Both halves of that contract are asserted against a stdlib pool as the
positive control (test_stdlib_pool_is_the_positive_control): without it,
"worker.daemon is True" and "worker not in _threads_queues" could both be
passing for a reason unrelated to this class.
"""

import subprocess
import sys
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues

from tools.daemon_pool import DaemonThreadPoolExecutor


def test_workers_are_daemon_threads():
    pool = DaemonThreadPoolExecutor(max_workers=2)
    try:
        info = pool.submit(
            lambda: (threading.current_thread().daemon, threading.current_thread())
        ).result(timeout=10)
        is_daemon, worker = info
        assert is_daemon is True
        # Not registered with concurrent.futures' atexit join hook.
        assert worker not in _threads_queues
    finally:
        pool.shutdown(wait=True)


def test_stdlib_pool_is_the_positive_control():
    """The two assertions above must be able to fail — prove it on stdlib."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        worker = pool.submit(threading.current_thread).result(timeout=10)
        assert worker.daemon is False
        assert worker in _threads_queues
    finally:
        pool.shutdown(wait=True)


def test_every_worker_of_a_saturated_pool_is_detached():
    """Cover the at-capacity branch, not just the first spawn.

    _adjust_thread_count() takes a different path once len(_threads) reaches
    max_workers, so a pool that only ever creates one worker does not
    exercise it.  A barrier forces all four to exist simultaneously.
    """
    max_workers = 4
    barrier = threading.Barrier(max_workers, timeout=30)
    pool = DaemonThreadPoolExecutor(max_workers=max_workers)
    try:

        def _park():
            barrier.wait()
            return threading.current_thread()

        futures = [pool.submit(_park) for _ in range(max_workers)]
        workers = [f.result(timeout=30) for f in futures]
        assert len({w.name for w in workers}) == max_workers
        for worker in workers:
            assert worker.daemon is True, worker.name
            assert worker not in _threads_queues, worker.name
        # Submitting again once the pool is at capacity must not create a
        # registered/non-daemon worker either.
        assert pool.submit(lambda: "ok").result(timeout=10) == "ok"
        for worker in pool._threads:
            assert worker.daemon is True, worker.name
            assert worker not in _threads_queues, worker.name
    finally:
        barrier.abort()
        pool.shutdown(wait=True)


def test_submit_from_a_daemon_thread_still_detaches_workers():
    """The caller-is-already-daemon shortcut must honour the same contract."""
    result = {}

    def _submit_from_daemon():
        pool = DaemonThreadPoolExecutor(max_workers=1)
        try:
            result["worker"] = pool.submit(threading.current_thread).result(timeout=10)
        finally:
            pool.shutdown(wait=True)

    caller = threading.Thread(target=_submit_from_daemon, daemon=True)
    caller.start()
    caller.join(timeout=30)
    assert not caller.is_alive()
    worker = result["worker"]
    assert worker.daemon is True
    assert worker not in _threads_queues


def test_results_and_initializer_work_like_stdlib():
    """Regression guard for CLAWD-3785.

    initializer/initargs are no longer forwarded by this class — they are
    plumbed by CPython, whose private plumbing changed shape in 3.14 and
    took the old hand-rolled _adjust_thread_count() with it (every submit
    raised AttributeError: '_initializer').
    """
    seen = []

    def _init(tag):
        seen.append(tag)

    pool = DaemonThreadPoolExecutor(max_workers=1, initializer=_init, initargs=("t",))
    try:
        assert pool.submit(lambda: 41 + 1).result(timeout=10) == 42
        assert seen == ["t"]
    finally:
        pool.shutdown(wait=True)


def test_idle_worker_reuse():
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        tid1 = pool.submit(threading.get_ident).result(timeout=10)
        time.sleep(0.05)  # let the worker park on the idle semaphore
        tid2 = pool.submit(threading.get_ident).result(timeout=10)
        assert tid1 == tid2
    finally:
        pool.shutdown(wait=True)


def test_one_in_flight_task_creates_exactly_one_worker():
    """_adjust_thread_count must not spawn a redundant worker per submit.

    The task blocks on an event, so the worker can never find the queue
    empty, release its idle-semaphore token and let a second submit look
    like a first one.  That makes the thread count deterministic instead
    of a race against how fast the previous task finished.
    """
    gate = threading.Event()
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        future = pool.submit(gate.wait, 30)
        assert len(pool._threads) == 1
        gate.set()
        assert future.result(timeout=30) is True
    finally:
        gate.set()
        pool.shutdown(wait=True)


_ABANDON_SCRIPT = (
    "import sys; sys.path.insert(0, %r)\n"
    "from %s import %s as Pool\n"
    "import time\n"
    "pool = Pool(max_workers=1)\n"
    "pool.submit(time.sleep, %d)\n"
    "time.sleep(0.3)\n"
    "pool.shutdown(wait=False)\n"
    "print('main-done', flush=True)\n"
)

# The abandoned worker's sleep. Both tests below let the child run to
# completion rather than killing it on a timeout: subprocess.run()'s
# kill-on-timeout goes through os.kill(), which tests/conftest.py's
# live-system guard intercepts, so a timeout would surface as a confusing
# RuntimeError *and* still block for the full sleep in Popen.__exit__.
_ABANDON_SLEEP_S = 6


def _run_abandon_script(module, cls):
    script = _ABANDON_SCRIPT % (str(_repo_root()), module, cls, _ABANDON_SLEEP_S)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=_ABANDON_SLEEP_S * 10,
    )
    return proc, time.monotonic() - started


def test_wedged_worker_does_not_block_interpreter_exit():
    """A worker stuck in a sleep must not hold the process open.

    The child's main thread is done ~0.3s in; the abandoned worker sleeps
    6s. Exiting in under half the worker's sleep is the whole contract.
    """
    proc, elapsed = _run_abandon_script("tools.daemon_pool", "DaemonThreadPoolExecutor")
    assert proc.returncode == 0, proc.stderr
    assert "main-done" in proc.stdout
    assert elapsed < _ABANDON_SLEEP_S / 2, (
        f"interpreter exit took {elapsed:.1f}s with a {_ABANDON_SLEEP_S}s "
        "abandoned worker — something joined it"
    )


def test_stdlib_pool_wedged_worker_does_block_exit():
    """Positive control: stdlib waits out the worker on the same script.

    Without this, the test above could be green because the script exits
    fast for some reason having nothing to do with daemon workers.
    """
    proc, elapsed = _run_abandon_script("concurrent.futures", "ThreadPoolExecutor")
    assert proc.returncode == 0, proc.stderr
    assert elapsed >= _ABANDON_SLEEP_S / 2, (
        f"stdlib ThreadPoolExecutor exited in {elapsed:.1f}s with an abandoned "
        f"{_ABANDON_SLEEP_S}s worker — the exit-blocking behaviour this module "
        "works around is gone, so the test above measures nothing"
    )


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]
