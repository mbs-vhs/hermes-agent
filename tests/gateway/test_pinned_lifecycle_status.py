"""Tests for the badge-free pinned gateway lifecycle (CLAWD-1376).

Each gateway keeps ONE pinned status message per home channel and EDITS it in
place on every online/offline transition — an edit produces no push
notification, and the one-time create+pin is done with
``disable_notification=True``, so the operator's chat list flips state with ZERO
badges. These tests pin down:

- first run sends ONE status message silently then pins it silently;
- subsequent transitions EDIT the same message (no new send, no new pin);
- the message id persists across "processes" (separate runner instances);
- a deleted/unpinned message (edit fails) is recreated + repinned;
- the knob is off by default, so the legacy CLAWD-1144 path is unaffected;
- the Telegram config loader parses the lifecycle_pinned knob + env override.
"""
import asyncio
import types

import pytest

from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
)
from gateway.run import GatewayRunner, _read_pinned_status


class _PinAdapter:
    """Minimal adapter exposing send / edit_message / pin_message.

    ``edit_ok`` controls whether edit_message succeeds (False simulates the
    operator having unpinned/deleted the message). Records every call so the
    tests can assert the silent-send + silent-pin invariants.
    """

    def __init__(self, *, lifecycle_pinned=True, edit_ok=True, notifications_mode="important"):
        self._lifecycle_pinned = lifecycle_pinned
        self._edit_ok = edit_ok
        # Drives the REAL TelegramAdapter._notification_kwargs so the silent-send
        # invariant is observable (every send records the disable_notification it
        # would have passed to Telegram). CLAWD-1376 FIX 1 must hold under "all".
        self._notifications_mode = notifications_mode
        self._next_id = 100
        self.sends: list = []
        self.edits: list = []
        self.pins: list = []

    def _send_disable_notification(self, metadata):
        from gateway.platforms.telegram import TelegramAdapter

        return TelegramAdapter._notification_kwargs(self, metadata).get(
            "disable_notification", False
        )

    async def send(self, chat_id, content, metadata=None):
        self._next_id += 1
        self.sends.append((str(chat_id), content, metadata))
        # Record what disable_notification the real adapter would compute for
        # this send under the configured mode, so badge-free is provable.
        self.last_send_silent = self._send_disable_notification(metadata)
        return types.SimpleNamespace(
            success=True, message_id=str(self._next_id), error=None,
        )

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append((str(chat_id), str(message_id), content))
        return types.SimpleNamespace(
            success=self._edit_ok, message_id=str(message_id), error=None,
        )

    async def pin_message(self, chat_id, message_id, *, disable_notification=True):
        self.pins.append((str(chat_id), str(message_id), disable_notification))
        return True


