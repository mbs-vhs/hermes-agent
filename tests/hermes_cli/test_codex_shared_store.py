"""Tests for the shared read-only Codex OAuth store (CLAWD-2378).

HERMES_CODEX_SHARED_STORE points a per-user *hardened* gateway at the fleet's
single shared Codex OAuth token. When set, resolution is READ-ONLY: the shared
token wins (OVERRIDE, not fallback-below-pool), the local pool/global store are
never read, and the refresh endpoint is never called — so agents never rotate
the single-use token (the CLAWD-1665 stale-shadow failure). The sole-writer
refresher (devops-process/scripts/refresh-codex-oauth.py) owns rotation.

Env UNSET → byte-identical to upstream behavior (see the T4 regression below,
which mirrors tests/hermes_cli/test_auth_codex_provider.py).
"""

import base64
import json
import time
from pathlib import Path

import hermes_cli.auth as auth_mod
from hermes_cli.auth import (
    DEFAULT_CODEX_BASE_URL,
    resolve_codex_runtime_credentials,
)


def _write_shared_file(
    path: Path,
    *,
    access_token: str = "shared-access",
    refresh_token: str = "shared-refresh",
    last_refresh: str = "2026-07-06T00:00:00Z",
) -> Path:
    """Write a refresher-shape shared Codex store to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "last_refresh": last_refresh,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def _setup_local_codex_auth(
    hermes_home: Path,
    *,
    access_token: str = "local-access",
    refresh_token: str = "local-refresh",
) -> Path:
    """Seed a local Codex provider-state into HERMES_HOME/auth.json."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    auth_store = {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "last_refresh": "2026-02-26T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        },
    }
    auth_file = hermes_home / "auth.json"
    auth_file.write_text(json.dumps(auth_store, indent=2))
    return auth_file


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("utf-8")
    return f"h.{encoded}.s"


# =============================================================================
# T1 — env set → shared token returned; local read + refresh never called
# =============================================================================


def test_env_set_returns_shared_token_without_touching_local_or_refresh(tmp_path, monkeypatch):
    shared_path = _write_shared_file(tmp_path / "shared" / "auth.json", access_token="shared-access-1")
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared_path))

    def _boom(*args, **kwargs):
        raise AssertionError("must not be called when the shared store is active")

    monkeypatch.setattr(auth_mod, "_read_codex_tokens", _boom)
    monkeypatch.setattr(auth_mod, "_refresh_codex_auth_tokens", _boom)
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _boom)
    monkeypatch.setattr(auth_mod, "_save_codex_tokens", _boom)

    creds = resolve_codex_runtime_credentials()

    assert creds["api_key"] == "shared-access-1"
    assert creds["source"] == "hermes-codex-shared-store"
    assert creds["provider"] == "openai-codex"
    assert creds["auth_mode"] == "chatgpt"
    assert creds["base_url"] == DEFAULT_CODEX_BASE_URL
    assert creds["last_refresh"] == "2026-07-06T00:00:00Z"


# =============================================================================
# T2 — near-expiry shared + force_refresh=True → still no refresh; file frozen
# =============================================================================


def test_force_refresh_ignored_and_shared_file_unchanged(tmp_path, monkeypatch):
    expiring = _jwt_with_exp(int(time.time()) - 10)
    shared_path = _write_shared_file(tmp_path / "shared" / "auth.json", access_token=expiring)
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared_path))

    def _boom(*args, **kwargs):
        raise AssertionError("shared-store resolution must never refresh/rotate")

    monkeypatch.setattr(auth_mod, "_refresh_codex_auth_tokens", _boom)
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _boom)
    monkeypatch.setattr(auth_mod, "_save_codex_tokens", _boom)

    before_content = shared_path.read_text()
    before_mtime = shared_path.stat().st_mtime_ns

    creds = resolve_codex_runtime_credentials(force_refresh=True, refresh_if_expiring=True)

    assert creds["api_key"] == expiring
    assert creds["source"] == "hermes-codex-shared-store"
    # No-rotate proof: the shared store is never written by a reader.
    assert shared_path.read_text() == before_content
    assert shared_path.stat().st_mtime_ns == before_mtime


# =============================================================================
# T3 — ANTI-1665: stale local + fresh shared + env set → fresh shared wins
# =============================================================================


def test_anti_1665_fresh_shared_beats_stale_local(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _setup_local_codex_auth(
        hermes_home,
        access_token="stale-local-access",
        refresh_token="stale-local-refresh",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    shared_path = _write_shared_file(tmp_path / "shared" / "auth.json", access_token="fresh-shared-access")
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared_path))

    creds = resolve_codex_runtime_credentials()

    assert creds["api_key"] == "fresh-shared-access"
    assert creds["source"] == "hermes-codex-shared-store"
    # The local store must be untouched — the agent is a read-only consumer.
    local = json.loads((hermes_home / "auth.json").read_text())
    assert local["providers"]["openai-codex"]["tokens"]["access_token"] == "stale-local-access"


