"""Tests for the symmetric "gateway online" recovery via IN-PLACE EDIT (CLAWD-1144).

CLAWD-1019 made external restarts announce "gateway online" with a fresh send.
CLAWD-1144 makes that announcement *symmetric and quiet*: the shutdown path now
records the home-channel down-DMs it actually delivered (platform/chat_id/
thread_id/message_id) into the recovery marker, and the next boot EDITS each of
those messages in place into the online notice. An edit produces no push
notification, so the chat list flips to "online" without a second alert.

These tests cover the new surface:

* ``_write_recovery_marker(interrupted, targets=, shutdown_ts=)`` — the extended
  marker payload (legacy single-arg shape still works).
* ``_read_recovery_marker()`` — dict / None / corrupt / non-dict tolerance.
* ``GatewayRunner._notify_active_sessions_of_shutdown()`` — now RETURNS the
  delivered home-channel targets and SKIPS Platform.EMAIL in the home loop.
* ``GatewayRunner._edit_recovery_notifications()`` — the in-place edit path.
* ``_send_home_channel_startup_notifications`` — now skips Platform.EMAIL.

Unit-helper round-trip tests for the legacy CLAWD-1019 path live in
``test_gateway_recovery_notification.py`` / ``…_callsites.py`` and must stay green.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)


_ONLINE_MSG = "♻️ Gateway online — Hermes is back and ready."


def _configure_home(runner, chat_id="home-42", name="Ops Home", thread_id=None):
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        name=name,
        thread_id=thread_id,
    )


# ─────────────────────────────────────────────────────────────────────────
# _write_recovery_marker — extended payload (targets + ts)
# ─────────────────────────────────────────────────────────────────────────


def test_write_marker_persists_targets_and_ts(tmp_path, monkeypatch):
    """targets= and shutdown_ts= are persisted into the marker JSON."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    targets = [
        {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
    ]
    gateway_run._write_recovery_marker(2, targets=targets, shutdown_ts=1717000000.0)

    data = json.loads((tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).read_text())
    assert data["interrupted"] == 2
    assert data["targets"] == targets
    assert data["ts"] == 1717000000.0


def test_write_marker_legacy_single_arg_has_no_targets_or_ts(tmp_path, monkeypatch):
    """The pre-CLAWD-1144 single-arg call writes only {"interrupted": N}."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    gateway_run._write_recovery_marker(3)

    data = json.loads((tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).read_text())
    assert data == {"interrupted": 3}
    assert "targets" not in data
    assert "ts" not in data


def test_write_marker_empty_targets_list_omitted(tmp_path, monkeypatch):
    """An empty targets list is falsy → key omitted (idle-but-no-home-DM case)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    gateway_run._write_recovery_marker(1, targets=[], shutdown_ts=None)

    data = json.loads((tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).read_text())
    assert data == {"interrupted": 1}


# ─────────────────────────────────────────────────────────────────────────
# _read_recovery_marker — dict / None / corrupt / non-dict
# ─────────────────────────────────────────────────────────────────────────


def test_read_marker_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    assert gateway_run._read_recovery_marker() is None


def test_read_marker_round_trips_written_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    targets = [{"platform": "telegram", "chat_id": "c", "thread_id": "t", "message_id": "m"}]
    gateway_run._write_recovery_marker(5, targets=targets, shutdown_ts=123.0)

    marker = gateway_run._read_recovery_marker()
    assert marker["interrupted"] == 5
    assert marker["targets"] == targets
    assert marker["ts"] == 123.0


def test_read_marker_corrupt_json_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).write_text("{not valid json")
    assert gateway_run._read_recovery_marker() is None


def test_read_marker_non_dict_json_returns_none(tmp_path, monkeypatch):
    """A JSON array/scalar (non-dict) is rejected → None (callers fresh-send)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).write_text("[1, 2, 3]")
    assert gateway_run._read_recovery_marker() is None


# ─────────────────────────────────────────────────────────────────────────
# _notify_active_sessions_of_shutdown — RETURNS delivered home targets
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_shutdown_returns_home_targets_with_message_id():
    """The home-channel down-DM is returned with the captured message_id."""
    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="msg-99"))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert targets == [
        {
            "platform": "telegram",
            "chat_id": "home-42",
            "thread_id": None,
            "message_id": "msg-99",
        }
    ]


@pytest.mark.asyncio
async def test_notify_shutdown_returns_target_with_thread_id():
    """Threaded home channel carries thread_id into the returned target."""
    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="parent-42", thread_id="topic-7")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-7"))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert targets == [
        {
            "platform": "telegram",
            "chat_id": "parent-42",
            "thread_id": "topic-7",
            "message_id": "m-7",
        }
    ]


@pytest.mark.asyncio
async def test_notify_shutdown_message_id_none_when_adapter_returns_none():
    """A SendResult without a message_id yields message_id=None (boot fresh-sends)."""
    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id=None))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert len(targets) == 1
    assert targets[0]["message_id"] is None


@pytest.mark.asyncio
async def test_notify_shutdown_excludes_failed_home_send():
    """A failed home-channel send (success=False) is NOT in the returned targets."""
    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="boom"))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert targets == []


@pytest.mark.asyncio
async def test_notify_shutdown_excludes_home_send_that_raises():
    """An exception during the home-channel send is swallowed and excluded."""
    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.send = AsyncMock(side_effect=RuntimeError("network gone"))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert targets == []


@pytest.mark.asyncio
async def test_notify_shutdown_no_home_channel_returns_empty():
    """No configured home channel → empty target list (idle bounce stays silent)."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="x"))

    targets = await runner._notify_active_sessions_of_shutdown()

    assert targets == []


