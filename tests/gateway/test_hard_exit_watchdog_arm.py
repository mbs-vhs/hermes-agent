"""Behavioural contract for arm_post_stop_exit_watchdog (CLAWD-2837 / CLAWD-1023).

The watchdog itself was previously a nested closure inside
``start_gateway``'s ``shutdown_signal_handler``, and the old contract test's
docstring declared it "not cleanly unit-testable … We do not force a test around
it." Parking it in ``gateway/hard_exit.py`` makes that statement false, so this
file is the test that disclaimer said would not exist.

Why it matters that these are tested and not merely moved: this is the shutdown
path for 11 live gateways, and a botched extraction fails **silently** until a
hang. Nothing here calls the real ``os._exit`` — it is monkeypatched, as is
``asyncio.sleep`` where the grace would otherwise be waited out.
"""

import asyncio
import logging

import pytest

import gateway.hard_exit as hard_exit
import gateway.run as gateway_run

# Captured at import time, before any test patches asyncio.sleep. Tests that
# replace sleep must await THIS, never asyncio.sleep, or they recurse.
_REAL_ASYNCIO_SLEEP = asyncio.sleep


class _FakeRunner:
    """Minimal stand-in for GatewayRunner's shutdown surface."""

    def __init__(self, *, exit_code=None, should_fail=False,
                 restart_requested=False, restart_via_service=False,
                 wait_raises=None):
        self.exit_code = exit_code
        self.should_exit_with_failure = should_fail
        self._restart_requested = restart_requested
        self._restart_via_service = restart_via_service
        self._wait_raises = wait_raises
        self.waited = False

    async def wait_for_shutdown(self):
        self.waited = True
        if self._wait_raises is not None:
            raise self._wait_raises


@pytest.fixture
def captured(monkeypatch):
    """Capture os._exit codes and skip the real grace sleep."""
    exits = []
    slept = []

    monkeypatch.setattr(hard_exit.os, "_exit", lambda code: exits.append(code))

    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *a, **kw):
        slept.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr(hard_exit.asyncio, "sleep", _fake_sleep)
    return {"exits": exits, "slept": slept}


async def _arm_and_settle(runner, captured, **kwargs):
    task = hard_exit.arm_post_stop_exit_watchdog(runner, **kwargs)
    await task
    return task


# ── (a) arm -> wait_for_shutdown awaited -> grace slept -> os._exit(code) ────

@pytest.mark.asyncio
async def test_arms_waits_sleeps_then_exits_with_resolved_code(captured):
    runner = _FakeRunner(exit_code=7)
    await _arm_and_settle(runner, captured,
                          signal_initiated=lambda: False, grace_seconds=20.0)
    assert runner.waited is True, "must await wait_for_shutdown() before the grace"
    assert captured["slept"] == [20.0], "must sleep exactly the effective grace"
    assert captured["exits"] == [7], "must os._exit with the resolved code"


@pytest.mark.asyncio
async def test_returns_a_task_so_the_caller_can_track_it(captured):
    runner = _FakeRunner()
    task = hard_exit.arm_post_stop_exit_watchdog(
        runner, lambda: False, grace_seconds=0.0
    )
    assert isinstance(task, asyncio.Task)
    await task


# ── (b) LATE BINDING — the headline trap ────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_initiated_is_read_at_fire_time_not_arm_time(captured, monkeypatch):
    """THE trap this refactor had to avoid. shutdown_signal_handler could fire more
    than once: a planned stop (flag False) then a later unexpected signal (flag
    True). The original closure read the start_gateway local at FIRE time. If the
    extraction had taken a plain bool, the value would be snapshotted at arm time
    and shutdown semantics would change silently.

    READ THE TENSE: since CLAWD-3786's ShutdownClassifier latch, that False -> True
    transition is UNREACHABLE in the real gateway — the verdict is latched for the
    process's life and the flag is set before the watchdog is armed. This test
    still passes because it drives a synthetic lambda over a dict, not the real
    closure, so it pins the CONTRACT of arm_post_stop_exit_watchdog (a callable is
    read at fire time) and NOT a sequence the gateway can still produce.

    Keeping it is deliberate: the contract is what a future caller relies on. But
    do not read a green run here as evidence that the live double-signal path
    works — nothing in this file exercises it, and it no longer exists.

    Here: arm with the flag False, flip it to True before the grace elapses, and
    assert the FIRED code reflects the new value (1, not 0)."""
    flag = {"signal_initiated": False}
    runner = _FakeRunner(restart_requested=False)

    # Capture the REAL sleep before patching: hard_exit.asyncio is the shared
    # asyncio module, so calling asyncio.sleep() inside the replacement would
    # call the replacement (infinite recursion — it did, on first run).
    real_sleep = _REAL_ASYNCIO_SLEEP

    async def _flip_then_sleep(delay, *a, **kw):
        flag["signal_initiated"] = True   # changes AFTER arming
        return await real_sleep(0)

    monkeypatch.setattr(hard_exit.asyncio, "sleep", _flip_then_sleep)
    await _arm_and_settle(
        runner, captured,
        signal_initiated=lambda: flag["signal_initiated"],
        grace_seconds=1.0,
    )

    # signal_initiated True + not restart_requested -> 1. A snapshot would give 0.
    assert captured["exits"] == [1], (
        "exit code did not reflect the post-arm flag value — signal_initiated was "
        "snapshotted at arm time instead of called at fire time"
    )


@pytest.mark.asyncio
async def test_rejects_a_bare_bool_by_failing_loudly(captured):
    """A bool is not callable, so a regression back to passing the bare name
    surfaces as a TypeError rather than silently wrong exit codes."""
    runner = _FakeRunner()
    with pytest.raises(TypeError):
        await _arm_and_settle(runner, captured,
                              signal_initiated=True, grace_seconds=0.0)


