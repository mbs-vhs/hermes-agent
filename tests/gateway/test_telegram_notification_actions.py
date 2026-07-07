"""Tests for Telegram notification action buttons."""

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    existing = sys.modules.get("telegram")
    if existing is not None and not isinstance(existing, MagicMock) and hasattr(existing, "__file__"):
        return

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request", "telegram.error"):
        if isinstance(sys.modules.get(name), MagicMock):
            sys.modules.pop(name, None)

    if importlib.util.find_spec("telegram") is not None:
        importlib.import_module("telegram")
        importlib.import_module("telegram.ext")
        importlib.import_module("telegram.constants")
        importlib.import_module("telegram.request")
        importlib.import_module("telegram.error")
        return

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    mod = MagicMock()
    mod.InlineKeyboardButton = InlineKeyboardButton
    mod.InlineKeyboardMarkup = InlineKeyboardMarkup
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules[name] = mod
    sys.modules["telegram.error"] = mod.error


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.notification_actions import NotificationActionStore
from gateway.platforms.telegram import TelegramAdapter


def _make_adapter(tmp_path):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._notification_action_store = NotificationActionStore(tmp_path / "telegram-actions.json")
    return adapter


def _make_update(data, *, chat_id=12345, user_id="12345", thread_id=None):
    query = MagicMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.message.message_thread_id = thread_id
    query.message.text = "Original notification"
    query.message.chat = SimpleNamespace(type="private" if int(chat_id) > 0 else "supergroup")
    query.from_user = SimpleNamespace(id=user_id, first_name="Morgan")
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


@pytest.mark.asyncio
async def test_send_attaches_notification_actions_only_to_first_chunk(tmp_path):
    adapter = _make_adapter(tmp_path)
    adapter.truncate_message = MagicMock(return_value=["first", "second"])
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    adapter._bot = bot

    result = await adapter.send(
        "12345",
        "hello",
        metadata={
            "notify": True,
            "notification_actions": {
                "notification_id": "notif-1",
                "source_type": "kanban",
                "source_id": "t_123",
            },
        },
    )

    assert result.success is True
    assert adapter._bot.send_message.call_count == 2
    first_kwargs = adapter._bot.send_message.call_args_list[0].kwargs
    second_kwargs = adapter._bot.send_message.call_args_list[1].kwargs
    assert first_kwargs.get("reply_markup") is not None
    assert "reply_markup" not in second_kwargs
    entry = next(iter(adapter._notification_action_store._entries.values()))
    assert entry.notification_id == "notif-1"
    assert entry.source_type == "kanban"
    assert entry.source_id == "t_123"
    assert entry.chat_id == "12345"
    assert entry.thread_id is None
    assert all(len(entry.callback_data(verb).encode("utf-8")) <= 64 for verb in entry.actions)


@pytest.mark.asyncio
async def test_empty_notification_actions_dict_opts_in(tmp_path):
    adapter = _make_adapter(tmp_path)
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    adapter._bot = bot

    result = await adapter.send("12345", "hello", metadata={"notify": True, "notification_actions": {}})

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs.get("reply_markup") is not None
    assert len(adapter._notification_action_store._entries) == 1


@pytest.mark.asyncio
async def test_send_without_notification_actions_is_unchanged(tmp_path):
    adapter = _make_adapter(tmp_path)
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    adapter._bot = bot

    result = await adapter.send("12345", "hello", metadata={"notify": True})

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert "reply_markup" not in kwargs
    assert adapter._notification_action_store._entries == {}