@pytest.mark.asyncio
async def test_notify_shutdown_skips_email_home_channel():
    """Platform.EMAIL home channel is SKIPPED in the home loop (backstop channel)."""
    runner, adapter = make_restart_runner()
    email_adapter = AsyncMock()
    email_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e-1"))
    runner.adapters[Platform.EMAIL] = email_adapter
    runner.config.platforms[Platform.EMAIL] = PlatformConfig(enabled=True, token="***")
    runner.config.platforms[Platform.EMAIL].home_channel = HomeChannel(
        platform=Platform.EMAIL,
        chat_id="ops@example.com",
        name="Email Home",
    )

    targets = await runner._notify_active_sessions_of_shutdown()

    # Email home channel produced NO target and the email adapter was not sent to.
    assert all(t["platform"] != "email" for t in targets)
    email_adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_notify_shutdown_email_active_session_still_notified():
    """Active-session interruption notices (first loop) STILL reach email convos —
    only the home-channel broadcast skips email."""
    runner, adapter = make_restart_runner()
    email_adapter = RestartTestAdapter()
    email_adapter.platform = Platform.EMAIL
    email_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e-1"))
    runner.adapters[Platform.EMAIL] = email_adapter
    runner.config.platforms[Platform.EMAIL] = PlatformConfig(enabled=True, token="***")

    # An active email session in flight.
    source = make_restart_source(chat_id="ops@example.com", chat_type="dm")
    source.platform = Platform.EMAIL
    session_key = gateway_run.build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner._cache_session_source(session_key, source)

    targets = await runner._notify_active_sessions_of_shutdown()

    # The active-session loop delivered to the email conversation...
    email_adapter.send.assert_awaited_once()
    assert email_adapter.send.await_args.args[0] == "ops@example.com"
    assert "shutting down" in email_adapter.send.await_args.args[1]
    # ...but it is NOT a home-channel target (no home channel configured for email).
    assert targets == []


# ─────────────────────────────────────────────────────────────────────────
# _edit_recovery_notifications — the in-place edit path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_recovery_edits_target_and_returns_key(tmp_path, monkeypatch):
    """A recorded target with a message_id is edited in place → returned as a key."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == {("telegram", "home-42", None)}
    adapter.edit_message.assert_awaited_once()
    call = adapter.edit_message.await_args
    assert call.args[0] == "home-42"
    assert call.args[1] == "m1"
    assert "Gateway online" in call.args[2]
    assert call.kwargs.get("finalize") is True


@pytest.mark.asyncio
async def test_edit_recovery_includes_downtime_when_ts_present(tmp_path, monkeypatch):
    """When the marker carries ts, the edited message reports the downtime."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    # Freeze "now" so the downtime math is deterministic.
    monkeypatch.setattr(gateway_run.time, "time", lambda: 1000.0)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
        shutdown_ts=940.0,  # 60s before frozen now
    )

    await runner._edit_recovery_notifications()

    sent_msg = adapter.edit_message.await_args.args[2]
    assert "Gateway online" in sent_msg
    assert "was down ~60s" in sent_msg


@pytest.mark.asyncio
async def test_edit_recovery_no_ts_omits_downtime(tmp_path, monkeypatch):
    """Without ts the message is the plain online notice (no "was down")."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    await runner._edit_recovery_notifications()

    sent_msg = adapter.edit_message.await_args.args[2]
    assert sent_msg == _ONLINE_MSG
    assert "was down" not in sent_msg


@pytest.mark.asyncio
async def test_edit_recovery_skips_target_without_message_id(tmp_path, monkeypatch):
    """A target whose message_id is None cannot be edited → not in returned set,
    no edit attempted (boot falls back to a fresh send)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": None}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_recovery_legacy_marker_without_targets_returns_empty(tmp_path, monkeypatch):
    """A pre-CLAWD-1144 marker ({"interrupted": N}) yields an empty edit set."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock()

    gateway_run._write_recovery_marker(3)  # legacy shape, no targets

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_recovery_no_marker_returns_empty(tmp_path, monkeypatch):
    """No marker on disk → empty set, no edit attempted."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock()

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_recovery_unknown_platform_skipped(tmp_path, monkeypatch):
    """A target naming a platform that isn't a valid Platform value is skipped."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "carrier-pigeon", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_recovery_no_adapter_for_platform_skipped(tmp_path, monkeypatch):
    """A valid platform with no connected adapter is skipped (no crash)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")

    # Target on DISCORD, but only a TELEGRAM adapter is connected.
    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "discord", "chat_id": "d-1", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()


