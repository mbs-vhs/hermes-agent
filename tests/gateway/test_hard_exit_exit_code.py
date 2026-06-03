"""Exhaustive tests for gateway.run._resolve_hung_shutdown_exit_code (CLAWD-1023).

The hard-exit watchdog added in CLAWD-1023 force-exits a hung gateway shutdown
via os._exit(code). The exit *code* it uses is computed by the module helper
``_resolve_hung_shutdown_exit_code(runner, signal_initiated)``, which is
documented to MIRROR ``start_gateway()``'s post-``wait_for_shutdown()`` exit
decision so the watchdog never changes shutdown semantics.

This file tests the helper exhaustively against that documented contract:

  start_gateway() exit ladder (gateway/run.py ~18685-18742, mapped to codes):
    1. should_exit_with_failure truthy  -> return False -> sys.exit(1)   => 1
    2. exit_code is not None            -> raise SystemExit(exit_code)   => exit_code
    3. signal_initiated and not _restart_requested -> return False       => 1
    4. _restart_via_service             -> raise SystemExit(75)          => 75
    5. (else)                           -> return True (no sys.exit)      => 0

The helper's ladder must match branch-for-branch, including PRECEDENCE: when
multiple flags are set, the FIRST matching rung wins.

KNOWN COVERAGE BOUNDARY (CLAWD-1023): the watchdog's *timing / os._exit*
behavior (the ``_hard_exit_watchdog`` nested closure: await wait_for_shutdown()
+ real asyncio.sleep(grace) + os._exit) is not cleanly unit-testable — it is a
nested closure capturing locals, performs a real sleep, and hard-exits the
interpreter. We do not force a test around it. The exit-code helper is the
testable, deterministic core of the watchdog and is what this file covers.
"""

import itertools
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.run import _resolve_hung_shutdown_exit_code


def make_runner(
    *,
    should_exit_with_failure=False,
    exit_code=None,
    restart_requested=False,
    restart_via_service=False,
):
    """A tiny stub runner exposing exactly the four attrs the helper reads."""
    return SimpleNamespace(
        should_exit_with_failure=should_exit_with_failure,
        exit_code=exit_code,
        _restart_requested=restart_requested,
        _restart_via_service=restart_via_service,
    )


def reference_exit_code(runner, signal_initiated):
    """Independent re-derivation of start_gateway()'s exit decision.

    This is the documented CONTRACT, written separately from the production
    helper so the test would fail if the helper drifts from the ladder. It maps
    start_gateway()'s return/SystemExit behavior to the integer the process
    actually exits with (return False -> sys.exit(1); raise SystemExit(n) -> n;
    return True -> 0).
    """
    if runner.should_exit_with_failure:
        return 1
    if runner.exit_code is not None:
        return runner.exit_code
    if signal_initiated and not runner._restart_requested:
        return 1
    if runner._restart_via_service:
        return GATEWAY_SERVICE_RESTART_EXIT_CODE  # 75
    return 0


# ---------------------------------------------------------------------------
# Rung 1: should_exit_with_failure (highest precedence)
# ---------------------------------------------------------------------------

def test_should_exit_with_failure_returns_1():
    runner = make_runner(should_exit_with_failure=True)
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=False) == 1


def test_should_exit_with_failure_wins_over_exit_code():
    # exit_code=75 would otherwise return 75, but failure flag takes precedence.
    runner = make_runner(should_exit_with_failure=True, exit_code=75)
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=False) == 1


def test_should_exit_with_failure_wins_over_everything():
    runner = make_runner(
        should_exit_with_failure=True,
        exit_code=0,
        restart_requested=True,
        restart_via_service=True,
    )
    # Every other rung is set; rung 1 must still win for both signal values.
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 1
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=False) == 1


# ---------------------------------------------------------------------------
# Rung 2: exit_code is not None (when failure flag is False)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [0, 1, 75, 2, 42, 130])
def test_exit_code_passthrough(code):
    runner = make_runner(exit_code=code)
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=False) == code


def test_exit_code_zero_is_honored_not_treated_as_unset():
    # exit_code=0 is "not None" -> must short-circuit to 0, NOT fall through to
    # the signal_initiated rung (which would return 1). Guards the classic
    # 0-vs-None bug.
    runner = make_runner(exit_code=0)
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 0


