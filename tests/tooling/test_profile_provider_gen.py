"""Hermetic tests for scripts/generate_profile_provider.py (ADR-072 P1b).

Proves the profile provider/model generator is:
- **surgical** — only ``model.default`` / ``model.provider`` change; every other
  key, value, and comment is preserved verbatim,
- **idempotent** — a second run with no manifest change is a byte-identical no-op,
- a working **drift gate** — ``--check`` detects an injected divergence and
  writes nothing.

Fully hermetic: a FIXTURE ``ProviderPolicy`` + a FIXTURE ``config.yaml`` in
``tmp_path``. No network, no real ~/.hermes profiles, no roster dependency.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from substrate_contract import ProviderDefault, ProviderPolicy


def _load_generator():
    """Import scripts/generate_profile_provider.py as a standalone module."""
    name = "_generate_profile_provider_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parents[2] / "scripts" / "generate_profile_provider.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's dataclass can resolve its own
    # (string) annotations via sys.modules under `from __future__ annotations`.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()


# A fixture policy that DIFFERS from the fixture config below, so a merge is a
# real change (default provider ∈ allowed_providers, per the manifest invariant).
FIXTURE_POLICY = ProviderPolicy(
    default=ProviderDefault(provider="openai-codex", model="gpt-5.5"),
    allowed_providers=("openai-codex", "xai-oauth"),
)

# pyyaml-canonical form (0-offset sequences) — how Hermes itself writes
# config.yaml — so the round-trip is a fixpoint for every non-model line.
FIXTURE_CONFIG = """\
# Hermes profile config (fixture, ADR-072 P1b test)
model:
  provider: xai-oauth
  default: grok-4
  base_url: ''
toolsets:
- hermes-cli
- workflow
agent:
  max_turns: 90
memory:
  provider: mnemosyne  # keep this comment exactly