@pytest.mark.asyncio
async def test_notification_finished_callback_removes_buttons_and_is_idempotent(tmp_path):
    adapter = _make_adapter(tmp_path)

    class Runner:
        def __init__(self):
            self.calls = 0

        def handle_message(self, *_args, **_kwargs):
            pass

        async def handle_notification_action(self, **_kwargs):
            self.calls += 1

    runner = Runner()
    adapter._message_handler = runner.handle_message
    entry = adapter._notification_action_store.register(
        notification_id="notif-1",
        chat_id="12345",
        user_id="12345",
        actions=True,
    )
    update, query = _make_update(entry.callback_data("finished"))

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())

    query.answer.assert_called_once_with(text="Finished ✅")
    query.edit_message_text.assert_called_once()
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None
    assert adapter._notification_action_store.get(entry.short_id).status == "finished"
    assert runner.calls == 1

    query.answer.reset_mock()
    query.edit_message_text.reset_mock()
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())
    query.answer.assert_called_once_with(text="already finished")
    query.edit_message_text.assert_not_called()
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_notification_callback_rejects_wrong_user_and_expired(tmp_path):
    adapter = _make_adapter(tmp_path)
    entry = adapter._notification_action_store.register(
        notification_id="notif-1",
        chat_id="12345",
        user_id="12345",
        ttl_seconds=1,
        now=1000,
    )

    wrong_update, wrong_query = _make_update(entry.callback_data("finished"), user_id="999")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(wrong_update, MagicMock())
    wrong_query.answer.assert_called_once()
    assert "does not belong" in wrong_query.answer.call_args.kwargs["text"]

    expired_update, expired_query = _make_update(entry.callback_data("finished"), user_id="12345")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "gateway.notification_actions.time.time", return_value=1002
    ):
        await adapter._handle_callback_query(expired_update, MagicMock())
    expired_query.answer.assert_called_once()
    assert "expired" in expired_query.answer.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_notification_snooze_first_click_shows_presets_without_committing(tmp_path):
    adapter = _make_adapter(tmp_path)
    snooze = adapter._notification_action_store.register(
        notification_id="notif-snooze",
        chat_id="12345",
        user_id="12345",
        actions={"snooze": {"seconds": 7200}},
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        snooze_update, snooze_query = _make_update(snooze.callback_data("snooze"))
        await adapter._handle_callback_query(snooze_update, MagicMock())

    persisted = adapter._notification_action_store.get(snooze.short_id)
    assert persisted is not None
    assert persisted.status == "open"
    assert persisted.snooze_until is None
    snooze_query.answer.assert_called_once_with(text="Choose a snooze duration.")
    snooze_query.edit_message_text.assert_called_once()
    kwargs = snooze_query.edit_message_text.call_args.kwargs
    assert "Choose a snooze duration" in kwargs["text"]
    markup = kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["1 hour", "Tomorrow morning", "Next week", "Custom…"]
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callback_data == [
        f"na:{snooze.short_id}:snooze_1h",
        f"na:{snooze.short_id}:snooze_tomorrow",
        f"na:{snooze.short_id}:snooze_next_week",
        f"na:{snooze.short_id}:snooze_custom",
    ]
    assert all(len(payload.encode("utf-8")) <= 64 for payload in callback_data)


@pytest.mark.asyncio
async def test_notification_snooze_preset_and_help_are_visible_and_persisted(tmp_path):
    adapter = _make_adapter(tmp_path)
    snooze = adapter._notification_action_store.register(
        notification_id="notif-snooze",
        chat_id="12345",
        user_id="12345",
        actions={"snooze": {"seconds": 7200}},
    )
    help_entry = adapter._notification_action_store.register(
        notification_id="notif-help",
        chat_id="12345",
        user_id="12345",
        actions={"help_me": {"label": "Help me"}},
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        snooze_update, snooze_query = _make_update(f"na:{snooze.short_id}:snooze_1h")
        await adapter._handle_callback_query(snooze_update, MagicMock())
        help_update, help_query = _make_update(help_entry.callback_data("help_me"))
        await adapter._handle_callback_query(help_update, MagicMock())

    assert snooze_query.answer.call_args.kwargs["text"] == "Snoozed for 1 hour ⏰"
    assert adapter._notification_action_store.get(snooze.short_id).status == "snoozed"
    assert adapter._notification_action_store.get(snooze.short_id).snooze_until is not None
    assert help_query.answer.call_args.kwargs["text"].startswith("Help requested:")
    persisted_help = adapter._notification_action_store.get(help_entry.short_id)
    assert persisted_help.status == "help_requested"
    assert persisted_help.help_handle.startswith("help:")


@pytest.mark.asyncio
async def test_notification_snooze_custom_defers_to_chat_without_terminal_state(tmp_path):
    adapter = _make_adapter(tmp_path)
    snooze = adapter._notification_action_store.register(
        notification_id="notif-snooze",
        chat_id="12345",
        user_id="12345",
        actions=True,
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        update, query = _make_update(f"na:{snooze.short_id}:snooze_custom")
        await adapter._handle_callback_query(update, MagicMock())

    persisted = adapter._notification_action_store.get(snooze.short_id)
    assert persisted is not None
    assert persisted.status == "open"
    assert persisted.snooze_until is None
    query.answer.assert_called_once_with(text="Reply in chat with the snooze time you want.", show_alert=True)
    query.edit_message_text.assert_called_once()
    assert "Custom snooze requested" in query.edit_message_text.call_args.kwargs["text"]
