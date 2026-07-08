"""LOCK-IN: ``/model X --global`` must be refused at every surface (ADR-072 /
CLAWD-2214).

Provider/model is manifest-governed — declared in
``substrate-contract/roster.yaml`` ``provider_policy`` and generated into each
profile's ``config.yaml`` by ``scripts/generate_profile_provider.py``. A manual
``--global`` switch persisting provider/model would clobber that generated
config, so the direct-persist path is neutralized at all four surfaces:

  1. cli.py ``_apply_model_switch_result``  (interactive CLI picker)
  2. cli.py ``_handle_model_switch``        (interactive CLI typed ``/model X --global``)
  3. gateway/run.py ``_handle_model_command`` (Telegram/Discord gateway)
  4. tui_gateway/server.py ``_apply_model_switch`` (TUI)

Each surface must:
  - NOT write config.yaml (no ``save_config_value`` / ``save_config`` call),
  - surface the manifest-governed refusal, and
  - still apply the in-session swap (in-memory + running-agent).

If any of these regress, a manual switch can silently clobber the
manifest-generated config again — the exact drift ADR-072 exists to end.
"""

import types

import yaml
import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #
def _fake_result():
    return types.SimpleNamespace(
        success=True,
        error_message="",
        new_model="gpt-5.5",
        target_provider="openrouter",
        provider_changed=True,
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        provider_label="OpenRouter",
        warning_message="",
        model_info=None,
    )


class _AgentRec:
    """Records the in-place switch_model() call (the running-agent swap)."""

    def __init__(self):
        self.switched = None
        self._config_context_length = None
        # attrs the TUI / gateway read off the agent
        self.provider = "old-provider"
        self.model = "old-model"
        self.base_url = ""
        self.api_key = "sk-old"

    def switch_model(self, **kwargs):
        self.switched = kwargs


class _StubCLI:
    """Minimal instance attrs the two cli.py methods read/write on ``self``."""

    def __init__(self, agent=None):
        self.agent = agent
        self.model = "old-model"
        self.provider = "old-provider"
        self.requested_provider = ""
        self.api_key = ""
        self._explicit_api_key = ""
        self.base_url = ""
        self._explicit_base_url = ""
        self.api_mode = ""
        self._pending_model_switch_note = ""

    def _confirm_expensive_model_switch(self, result):
        # Stub: upstream's cli.py gates an expensive-model switch behind this
        # confirmation; these ADR-072 persist tests aren't about the cost prompt,
        # so always confirm so the switch proceeds to the persist decision.
        return True


# --------------------------------------------------------------------------- #
# Surface 1 — CLI picker: _apply_model_switch_result
# --------------------------------------------------------------------------- #
def test_lockin_cli_picker_refuses_persist(monkeypatch):
    import cli as cli_mod

    captured: list[str] = []
    saves: list = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: captured.append(str(s)))
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: saves.append(a))
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length", lambda *a, **k: 0
    )

    agent = _AgentRec()
    stub = _StubCLI(agent=agent)
    cli_mod.HermesCLI._apply_model_switch_result(stub, _fake_result(), True)

    joined = "\n".join(captured)
    assert saves == [], "picker path must not persist provider/model to config"
    assert "manifest-governed" in joined
    assert "ADR-072" in joined
    # In-session swap still applied.
    assert stub.model == "gpt-5.5"
    assert stub.provider == "openrouter"
    assert agent.switched is not None


# --------------------------------------------------------------------------- #
# Surface 2 — CLI typed: _handle_model_switch
# --------------------------------------------------------------------------- #
def test_lockin_cli_typed_refuses_persist(monkeypatch):
    import cli as cli_mod

    captured: list[str] = []
    saves: list = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: captured.append(str(s)))
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: saves.append(a))
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model", lambda **kw: _fake_result()
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length", lambda *a, **k: 0
    )

    def _no_inventory(*a, **k):
        raise RuntimeError("inventory unavailable in test")

    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", _no_inventory)

    agent = _AgentRec()
    stub = _StubCLI(agent=agent)
    cli_mod.HermesCLI._handle_model_switch(stub, "/model gpt-5.5 --global")

    joined = "\n".join(captured)
    assert saves == [], "typed /model --global must not persist provider/model"
    assert "manifest-governed" in joined
    assert "ADR-072" in joined
    # In-session swap still applied.
    assert stub.model == "gpt-5.5"
    assert stub.provider == "openrouter"
    assert agent.switched is not None


# --------------------------------------------------------------------------- #
# Surface 3 — gateway: _handle_model_command
# --------------------------------------------------------------------------- #
def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    return runner


@pytest.mark.asyncio
async def test_lockin_gateway_refuses_persist(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"model": {"default": "old-model", "provider": "openai-codex"}, "providers": {}}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: types.SimpleNamespace(
            success=True,
            new_model="gpt-5.5",
            target_provider="openrouter",
            provider_changed=True,
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_label="OpenRouter",
            warning_message="",
            model_info=None,
        ),
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)

    runner = _make_runner()
    event = MessageEvent(
        text="/model gpt-5.5 --global",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )
    result = await runner._handle_model_command(event)

    assert "manifest-governed" in result
    assert "ADR-072" in result
    # config.yaml left untouched — the roster-generated block is not clobbered.
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "old-model"
    assert written["model"]["provider"] == "openai-codex"
    # In-session swap still applied (the next turn reads this override).
    overrides = list(runner._session_model_overrides.values())
    assert len(overrides) == 1
    assert overrides[0]["model"] == "gpt-5.5"
    assert overrides[0]["provider"] == "openrouter"


# --------------------------------------------------------------------------- #
# Surface 4 — TUI: _apply_model_switch
# --------------------------------------------------------------------------- #
def test_lockin_tui_refuses_persist(monkeypatch):
    from tui_gateway import server

    saved: dict = {}
    agent = _AgentRec()
    result = types.SimpleNamespace(
        success=True,
        new_model="gpt-5.5",
        target_provider="openrouter",
        api_key="sk-new",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        warning_message="",
    )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: result)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda sid, session: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved.update(cfg))

    session = {"agent": agent, "session_key": "session-key"}
    out = server._apply_model_switch("sid", session, "gpt-5.5 --global")

    assert out["value"] == "gpt-5.5"
    assert saved == {}, "TUI /model --global must not persist provider/model"
    assert "manifest-governed" in out["warning"]
    assert "ADR-072" in out["warning"]
    # In-session swap still applied (running-agent in-place switch).
    assert agent.switched is not None