@pytest.mark.asyncio
async def test_edit_recovery_respects_restart_notification_optout(tmp_path, monkeypatch):
    """gateway_restart_notification=False suppresses the edit → not in returned set."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_edit_recovery_edit_failure_false_result_not_in_set(tmp_path, monkeypatch):
    """An edit returning success=False → target NOT returned (caller fresh-sends)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=False, error="message to edit not found")
    )

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()
    adapter.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_recovery_edit_exception_not_in_set(tmp_path, monkeypatch):
    """An exception from edit_message is swallowed → target NOT returned."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(side_effect=RuntimeError("api error"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == set()


@pytest.mark.asyncio
async def test_edit_recovery_preserves_thread_id_in_key(tmp_path, monkeypatch):
    """The returned key carries thread_id so the boot's fresh-send skip lines up."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="parent-42", thread_id="topic-7")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {
                "platform": "telegram",
                "chat_id": "parent-42",
                "thread_id": "topic-7",
                "message_id": "m1",
            }
        ],
    )

    edited = await runner._edit_recovery_notifications()

    assert edited == {("telegram", "parent-42", "topic-7")}


# ─────────────────────────────────────────────────────────────────────────
# Boot wiring: edited targets are SKIPPED by the fresh-send fallback;
# unedited targets fall back to a fresh send. (Drives the real boot gate.)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_edited_target_gets_no_duplicate_fresh_send(tmp_path, monkeypatch):
    """End-to-end boot gate: a target that was edited in place must NOT also get a
    fresh "gateway online" send (no duplicate)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fresh"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    await runner._send_post_connect_lifecycle_notifications()

    # The down-DM was edited in place; no fresh broadcast send to the same home.
    adapter.edit_message.assert_awaited_once()
    adapter.send.assert_not_called()
    # Marker cleared → fires exactly once.
    assert gateway_run._recovery_notification_pending() is False


@pytest.mark.asyncio
async def test_boot_unedited_target_falls_back_to_fresh_send(tmp_path, monkeypatch):
    """A target with no message_id can't be edited → boot falls back to a fresh
    home-channel send so recovery is still announced."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="m1"))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fresh"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": None}
        ],
    )

    await runner._send_post_connect_lifecycle_notifications()

    # No editable message id → no edit, but a fresh online send WAS made.
    adapter.edit_message.assert_not_called()
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _ONLINE_MSG
    assert gateway_run._recovery_notification_pending() is False


@pytest.mark.asyncio
async def test_boot_failed_edit_falls_back_to_fresh_send(tmp_path, monkeypatch):
    """An edit that fails (success=False) is NOT in skip_targets → the boot
    fresh-sends to that home channel as a fallback."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    _configure_home(runner, chat_id="home-42")
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=False, error="not found")
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fresh"))

    gateway_run._write_recovery_marker(
        1,
        targets=[
            {"platform": "telegram", "chat_id": "home-42", "thread_id": None, "message_id": "m1"}
        ],
    )

    await runner._send_post_connect_lifecycle_notifications()

    adapter.edit_message.assert_awaited_once()
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == _ONLINE_MSG
    assert gateway_run._recovery_notification_pending() is False


# ─────────────────────────────────────────────────────────────────────────
# _send_home_channel_startup_notifications — skips Platform.EMAIL
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_notifications_skip_email_home(tmp_path, monkeypatch):
    """A configured EMAIL home channel is skipped by the online broadcast."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    # No telegram home; only an email home configured.
    email_adapter = AsyncMock()
    email_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e-1"))
    runner.adapters[Platform.EMAIL] = email_adapter
    runner.config.platforms[Platform.EMAIL] = PlatformConfig(enabled=True, token="***")
    runner.config.platforms[Platform.EMAIL].home_channel = HomeChannel(
        platform=Platform.EMAIL,
        chat_id="ops@example.com",
        name="Email Home",
    )

    delivered = await runner._send_home_channel_startup_notifications()

    assert all(t[0] != "email" for t in delivered)
    email_adapter.send.assert_not_called()
