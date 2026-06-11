"""Integration guard for the recent-seeding append seam (CLAWD-1542 Phase S).

The append rides ``AIAgent._sync_external_memory_for_turn`` — the same
turn-completion helper that mirrors into external memory providers. It must:

  * fire the fire-and-forget append on a NORMAL completed turn,
  * be SKIPPED on an interrupted turn (shares the #15218 interrupted gate),
  * be inert when the master flag is OFF,
  * be inert when ``_shared_conversation_id`` is empty.

We patch ``agent.recent_seeding.append_turn_async`` to observe the call without
touching real HTTP.
"""
from unittest.mock import MagicMock, patch

import pytest


def _bare_agent(shared_id="minerva:morgan", with_memory=True):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock() if with_memory else None
    agent.session_id = "sess-1"
    agent._shared_conversation_id = shared_id
    return agent


class TestAppendSeam:
    def test_append_fires_on_normal_turn(self):
        agent = _bare_agent()
        with patch("agent.recent_seeding.append_turn_async") as append:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=False,
            )
        append.assert_called_once()
        args = append.call_args.args
        assert args[0] == "minerva:morgan"
        assert args[1] == "what's up"
        assert args[2] == "not much"

    def test_append_skipped_when_interrupted(self):
        agent = _bare_agent()
        with patch("agent.recent_seeding.append_turn_async") as append:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",  # looks complete but partial
                interrupted=True,
            )
        append.assert_not_called()

    def test_append_fires_even_without_memory_manager(self):
        """Seeding is independent of whether an external memory provider is
        configured — the append seam sits before the memory-manager guard."""
        agent = _bare_agent(with_memory=False)
        with patch("agent.recent_seeding.append_turn_async") as append:
            agent._sync_external_memory_for_turn(
                original_user_message="ping",
                final_response="pong",
                interrupted=False,
            )
        append.assert_called_once()

    def test_append_inert_when_flag_off(self, monkeypatch):
        """Flag OFF: append_turn_async is still *called* (the gate lives inside
        it) but performs zero HTTP. Assert the real helper makes no httpx call."""
        monkeypatch.delenv("HERMES_RECENT_SEEDING_ENABLED", raising=False)
        agent = _bare_agent()
        with patch("httpx.Client") as client_cls:
            agent._sync_external_memory_for_turn(
                original_user_message="ping",
                final_response="pong",
                interrupted=False,
            )
        client_cls.assert_not_called()

    def test_append_inert_when_shared_id_empty(self, monkeypatch):
        monkeypatch.setenv("HERMES_RECENT_SEEDING_ENABLED", "1")
        agent = _bare_agent(shared_id="")
        with patch("httpx.Client") as client_cls:
            agent._sync_external_memory_for_turn(
                original_user_message="ping",
                final_response="pong",
                interrupted=False,
            )
        client_cls.assert_not_called()
