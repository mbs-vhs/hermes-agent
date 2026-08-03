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

    def _turn_ctx_src(self):
        path = Path(__file__).resolve().parents[2] / "agent" / "turn_context.py"
        return path.read_text(encoding="utf-8")

    def test_seed_goes_into_user_injections(self):
        """The seed is composed into the USER message's API content.

        Was a literal check for ``_injections.append(_recent_seed_block)`` in
        ``conversation_loop.py``. The v2026.7.20 merge (CLAWD-3009) moved that
        composition into ``turn_context.compose_user_api_content`` — upstream's
        single source of the user-message API bytes — so the literal is gone
        while the invariant is not. Asserted BEHAVIOURALLY against the real
        helper rather than re-pinning a new string, since a string check would
        have to be rewritten by whoever next moves the seam and proves nothing
        about what it does.
        """
        from agent.turn_context import compose_user_api_content

        seed = format_seed_block([
            {"role": "user", "content": "earlier on voice: book a table"},
            {"role": "assistant", "content": "booked for 7pm"},
        ])
        assert seed  # sanity

        composed = compose_user_api_content(
            "what time is my reservation", "", "", seed
        )
        assert composed is not None, "the seed was dropped entirely"
        assert seed in composed, "the seed is not in the user-message API content"
        assert composed.startswith("what time is my reservation"), (
            "the seed must be APPENDED to the user's own text, not prepended"
        )

        # Empty seed injects nothing (no trailing separator churn).
        assert compose_user_api_content("hi", "", "", "") is None

    def test_stamp_site_and_wire_site_compose_the_seed_identically(self):
        """The prompt-cache invariant the merge introduced, and its hazard.

        v2026.7.20 made the prologue persist an ``api_content`` sidecar — the
        exact bytes sent for this turn — which every later turn replays verbatim
        so the provider's cache prefix stays stable. If the seed were composed
        only at the ``api_messages`` build (where the fork used to do it) the
        stamp and the wire would differ by exactly the seed block, and every
        subsequent turn would re-prefill from the injection point. So BOTH call
        sites must pass the seed.
        """
        conv = self._conv_src()
        ctx = self._turn_ctx_src()

        assert "recent_seed_block=_recent_seed_block" in conv, (
            "conversation_loop no longer hands the seed to build_turn_context, "
            "so the prologue's api_content stamp will omit it"
        )
        assert "recent_seed_block" in ctx, (
            "turn_context does not accept/forward the seed"
        )
        # The stamp site inside build_turn_context must pass it through.
        stamp = ctx.index("_api_content = compose_user_api_content(")
        assert "recent_seed_block" in ctx[stamp:stamp + 300], (
            "build_turn_context stamps api_content WITHOUT the seed — the "
            "persisted sidecar and the wire will diverge by the seed block"
        )
        # The loop's live-compose fallback must pass it too.
        live = conv.index("_composed = compose_user_api_content(")
        assert "_recent_seed_block" in conv[live:live + 300], (
            "the api_messages live-compose path omits the seed, so callers "
            "that bypass prologue stamping silently lose it"
        )

    def test_seed_is_read_before_the_prologue(self):
        """Ordering guard. The prologue stamps ``api_content``; a seed read
        AFTER that call cannot reach the stamp. This broke silently once during
        the v2026.7.20 merge and is invisible in any behavioural test that does
        not run the real prologue."""
        src = self._conv_src()
        read_at = src.index("read_recent_seed as _read_recent_seed")
        prologue_at = src.index("_ctx = build_turn_context(")
        assert read_at < prologue_at, (
            "the recent-seed read moved BELOW build_turn_context; the prologue "
            "will stamp api_content without the seed"
        )

    def test_seed_not_in_system_prompt_build(self):
        src = self._conv_src()
        # locate the effective_system assignment; the seed var must not appear
        # between it and the system-message prepend.
        start = src.index("effective_system = active_system_prompt")
        window = src[start:start + 600]
        assert "_recent_seed_block" not in window, (
            "recent seed must never enter the system prompt (breaks cache prefix)"
        )