"""


def _write_config(tmp_path: Path, text: str = FIXTURE_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_merges_only_model_block_and_preserves_everything_else(tmp_path):
    cfg = _write_config(tmp_path)
    result = GEN.apply_provider_policy(cfg, FIXTURE_POLICY)

    assert result.changed is True
    assert result.wrote is True

    out = cfg.read_text(encoding="utf-8")
    # The generated model block carries the manifest values.
    assert "provider: openai-codex" in out
    assert "default: gpt-5.5" in out
    # The pre-existing (non-generated) model key is untouched.
    assert "base_url: ''" in out
    # Comments + non-model keys survive verbatim.
    assert "# Hermes profile config (fixture, ADR-072 P1b test)" in out
    assert "provider: mnemosyne  # keep this comment exactly" in out

    # Only the two model value lines changed vs the input — everything else byte-identical.
    changed_lines = [
        (a, b)
        for a, b in zip(FIXTURE_CONFIG.splitlines(), out.splitlines())
        if a != b
    ]
    assert changed_lines == [
        ("  provider: xai-oauth", "  provider: openai-codex"),
        ("  default: grok-4", "  default: gpt-5.5"),
    ]
    assert len(out.splitlines()) == len(FIXTURE_CONFIG.splitlines())

    # Every non-model key is preserved (semantic check, independent of formatting).
    import yaml

    before = yaml.safe_load(FIXTURE_CONFIG)
    after = yaml.safe_load(out)
    before.pop("model")
    after.pop("model")
    assert before == after


def test_backup_written_on_change(tmp_path):
    cfg = _write_config(tmp_path)
    result = GEN.apply_provider_policy(cfg, FIXTURE_POLICY)

    assert result.backup_path == cfg.with_name("config.yaml.bak")
    assert result.backup_path.exists()
    # The .bak holds the pre-merge original verbatim.
    assert result.backup_path.read_text(encoding="utf-8") == FIXTURE_CONFIG


def test_idempotent_second_run_is_byte_identical_noop(tmp_path):
    cfg = _write_config(tmp_path)

    first = GEN.apply_provider_policy(cfg, FIXTURE_POLICY)
    assert first.wrote is True
    generated = cfg.read_text(encoding="utf-8")

    # Second run: nothing to change.
    second = GEN.apply_provider_policy(cfg, FIXTURE_POLICY)
    assert second.changed is False
    assert second.wrote is False
    assert second.backup_path is None
    assert cfg.read_text(encoding="utf-8") == generated


def test_check_mode_detects_drift_and_writes_nothing(tmp_path):
    cfg = _write_config(tmp_path)  # xai-oauth/grok-4 diverges from the manifest
    result = GEN.apply_provider_policy(cfg, FIXTURE_POLICY, check=True)

    assert result.changed is True
    assert result.wrote is False
    assert result.backup_path is None
    # --check must not mutate the target.
    assert cfg.read_text(encoding="utf-8") == FIXTURE_CONFIG
    # No backup created in check mode.
    assert not cfg.with_name("config.yaml.bak").exists()


def test_check_mode_in_sync_reports_no_change(tmp_path):
    # A config already matching the manifest.
    in_sync = FIXTURE_CONFIG.replace("provider: xai-oauth", "provider: openai-codex").replace(
        "default: grok-4", "default: gpt-5.5"
    )
    cfg = _write_config(tmp_path, in_sync)
    result = GEN.apply_provider_policy(cfg, FIXTURE_POLICY, check=True)
    assert result.changed is False
    assert result.wrote is False


def test_include_allowed_emits_allowed_providers(tmp_path):
    cfg = _write_config(tmp_path)
    GEN.apply_provider_policy(cfg, FIXTURE_POLICY, include_allowed=True)

    import yaml

    model = yaml.safe_load(cfg.read_text(encoding="utf-8"))["model"]
    assert model["allowed_providers"] == ["openai-codex", "xai-oauth"]
    assert model["provider"] == "openai-codex"
    assert model["default"] == "gpt-5.5"


def test_main_check_returns_nonzero_on_drift(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setattr(GEN, "provider_policy_for", lambda _id: FIXTURE_POLICY)

    rc = GEN.main(["--profile", "engineer", "--config", str(cfg), "--check"])
    assert rc == 1
    # --check left the file untouched.
    assert cfg.read_text(encoding="utf-8") == FIXTURE_CONFIG


def test_main_check_returns_zero_when_in_sync(tmp_path, monkeypatch):
    in_sync = FIXTURE_CONFIG.replace("provider: xai-oauth", "provider: openai-codex").replace(
        "default: grok-4", "default: gpt-5.5"
    )
    cfg = _write_config(tmp_path, in_sync)
    monkeypatch.setattr(GEN, "provider_policy_for", lambda _id: FIXTURE_POLICY)

    rc = GEN.main(["--profile", "engineer", "--config", str(cfg), "--check"])
    assert rc == 0


def test_main_writes_via_home_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home)  # writes <home>/config.yaml
    monkeypatch.setattr(GEN, "provider_policy_for", lambda _id: FIXTURE_POLICY)

    rc = GEN.main(["--profile", "engineer", "--home", str(home)])
    assert rc == 0
    out = (home / "config.yaml").read_text(encoding="utf-8")
    assert "provider: openai-codex" in out
    assert "default: gpt-5.5" in out
    assert (home / "config.yaml.bak").exists()


def test_main_missing_profile_returns_error(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)

    def _raise(_id):
        raise KeyError(_id)

    monkeypatch.setattr(GEN, "provider_policy_for", _raise)
    rc = GEN.main(["--profile", "nope", "--config", str(cfg)])
    assert rc == 2


def test_scalar_model_is_replaced_with_mapping(tmp_path):
    cfg = _write_config(tmp_path, "model: grok-4\ntoolsets:\n- hermes-cli\n")
    GEN.apply_provider_policy(cfg, FIXTURE_POLICY)

    import yaml

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["model"] == {"provider": "openai-codex", "default": "gpt-5.5"}
    assert data["toolsets"] == ["hermes-cli"]
