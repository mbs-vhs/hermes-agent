"""Coverage for the v0.18 B1 fix: `_resolve_lifecycle_pinned` (CLAWD-1376).

When Telegram moved to `plugins/platforms/telegram/adapter.py` in the v0.18
merge, the port carried `_notifications_mode` but DROPPED the
`gateway/run.py::_create_adapter()` post-construction step that resolved the
badge-free pinned-lifecycle opt-in. Review item B1 restored it as the standalone
`_resolve_lifecycle_pinned()` (env `HERMES_TELEGRAM_LIFECYCLE_PINNED` first, else
config `display.platforms.telegram.lifecycle_pinned`, else False) and wired it
back into `_build_adapter()`.

The pre-existing `tests/gateway/test_pinned_lifecycle_status.py` only ever sets
`adapter._lifecycle_pinned` DIRECTLY on a fake adapter — it never exercises the
resolution itself (despite a docstring line claiming it does). These tests pin
the actual resolution logic so a future re-port / re-merge that drops the flag
again fails loudly instead of silently reverting the fleet to the CLAWD-1144
fresh-DM-per-restart (badge-spam) lifecycle.

Tester note: added by the dedicated v0.18-merge tester (test-only). It drives
the REAL `_resolve_lifecycle_pinned` / `_build_adapter` code, not a
reimplementation.
"""
import types

import pytest

from plugins.platforms.telegram import adapter as tg_adapter


# ── env override ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
def test_env_truthy_values_enable(monkeypatch, value):
    """Every documented truthy spelling of the env var flips the flag on,
    case-insensitively."""
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", value)
    assert tg_adapter._resolve_lifecycle_pinned() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", "nope", " "])
def test_env_falsey_values_disable(monkeypatch, value):
    """Anything outside the truthy set (including explicit disables and junk)
    resolves to False."""
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", value)
    # Config must NOT be consulted when the env var is present (even if falsey),
    # so a config that says "true" cannot override an env "off". Make the config
    # loud so a regression that consulted it would flip this to True.
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"lifecycle_pinned": "true"}}}},
    )
    assert tg_adapter._resolve_lifecycle_pinned() is False


def test_env_is_stripped(monkeypatch):
    """Leading/trailing whitespace around a truthy value still enables."""
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", "  on  ")
    assert tg_adapter._resolve_lifecycle_pinned() is True


# ── config fallback (env absent) ─────────────────────────────────────────────

@pytest.mark.parametrize("cfg_value,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), (True, True),
    ("0", False), ("false", False), ("off", False), (False, False),
])
def test_config_fallback_when_env_absent(monkeypatch, cfg_value, expected):
    """With the env var unset, the flag is read from
    display.platforms.telegram.lifecycle_pinned and normalized the same way."""
    monkeypatch.delenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", raising=False)
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"lifecycle_pinned": cfg_value}}}},
    )
    assert tg_adapter._resolve_lifecycle_pinned() is expected


def test_defaults_false_when_env_and_config_absent(monkeypatch):
    """No env, no config key → default OFF (preserves CLAWD-1144 legacy path)."""
    monkeypatch.delenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", raising=False)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: {})
    assert tg_adapter._resolve_lifecycle_pinned() is False


def test_config_load_failure_defaults_false(monkeypatch):
    """A broken config loader must not raise out of resolution — defaults OFF."""
    monkeypatch.delenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", raising=False)

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("gateway.config.load_gateway_config", _boom)
    assert tg_adapter._resolve_lifecycle_pinned() is False


# ── env precedence over config ───────────────────────────────────────────────

def test_env_on_beats_config_off(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", "1")
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"lifecycle_pinned": "false"}}}},
    )
    assert tg_adapter._resolve_lifecycle_pinned() is True


def test_env_off_beats_config_on(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", "off")
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"lifecycle_pinned": "true"}}}},
    )
    assert tg_adapter._resolve_lifecycle_pinned() is False


# ── integration: _build_adapter wires the flag onto the adapter ──────────────

def test_build_adapter_applies_resolved_flag(monkeypatch):
    """The B1 regression was that `_build_adapter` (the plugin port) stopped
    applying the flag. Assert the resolved value lands on the constructed
    adapter — the actual post-construction step B1 restored."""
    class _FakeTelegramAdapter:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(tg_adapter, "TelegramAdapter", _FakeTelegramAdapter)
    monkeypatch.setenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", "yes")

    built = tg_adapter._build_adapter(types.SimpleNamespace())
    assert built._lifecycle_pinned is True


def test_build_adapter_defaults_flag_off(monkeypatch):
    class _FakeTelegramAdapter:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(tg_adapter, "TelegramAdapter", _FakeTelegramAdapter)
    monkeypatch.delenv("HERMES_TELEGRAM_LIFECYCLE_PINNED", raising=False)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: {})

    built = tg_adapter._build_adapter(types.SimpleNamespace())
    assert built._lifecycle_pinned is False
