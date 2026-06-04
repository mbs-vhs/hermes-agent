"""Call-site tests for the external-restart "gateway online" recovery marker (CLAWD-1019).

The unit-level helper tests live in ``test_gateway_recovery_notification.py``.
This file exercises the two CALL SITES that wire those helpers into the gateway
lifecycle:

* the **shutdown-side write** — ``if _pre_drain_keys and not self._restart_requested:
  _write_recovery_marker(...)`` inside ``GatewayRunner.stop()``. This is driven
  end-to-end through ``runner.stop()`` (the same way ``test_gateway_shutdown.py``
  exercises the drain/interrupt paths).

* the **boot-side gate** — the ``if /restart … elif _recovery_notification_pending():``
  branch that fires the home-channel "gateway online" message. CLAWD-1019 extracted
  this out of the giant ``start()`` method into
  ``GatewayRunner._send_post_connect_lifecycle_notifications()``, so the boot tests
  here drive that REAL method directly (via the ``_run_boot_gate`` shim + the
  ``RestartTestAdapter`` capturing sent messages). A break in the ``if/elif`` wiring
  is therefore caught here (revert-verified). The only line not under unit test is
  ``start()``'s single ``await self._send_post_connect_lifecycle_notifications()``
  call, which was live-verified during the CLAWD-1019 fleet deploy (a seeded-marker
  restart on the ``legal`` gateway fired the home-channel send).
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import make_restart_runner


# Exercise the REAL boot-gate logic. CLAWD-1019 extracted the gate out of
# start() into GatewayRunner._send_post_connect_lifecycle_notifications(), so
# these tests now drive production code directly (no replica to drift) — a break
# in the if/elif wiring is caught here. The only line not covered is start()'s
# single call to this method (live-verified during the CLAWD-1019 fleet deploy).
async def _run_boot_gate(runner) -> None:
    await runner._send_post_connect_lifecycle_notifications()


def _configure_home(runner, chat_id="home-42", name="Ops Home", thread_id=None):
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        name=name,
        thread_id=thread_id,
    )


_ONLINE_MSG = "♻️ Gateway online — Hermes is back and ready."


# ─────────────────────────────────────────────────────────────────────────
# Boot-side gate: marker present -> "gateway online" delivered + marker cleared
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_recovery_marker_delivers_online_and_clears(tmp_path, monkeypatch):
    """After _write_recovery_marker(n), a boot-style pass delivers the home-channel
    "gateway online" message AND clears the marker (RestartTestAdapter captures it)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner)

    gateway_run._write_recovery_marker(3)
    assert gateway_run._recovery_notification_pending() is True

    await _run_boot_gate(runner)

    # Real notifier path delivered the online message to the configured home.
    assert adapter.sent == [_ONLINE_MSG]
    assert adapter.sent_calls[0][0] == "home-42"
    # Marker cleared → fires exactly once.
    assert gateway_run._recovery_notification_pending() is False
    assert not (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).exists()


