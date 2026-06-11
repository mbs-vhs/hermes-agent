"""run_agent-level branch-selection guard for the thread-canonical seam.

CLAWD-1621 / ADR-067 either-or: the ``_sync_external_memory_for_turn`` seam
in ``run_agent.py`` dispatches on ``thread_canonical_enabled()`` —

    if thread_canonical_enabled():
        append_turn_canonical_async(...)   # flag ON  -> clawd thread write
    else:
        append_turn_async(...)             # flag OFF -> direct convturns append

The recent_seeding unit tests cover each helper's *internal* gating, but
nothing asserts the run_agent dispatch picks the RIGHT arm per the flag and
NEVER fires both (a double-land regression). This test closes that gap by
patching both helpers + the gate and asserting exactly-one-arm selection.

Both helpers are imported *inside* the seam via
``from agent.recent_seeding import (...)`` so the patch targets live on the
``agent.recent_seeding`` module (late binding), matching the precedent in
``test_recent_seeding_append.py``.
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


class TestCanonicalBranchSelection:
    def test_flag_on_selects_canonical_only(self):
        agent = _bare_agent()
        with patch("agent.recent_seeding.thread_canonical_enabled", return_value=True), \
                patch("agent.recent_seeding.append_turn_canonical_async") as canonical, \
                patch("agent.recent_seeding.append_turn_async") as direct:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=False,
            )
        canonical.assert_called_once()
        # The direct convturns arm must NOT also fire (no double-land).
        direct.assert_not_called()
        args = canonical.call_args.args
        assert args[0] == "minerva:morgan"
        assert args[1] == "what's up"
        assert args[2] == "not much"

    def test_flag_off_selects_direct_only(self):
        agent = _bare_agent()
        with patch("agent.recent_seeding.thread_canonical_enabled", return_value=False), \
                patch("agent.recent_seeding.append_turn_canonical_async") as canonical, \
                patch("agent.recent_seeding.append_turn_async") as direct:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=False,
            )
        direct.assert_called_once()
        # The canonical thread-write arm must NOT fire when the flag is off.
        canonical.assert_not_called()
        args = direct.call_args.args
        assert args[0] == "minerva:morgan"
        assert args[1] == "what's up"
        assert args[2] == "not much"

    def test_interrupted_turn_fires_neither_arm(self):
        """The interrupted gate (#15218) sits BEFORE the either-or dispatch:
        a partial turn lands in neither convturns nor the canonical thread,
        regardless of the flag."""
        agent = _bare_agent()
        with patch("agent.recent_seeding.thread_canonical_enabled", return_value=True), \
                patch("agent.recent_seeding.append_turn_canonical_async") as canonical, \
                patch("agent.recent_seeding.append_turn_async") as direct:
            agent._sync_external_memory_for_turn(
                original_user_message="what's up",
                final_response="not much",
                interrupted=True,
            )
        canonical.assert_not_called()
        direct.assert_not_called()
