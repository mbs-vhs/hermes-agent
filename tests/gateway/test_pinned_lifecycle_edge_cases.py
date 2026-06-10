"""Edge-case coverage for the badge-free pinned gateway lifecycle (CLAWD-1376).

Complements ``test_pinned_lifecycle_status.py`` with the boundaries surfaced in
review of hermes-agent#12:

- a stale/deleted pin is recreated whether ``edit_message`` reports failure via
  ``success=False`` OR by returning ``None`` (both mean "the pin is gone");
- a *pure unpin* where the message is still editable is NOT re-pinned — the
  status text updates in place via the edit, but the gateway does not probe pin
  state and re-pin (documented accepted boundary at the recreate site);
- the lifecycle create send is force-silent for a non-engineer profile under
  the ``important``/``silent`` modes AND (post-FIX-1) under ``all`` mode;
- FIX 2: a reconnect-success refreshes the pin to "online" badge-free.
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
from gateway.run import GatewayRunner, _read_pinned_status, _write_pinned_status


class _PinAdapter:
    """Adapter stub mirroring the lifecycle contract.

    ``edit_mode`` selects how a stale pin presents on edit:
      - "ok"      → edit succeeds (message still there);
      - "false"   → edit returns success=False (deleted/too-old);
      - "none"    → edit returns None (adapter signalled gone via None);
      - "unpin"   → edit succeeds (message editable) but it was *unpinned* —
                    the gateway edits text and does NOT re-pin.

    Records sends/edits/pins and the real ``disable_notification`` each send
    would carry so badge-free is provable under any notification mode.
    """

    def __init__(self, *, lifecycle_pinned=True, edit_mode="ok", notifications_mode="important"):
        self.platform = Platform.TELEGRAM
        self._lifecycle_pinned = lifecycle_pinned
        self._edit_mode = edit_mode
        self._notifications_mode = notifications_mode
        self._next_id = 200
        self.sends: list = []
        self.edits: list = []
        self.pins: list = []
        self.last_send_silent = None

    def _send_disable_notification(self, metadata):
        from gateway.platforms.telegram import TelegramAdapter

        return TelegramAdapter._notification_kwargs(self, metadata).get(
            "disable_notification", False
        )

    async def send(self, chat_id, content, metadata=None):
        self._next_id += 1
        self.sends.append((str(chat_id), content, metadata))
        self.last_send_silent = self._send_disable_notification(metadata)
        return types.SimpleNamespace(success=True, message_id=str(self._next_id), error=None)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append((str(chat_id), str(message_id), content))
        if self._edit_mode == "none":
            return None
        success = self._edit_mode != "false"
        return types.SimpleNamespace(success=success, message_id=str(message_id), error=None)

    async def pin_message(self, chat_id, message_id, *, disable_notification=True):
        self.pins.append((str(chat_id), str(message_id), disable_notification))
        return True


def _make_runner(adapter, *, home_thread=None):
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


# --------------------------------------------------------------------------
# Stale/deleted pin → recreate, covering BOTH failure shapes.
# --------------------------------------------------------------------------

def test_stale_pin_recreated_when_edit_returns_success_false(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    _write_pinned_status({"telegram:555:": "999"})
    adapter = _PinAdapter(edit_mode="false")

    updated = _run(_make_runner(adapter)._update_pinned_lifecycle_status("online"))

    assert updated is True
    assert len(adapter.edits) == 1          # tried the stale id first
    assert adapter.edits[0][1] == "999"
    assert len(adapter.sends) == 1          # then recreated
    assert len(adapter.pins) == 1           # and repinned
    assert _read_pinned_status() == {"telegram:555:": "201"}


def test_stale_pin_recreated_when_edit_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    _write_pinned_status({"telegram:555:": "999"})
    adapter = _PinAdapter(edit_mode="none")

    updated = _run(_make_runner(adapter)._update_pinned_lifecycle_status("online"))

    assert updated is True
    assert len(adapter.edits) == 1          # tried the stale id first
    assert adapter.edits[0][1] == "999"
    assert len(adapter.sends) == 1          # None counts as "gone" → recreate
    assert len(adapter.pins) == 1
    assert _read_pinned_status() == {"telegram:555:": "201"}


# --------------------------------------------------------------------------
# Pure unpin: still editable → edits text, NOT re-pinned (accepted boundary).
# --------------------------------------------------------------------------

def test_pure_unpin_edits_text_but_is_not_repinned(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    _write_pinned_status({"telegram:555:": "999"})
    # The operator unpinned the status but the message is still there/editable.
    adapter = _PinAdapter(edit_mode="unpin")

    updated = _run(_make_runner(adapter)._update_pinned_lifecycle_status("offline"))

    assert updated is True
    # The edit succeeded, so the text refreshed in place ...
    assert len(adapter.edits) == 1
    assert "offline" in adapter.edits[0][2].lower()
    # ... but the gateway does NOT probe pin-state and re-pin — no new send,
    # no new pin. The same id stays in the store.
    assert adapter.sends == []
    assert adapter.pins == []
    assert _read_pinned_status() == {"telegram:555:": "999"}


# --------------------------------------------------------------------------
# Non-engineer profile create send is silent under important/silent AND all.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["important", "silent", "all"])
def test_non_engineer_create_send_is_silent_under_every_mode(monkeypatch, tmp_path, mode):
    # A non-engineer (loud-alert-free) profile runs important/silent/all; the
    # lifecycle create send must never push a badge in ANY of them (FIX 1).
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    adapter = _PinAdapter(notifications_mode=mode)

    updated = _run(_make_runner(adapter)._update_pinned_lifecycle_status("online"))

    assert updated is True
    assert len(adapter.sends) == 1
    _chat, _content, metadata = adapter.sends[0]
    assert metadata is not None and metadata.get("notify") is False
    # Real _notification_kwargs computed disable_notification=True regardless.
    assert adapter.last_send_silent is True
    # Pin is silent too.
    assert adapter.pins[0][2] is True


# --------------------------------------------------------------------------
# FIX 2: reconnect-success refreshes the pin to "online", badge-free.
# --------------------------------------------------------------------------

def test_reconnect_success_edits_pin_online_without_badge(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    import time as _time

    # A pin already exists (status was created on a prior boot); the reconnect
    # must EDIT it to "online" in place, with no badge and no new send/pin.
    _write_pinned_status({"telegram:555:": "101"})

    new_adapter = _PinAdapter(notifications_mode="all")
    runner = _make_runner(new_adapter)
    runner._running = True
    runner._voice_mode = None

    # The reconnect builds a *fresh* adapter; the watcher swaps it into
    # self.adapters, then calls _update_pinned_lifecycle_status which iterates
    # self.adapters. Seed the failed queue so exactly one platform is eligible.
    runner._failed_platforms = {
        Platform.TELEGRAM: {
            "config": runner.config.platforms[Platform.TELEGRAM],
            "attempts": 0,
            "next_retry": _time.monotonic() - 1,  # eligible now
        }
    }

    # Stub the reconnect collaborators so the success branch runs deterministically.
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, cfg: new_adapter)
    monkeypatch.setattr(runner, "_connect_adapter_with_timeout",
                        lambda adapter, platform: _async_true())
    monkeypatch.setattr(runner, "_sync_voice_mode_state_to_adapter", lambda adapter: None)
    monkeypatch.setattr(runner, "_update_platform_runtime_status",
                        lambda *a, **k: None)
    new_adapter.set_message_handler = lambda fn: None
    new_adapter.set_fatal_error_handler = lambda fn: None
    new_adapter.set_session_store = lambda store: None
    new_adapter.set_busy_session_handler = lambda fn: None
    runner._handle_message = None
    runner._handle_adapter_fatal_error = None
    runner.session_store = None
    runner._handle_active_session_busy_message = None
    runner._busy_text_mode = False
    runner.delivery_router = types.SimpleNamespace(adapters=None)

    # build_channel_directory is imported lazily inside the branch; stub it.
    import gateway.channel_directory as cd
    async def _noop_build(adapters):
        return None
    monkeypatch.setattr(cd, "build_channel_directory", _noop_build)

    # Stop the loop after one full iteration so the test terminates.
    monkeypatch.setattr("gateway.run.asyncio.sleep", _make_one_shot_sleep(runner))

    _run(runner._platform_reconnect_watcher())

    # The reconnect-success branch edited the existing pin to "online" ...
    assert len(new_adapter.edits) == 1
    assert new_adapter.edits[0][1] == "101"
    assert "online" in new_adapter.edits[0][2].lower()
    # ... in place — no new send, no new pin ...
    assert new_adapter.sends == []
    assert new_adapter.pins == []
    # ... and badge-free: the platform was removed from the failed queue.
    assert Platform.TELEGRAM not in runner._failed_platforms


async def _async_true(*args, **kwargs):
    return True


def _make_one_shot_sleep(runner):
    """Return an async sleep stub that lets the watcher run one iteration.

    The watcher sleeps 10s up front then loops; after the first reconnect pass
    the failed queue is empty, so it enters the idle-sleep path. We flip
    ``_running`` off on the second sleep call so the loop exits cleanly.
    """
    state = {"calls": 0}

    async def _sleep(seconds):
        state["calls"] += 1
        if state["calls"] >= 2:
            runner._running = False
        return None

    return _sleep