# ── (c) an exception from wait_for_shutdown() is swallowed, exit still fires ──

@pytest.mark.asyncio
async def test_exception_from_wait_for_shutdown_is_swallowed_and_exit_still_fires(captured):
    """If wait_for_shutdown() raises, the watchdog must still force the exit —
    otherwise the one path that guarantees the process dies is the one that
    silently disappears."""
    runner = _FakeRunner(exit_code=3, wait_raises=RuntimeError("boom"))
    await _arm_and_settle(runner, captured,
                          signal_initiated=lambda: False, grace_seconds=0.0)
    assert captured["exits"] == [3]


# ── (d) grace resolution: default, env override, explicit injection ──────────
# The card requires _HARD_EXIT_GRACE_SEC stay an IMPORT-TIME constant, so
# monkeypatch.setenv cannot affect an already-imported module attribute. Each
# mechanism is therefore tested by the only means that actually exercises it.

def test_grace_helper_reads_env_directly(monkeypatch):
    monkeypatch.setenv("HERMES_HARD_EXIT_GRACE_SEC", "42.5")
    assert hard_exit._hard_exit_grace_seconds() == 42.5


def test_grace_helper_defaults_to_20(monkeypatch):
    monkeypatch.delenv("HERMES_HARD_EXIT_GRACE_SEC", raising=False)
    assert hard_exit._hard_exit_grace_seconds() == 20.0


def _fresh_hard_exit_copy():
    """Import a PRIVATE copy of gateway.hard_exit under a throwaway name.

    Deliberately not importlib.reload(hard_exit): reload rebinds new function
    objects on the shared module, which silently breaks gateway.run's re-bind
    identity for every later test in this file (it did, on first run). A private
    copy exercises the import-time constant path without mutating shared state.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_hard_exit_probe_copy", hard_exit.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bad", ["", "abc", "not-a-number"])
def test_malformed_grace_does_not_raise_at_import(monkeypatch, bad):
    """A malformed env value must never crash module import — that would take
    every gateway down at once."""
    monkeypatch.setenv("HERMES_HARD_EXIT_GRACE_SEC", bad)
    assert hard_exit._hard_exit_grace_seconds() == 20.0
    fresh = _fresh_hard_exit_copy()   # import must not raise
    assert fresh._hard_exit_grace_seconds() == 20.0


def test_import_time_constant_honours_env(monkeypatch):
    """The import-time path, exercised the only way it can be: a fresh import
    with the env set."""
    monkeypatch.setenv("HERMES_HARD_EXIT_GRACE_SEC", "5")
    fresh = _fresh_hard_exit_copy()
    assert fresh._hard_exit_grace_seconds() == 5.0


@pytest.mark.asyncio
async def test_explicit_grace_seconds_overrides_the_env(monkeypatch, captured):
    monkeypatch.setenv("HERMES_HARD_EXIT_GRACE_SEC", "99")
    runner = _FakeRunner()
    await _arm_and_settle(runner, captured,
                          signal_initiated=lambda: False, grace_seconds=1.5)
    assert captured["slept"] == [1.5]


@pytest.mark.asyncio
async def test_logs_the_effective_grace_not_the_module_constant(captured, caplog):
    """Once grace_seconds is an override, formatting the module constant would
    make the forensic warning lie about how long it actually waited."""
    runner = _FakeRunner()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await _arm_and_settle(runner, captured,
                              signal_initiated=lambda: False, grace_seconds=3.0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the hard-exit warning must be emitted"
    assert "3s" in warnings[0] or "3 " in warnings[0], (
        f"warning must report the EFFECTIVE grace (3.0), got: {warnings[0]!r}"
    )


# ── the re-bind contract ────────────────────────────────────────────────────

def test_run_py_rebinds_the_resolver_so_existing_importers_keep_working():
    """tests/gateway/test_hard_exit_exit_code.py does
    `from gateway.run import _resolve_hung_shutdown_exit_code`. That import is
    deliberately left unchanged — it is the live proof the re-bind holds, and it
    also keeps that file importing gateway.run so it doubles as an import-health
    check. Asserting the identity belongs here."""
    assert (
        gateway_run._resolve_hung_shutdown_exit_code
        is hard_exit._resolve_hung_shutdown_exit_code
    )
    assert gateway_run._hard_exit_grace_seconds is hard_exit._hard_exit_grace_seconds


def test_arm_call_site_passes_a_callable_not_a_bare_name():
    """Pin the wiring at the only place it can regress. `rg` the source rather
    than the object, because the call site is inside a nested handler that no test
    invokes."""
    import inspect
    import re

    source = inspect.getsource(gateway_run)
    calls = re.findall(r"arm_post_stop_exit_watchdog\((.*?)\n\s*\)", source, re.S)
    assert len(calls) == 1, f"expected exactly one arm call site, found {len(calls)}"
    assert "lambda:" in calls[0], (
        "the arm call must pass a zero-arg callable (lambda: _signal_initiated_shutdown), "
        f"not a bare name — got: {calls[0]!r}"
    )


def test_logger_name_preserved():
    assert hard_exit.logger.name == "gateway.run"


def test_delete_at_merge_tag_present():
    """The tag is the mechanism that stops this parking space becoming permanent."""
    from pathlib import Path

    text = Path(hard_exit.__file__).read_text(encoding="utf-8")
    assert "DELETE AT MERGE (CLAWD-2837)" in text
    assert "shutdown_watchdog.py" in text, "must name upstream's replacement module"
