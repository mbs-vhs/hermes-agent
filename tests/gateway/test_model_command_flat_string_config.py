"""Tests for the gateway ``/model X --global`` persist path after ADR-072
(CLAWD-2214) neutralized it.

Provider/model is manifest-governed: ``substrate-contract/roster.yaml``
``provider_policy`` → ``scripts/generate_profile_provider.py`` → each profile's
``config.yaml``. A manual ``/model X --global`` from Telegram/Discord must NOT
write ``config.yaml`` (that would clobber the roster-generated config), and must
surface a refusal instead. The in-session switch (the session override the next
turn reads) still applies.

These cases exercise the three ``model:`` config shapes the old persist branch
had to coerce (flat string / missing / proper dict) and assert that *regardless
of shape* the file is left untouched.
"""

import yaml
import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    return runner


def _make_event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )


def _fake_switch_result():
    """Build a successful ModelSwitchResult that bypasses real provider resolution."""
    from hermes_cli.model_switch import ModelSwitchResult

    return ModelSwitchResult(
        success=True,
        new_model="gpt-5.5",
        target_provider="openrouter",
        provider_changed=True,
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        provider_label="OpenRouter",
        is_global=True,
    )


def _setup_isolated_home(tmp_path, monkeypatch, model_yaml_value):
    """Write a config.yaml with the given ``model:`` value and stub the heavy bits."""
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"model": model_yaml_value, "providers": {}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: _fake_switch_result(),
    )
    # save_config writes to ``get_hermes_home() / config.yaml`` — point it here.
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)
    return cfg_path


def _assert_session_switch_applied(runner):
    """The in-session swap must still populate the session override the next
    turn reads, even though the --global persist is refused."""
    overrides = list(runner._session_model_overrides.values())
    assert len(overrides) == 1, (
        "session-only switch should have set exactly one override, got %r"
        % (runner._session_model_overrides,)
    )
    assert overrides[0]["model"] == "gpt-5.5"
    assert overrides[0]["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_model_global_refuses_persist_with_flat_string_model(tmp_path, monkeypatch):
    """``model: deepseek-v4-flash`` (flat string): ``/model X --global`` must
    refuse to persist and leave the flat string untouched.
    """
    cfg_path = _setup_isolated_home(tmp_path, monkeypatch, "deepseek-v4-flash")

    runner = _make_runner()
    result = await runner._handle_model_command(
        _make_event("/model gpt-5.5 --global")
    )

    # The confirmation surfaces the manifest-governed refusal.
    assert result is not None
    assert "gpt-5.5" in result
    assert "manifest-governed" in result
    assert "ADR-072" in result

    # config.yaml is left exactly as written — no persist.
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"] == "deepseek-v4-flash"

    _assert_session_switch_applied(runner)


@pytest.mark.asyncio
async def test_model_global_refuses_persist_with_missing_model(tmp_path, monkeypatch):
    """``model:`` key absent entirely: the refusal must not create it."""
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"providers": {}}), encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: _fake_switch_result(),
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)

    runner = _make_runner()
    result = await runner._handle_model_command(
        _make_event("/model gpt-5.5 --global")
    )

    assert result is not None
    assert "manifest-governed" in result

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "model" not in written

    _assert_session_switch_applied(runner)


@pytest.mark.asyncio
async def test_model_global_refuses_persist_with_proper_dict_model(tmp_path, monkeypatch):
    """Already-nested ``model: {default, provider}``: the refusal must leave the
    existing dict untouched (no clobber of the roster-generated block).
    """
    cfg_path = _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )

    runner = _make_runner()
    result = await runner._handle_model_command(
        _make_event("/model gpt-5.5 --global")
    )

    assert result is not None
    assert "manifest-governed" in result

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "old-model"
    assert written["model"]["provider"] == "openai-codex"

    _assert_session_switch_applied(runner)
