"""Truth table for TelegramAdapter._notification_kwargs across the three
notification modes (silent / important / all).

Guards the load-bearing invariant introduced with the ``silent`` mode
(feat/telegram-silent-notifications-mode): ``silent`` suppresses ALL pushes,
overriding even an explicit ``metadata["notify"] = True``. ``_notification_kwargs``
only reads ``self._notifications_mode``, so a lightweight stub is sufficient and
avoids constructing a full adapter + bot client.
"""
import types

from gateway.platforms.telegram import TelegramAdapter


def _nk(mode, metadata):
    stub = types.SimpleNamespace(_notifications_mode=mode)
    return TelegramAdapter._notification_kwargs(stub, metadata)


def test_silent_always_suppresses_even_notify():
    assert _nk("silent", None) == {"disable_notification": True}
    assert _nk("silent", {}) == {"disable_notification": True}
    # Load-bearing: silent overrides an explicit notify request.
    assert _nk("silent", {"notify": True}) == {"disable_notification": True}


def test_important_silent_unless_notify():
    assert _nk("important", None) == {"disable_notification": True}
    assert _nk("important", {"notify": False}) == {"disable_notification": True}
    assert _nk("important", {"notify": True}) == {}


def test_all_always_pushes():
    assert _nk("all", None) == {}
    assert _nk("all", {"notify": True}) == {}


def test_default_mode_is_important():
    stub = types.SimpleNamespace()  # no _notifications_mode attr set
    assert TelegramAdapter._notification_kwargs(stub, None) == {"disable_notification": True}