def test_exit_code_wins_over_signal_and_restart_via_service():
    runner = make_runner(exit_code=42, restart_via_service=True)
    # signal_initiated True, _restart_requested False would give rung-3 == 1,
    # and _restart_via_service would give 75; exit_code must win over both.
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 42


# ---------------------------------------------------------------------------
# Rung 3: signal_initiated and not _restart_requested -> 1
# ---------------------------------------------------------------------------

def test_signal_initiated_without_restart_request_returns_1():
    runner = make_runner()  # all False, exit_code None
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 1


def test_signal_initiated_with_restart_request_falls_through():
    # _restart_requested True suppresses rung 3 (planned restart). With nothing
    # else set, falls all the way to rung 5 -> 0 (NOT 1).
    runner = make_runner(restart_requested=True)
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 0


def test_signal_initiated_with_restart_request_and_via_service_returns_75():
    # Signal + restart requested falls past rung 3; _restart_via_service catches
    # at rung 4 -> 75 (a planned service restart triggered by a signal).
    runner = make_runner(restart_requested=True, restart_via_service=True)
    assert (
        _resolve_hung_shutdown_exit_code(runner, signal_initiated=True)
        == GATEWAY_SERVICE_RESTART_EXIT_CODE
    )


# ---------------------------------------------------------------------------
# Rung 4: _restart_via_service -> 75
# ---------------------------------------------------------------------------

def test_restart_via_service_returns_75():
    runner = make_runner(restart_via_service=True)
    assert (
        _resolve_hung_shutdown_exit_code(runner, signal_initiated=False)
        == GATEWAY_SERVICE_RESTART_EXIT_CODE
    )
    assert GATEWAY_SERVICE_RESTART_EXIT_CODE == 75


# ---------------------------------------------------------------------------
# Rung 5: nothing set -> 0 (planned stop / takeover, non-signal)
# ---------------------------------------------------------------------------

def test_all_false_non_signal_returns_0():
    runner = make_runner()
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=False) == 0


# ---------------------------------------------------------------------------
# Stub-shape parity: a MagicMock with the same attrs behaves identically
# ---------------------------------------------------------------------------

def test_magicmock_runner_stub_matches_simplenamespace():
    runner = MagicMock()
    runner.should_exit_with_failure = False
    runner.exit_code = None
    runner._restart_requested = False
    runner._restart_via_service = True
    assert (
        _resolve_hung_shutdown_exit_code(runner, signal_initiated=False)
        == GATEWAY_SERVICE_RESTART_EXIT_CODE
    )


def test_missing_should_exit_with_failure_attr_defaults_false():
    # The helper uses getattr(runner, "should_exit_with_failure", False), so a
    # runner lacking that attr must not raise and must behave as if it were
    # False (here: falls through to signal rung -> 1).
    runner = SimpleNamespace(
        exit_code=None, _restart_requested=False, _restart_via_service=False
    )
    assert _resolve_hung_shutdown_exit_code(runner, signal_initiated=True) == 1


# ---------------------------------------------------------------------------
# Exhaustive precedence sweep: helper == documented contract for EVERY combo
# ---------------------------------------------------------------------------

_EXIT_CODES = [None, 0, 1, 75]
_BOOLS = [False, True]


@pytest.mark.parametrize(
    "sef,exit_code,restart_requested,restart_via_service,signal_initiated",
    list(
        itertools.product(_BOOLS, _EXIT_CODES, _BOOLS, _BOOLS, _BOOLS)
    ),
)
def test_helper_matches_start_gateway_contract_for_every_combo(
    sef, exit_code, restart_requested, restart_via_service, signal_initiated
):
    runner = make_runner(
        should_exit_with_failure=sef,
        exit_code=exit_code,
        restart_requested=restart_requested,
        restart_via_service=restart_via_service,
    )
    expected = reference_exit_code(runner, signal_initiated)
    actual = _resolve_hung_shutdown_exit_code(runner, signal_initiated)
    assert actual == expected, (
        f"helper diverged from start_gateway contract: "
        f"sef={sef} exit_code={exit_code} restart_requested={restart_requested} "
        f"restart_via_service={restart_via_service} "
        f"signal_initiated={signal_initiated} -> "
        f"expected {expected}, got {actual}"
    )
