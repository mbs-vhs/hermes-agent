"""Behavioural tests for the on_memory_write conversation_id wiring (CLAWD-1565).

This is the *active MEMORIALIZE* counterpart to the auto-capture path covered by
``tests/agent/test_shared_conversation_id.py``. When the agent (or a tool) writes
to memory explicitly, the same shared (person, agent) conversation_id must be
threaded through ``build_memory_write_metadata`` → ``MemoryManager.on_memory_write``
→ the external provider → ``adapter.memorialize`` — under the SAME flag gating as
the auto-capture path.

Wiring under test:
  * HAF ``agent.background_review.build_memory_write_metadata`` now stamps
    ``conversation_id = getattr(agent, "_shared_conversation_id", "") or ""`` into
    the metadata dict; the trailing ``{k: v for ... if v not in {None, ""}}`` filter
    drops empties — so CLI / stranger turns (empty id) OMIT the key entirely.
  * HAF ``MemoryManager.on_memory_write`` fans the (already-built) metadata —
    including any ``conversation_id`` — to metadata-accepting providers, while
    legacy 3-arg providers keep working.
  * HMP ``mnemosyne.MnemosyneMemoryProvider.on_memory_write`` strips
    ``conversation_id`` from the metadata UNLESS ``shared_conversation`` is truthy
    AND the id is non-empty, before calling ``adapter.memorialize``. Stripped /
    absent ⇒ ``adapter.build_payload`` falls back to ``parent_session_id``
    (byte-identical to prior behaviour).

The flag-OFF strip is positively asserted via ``"conversation_id" not in meta``
(key ABSENT, not present-with-empty). Revert-validation note inline.
"""

import sys
import threading
import types
from pathlib import Path

import pytest

from agent.background_review import build_memory_write_metadata
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


# ---------------------------------------------------------------------------
# Lightweight fake agent for build_memory_write_metadata. The production
# function reads a handful of attributes off the agent object; we give it the
# minimum surface and let getattr defaults cover the rest.
# ---------------------------------------------------------------------------


def _make_agent(*, shared_conversation_id, session_id="sess-1", parent="parent-9",
                platform="telegram"):
    agent = types.SimpleNamespace()
    agent.session_id = session_id
    agent._parent_session_id = parent
    agent.platform = platform
    if shared_conversation_id is not None:
        agent._shared_conversation_id = shared_conversation_id
    # write-origin / context attributes are read with getattr-defaults; leave unset.
    return agent


# ---------------------------------------------------------------------------
# 1. build_memory_write_metadata: include vs omit conversation_id
# ---------------------------------------------------------------------------


class TestBuildMemoryWriteMetadataConversationId:
    def test_includes_conversation_id_when_set(self):
        agent = _make_agent(shared_conversation_id="minerva:morgan")
        meta = build_memory_write_metadata(agent)
        assert meta["conversation_id"] == "minerva:morgan"
        # sibling provenance still present
        assert meta["session_id"] == "sess-1"
        assert meta["parent_session_id"] == "parent-9"

    def test_omits_key_when_empty_string(self):
        """CLI / stranger: empty shared id => the trailing filter drops it =>
        the KEY IS ABSENT (not present-with-empty). Revert-validation: if the
        filter or the `or ""` were removed and an empty value leaked through,
        this assertion would catch it (key would be present)."""
        agent = _make_agent(shared_conversation_id="")
        meta = build_memory_write_metadata(agent)
        assert "conversation_id" not in meta

    def test_omits_key_when_attribute_missing(self):
        """No _shared_conversation_id attribute at all (defensive getattr path)
        => `getattr(..., "") or ""` => "" => filtered out => key absent."""
        agent = _make_agent(shared_conversation_id=None)  # attribute never set
        assert not hasattr(agent, "_shared_conversation_id")
        meta = build_memory_write_metadata(agent)
        assert "conversation_id" not in meta

    def test_omits_key_when_none(self):
        """Attribute present but None => `None or ""` => "" => key absent."""
        agent = _make_agent(shared_conversation_id=None)
        agent._shared_conversation_id = None
        meta = build_memory_write_metadata(agent)
        assert "conversation_id" not in meta


# ---------------------------------------------------------------------------
# 2. MemoryManager.on_memory_write fans the metadata (incl. conversation_id)
# ---------------------------------------------------------------------------


class _MetadataRecordingProvider(MemoryProvider):
    """External provider that opts into metadata and records every write."""

    def __init__(self, name="ext"):
        self._name = name
        self.writes: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:  # pragma: no cover - unused
        return True

    def initialize(self, session_id, **kwargs):  # pragma: no cover - unused
        pass

    def get_tool_schemas(self):
        return []

    def on_memory_write(self, action, target, content, metadata=None):
        self.writes.append(
            {"action": action, "target": target, "content": content,
             "metadata": dict(metadata or {})}
        )