# =============================================================================
# T4 — env UNSET → byte-identical to today (reads local store, refreshes)
# =============================================================================


def test_env_unset_uses_local_store_and_refreshes(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CODEX_SHARED_STORE", raising=False)
    hermes_home = tmp_path / "hermes"
    expiring = _jwt_with_exp(int(time.time()) - 10)
    _setup_local_codex_auth(hermes_home, access_token=expiring, refresh_token="refresh-old")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    called = {"count": 0}

    def _fake_refresh(tokens, timeout_seconds):
        called["count"] += 1
        return {"access_token": "access-new", "refresh_token": "refresh-new"}

    monkeypatch.setattr(auth_mod, "_refresh_codex_auth_tokens", _fake_refresh)

    creds = resolve_codex_runtime_credentials()

    assert called["count"] == 1
    assert creds["api_key"] == "access-new"
    assert creds["source"] == "hermes-auth-store"


# =============================================================================
# T5 — round-trip: read hook parses the refresher-written shape
# =============================================================================


def test_read_hook_parses_refresher_shape(tmp_path, monkeypatch):
    shared_path = _write_shared_file(
        tmp_path / "shared" / "auth.json",
        access_token="rt-access",
        refresh_token="rt-refresh",
        last_refresh="2026-07-06T12:34:56Z",
    )
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared_path))

    state = auth_mod._read_codex_shared_store()

    assert state is not None
    assert state["tokens"]["access_token"] == "rt-access"
    assert state["tokens"]["refresh_token"] == "rt-refresh"
    assert state["last_refresh"] == "2026-07-06T12:34:56Z"


# =============================================================================
# Helper edge cases
# =============================================================================


def test_shared_store_path_none_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_CODEX_SHARED_STORE", raising=False)
    assert auth_mod._codex_shared_store_path() is None


def test_shared_store_path_resolves_when_set(tmp_path, monkeypatch):
    target = tmp_path / "shared" / "auth.json"
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(target))
    assert auth_mod._codex_shared_store_path() == target


def test_read_hook_none_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(tmp_path / "shared" / "absent.json"))
    assert auth_mod._read_codex_shared_store() is None


def test_read_hook_none_on_malformed_json(tmp_path, monkeypatch):
    path = tmp_path / "shared" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(path))
    assert auth_mod._read_codex_shared_store() is None


def test_read_hook_none_on_missing_access_token(tmp_path, monkeypatch):
    path = tmp_path / "shared" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"refresh_token": "r"}, "last_refresh": "x"}))
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(path))
    assert auth_mod._read_codex_shared_store() is None


def test_env_set_but_malformed_shared_falls_through_to_local(tmp_path, monkeypatch):
    # Env set but the shared file is unparseable → read hook returns None →
    # resolution falls through to the normal local-store path (fail-open to the
    # local store; a hardened profile with an empty local store fails closed).
    shared = tmp_path / "shared" / "auth.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("garbage-not-json")
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared))

    hermes_home = tmp_path / "hermes"
    _setup_local_codex_auth(hermes_home, access_token="local-access", refresh_token="local-refresh")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)

    assert creds["api_key"] == "local-access"
    assert creds["source"] == "hermes-auth-store"


# =============================================================================
# T6 — parallel pool gate (Q3): shared store active → codex pool stays empty
# =============================================================================


def test_pool_empty_when_shared_store_active(tmp_path, monkeypatch):
    from agent.credential_pool import load_pool

    hermes_home = tmp_path / "hermes"
    _setup_local_codex_auth(hermes_home, access_token="local-access", refresh_token="local-refresh")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    shared_path = _write_shared_file(tmp_path / "shared" / "auth.json")
    monkeypatch.setenv("HERMES_CODEX_SHARED_STORE", str(shared_path))

    pool = load_pool("openai-codex")

    # Even with a seed-able local provider-state, the pool is empty so runtime
    # resolution can never take the rotation path — it falls through to
    # resolve_codex_runtime_credentials(), which returns the shared token.
    assert pool.has_credentials() is False
    assert pool.entries() == []


def test_pool_seeds_local_when_shared_store_unset(tmp_path, monkeypatch):
    from agent.credential_pool import load_pool

    monkeypatch.delenv("HERMES_CODEX_SHARED_STORE", raising=False)
    hermes_home = tmp_path / "hermes"
    _setup_local_codex_auth(hermes_home, access_token="local-access", refresh_token="local-refresh")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    pool = load_pool("openai-codex")

    # Env unset → byte-identical to upstream: the local provider-state seeds the
    # device_code pool entry.
    assert pool.has_credentials() is True
