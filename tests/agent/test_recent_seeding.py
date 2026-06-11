"""Unit tests for the recent cross-surface seeding helper (CLAWD-1542 Phase S).

Covers the gate/flag handling, the fail-open read path, the formatted block
shape, and the fire-and-forget append. All HTTP is mocked — no real clawd is
contacted. The integration seams (conversation_loop injection, run_agent
append) are exercised in their own test files; this file pins the helper's
contract in isolation.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from agent import recent_seeding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(monkeypatch, **kwargs):
    for key in (
        "HERMES_RECENT_SEEDING_ENABLED",
        "CLAWD_BASE_URL",
        "CLAWD_API_AUTH_TOKEN",
        "HERMES_RECENT_SEEDING_LIMIT",
        "HERMES_RECENT_SEEDING_READ_TIMEOUT",
        "HERMES_RECENT_SEEDING_WRITE_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    for k, v in kwargs.items():
        monkeypatch.setenv(k, v)


def _resp(status_code=200, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload if payload is not None else {}
    return r


def _client_cm(client):
    """Wrap a mock client so ``with httpx.Client(...) as c`` yields it."""
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


# ---------------------------------------------------------------------------
# 1. Master gate
# ---------------------------------------------------------------------------


class TestSeedingEnabled:
    def test_default_off(self, monkeypatch):
        _set_env(monkeypatch)
        assert recent_seeding.seeding_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch, val):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED=val)
        assert recent_seeding.seeding_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, monkeypatch, val):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED=val)
        assert recent_seeding.seeding_enabled() is False


# ---------------------------------------------------------------------------
# 2. Flag OFF => fully inert (zero httpx calls)
# ---------------------------------------------------------------------------


class TestInertWhenDisabled:
    def test_read_no_httpx_when_disabled(self, monkeypatch):
        _set_env(monkeypatch)  # disabled
        with patch.object(recent_seeding, "httpx", create=True) as hx:
            out = recent_seeding.read_recent_seed("minerva:morgan")
        assert out == ""
        hx.Client.assert_not_called()

    def test_append_no_httpx_when_disabled(self, monkeypatch):
        _set_env(monkeypatch)  # disabled
        with patch.object(recent_seeding, "httpx", create=True) as hx:
            t = recent_seeding.append_turn_async("minerva:morgan", "hi", "hello")
        assert t is None
        hx.Client.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Empty conversation_id => no read, no write
# ---------------------------------------------------------------------------


class TestEmptyConversationId:
    def test_read_empty_id(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        with patch("httpx.Client") as client_cls:
            out = recent_seeding.read_recent_seed("")
        assert out == ""
        client_cls.assert_not_called()

    def test_append_empty_id(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        with patch("httpx.Client") as client_cls:
            t = recent_seeding.append_turn_async("", "hi", "hello")
        assert t is None
        client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# 4. format_seed_block
# ---------------------------------------------------------------------------


class TestFormatSeedBlock:
    def test_empty_turns(self):
        assert recent_seeding.format_seed_block([]) == ""

    def test_skips_blank_and_malformed(self):
        block = recent_seeding.format_seed_block([
            {"role": "user", "content": ""},      # blank content -> skip
            {"role": "", "content": "orphan"},    # blank role -> skip
            "not-a-dict",                          # malformed -> skip
        ])
        assert block == ""

    def test_happy_path_shape(self):
        block = recent_seeding.format_seed_block([
            {"role": "user", "content": "what's the weather"},
            {"role": "assistant", "content": "sunny"},
        ])
        assert block.startswith("<recent-shared-context>")
        assert block.endswith("</recent-shared-context>")
        assert "user: what's the weather" in block
        assert "assistant: sunny" in block
        # framed as reference data, not user input
        assert "NOT new user input" in block


# ---------------------------------------------------------------------------
# 5. read_recent_seed — happy path + fail-open
# ---------------------------------------------------------------------------


class TestReadRecentSeed:
    def test_happy_path(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1",
                 CLAWD_API_AUTH_TOKEN="tok")
        client = MagicMock()
        client.get.return_value = _resp(200, {
            "conversation_id": "minerva:morgan",
            "turns": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong"},
            ],
            "count": 2,
        })
        with patch("httpx.Client", return_value=_client_cm(client)) as client_cls:
            out = recent_seeding.read_recent_seed("minerva:morgan")
        # block returned and well-formed
        assert "user: ping" in out
        assert "assistant: pong" in out
        # auth header threaded through
        _, kwargs = client_cls.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        # read timeout default applied (hard cap)
        assert kwargs["timeout"] == 1.5
        # limit param sent
        _, get_kwargs = client.get.call_args
        assert get_kwargs["params"]["limit"] == 8

    def test_custom_limit_and_timeout(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1",
                 HERMES_RECENT_SEEDING_LIMIT="3",
                 HERMES_RECENT_SEEDING_READ_TIMEOUT="0.5")
        client = MagicMock()
        client.get.return_value = _resp(200, {"turns": []})
        with patch("httpx.Client", return_value=_client_cm(client)) as client_cls:
            recent_seeding.read_recent_seed("c")
        _, kwargs = client_cls.call_args
        assert kwargs["timeout"] == 0.5
        _, get_kwargs = client.get.call_args
        assert get_kwargs["params"]["limit"] == 3

    def test_empty_turns_returns_empty(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        client = MagicMock()
        client.get.return_value = _resp(200, {"turns": []})
        with patch("httpx.Client", return_value=_client_cm(client)):
            assert recent_seeding.read_recent_seed("c") == ""

    def test_non_200_fails_open(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        client = MagicMock()
        client.get.return_value = _resp(500, {"turns": [{"role": "user", "content": "x"}]})
        with patch("httpx.Client", return_value=_client_cm(client)):
            # non-200 => empty seed, no raise
            assert recent_seeding.read_recent_seed("c") == ""

    def test_connection_error_fails_open(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        client = MagicMock()
        client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=_client_cm(client)):
            # must NOT raise
            assert recent_seeding.read_recent_seed("c") == ""

    def test_timeout_fails_open(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        import httpx

        client = MagicMock()
        client.get.side_effect = httpx.ReadTimeout("slow")
        with patch("httpx.Client", return_value=_client_cm(client)):
            assert recent_seeding.read_recent_seed("c") == ""

    def test_parse_error_fails_open(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        r = MagicMock()
        r.status_code = 200
        r.json.side_effect = ValueError("bad json")
        client = MagicMock()
        client.get.return_value = r
        with patch("httpx.Client", return_value=_client_cm(client)):
            assert recent_seeding.read_recent_seed("c") == ""


# ---------------------------------------------------------------------------
# 6. append_turn_async — both POSTs, order, gating
# ---------------------------------------------------------------------------


class TestAppendTurnAsync:
    def test_fires_both_posts_in_order(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1",
                 CLAWD_API_AUTH_TOKEN="tok",
                 HERMES_RECENT_SEEDING_WRITE_TIMEOUT="2.0")
        client = MagicMock()
        with patch("httpx.Client", return_value=_client_cm(client)) as client_cls:
            t = recent_seeding.append_turn_async("minerva:morgan", "hi", "hello")
            assert t is not None
            t.join(timeout=5)
        # two POSTs: user then assistant (chronological)
        assert client.post.call_count == 2
        first, second = client.post.call_args_list
        assert first.kwargs["json"]["role"] == "user"
        assert first.kwargs["json"]["content"] == "hi"
        assert second.kwargs["json"]["role"] == "assistant"
        assert second.kwargs["json"]["content"] == "hello"
        # write timeout applied
        _, kwargs = client_cls.call_args
        assert kwargs["timeout"] == 2.0

    def test_skips_when_either_side_empty(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        with patch("httpx.Client") as client_cls:
            assert recent_seeding.append_turn_async("c", "", "hello") is None
            assert recent_seeding.append_turn_async("c", "hi", "") is None
            assert recent_seeding.append_turn_async("c", None, "hello") is None
        client_cls.assert_not_called()

    def test_swallows_post_errors(self, monkeypatch):
        _set_env(monkeypatch, HERMES_RECENT_SEEDING_ENABLED="1")
        client = MagicMock()
        client.post.side_effect = ConnectionError("boom")
        with patch("httpx.Client", return_value=_client_cm(client)):
            t = recent_seeding.append_turn_async("c", "hi", "hello")
            assert t is not None
            # worker thread must not propagate — join completes cleanly
            t.join(timeout=5)
            assert not t.is_alive()