class _LegacyProvider(MemoryProvider):
    """External provider on the OLD 3-arg signature (no metadata param)."""

    def __init__(self, name="legacy"):
        self._name = name
        self.writes: list[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:  # pragma: no cover - unused
        return True

    def initialize(self, session_id, **kwargs):  # pragma: no cover - unused
        pass

    def get_tool_schemas(self):
        return []

    def on_memory_write(self, action, target, content):
        self.writes.append((action, target, content))


class TestOnMemoryWriteFansConversationId:
    def test_metadata_provider_receives_conversation_id(self):
        """End-to-end HAF side: build the metadata off an agent with a shared
        id, fan it through the manager, and confirm the provider's hook sees
        conversation_id in the metadata."""
        agent = _make_agent(shared_conversation_id="minerva:morgan")
        meta = build_memory_write_metadata(agent)

        mgr = MemoryManager()
        p = _MetadataRecordingProvider("mnemosyne")
        mgr.add_provider(p)

        mgr.on_memory_write("add", "memory", "a durable fact", metadata=meta)

        assert len(p.writes) == 1
        assert p.writes[0]["metadata"]["conversation_id"] == "minerva:morgan"
        assert p.writes[0]["action"] == "add"

    def test_metadata_provider_omits_conversation_id_for_cli(self):
        """CLI turn (empty shared id): build omits the key, and the provider's
        metadata therefore has no conversation_id key."""
        agent = _make_agent(shared_conversation_id="")
        meta = build_memory_write_metadata(agent)

        mgr = MemoryManager()
        p = _MetadataRecordingProvider("mnemosyne")
        mgr.add_provider(p)

        mgr.on_memory_write("add", "memory", "a durable fact", metadata=meta)

        assert "conversation_id" not in p.writes[0]["metadata"]

    def test_legacy_provider_still_works_without_error(self):
        """A no-metadata (3-arg) provider must keep working even when the
        manager is handed metadata containing conversation_id."""
        agent = _make_agent(shared_conversation_id="minerva:morgan")
        meta = build_memory_write_metadata(agent)

        mgr = MemoryManager()
        legacy = _LegacyProvider("legacy_ext")
        mgr.add_provider(legacy)

        # Must not raise.
        mgr.on_memory_write("add", "memory", "a durable fact", metadata=meta)
        assert legacy.writes == [("add", "memory", "a durable fact")]

    def test_builtin_is_not_notified(self):
        """The builtin provider is the source of the write — it must be skipped
        so conversation_id never round-trips back into the builtin store."""
        agent = _make_agent(shared_conversation_id="minerva:morgan")
        meta = build_memory_write_metadata(agent)

        mgr = MemoryManager()
        builtin = _MetadataRecordingProvider("builtin")
        ext = _MetadataRecordingProvider("mnemosyne")
        mgr.add_provider(builtin)
        mgr.add_provider(ext)

        mgr.on_memory_write("add", "memory", "fact", metadata=meta)
        assert builtin.writes == []
        assert ext.writes[0]["metadata"]["conversation_id"] == "minerva:morgan"


# ---------------------------------------------------------------------------
# 4. Convergence sanity — an explicit-write turn and an auto-capture (sync_turn)
#    turn for the SAME (person, agent) carry the SAME conversation_id.
#    Pure HAF-side: build_memory_write_metadata (explicit write) and the value
#    sync_all forwards to sync_turn both source the same agent attribute.
# ---------------------------------------------------------------------------


class _CidSyncRecorder(MemoryProvider):
    def __init__(self, name="rec"):
        self._name = name
        self.sync_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:  # pragma: no cover
        return True

    def initialize(self, session_id, **kwargs):  # pragma: no cover
        pass

    def get_tool_schemas(self):
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", conversation_id=""):
        self.sync_calls.append({"conversation_id": conversation_id})


class TestExplicitAndAutoCaptureConverge:
    def test_explicit_write_and_sync_turn_share_conversation_id(self):
        agent = _make_agent(shared_conversation_id="minerva:morgan")

        # Explicit-write path: metadata carries the id.
        meta = build_memory_write_metadata(agent)
        explicit_cid = meta["conversation_id"]

        # Auto-capture path: run_agent forwards the same agent attribute as the
        # conversation_id kwarg to sync_all -> sync_turn.
        mgr = MemoryManager()
        rec = _CidSyncRecorder("mnemosyne")
        mgr.add_provider(rec)
        mgr.sync_all("u", "a", session_id="sess-1",
                     conversation_id=getattr(agent, "_shared_conversation_id", "") or "")
        sync_cid = rec.sync_calls[0]["conversation_id"]

        assert explicit_cid == sync_cid == "minerva:morgan"


# ---------------------------------------------------------------------------
# 3. HMP provider-boundary (mnemosyne). Mirrors P1's TestMnemosyneProviderBoundaryFlag
#    but for the ACTIVE on_memory_write strip (CLAWD-1565), pointed at the 1565b
#    HMP worktree. importorskip-guarded.
# ---------------------------------------------------------------------------


_HMP_PATH = Path("/tmp/1565b-worktrees/hmp")


def _import_mnemosyne():
    if str(_HMP_PATH) not in sys.path and _HMP_PATH.exists():
        sys.path.insert(0, str(_HMP_PATH))
    return pytest.importorskip(
        "mnemosyne",
        reason="mnemosyne provider (HMP 1565b worktree) not on sys.path",
    )


def _make_provider(mnemosyne_mod, *, shared_conversation):
    """Build a bare provider with the breaker closed and an adapter that records
    the metadata passed to memorialize. on_memory_write is synchronous (no
    daemon thread), so no join is needed."""
    provider = mnemosyne_mod.MnemosyneMemoryProvider()
    provider._config = {
        "auto_capture": True,
        "shared_conversation": shared_conversation,
    }
    provider._shutting_down = threading.Event()
    provider._breaker_open_until = 0.0
    provider._consecutive_failures = 0
    return provider


class _RecordingAdapter:
    """Captures the metadata passed to memorialize."""

    def __init__(self):
        self.calls: list[dict] = []

    def memorialize(self, action, target, content, metadata=None):
        self.calls.append({"action": action, "target": target, "content": content,
                           "metadata": dict(metadata or {})})
        return {"memory_item_id": "mi-1", "created": True}


class TestMnemosyneOnMemoryWriteStrip:
    """Acceptance gate for the active write strip: flag ON + non-empty id =>
    conversation_id reaches the adapter; flag OFF/unset or empty id => the key
    is stripped (absent) and the adapter falls back to parent_session_id."""

    def _capture_meta(self, provider, *, action="add", target="memory",
                      content="durable fact", metadata):
        adapter = _RecordingAdapter()
        provider._adapter = adapter
        provider.on_memory_write(action, target, content, metadata=metadata)
        assert adapter.calls, "memorialize was never called"
        return adapter.calls[-1]["metadata"]

    def test_flag_on_nonempty_id_reaches_adapter(self):
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=True)

        meta = self._capture_meta(
            provider,
            metadata={"conversation_id": "minerva:morgan", "parent_session_id": "ps-1"},
        )
        # Provider-side: conversation_id survives the strip.
        assert meta.get("conversation_id") == "minerva:morgan"

        # Adapter-side: build_payload maps it into the conversation_id slot
        # (NOT the parent_session_id fallback).
        from mnemosyne.adapter import MnemosyneAdapter
        adapter = object.__new__(MnemosyneAdapter)
        adapter.profile = "minerva"
        adapter.requester_role = "agent"
        payload = adapter.build_payload("add", "memory", "durable fact", meta)
        assert payload["metadata"]["conversation_id"] == "minerva:morgan"

    def test_flag_off_strips_key_and_falls_back_to_parent(self):
        """Flag OFF: even with a non-empty conversation_id in the incoming
        metadata, on_memory_write must STRIP the key (absent) so the adapter
        falls back to parent_session_id — byte-identical to prior behaviour.

        Revert-validation: this is the zero-change smoke. If the strip
          `if not (shared_conversation and conversation_id): meta.pop(...)`
        were removed, conversation_id would leak through with the flag OFF and
        `assert "conversation_id" not in meta` would FAIL — and the adapter slot
        would become "minerva:morgan" instead of "ps-1", failing the second
        assertion too."""
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=False)

        meta = self._capture_meta(
            provider,
            metadata={"conversation_id": "minerva:morgan", "parent_session_id": "ps-1"},
        )
        assert "conversation_id" not in meta

        from mnemosyne.adapter import MnemosyneAdapter
        adapter = object.__new__(MnemosyneAdapter)
        adapter.profile = "minerva"
        adapter.requester_role = "agent"
        payload = adapter.build_payload("add", "memory", "durable fact", meta)
        assert payload["metadata"]["conversation_id"] == "ps-1"

    def test_flag_unset_strips_key(self):
        """shared_conversation absent from config entirely (falsy via .get) =>
        key stripped."""
        mnemosyne_mod = _import_mnemosyne()
        provider = mnemosyne_mod.MnemosyneMemoryProvider()
        provider._config = {"auto_capture": True}  # no shared_conversation key
        provider._shutting_down = threading.Event()
        provider._breaker_open_until = 0.0
        provider._consecutive_failures = 0

        meta = self._capture_meta(
            provider,
            metadata={"conversation_id": "minerva:morgan", "parent_session_id": "ps-1"},
        )
        assert "conversation_id" not in meta

    def test_flag_on_but_empty_id_strips_key(self):
        """Flag ON but conversation_id empty (CLI/stranger leaked an empty
        value) => the strip's `and conversation_id` clause drops it. The flag
        alone is not enough."""
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=True)

        meta = self._capture_meta(
            provider,
            metadata={"conversation_id": "", "parent_session_id": "ps-1"},
        )
        assert "conversation_id" not in meta

    def test_flag_on_but_no_id_key_no_error(self):
        """Flag ON and the incoming metadata never had conversation_id (the
        common case after build_memory_write_metadata omitted it) => no key,
        no error, adapter falls back to parent_session_id."""
        mnemosyne_mod = _import_mnemosyne()
        provider = _make_provider(mnemosyne_mod, shared_conversation=True)

        meta = self._capture_meta(
            provider,
            metadata={"parent_session_id": "ps-1"},
        )
        assert "conversation_id" not in meta
