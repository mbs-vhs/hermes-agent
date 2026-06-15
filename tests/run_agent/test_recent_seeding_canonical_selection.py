"""run_agent no longer branches on the thread-canonical flag (CLAWD-1621/ADR-067).

The canonical clawd-thread write was RELOCATED out of Hermes core into the
mnemosyne provider (``~/dev/hermes-mnemosyne-provider``). Before the relocation
``_sync_external_memory_for_turn`` dispatched on ``thread_canonical_enabled()``
(``append_turn_canonical_async`` vs ``append_turn_async``) — plugin-specific
logic in a core file. Now core calls ``append_turn_async`` UNCONDITIONALLY; the
helper self-suppresses when the canonical write owns convturns
(``HERMES_THREAD_CANONICAL`` on), and the canonical write itself lives in the
provider.

This test pins that invariant: the run_agent seam always calls
``append_turn_async`` exactly once on a normal turn regardless of the flag, and
never calls it on an interrupted turn. (The helper's internal suppress gate is
covered in ``tests/agent/test_recent_seeding.py``; the provider's canonical
write is covered in the provider repo.)
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


class TestCoreDoesNotBranchOnCanonicalFlag:
    @pytest.mark.parametrize("canonical", ["1", "0", ""])
    def test_always_calls_append_turn_async(self, monkeypatch, canonical):
        """Normal turn: core calls append_turn_async exactly once whether the
        canonical flag is on, off, or unset — no in-core branch on the flag."""
        if canonical:
            monkeypatch.setenv("HERMES_THREAD_CANONICAL", canonical)
        else:
            monkeypatch.delenv("HERMES_THREAD_CANONICAL", raising=False)
        agent = _bare_agent()
        with patch("agent.recent_seeding.append_turn_async") as direct:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=False,
            )
        direct.assert_called_once()
        args = direct.call_args.args
        assert args[0] == "minerva:morgan"
        assert args[1] == "what's up"
        assert args[2] == "not much"

    def test_no_canonical_helper_imported_in_core(self):
        """The relocated helper must NOT be referenced by core anymore — it was
        removed from recent_seeding (lives in the provider now)."""
        from agent import recent_seeding

        assert not hasattr(recent_seeding, "append_turn_canonical_async")

    def test_interrupted_turn_fires_no_append(self):
        """The interrupted gate (#15218) sits BEFORE the append: a partial turn
        lands in neither convturns nor the canonical thread, flag regardless."""
        agent = _bare_agent()
        with patch("agent.recent_seeding.append_turn_async") as direct:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=True,
            )
        direct.assert_not_called()