@pytest.mark.asyncio
async def test_boot_recovery_marker_preserves_home_thread(tmp_path, monkeypatch):
    """A threaded home channel still receives the online message with thread metadata."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="parent-42", thread_id="topic-7")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="home"))

    gateway_run._write_recovery_marker(1)
    await _run_boot_gate(runner)

    adapter.send.assert_awaited_once_with(
        "parent-42",
        _ONLINE_MSG,
        metadata={"thread_id": "topic-7"},
    )
    assert gateway_run._recovery_notification_pending() is False


# ─────────────────────────────────────────────────────────────────────────
# Boot-side gate: fires once — a second boot with no marker sends nothing
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_recovery_fires_once_then_silent(tmp_path, monkeypatch):
    """First boot (marker present) sends; second boot (marker gone) sends nothing."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner)

    gateway_run._write_recovery_marker(2)
    await _run_boot_gate(runner)
    assert adapter.sent == [_ONLINE_MSG]

    # Second boot, no marker, no /restart marker → completely silent.
    adapter.sent.clear()
    adapter.sent_calls.clear()
    await _run_boot_gate(runner)
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_boot_no_marker_cold_boot_is_silent(tmp_path, monkeypatch):
    """A cold boot with neither marker present must not announce anything."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner)

    assert gateway_run._recovery_notification_pending() is False
    await _run_boot_gate(runner)

    assert adapter.sent == []


# ─────────────────────────────────────────────────────────────────────────
# Boot-side gate: per-platform opt-out mutes the message (marker still cleared)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_recovery_respects_restart_notification_optout(tmp_path, monkeypatch):
    """With gateway_restart_notification=False, the recovery boot sends nothing.

    The notifier itself suppresses the send; the gate still clears the marker so
    a muted platform doesn't leak the marker into the next boot.
    """
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner)
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    adapter.send = AsyncMock()

    gateway_run._write_recovery_marker(1)
    await _run_boot_gate(runner)

    adapter.send.assert_not_called()
    # Marker is still cleared so the suppressed message can't resurface.
    assert gateway_run._recovery_notification_pending() is False


# ─────────────────────────────────────────────────────────────────────────
# Boot-side gate: /restart marker supersedes a same-cycle recovery marker
# (no double-send) — exercises the "drop it so it can't double-fire" comment.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_restart_and_recovery_marker_both_present_single_send(
    tmp_path, monkeypatch
):
    """Both .restart_notify.json AND .gateway_recovery_notify.json present:
    the home channel gets exactly ONE "gateway online" message, the /restart
    originator gets its own "restarted" DM, and BOTH markers are cleared."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    # Home channel differs from the /restart originator chat so the home ping
    # is not skipped as a duplicate of the originator DM.
    _configure_home(runner, chat_id="home-42")

    # /restart originator marker (different chat).
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "originator-1"})
    )
    # Same-cycle external-recovery marker.
    gateway_run._write_recovery_marker(2)

    await _run_boot_gate(runner)

    # Originator "restarted" DM + home "online" broadcast = 2 sends, ONE online msg.
    online_sends = [c for c in adapter.sent if c == _ONLINE_MSG]
    assert len(online_sends) == 1, f"expected exactly one online msg, got {adapter.sent}"
    originator_sends = [
        (cid, content) for (cid, content, _md) in adapter.sent_calls
        if cid == "originator-1"
    ]
    assert len(originator_sends) == 1
    assert "restarted" in originator_sends[0][1].lower()

    # BOTH markers cleared — the recovery marker must NOT survive to double-fire.
    assert not (tmp_path / ".restart_notify.json").exists()
    assert gateway_run._recovery_notification_pending() is False


@pytest.mark.asyncio
async def test_boot_restart_marker_clears_recovery_even_when_home_same_chat(
    tmp_path, monkeypatch
):
    """Stress the de-dup: /restart originator == home channel. The home broadcast
    is skipped (originator already got a direct notice), but the recovery marker
    is STILL cleared by the if-branch so it can't fire on the next boot."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="42")  # same as originator below

    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    gateway_run._write_recovery_marker(1)

    await _run_boot_gate(runner)

    # Originator got exactly one "restarted" DM; the home broadcast to the same
    # target was skipped → no "online" message at all.
    assert adapter.sent.count(_ONLINE_MSG) == 0
    restart_dms = [c for c in adapter.sent if "restarted" in c.lower()]
    assert len(restart_dms) == 1
    # Recovery marker cleared by the if-branch (CLAWD-1019 "supersedes" comment).
    assert gateway_run._recovery_notification_pending() is False


# ─────────────────────────────────────────────────────────────────────────
# Boot-side gate: STALE marker from a prior restart — verify it still fires
# exactly once and self-clears (it is by design "owed" until delivered).
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_stale_marker_from_prior_restart_fires_then_clears(
    tmp_path, monkeypatch
):
    """A marker left by an earlier crash/restart is delivered on the NEXT boot and
    cleared — it does not accumulate or re-fire across subsequent boots."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner)

    # Simulate a marker that survived from a prior restart cycle.
    (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).write_text(
        json.dumps({"interrupted": 5})
    )
    assert gateway_run._recovery_notification_pending() is True

    await _run_boot_gate(runner)
    assert adapter.sent == [_ONLINE_MSG]
    assert gateway_run._recovery_notification_pending() is False

    # Third boot: marker gone → silent.
    adapter.sent.clear()
    await _run_boot_gate(runner)
    assert adapter.sent == []


