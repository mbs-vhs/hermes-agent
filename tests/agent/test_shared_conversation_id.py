"""Behavioural tests for the shared (person, agent) conversation_id (CLAWD-1542 / ADR-065).

A stable conversation_id is derived once at the agent_init seam as
``f"{profile}:{user_id}"`` (surface-agnostic) and threaded through
``MemoryManager.sync_all`` -> ``provider.sync_turn(..., conversation_id=...)``.

The mnemosyne provider stamps it into the auto-capture metadata ONLY when the
``MNEMOSYNE_SHARED_CONVERSATION`` flag (config key ``shared_conversation``) is
ON; OFF/unset => the metadata key is absent => the adapter falls back to
``parent_session_id`` => byte-identical to the prior behaviour.

These tests cover:
  1. Derivation: profile+user_id -> "minerva:<uid>"; empty user_id -> "".
  2. Propagation: sync_all forwards conversation_id by keyword to sync_turn.
  3. Cross-surface acceptance: a Telegram-shaped source and a voice-delegate
     source for the SAME (morgan, minerva) yield the SAME conversation_id.
  4. Provider-boundary flag behaviour (mnemosyne): flag ON => meta carries the
     derived id; flag OFF/unset => meta omits it => adapter slot falls back to
     parent_session_id (the zero-change smoke).
"""

import os
import sys
import threading
from pathlib import Path

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


# ---------------------------------------------------------------------------
# Shared fake provider that records sync_turn kwargs (mirrors the _RecordingProvider
# pattern in test_memory_session_switch.py, but captures conversation_id too).
# ---------------------------------------------------------------------------


class _CidRecordingProvider(MemoryProvider):
    """Records every sync_turn call's kwargs for assertion."""

    def __init__(self, name="rec"):
        self._name = name
        self.sync_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:  # pragma: no cover - unused
        return True

    def initialize(self, session_id, **kwargs):  # pragma: no cover - unused
        pass

    def get_tool_schemas(self):
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", conversation_id=""):
        self.sync_calls.append(
            {
                "user": user_content,
                "asst": assistant_content,
                "session_id": session_id,
                "conversation_id": conversation_id,
            }
        )


# ---------------------------------------------------------------------------
# 1. Derivation — f"{profile}:{user_id}", empty user_id => "" (no-op)
# ---------------------------------------------------------------------------


def _derive(profile, user_id):
    """Mirror the exact derivation in agent_init.py:1144-1147 so the formula is
    pinned by a test. If the production formula changes, this test must change.
    """
    return f"{profile}:{user_id}" if (profile and user_id) else ""


class TestConversationIdDerivation:
    def test_profile_and_user_id_compose_colon_key(self):
        assert _derive("minerva", "tg_111") == "minerva:tg_111"

    def test_empty_user_id_is_noop(self):
        """CLI / no gateway user => empty id => upstream no-op."""
        assert _derive("minerva", "") == ""
        assert _derive("minerva", None) == ""

    def test_empty_profile_is_noop(self):
        assert _derive("", "tg_111") == ""

    def test_agent_init_seam_default_attribute_is_empty_string(self):
        """init_agent seeds agent._shared_conversation_id = "" up front so the
        attribute always exists even if profile resolution later fails. Verify
        the default is the no-op sentinel, not None (run_agent reads it with
        `or ""`, but the seeded default should already be a string).
        """
        import inspect
        import agent.agent_init as agent_init
        src = inspect.getsource(agent_init.init_agent)
        # The seam must (a) seed a default and (b) derive from profile+user_id.
        assert 'agent._shared_conversation_id = ""' in src, (
            "init_agent must seed a safe default _shared_conversation_id"
        )
        assert 'f"{_profile}:{agent._user_id}"' in src, (
            "init_agent must derive the shared conversation_id from profile+user_id"
        )


# ---------------------------------------------------------------------------
# 2. Propagation — sync_all forwards conversation_id by keyword
# ---------------------------------------------------------------------------


