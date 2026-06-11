"""Injection-seam guard for recent seeding (CLAWD-1542 Phase S).

The seed MUST be injected into the current turn's USER message, never the
system prompt — otherwise the cached system prefix changes byte-for-byte every
turn and Anthropic prompt caching is defeated.

This file pins that invariant two ways:

  1. Behavioural: replicate the exact injection transform used in
     ``conversation_loop.run_conversation`` (append the seed block to the
     current user message's content; build the system message from the
     system prompt alone) and assert the seed lands in the user message and is
     ABSENT from the system message.

  2. Source-shape: assert the production wiring reads
     ``_shared_conversation_id`` into ``read_recent_seed`` and appends the
     result via the ``_injections`` user-message path — and that the
     system-message build (``effective_system``) does NOT reference the seed.
"""
from pathlib import Path

from agent.recent_seeding import format_seed_block


def _build_api_messages(messages, current_turn_user_idx, system_prompt, seed_block):
    """Mirror of the conversation_loop injection contract under test.

    Replicates: seed appended to the current user message's content; system
    message built from the system prompt only (seed never touches it).
    """
    api_messages = []
    for idx, msg in enumerate(messages):
        api_msg = msg.copy()
        if idx == current_turn_user_idx and msg.get("role") == "user":
            injections = []
            if seed_block:
                injections.append(seed_block)
            if injections:
                base = api_msg.get("content", "")
                if isinstance(base, str):
                    api_msg["content"] = base + "\n\n" + "\n\n".join(injections)
        api_messages.append(api_msg)
    if system_prompt:
        api_messages = [{"role": "system", "content": system_prompt}] + api_messages
    return api_messages


class TestInjectionTarget:
    def test_seed_lands_in_user_message_not_system(self):
        seed = format_seed_block([
            {"role": "user", "content": "earlier on voice: book a table"},
            {"role": "assistant", "content": "booked for 7pm"},
        ])
        assert seed  # sanity
        messages = [
            {"role": "user", "content": "what time is my reservation"},
        ]
        system_prompt = "You are a helpful assistant. [STABLE CACHE PREFIX]"

        api_messages = _build_api_messages(
            messages, current_turn_user_idx=0,
            system_prompt=system_prompt, seed_block=seed,
        )

        system_msg = api_messages[0]
        user_msg = api_messages[1]
        assert system_msg["role"] == "system"
        # cache prefix is byte-stable: seed is NOT in the system prompt
        assert system_msg["content"] == system_prompt
        assert "recent-shared-context" not in system_msg["content"]
        # seed IS appended to the user message
        assert "recent-shared-context" in user_msg["content"]
        assert "what time is my reservation" in user_msg["content"]

    def test_empty_seed_injects_nothing(self):
        messages = [{"role": "user", "content": "hi"}]
        api_messages = _build_api_messages(
            messages, current_turn_user_idx=0,
            system_prompt="SYS", seed_block="",
        )
        # user message unchanged when seed empty
        assert api_messages[1]["content"] == "hi"


class TestSourceShape:
    """Static guard: the production seam wires the read + user-message inject
    and keeps the seed out of the system prompt."""

    def _conv_src(self):
        path = Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
        return path.read_text(encoding="utf-8")

    def test_reads_shared_conversation_id(self):
        src = self._conv_src()
        assert "read_recent_seed" in src
        assert "_shared_conversation_id" in src

    def test_seed_goes_into_user_injections(self):
        src = self._conv_src()
        # the seed variable is appended to the user-message _injections list
        assert "_injections.append(_recent_seed_block)" in src

    def test_seed_not_in_system_prompt_build(self):
        src = self._conv_src()
        # locate the effective_system assignment; the seed var must not appear
        # between it and the system-message prepend.
        start = src.index("effective_system = active_system_prompt")
        window = src[start:start + 600]
        assert "_recent_seed_block" not in window, (
            "recent seed must never enter the system prompt (breaks cache prefix)"
        )