# ─────────────────────────────────────────────────────────────────────────
# Shutdown-side WRITE call site — driven end-to-end through runner.stop()
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_external_shutdown_with_inflight_work_writes_marker(
    tmp_path, monkeypatch
):
    """External shutdown (restart NOT requested) that interrupts in-flight work
    writes the recovery marker via runner.stop()."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.0  # force the drain to interrupt immediately
    runner._running_agents = {"session-a": MagicMock(), "session-b": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()  # restart defaults to False → external shutdown

    marker = tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER
    assert marker.exists(), "external shutdown with in-flight work must leave a marker"
    assert json.loads(marker.read_text())["interrupted"] == 2


@pytest.mark.asyncio
async def test_stop_restart_requested_does_not_write_recovery_marker(
    tmp_path, monkeypatch
):
    """In-band /restart (restart=True) writes its OWN .restart_notify.json and must
    NOT leave the recovery marker — else the next boot double-announces."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.0
    runner._running_agents = {"session-a": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True)

    assert not (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).exists()


@pytest.mark.asyncio
async def test_stop_external_shutdown_no_inflight_work_writes_no_marker(
    tmp_path, monkeypatch
):
    """External shutdown with NO running agents AND no home channel interrupts
    nothing and announces nothing → no marker (a truly silent idle bounce)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._running_agents = {}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert not (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).exists()


# ─────────────────────────────────────────────────────────────────────────
# CLAWD-1144: shutdown-side write now records home-channel targets + ts, and
# fires for IDLE shutdowns that ANNOUNCED to a home channel (formerly
# in-flight-only). /restart still writes no marker.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_records_home_targets_and_ts_in_marker(tmp_path, monkeypatch):
    """An external shutdown that DMs the home channel records that target (with
    message_id) and a ts into the marker so the next boot can edit it in place."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="down-msg"))
    runner._restart_drain_timeout = 0.0
    runner._running_agents = {"session-a": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    marker = tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["targets"] == [
        {
            "platform": "telegram",
            "chat_id": "home-42",
            "thread_id": None,
            "message_id": "down-msg",
        }
    ]
    assert isinstance(data["ts"], (int, float)) and data["ts"] > 0


@pytest.mark.asyncio
async def test_stop_idle_but_announced_to_home_writes_marker(tmp_path, monkeypatch):
    """CLAWD-1144 change: an IDLE shutdown (no in-flight agents) that still
    announced to the home channel now leaves a marker — previously the marker
    was in-flight-only, making idle restarts loud down / silent up."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="down-msg"))
    runner._running_agents = {}  # IDLE — nothing in flight

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    marker = tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER
    assert marker.exists(), "idle shutdown that announced to home must leave a marker"
    data = json.loads(marker.read_text())
    assert data["interrupted"] == 0  # no in-flight agents
    assert data["targets"][0]["chat_id"] == "home-42"


@pytest.mark.asyncio
async def test_stop_restart_requested_writes_no_marker_even_with_home(
    tmp_path, monkeypatch
):
    """/restart (restart=True) writes its own .restart_notify.json and must NOT
    leave the recovery marker, even when a home channel was announced to."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="down-msg"))
    runner._restart_drain_timeout = 0.0
    runner._running_agents = {"session-a": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True)

    assert not (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).exists()


@pytest.mark.asyncio
async def test_stop_external_shutdown_marker_write_failure_does_not_block_stop(
    tmp_path, monkeypatch
):
    """A marker-write failure during shutdown must be swallowed: stop() still
    completes and disconnects adapters (notification I/O must never block teardown)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(gateway_run, "atomic_json_write", _boom)

    runner, adapter = make_restart_runner()
    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock
    runner._restart_drain_timeout = 0.0
    runner._running_agents = {"session-a": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()  # must NOT raise

    disconnect_mock.assert_awaited_once()
    assert runner._shutdown_event.is_set() is True
    assert not (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).exists()


# ─────────────────────────────────────────────────────────────────────────
# Full round trip: stop() writes the marker, a subsequent boot delivers + clears.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_then_boot_round_trip(tmp_path, monkeypatch):
    """End-to-end: an external shutdown that interrupts work leaves a marker, and
    the next boot announces "gateway online" exactly once, then clears it."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    # 1) External shutdown interrupts in-flight work → marker written.
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.0
    runner._running_agents = {"session-a": MagicMock()}
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()
    assert gateway_run._recovery_notification_pending() is True

    # 2) Fresh boot reads the marker, announces online, clears it.
    boot_runner, boot_adapter = make_restart_runner()
    _configure_home(boot_runner)
    await _run_boot_gate(boot_runner)

    assert boot_adapter.sent == [_ONLINE_MSG]
    assert gateway_run._recovery_notification_pending() is False