def _make_runner(adapter, *, home_thread=None):
    """Minimal GatewayRunner with one Telegram home channel + the given adapter."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="test",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="555",
                    name="Home",
                    thread_id=home_thread,
                ),
            )
        }
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    return runner


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_first_online_sends_once_and_pins_silently(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter()
    runner = _make_runner(adapter)

    updated = _run(runner._update_pinned_lifecycle_status("online"))

    assert updated is True
    # Exactly one send (the create) and one pin; no edit on first run.
    assert len(adapter.sends) == 1
    assert len(adapter.pins) == 1
    assert adapter.edits == []
    # The pin is explicitly silent — zero badge.
    _chat, _mid, disable_notification = adapter.pins[0]
    assert disable_notification is True
    # The created message id was persisted for the next process.
    assert _read_pinned_status() == {"telegram:555:": "101"}


def test_second_transition_edits_in_place_no_new_send(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter()
    runner = _make_runner(adapter)

    _run(runner._update_pinned_lifecycle_status("online"))
    # Subsequent offline transition must EDIT the same message — no new send,
    # no new pin (the message is already pinned).
    _run(runner._update_pinned_lifecycle_status("offline"))

    assert len(adapter.sends) == 1  # still just the original create
    assert len(adapter.pins) == 1
    assert len(adapter.edits) == 1
    edited_chat, edited_mid, edited_content = adapter.edits[0]
    assert edited_mid == "101"
    assert "offline" in edited_content.lower()


def test_id_persists_across_processes(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    # "Process 1" — boot creates+pins the status.
    a1 = _PinAdapter()
    _run(_make_runner(a1)._update_pinned_lifecycle_status("online"))
    assert len(a1.sends) == 1 and len(a1.pins) == 1

    # "Process 2" — a fresh runner/adapter (the offline edit runs in a different
    # process than the online create). It must EDIT the persisted id, not send.
    a2 = _PinAdapter()
    _run(_make_runner(a2)._update_pinned_lifecycle_status("offline"))
    assert a2.sends == []
    assert a2.pins == []
    assert len(a2.edits) == 1
    assert a2.edits[0][1] == "101"


def test_deleted_message_is_recreated_and_repinned(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter()
    _run(_make_runner(adapter)._update_pinned_lifecycle_status("online"))

    # Operator unpins/deletes — the next edit fails, so a fresh runner must
    # recreate + repin (edit attempted first, then send + pin).
    gone = _PinAdapter(edit_ok=False)
    updated = _run(_make_runner(gone)._update_pinned_lifecycle_status("offline"))

    assert updated is True
    assert len(gone.edits) == 1   # tried to edit the stale id first
    assert len(gone.sends) == 1   # then recreated
    assert len(gone.pins) == 1    # and repinned
    # The new id replaced the stale one in the store.
    assert _read_pinned_status() == {"telegram:555:": "101"}


def test_pinned_mode_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter(lifecycle_pinned=False)
    runner = _make_runner(adapter)

    updated = _run(runner._update_pinned_lifecycle_status("online"))

    # Adapter is not in pinned mode → no send/edit/pin, returns False so the
    # caller falls through to the legacy CLAWD-1144 path.
    assert updated is False
    assert adapter.sends == []
    assert adapter.edits == []
    assert adapter.pins == []


def test_adapter_lifecycle_pinned_requires_flag_and_capability():
    runner = object.__new__(GatewayRunner)
    # Flag set + pin_message present → eligible.
    assert runner._adapter_lifecycle_pinned(_PinAdapter()) is True
    # Flag off → not eligible.
    assert runner._adapter_lifecycle_pinned(_PinAdapter(lifecycle_pinned=False)) is False
    # Flag set but no pin_message capability → not eligible.
    no_pin = types.SimpleNamespace(_lifecycle_pinned=True)
    assert runner._adapter_lifecycle_pinned(no_pin) is False


def test_thread_aware_home_channel_keys_distinctly(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter()
    runner = _make_runner(adapter, home_thread="77")

    _run(runner._update_pinned_lifecycle_status("online"))

    assert _read_pinned_status() == {"telegram:555:77": "101"}
    # The send carried the thread metadata so it lands in the right topic, plus
    # the force-silence notify=False (CLAWD-1376 FIX 1).
    _chat, _content, metadata = adapter.sends[0]
    assert metadata == {"notify": False, "thread_id": "77"}


def test_create_send_is_silent_even_under_all_mode(monkeypatch, tmp_path):
    # CLAWD-1376 FIX 1: under notifications_mode="all" a bare send pushes a
    # badge, but the lifecycle create/recreate send carries notify=False, which
    # force-silences it in EVERY mode. The created+pinned status must NOT push.
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter(notifications_mode="all")
    runner = _make_runner(adapter)

    updated = _run(runner._update_pinned_lifecycle_status("online"))

    assert updated is True
    assert len(adapter.sends) == 1
    # The create send carried notify=False ...
    _chat, _content, metadata = adapter.sends[0]
    assert metadata is not None and metadata.get("notify") is False
    # ... and the real _notification_kwargs computed disable_notification=True
    # for it despite "all" mode — zero badge.
    assert adapter.last_send_silent is True
    # The pin is silent too.
    assert adapter.pins[0][2] is True
