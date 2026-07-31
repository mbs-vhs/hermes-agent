"""Behavioural guard for the fork-local recent-seed injection at the
composition seam (CLAWD-1542 Phase S / ADR-065).

Why this file exists
--------------------
``tests/run_agent/test_recent_seeding_injection.py::TestSourceShape``
pinned the wiring by *source text*: it asserted the literal
``_injections.append(_recent_seed_block)`` appeared in
``agent/conversation_loop.py``. Upstream v2026.7.20 moved the composition
into ``agent.turn_context.compose_user_api_content`` (now "the single source
of that composition"), so that literal no longer exists — the guard failed on
a pure refactor while the behaviour it cared about was intact.

A source-text assertion cannot distinguish "the feature was deleted" from
"the feature moved", which is exactly the question a 2493-file upstream merge
raises. So pin the invariant behaviourally instead, against the production
function.

The fork-local invariant, restated:

  * ``recent_seed_block`` is appended to the API copy of the current turn's
    user message — never to the system prompt (a per-turn-changing system
    prefix defeats Anthropic prompt caching).
  * The clean stored content stays an untouched prefix of the composed value.
  * Injection order is prefetch -> recent seed -> plugin context. The
    docstring calls the order load-bearing: a reorder changes the bytes, and
    therefore the cache prefix, for every existing session.

``recent_seed_block`` is a fork-local trailing parameter with no upstream
equivalent, which is precisely why an upstream merge can drop it silently.
Before this file, every existing call to ``compose_user_api_content`` in the
test suite passed 2-3 positional args and let the seed default to ``""`` —
the parameter had no behavioural coverage at all.
"""

from __future__ import annotations

import pytest

from agent.memory_manager import build_memory_context_block
from agent.recent_seeding import format_seed_block
from agent.turn_context import compose_user_api_content


SEED = format_seed_block(
    [
        {"role": "user", "content": "what did we decide about the pricing tier?"},
        {"role": "assistant", "content": "we settled on usage-based billing"},
    ]
)


def test_format_seed_block_produces_a_nonempty_fenced_block():
    """Guard the guard: the rest of this file is vacuous if SEED is empty."""
    assert SEED, "format_seed_block returned empty — fixture no longer realistic"


class TestSeedReachesTheUserMessage:
    def test_seed_alone_is_appended_to_user_content(self):
        out = compose_user_api_content("hello", "", "", SEED)
        assert out is not None, (
            "recent seed produced no injection — the fork-local seed no longer "
            "reaches the API copy of the user message"
        )
        assert SEED in out
        assert out == "hello\n\n" + SEED

    def test_clean_content_stays_an_untouched_prefix(self):
        out = compose_user_api_content("hello", "", "", SEED)
        assert out.startswith("hello")

    def test_seed_survives_alongside_prefetch_and_plugin_context(self):
        out = compose_user_api_content("hello", "likes tea", "PLUGIN-CTX", SEED)
        assert SEED in out
        assert "PLUGIN-CTX" in out
        assert build_memory_context_block("likes tea") in out


class TestInjectionOrderIsLoadBearing:
    def test_order_is_prefetch_then_seed_then_plugin(self):
        out = compose_user_api_content("hello", "likes tea", "PLUGIN-CTX", SEED)
        prefetch = build_memory_context_block("likes tea")
        assert out.index(prefetch) < out.index(SEED) < out.index("PLUGIN-CTX"), (
            "injection order changed — this rewrites the cache prefix for every "
            "existing session (see compose_user_api_content docstring)"
        )

    def test_seed_precedes_plugin_context_without_prefetch(self):
        out = compose_user_api_content("hello", "", "PLUGIN-CTX", SEED)
        assert out.index(SEED) < out.index("PLUGIN-CTX")


class TestNegativeControls:
    """Sensitivity checks: assertions above must be able to fail."""

    def test_no_seed_means_no_seed_text(self):
        out = compose_user_api_content("hello", "", "PLUGIN-CTX", "")
        assert out == "hello\n\nPLUGIN-CTX"
        assert SEED not in out

    def test_nothing_to_inject_returns_none(self):
        assert compose_user_api_content("hello", "", "", "") is None

    def test_seed_does_not_rescue_non_string_content(self):
        """Multimodal turns are sent as-is; the seed must not force a string
        composition that would drop the image blocks."""
        blocks = [{"type": "text", "text": "hello"}]
        assert compose_user_api_content(blocks, "", "", SEED) is None


class TestSeedIsNotSystemPromptMaterial:
    def test_seed_block_is_not_emitted_by_the_memory_context_builder(self):
        """The seed rides the user-message path, not the system-prompt/memory
        path. If a merge ever re-routes it through the system prefix, the
        block would have to come out of a different builder than this one."""
        assert SEED != build_memory_context_block("likes tea")
        assert SEED not in (build_memory_context_block("likes tea") or "")


@pytest.mark.parametrize(
    "prefetch,plugin",
    [("", ""), ("likes tea", ""), ("", "PLUGIN-CTX"), ("likes tea", "PLUGIN-CTX")],
)
def test_seed_present_in_every_injection_combination(prefetch, plugin):
    out = compose_user_api_content("hello", prefetch, plugin, SEED)
    assert out is not None and SEED in out