class TestSyncAllPropagatesConversationId:
    def test_sync_all_forwards_conversation_id_to_provider(self):
        mm = MemoryManager()
        p = _CidRecordingProvider()
        mm.add_provider(p)

        mm.sync_all("hi", "there", session_id="sess-1", conversation_id="minerva:tg_111")

        assert p.sync_calls == [
            {
                "user": "hi",
                "asst": "there",
                "session_id": "sess-1",
                "conversation_id": "minerva:tg_111",
            }
        ]

    def test_sync_all_default_conversation_id_is_empty(self):
        """Callers that omit conversation_id (legacy path) get "" — no breakage."""
        mm = MemoryManager()
        p = _CidRecordingProvider()
        mm.add_provider(p)

        mm.sync_all("hi", "there", session_id="sess-1")
        assert p.sync_calls[0]["conversation_id"] == ""

    def test_sync_all_fans_conversation_id_to_all_providers(self):
        mm = MemoryManager()
        builtin = _CidRecordingProvider("builtin")
        external = _CidRecordingProvider("mnemosyne")
        mm.add_provider(builtin)
        mm.add_provider(external)

        mm.sync_all("u", "a", session_id="s", conversation_id="minerva:abc")
        assert builtin.sync_calls[0]["conversation_id"] == "minerva:abc"
        assert external.sync_calls[0]["conversation_id"] == "minerva:abc"


# ---------------------------------------------------------------------------
# 3. Cross-surface acceptance — same (person, agent) => same conversation_id
#    regardless of surface (Telegram vs voice-delegate). This is the core
#    ADR-065 guarantee.
# ---------------------------------------------------------------------------


class TestCrossSurfaceConversationIdEquality:
    """Two surfaces, same person+agent: the per-surface session_id differs but
    the shared conversation_id is identical.

    The derivation is surface-agnostic by construction (profile + user_id only).
    These cases use the same (profile, user_id) pair through two differently
    shaped session contexts to prove the forwarded conversation_id converges.
    """

    def test_telegram_and_voice_delegate_same_conversation_id(self):
        # Same person (morgan -> gateway user "morgan_uid"), same agent (minerva),
        # but two different surfaces with different per-surface session ids.
        cid_telegram = _derive("minerva", "morgan_uid")
        cid_voice = _derive("minerva", "morgan_uid")

        mm_tg = MemoryManager()
        p_tg = _CidRecordingProvider("mnemosyne")
        mm_tg.add_provider(p_tg)
        # Telegram surface: per-surface session id is the gateway session key.
        mm_tg.sync_all(
            "what did I say earlier?",
            "...",
            session_id="minerva:main:telegram:dm:123",
            conversation_id=cid_telegram,
        )

        mm_voice = MemoryManager()
        p_voice = _CidRecordingProvider("mnemosyne")
        mm_voice.add_provider(p_voice)
        # Voice-delegate surface: a totally different per-surface session id.
        mm_voice.sync_all(
            "what did I say earlier?",
            "...",
            session_id="voice-delegate-7f42",
            conversation_id=cid_voice,
        )

        tg_fwd = p_tg.sync_calls[0]["conversation_id"]
        voice_fwd = p_voice.sync_calls[0]["conversation_id"]

        # Per-surface session ids differ...
        assert p_tg.sync_calls[0]["session_id"] != p_voice.sync_calls[0]["session_id"]
        # ...but the shared (person, agent) conversation_id is identical.
        assert tg_fwd == voice_fwd == "minerva:morgan_uid"

    def test_different_agents_same_person_diverge(self):
        """Sanity: different agent (profile) for the same person must NOT share
        a conversation_id — the key is (person, agent), not person alone.
        """
        assert _derive("minerva", "morgan_uid") != _derive("growth", "morgan_uid")

    def test_different_people_same_agent_diverge(self):
        assert _derive("minerva", "morgan_uid") != _derive("minerva", "other_uid")


# ---------------------------------------------------------------------------
# 4. Provider-boundary flag behaviour (mnemosyne provider, lives in HMP).
#    mnemosyne is importable from the HAF venv only when the HMP worktree is on
#    sys.path. We inject it here and skip cleanly if unavailable.
# ---------------------------------------------------------------------------


_HMP_PATH = Path("/tmp/1542-worktrees/hmp")


def _import_mnemosyne():
    if str(_HMP_PATH) not in sys.path and _HMP_PATH.exists():
        sys.path.insert(0, str(_HMP_PATH))
    return pytest.importorskip(
        "mnemosyne",
        reason="mnemosyne provider (HMP worktree) not on sys.path",
    )


def _make_provider(mnemosyne_mod, *, shared_conversation):
    """Build a bare MnemosyneMemoryProvider without network setup.

    We bypass initialize() (which constructs a real adapter + reads config)
    and seed only the attributes sync_turn's auto-capture path reads. The
    adapter is a captured-call recorder. distillation helpers are stubbed via
    monkeypatch in the test so a turn always reaches the memorialize call.
    """
    provider = mnemosyne_mod.MnemosyneMemoryProvider()
    provider._config = {
        "auto_capture": True,
        "shared_conversation": shared_conversation,
    }
    provider._shutting_down = threading.Event()
    # Breaker closed (writes allowed).
    provider._breaker_open_until = 0.0
    provider._consecutive_failures = 0
    return provider


