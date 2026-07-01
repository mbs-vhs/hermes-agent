"""Tests for the Agora virtual platform plugin."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_agora = load_plugin_adapter("agora")

AgoraAdapter = _agora.AgoraAdapter
check_requirements = _agora.check_requirements
validate_config = _agora.validate_config
is_connected = _agora.is_connected
_env_enablement = _agora._env_enablement
_standalone_send = _agora._standalone_send
DEFAULT_CID = _agora.DEFAULT_CID


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "msg-123"}


class _AsyncClient:
    last_post = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_post = {"url": url, "json": json, "headers": headers}
        return _Response()


def test_platform_enum_resolves_via_plugin_scan():
    p = Platform("agora")
    assert p.value == "agora"
    assert Platform("agora") is p


def test_requirements_enabled_by_clawd_token(monkeypatch):
    monkeypatch.setattr(_agora, "HTTPX_AVAILABLE", True)
    monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "tok")
    assert check_requirements() is True


def test_requirements_can_be_explicitly_enabled_without_token(monkeypatch):
    monkeypatch.setattr(_agora, "HTTPX_AVAILABLE", True)
    monkeypatch.delenv("CLAWD_API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("AGORA_ENABLED", "true")
    assert check_requirements() is True


def test_env_enablement_seeds_home_channel_from_minerva_env(monkeypatch):
    monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAWD_BASE_URL", "http://clawd.local/")
    monkeypatch.setenv("AGORA_HOME_CHANNEL", "minerva:morgan")
    monkeypatch.setenv("AGORA_HOME_CHANNEL_NAME", "Agora Home")

    seed = _env_enablement()

    assert seed["base_url"] == "http://clawd.local"
    assert seed["default_cid"] == "minerva:morgan"
    assert seed["token"] == "tok"
    assert seed["home_channel"] == {"chat_id": "minerva:morgan", "name": "Agora Home"}


def test_validate_config_accepts_token_or_explicit_unauth_flag(monkeypatch):
    monkeypatch.delenv("CLAWD_API_AUTH_TOKEN", raising=False)
    assert validate_config(PlatformConfig(enabled=True, extra={"token": "tok"})) is True
    assert validate_config(PlatformConfig(enabled=True, extra={"enabled_without_token": True})) is True
    assert validate_config(PlatformConfig(enabled=True, extra={})) is False


def test_adapter_connect_marks_virtual_target_connected():
    adapter = AgoraAdapter(PlatformConfig(enabled=True, extra={"token": "tok"}))
    assert _run(adapter.connect()) is True
    assert adapter.is_connected is True
    _run(adapter.disconnect())
    assert adapter.is_connected is False


def test_standalone_send_posts_assistant_turn_to_clawd(monkeypatch):
    monkeypatch.setattr(_agora, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(_agora, "httpx", SimpleNamespace(AsyncClient=_AsyncClient))
    pconfig = PlatformConfig(
        enabled=True,
        extra={"token": "tok", "base_url": "http://clawd.local", "privacy_class": "work_cor"},
    )

    result = _run(_standalone_send(pconfig, "minerva:morgan", "hello"))

    assert result["success"] is True
    assert result["platform"] == "agora"
    assert result["chat_id"] == "minerva:morgan"
    assert _AsyncClient.last_post is not None
    post = _AsyncClient.last_post
    assert post["url"] == "http://clawd.local/chat/conversation/minerva%3Amorgan/turn"
    assert post["headers"]["Authorization"] == "Bearer tok"
    assert post["json"]["role"] == "assistant"
    assert post["json"]["content"] == "hello"
    assert post["json"]["privacy_class"] == "work_cor"
    assert post["json"]["idempotency_key"].startswith("hermes-agora:")


def test_standalone_send_defaults_to_minerva_morgan(monkeypatch):
    monkeypatch.setattr(_agora, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(_agora, "httpx", SimpleNamespace(AsyncClient=_AsyncClient))
    pconfig = PlatformConfig(enabled=True, extra={"token": "tok"})

    result = _run(_standalone_send(pconfig, "", "hello"))

    assert result["success"] is True
    assert result["chat_id"] == DEFAULT_CID
