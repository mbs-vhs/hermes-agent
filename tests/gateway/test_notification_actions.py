"""Tests for server-side notification action registry."""

from gateway.notification_actions import NotificationActionStore


def test_notification_action_callback_data_stays_under_telegram_cap(tmp_path):
    store = NotificationActionStore(tmp_path / "actions.json")

    entry = store.register(
        notification_id="notif-1",
        source_type="kanban",
        source_id="t_123",
        chat_id="12345",
        user_id="12345",
        thread_id="99",
        actions=True,
        now=1000,
    )

    for verb in entry.actions:
        payload = entry.callback_data(verb)
        assert payload.startswith(f"na:{entry.short_id}:")
        assert len(payload.encode("utf-8")) <= 64


def test_notification_action_expiry_and_context_auth(tmp_path):
    store = NotificationActionStore(tmp_path / "actions.json")
    entry = store.register(
        notification_id="notif-1",
        chat_id="12345",
        user_id="42",
        thread_id="7",
        ttl_seconds=60,
        now=1000,
    )

    assert entry.is_expired(now=1059) is False
    assert entry.is_expired(now=1060) is True
    assert store.verify_context(entry, chat_id="12345", user_id="42", thread_id="7") is True
    assert store.verify_context(entry, chat_id="999", user_id="42", thread_id="7") is False
    assert store.verify_context(entry, chat_id="12345", user_id="99", thread_id="7") is False
    assert store.verify_context(entry, chat_id="12345", user_id="42", thread_id="8") is False


def test_notification_actions_are_idempotent_and_persisted(tmp_path):
    path = tmp_path / "actions.json"
    store = NotificationActionStore(path)
    entry = store.register(notification_id="notif-1", chat_id="12345", user_id="12345")

    changed, label = store.finish(entry, user_id="12345", now=1000)
    assert changed is True
    assert label == "finished"

    changed, label = store.finish(entry, user_id="12345", now=1001)
    assert changed is False
    assert label == "already finished"

    reloaded = NotificationActionStore(path)
    persisted = reloaded.get(entry.short_id)
    assert persisted is not None
    assert persisted.status == "finished"
    assert persisted.resolved_by == "12345"