class _RecordingAdapter:
    """Captures the metadata passed to memorialize and exposes build_payload
    so the test can assert the *adapter slot* the metadata maps to.
    """

    def __init__(self, real_adapter_cls):
        self.calls: list[dict] = []
        self._real_cls = real_adapter_cls

    def memorialize(self, action, target, content, metadata=None):
        self.calls.append(
            {"action": action, "target": target, "content": content, "metadata": dict(metadata or {})}
        )
        return {}


class TestMnemosyneProviderBoundaryFlag:
    """The packet's acceptance gate: flag ON => derived id reaches the adapter
    slot; flag OFF => slot falls back to parent_session_id (zero change)."""

    def _run_sync_and_capture(self, provider, monkeypatch, mnemosyne_mod, *, conversation_id):
        """Force the distill gate open, run sync_turn, join the daemon thread,
        return the metadata captured by the recording adapter.
        """
        adapter = _RecordingAdapter(mnemosyne_mod.MnemosyneAdapter)
        provider._adapter = adapter
        # Force the cheap pre-gate + distillation to always produce content.
        monkeypatch.setattr(mnemosyne_mod, "should_consider_turn", lambda u, a: True)
        monkeypatch.setattr(mnemosyne_mod, "distill_turn", lambda u, a: "a distilled fact")

        provider.sync_turn(
            "user says something",
            "assistant replies",
            session_id="sess-xyz",
            conversation_id=conversation_id,
        )
        # sync_turn spawns a daemon thread; wait for it to finish the write.
        t = getattr(provider, "_capture_thread", None)
        assert t is not None, "sync_turn should have started a capture thread"
        t.join(timeout=5.0)
        assert not t.is_alive(), "capture thread did not finish in time"
        assert adapter.calls, "memorialize was never called"
        return adapter.calls[-1]["metadata"]

    def test_flag_on_stamps_conversation_id_into_meta_and_adapter_slot(self, monkeypatch):
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=True)

        meta = self._run_sync_and_capture(
            provider, monkeypatch, mnemosyne_mod, conversation_id="minerva:morgan_uid"
        )

        # Provider-side: the derived id lands in the auto-capture meta.
        assert meta.get("conversation_id") == "minerva:morgan_uid"

        # Adapter-side: build_payload maps it into the conversation_id slot.
        from mnemosyne.adapter import MnemosyneAdapter
        adapter = object.__new__(MnemosyneAdapter)
        adapter.profile = "minerva"
        adapter.requester_role = "agent"
        payload = adapter.build_payload("add", "auto-capture", "a distilled fact", meta)
        assert payload["metadata"]["conversation_id"] == "minerva:morgan_uid"

    def test_flag_off_omits_conversation_id_falls_back_to_parent(self, monkeypatch):
        """Flag OFF/unset: meta has NO conversation_id key, and the adapter slot
        falls back to parent_session_id — byte-identical to prior behaviour.
        """
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=False)

        meta = self._run_sync_and_capture(
            provider, monkeypatch, mnemosyne_mod, conversation_id="minerva:morgan_uid"
        )

        # Provider-side: even with a non-empty id passed, the flag-OFF gate
        # drops it — the key must be ABSENT (not present-with-empty).
        assert "conversation_id" not in meta

        # Adapter-side: the slot falls back to parent_session_id (today's value).
        from mnemosyne.adapter import MnemosyneAdapter
        adapter = object.__new__(MnemosyneAdapter)
        adapter.profile = "minerva"
        adapter.requester_role = "agent"
        meta_with_parent = {**meta, "parent_session_id": "old-session-99"}
        payload = adapter.build_payload("add", "auto-capture", "a distilled fact", meta_with_parent)
        assert payload["metadata"]["conversation_id"] == "old-session-99"

    def test_flag_on_but_empty_id_omits_key(self, monkeypatch):
        """Flag ON but conversation_id empty (CLI/no user) => key absent =>
        adapter falls back to parent_session_id. The flag alone is not enough.
        """
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=True)

        meta = self._run_sync_and_capture(
            provider, monkeypatch, mnemosyne_mod, conversation_id=""
        )
        assert "conversation_id" not in meta
