"""The GATE on the fork-local capability-honesty block, which nothing pinned.

Two tests already touch ``CAPABILITY_HONESTY_GUIDANCE``:

* ``tests/agent/test_prompt_builder.py::TestGuidanceConstants::
  test_capability_honesty_guidance_forbids_unbacked_action_claims`` — asserts the
  *text* of the constant.
* ``tests/agent/test_system_prompt.py::
  test_coding_prompt_preserves_legacy_workspace_order`` — asserts its *position*,
  and only on an agent that HAS tools (``valid_tool_names=["read_file"]``).

Neither asserts the condition it is emitted under.  ``agent/system_prompt.py``
guards the append with ``if agent.valid_tool_names:`` (CLAWD-1815: the rule is
about claiming an action you did not take via a tool call, so it is meaningless
with no tools loaded).  Dropping that guard — emitting it unconditionally —
passes both existing tests and every other test in the tree, while adding a
block to the cached static prefix of every tool-less session: a prompt-cache
change no assertion would report.

This file pins the gate in both directions, and asserts the STEER note travels
with it (same guard, same rationale) so a half-removed guard is visible too.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _static_prefix(agent, monkeypatch):
    """The cached static prefix, with the two blocks under test stubbed.

    Stubbed rather than matched by substring so this stays a test of the GATE,
    not a second copy of the prompt text (which test_prompt_builder.py owns).
    """
    import agent.system_prompt as system_prompt

    monkeypatch.setattr(system_prompt, "CAPABILITY_HONESTY_GUIDANCE", "<<HONESTY>>")
    monkeypatch.setattr(system_prompt, "STEER_CHANNEL_NOTE", "<<STEER>>")
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        parts = build_system_prompt_parts(agent)
    # build_system_prompt_parts returns the three cache tiers; the two blocks
    # under test are appended to `stable_parts`, i.e. the "stable" tier.
    assert set(parts) == {"stable", "context", "volatile"}, sorted(parts)
    return parts["stable"]


def test_capability_honesty_is_emitted_when_tools_are_loaded(monkeypatch):
    """Control for the negative test below.

    If the stub never landed, the ``not in`` assertion in the tool-less test
    would be vacuously true forever.
    """
    static = _static_prefix(_make_agent(valid_tool_names=["read_file"]), monkeypatch)
    assert "<<HONESTY>>" in static, static
    assert "<<STEER>>" in static, static


def test_capability_honesty_is_omitted_when_no_tools_are_loaded(monkeypatch):
    """A tool-less profile (e.g. a transparent memory-only plugin session) has
    no tool call to be honest *about*; the block is gated on
    ``agent.valid_tool_names`` and must not reach the cached static prefix."""
    static = _static_prefix(_make_agent(valid_tool_names=[]), monkeypatch)
    assert "<<HONESTY>>" not in static, (
        "CAPABILITY_HONESTY_GUIDANCE reached a tool-less agent's cached static "
        "prompt prefix; agent/system_prompt.py gates it on "
        f"`if agent.valid_tool_names:`. Static prefix was:\n{static}"
    )
    assert "<<STEER>>" not in static, (
        "STEER_CHANNEL_NOTE reached a tool-less agent's prompt; steering only "
        f"lands inside tool results. Static prefix was:\n{static}"
    )
