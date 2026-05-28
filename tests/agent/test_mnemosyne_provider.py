"""Unit tests for the ADR-058 mnemosyne memory-provider plugin.

Mocked-only: clawd and mnemosyne are never hit. The recall transport
(subprocess CLI) is mocked at ``compose_via_cli`` / ``subprocess.run``; the
memorialize transport (POST /admin/memory-items) is mocked with an injected
``httpx.MockTransport`` client. Coverage maps to lens-C §C.1.1–§C.1.6:

  * prefetch query-building + provenance + empty-on-no-hits + non-blocking
    + error-swallowing
  * ContextBundle injection (flatten, no stray fence, placeholder not leaked)
  * on_memory_write payload (admin route, source=hermes, required fields,
    skip remove, control chars, non-blocking error tolerance)
  * requester_role propagation (the moat) — capture, mapping, carried,
    fallback
  * dedupe / idempotency (deterministic key, shared backfill helper,
    created=false == success)
  * manager-level discovery + only-one-external-provider
"""

import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

# The plugin lives in a clean dev location; import it for unit testing the
# internals without copying it into the fork tree.
_PLUGIN_PARENT = str(Path.home() / "dev" / "hermes-mnemosyne-provider")
if _PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _PLUGIN_PARENT)

import mnemosyne as _mnemosyne_pkg  # noqa: E402
from mnemosyne import MnemosyneMemoryProvider, register  # noqa: E402
from mnemosyne.adapter import (  # noqa: E402
    DEFAULT_ROLE,
    PROFILE_TO_ROLE,
    VALID_ROLES,
    MnemosyneAdapter,
    validate_profile_mapping,
)
from mnemosyne.clawd_client import (  # noqa: E402
    ClawdWriteError,
    RecallTransportError,
    compose_via_cli,
    post_memory_item,
)
from mnemosyne.adapter import _CONTROL_TRANS  # noqa: E402
from mnemosyne.dedupe import DEDUPE_KEY_MAX_LEN, compute_dedupe_key  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bundle(synthesis="<placeholder — Dialectic synthesis lands in Phase 2>", excerpts=None):
    return {
        "synthesis": synthesis,
        "excerpts": excerpts or [],
        "provenance": [],
        "token_count": 0,
        "provider_used": "clawd_native",
        "debug": None,
    }


def _excerpt(content, mid="m1", cite=None, score=0.9):
    return {
        "content": content,
        "memory_item_id": mid,
        "cite": cite or f"memory_item:{mid}",
        "score": score,
        "role_relevance": None,
    }


def _mock_http_client(handler):
    """Build an httpx.Client with a MockTransport from a request->Response fn."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_ingest_response(request, *, created=True, mid="11111111-1111-1111-1111-111111111111"):
    return httpx.Response(
        200,
        json={"memory_item_id": mid, "source": "hermes", "created": created, "backend_status": "live"},
    )


# ===========================================================================
# §C.1.1 — prefetch query-building → compose_context (via CLI transport)
# ===========================================================================


class TestPrefetchQueryBuilding:
    def test_recall_builds_query_from_user_message(self, monkeypatch):
        captured = {}

        def fake_compose(**kwargs):
            captured.update(kwargs)
            return _bundle(excerpts=[_excerpt("a fact")])

        monkeypatch.setattr("mnemosyne.adapter.compose_via_cli", fake_compose)
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.recall("what did we decide about retries?")
        assert captured["query"] == "what did we decide about retries?"

    def test_recall_passes_requester_agent_id(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.recall("q")
        assert captured["requester_agent_id"] == "hermes_engineer"

    def test_recall_returns_empty_on_no_hits(self, monkeypatch):
        # Empty excerpts + Phase-0 placeholder synthesis ⇒ "".
        monkeypatch.setattr("mnemosyne.adapter.compose_via_cli", lambda **kw: _bundle(excerpts=[]))
        adapter = MnemosyneAdapter(profile="engineer")
        assert adapter.recall("q") == ""

    def test_recall_query_is_capped(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.recall("x" * 5000)
        assert len(captured["query"]) <= 2048

    def test_recall_budget_clamped(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.recall("q")
        assert 256 <= captured["token_budget"] <= 4000

    def test_prefetch_is_nonblocking_and_uses_background_thread(self, monkeypatch):
        # queue_prefetch spawns the thread; prefetch joins-with-timeout and
        # returns the cached result. The recall call must run off the
        # critical path (in queue_prefetch), not inside prefetch().
        slow_started = {"ran": False}

        def fake_compose(**kwargs):
            slow_started["ran"] = True
            return _bundle(excerpts=[_excerpt("bg fact")])

        monkeypatch.setattr("mnemosyne.adapter.compose_via_cli", fake_compose)
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        # Before queue_prefetch, prefetch returns "" (nothing cached).
        assert p.prefetch("q") == ""
        p.queue_prefetch("q")
        # join the thread by calling prefetch (it joins internally)
        result = p.prefetch("q")
        assert slow_started["ran"] is True
        assert "bg fact" in result

    def test_prefetch_swallows_backend_error(self, monkeypatch):
        def boom(**kwargs):
            raise RecallTransportError("clawd down")

        monkeypatch.setattr("mnemosyne.adapter.compose_via_cli", boom)
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        p.queue_prefetch("q")
        # Must NOT raise; returns "".
        assert p.prefetch("q") == ""


# ===========================================================================
# §C.1.2 — ContextBundle injection
# ===========================================================================


class TestContextBundleInjection:
    def test_bundle_flattened_to_text(self, monkeypatch):
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: _bundle(excerpts=[_excerpt("fact one", "a"), _excerpt("fact two", "b")]),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        out = adapter.recall("q")
        assert "fact one" in out
        assert "fact two" in out
        assert "(cite: memory_item:a)" in out
        assert "(cite: memory_item:b)" in out

    def test_adapter_emits_no_stray_memory_context_fence(self, monkeypatch):
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: _bundle(excerpts=[_excerpt("fact")]),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        out = adapter.recall("q")
        assert "<memory-context>" not in out
        assert "</memory-context>" not in out

    def test_synthesis_placeholder_not_leaked_as_fact(self, monkeypatch):
        # Phase-0 placeholder synthesis must NOT be injected as a fact; only
        # excerpts are rendered.
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: _bundle(
                synthesis="<placeholder — Dialectic synthesis lands in Phase 2>",
                excerpts=[_excerpt("real excerpt")],
            ),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        out = adapter.recall("q")
        assert "placeholder" not in out
        assert "real excerpt" in out

    def test_real_synthesis_is_rendered(self, monkeypatch):
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: _bundle(synthesis="The team uses 3 retries.", excerpts=[_excerpt("x")]),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        out = adapter.recall("q")
        assert "The team uses 3 retries." in out
        assert "synthesized for engineer" in out


# ===========================================================================
# §C.1.3 — on_memory_write payload correctness (POST /admin/memory-items)
# ===========================================================================


class TestMemorializePayload:
    def test_posts_to_admin_memory_items(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return _ok_ingest_response(request)

        client = _mock_http_client(handler)
        adapter = MnemosyneAdapter(profile="engineer", clawd_base_url="http://clawd.test")
        monkeypatch.setattr(
            "mnemosyne.adapter.post_memory_item",
            lambda payload, **kw: post_memory_item(payload, client=client, **{k: v for k, v in kw.items() if k != "client"}),
        )
        adapter.memorialize("add", "memory", "learned a thing")
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/admin/memory-items")

    def test_sets_source_hermes(self, monkeypatch):
        adapter = MnemosyneAdapter(profile="engineer")
        payload = adapter.build_payload("add", "memory", "content here")
        assert payload["source"] == "hermes"

    def test_required_fields_present(self):
        adapter = MnemosyneAdapter(profile="engineer")
        payload = adapter.build_payload("add", "memory", "the agent learned X about Y")
        assert payload["title"]
        assert payload["canonical_summary"] == "the agent learned X about Y"
        assert payload["importance"] == {}  # dict on this surface, not float

    def test_skips_remove_action(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "mnemosyne.adapter.post_memory_item",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _ok_ingest_response(None),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.memorialize("remove", "memory", "content")
        assert called["n"] == 0

    def test_control_chars_stripped_before_post(self):
        adapter = MnemosyneAdapter(profile="engineer")
        payload = adapter.build_payload("add", "memory", "good\x00bad\x07text")
        assert "\x00" not in payload["canonical_summary"]
        assert "\x07" not in payload["canonical_summary"]
        assert "goodbadtext" == payload["canonical_summary"]

    def test_metadata_carries_profile_and_role(self):
        adapter = MnemosyneAdapter(profile="engineer")
        payload = adapter.build_payload("add", "memory", "x", metadata={"session_id": "sess-1"})
        assert payload["metadata"]["hermes_profile"] == "engineer"
        assert payload["metadata"]["requester_role"] == "engineer"
        assert payload["metadata"]["session_id"] == "sess-1"

    def test_on_memory_write_nonblocking_on_5xx(self, monkeypatch):
        def handler(request):
            return httpx.Response(500, json={"error_code": "memory_ingest.upsert_failed"})

        client = _mock_http_client(handler)
        monkeypatch.setattr(
            "mnemosyne.adapter.post_memory_item",
            lambda payload, **kw: post_memory_item(payload, client=client),
        )
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        # Must NOT raise into the turn loop.
        p.on_memory_write("add", "memory", "content")

    def test_on_memory_write_nonblocking_on_timeout(self, monkeypatch):
        def boom(payload, **kw):
            raise ClawdWriteError("timed out", status_code=None, permanent=False)

        monkeypatch.setattr("mnemosyne.adapter.post_memory_item", boom)
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        p.on_memory_write("add", "memory", "content")  # no raise

    def test_422_validation_is_permanent_not_retried(self, monkeypatch):
        def handler(request):
            return httpx.Response(422, json={"detail": "bad source"})

        client = _mock_http_client(handler)
        with pytest.raises(ClawdWriteError) as ei:
            post_memory_item({"source": "hermes", "title": "t", "canonical_summary": "c"}, client=client)
        assert ei.value.permanent is True
        assert ei.value.status_code == 422


# ===========================================================================
# §C.1.4 — requester_role propagation (the scoring moat)
# ===========================================================================


class TestRequesterRoleMoat:
    def test_initialize_captures_agent_identity(self):
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="research", agent_workspace="hermes")
        assert p._adapter.profile == "research"

    def test_profile_maps_to_requester_role(self):
        assert MnemosyneAdapter(profile="engineer").requester_role == "engineer"
        assert MnemosyneAdapter(profile="librarian").requester_role == "librarian"
        assert MnemosyneAdapter(profile="research").requester_role in VALID_ROLES

    def test_compose_context_carries_requester_role(self, monkeypatch):
        # THE most important test: the moat must survive the adapter.
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        adapter = MnemosyneAdapter(profile="research")
        adapter.recall("q")
        assert captured["requester_role"] == PROFILE_TO_ROLE["research"]
        assert captured["requester_role"] in VALID_ROLES

    def test_missing_agent_identity_falls_back(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        p = MnemosyneMemoryProvider()
        p.initialize("s1")  # no agent_identity (CLI path)
        assert p._adapter.requester_role == DEFAULT_ROLE
        assert p._adapter.requester_role in VALID_ROLES

    def test_unmapped_profile_falls_back_to_valid_role(self):
        adapter = MnemosyneAdapter(profile="totally-unknown-profile")
        assert adapter.requester_role == DEFAULT_ROLE
        assert adapter.requester_role in VALID_ROLES

    def test_per_turn_role_override(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: captured.update(kw) or _bundle(),
        )
        adapter = MnemosyneAdapter(profile="engineer")
        adapter.recall("q", requester_role="reviewer")
        assert captured["requester_role"] == "reviewer"

    def test_all_profile_roles_valid(self):
        # Load-time validation invariant: every mapped role is a valid enum.
        for profile, role in PROFILE_TO_ROLE.items():
            assert role in VALID_ROLES, f"{profile}->{role} invalid"

    def test_validate_profile_mapping_passes(self):
        validate_profile_mapping()  # no raise


# ===========================================================================
# §C.1.5 — dedupe / idempotency vs CLAWD-621 backfill
# ===========================================================================


class TestDedupeIdempotency:
    def test_deterministic_dedupe_key(self):
        a = compute_dedupe_key("engineer", "the team uses 3 retries")
        b = compute_dedupe_key("engineer", "the team uses 3 retries")
        assert a == b

    def test_dedupe_key_format_and_length(self):
        key = compute_dedupe_key("engineer", "some content")
        assert key.startswith("hermes_engineer:")
        assert len(key) <= DEDUPE_KEY_MAX_LEN

    def test_dedupe_key_differs_by_content(self):
        assert compute_dedupe_key("engineer", "a") != compute_dedupe_key("engineer", "b")

    def test_dedupe_key_differs_by_profile(self):
        assert compute_dedupe_key("engineer", "x") != compute_dedupe_key("research", "x")

    def test_dedupe_key_namespace_matches_backfill(self):
        # Both the plugin write path and the §C.4 backfill MUST call the same
        # helper so a plugin-written item is recognised and merged. We assert
        # the adapter's build_payload uses the SAME compute_dedupe_key output.
        adapter = MnemosyneAdapter(profile="engineer")
        payload = adapter.build_payload("add", "memory", "shared content")
        backfill_key = compute_dedupe_key("engineer", "shared content")
        assert payload["dedupe_key"] == backfill_key

    def test_created_false_is_not_an_error(self, monkeypatch):
        def handler(request):
            return _ok_ingest_response(request, created=False)

        client = _mock_http_client(handler)
        resp = post_memory_item(
            {"source": "hermes", "title": "t", "canonical_summary": "c"}, client=client
        )
        assert resp["created"] is False  # merge — handled as success, no raise

    def test_provider_treats_merge_as_success(self, monkeypatch):
        def handler(request):
            return _ok_ingest_response(request, created=False)

        client = _mock_http_client(handler)
        monkeypatch.setattr(
            "mnemosyne.adapter.post_memory_item",
            lambda payload, **kw: post_memory_item(payload, client=client),
        )
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        p.on_memory_write("add", "memory", "content")
        # breaker should not have tripped (merge is success)
        assert p._consecutive_failures == 0


# ===========================================================================
# §C.1.6 — manager-level discovery + only-one-external-provider
# ===========================================================================


class TestManagerIntegration:
    def _install_plugin(self, tmp_path):
        """Copy the real plugin tree into a temp $HERMES_HOME/plugins/."""
        import shutil

        src = Path.home() / "dev" / "hermes-mnemosyne-provider" / "mnemosyne"
        dst = tmp_path / "plugins" / "mnemosyne"
        shutil.copytree(src, dst)
        return dst

    def test_plugin_discovered_from_hermes_home(self, tmp_path, monkeypatch):
        from plugins.memory import discover_memory_providers, load_memory_provider

        self._install_plugin(tmp_path)
        monkeypatch.setattr(
            "plugins.memory._get_user_plugins_dir", lambda: tmp_path / "plugins"
        )
        names = [n for n, _, _ in discover_memory_providers()]
        assert "mnemosyne" in names
        provider = load_memory_provider("mnemosyne")
        assert provider is not None
        assert provider.name == "mnemosyne"

    def test_loaded_plugin_is_available(self, tmp_path, monkeypatch):
        from plugins.memory import load_memory_provider

        self._install_plugin(tmp_path)
        monkeypatch.setattr(
            "plugins.memory._get_user_plugins_dir", lambda: tmp_path / "plugins"
        )
        provider = load_memory_provider("mnemosyne")
        assert provider.is_available() is True
        assert provider.get_tool_schemas() == []  # transparent memory for v1

    def test_only_one_external_provider(self):
        from agent.memory_manager import MemoryManager

        mgr = MemoryManager()
        p1 = MnemosyneMemoryProvider()
        mgr.add_provider(p1)
        # a second external provider is rejected (silently, with a warning) —
        # the registered set still holds only the first external provider.
        p2 = MnemosyneMemoryProvider()
        mgr.add_provider(p2)
        externals = [p for p in mgr.providers if p.name != "builtin"]
        assert externals == [p1]

    def test_register_validates_mapping(self):
        class _Collector:
            provider = None

            def register_memory_provider(self, p):
                self.provider = p

        c = _Collector()
        register(c)  # validate_profile_mapping runs; no raise
        assert isinstance(c.provider, MnemosyneMemoryProvider)


# ===========================================================================
# Recall transport (subprocess CLI) — mocked subprocess.run
# ===========================================================================


class TestRecallTransport:
    def test_compose_via_cli_parses_json_stdout(self, monkeypatch):
        import json as _json
        import subprocess as _sp

        class _Proc:
            returncode = 0
            stdout = _json.dumps(_bundle(excerpts=[_excerpt("x")]))
            stderr = ""

        monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())
        out = compose_via_cli(
            query="q", requester_role="engineer", requester_agent_id="hermes_engineer",
            task_kind=None, token_budget=4000, cli_command=["python", "-m", "x"],
        )
        assert out["excerpts"][0]["content"] == "x"

    def test_compose_via_cli_nonzero_exit_raises(self, monkeypatch):
        import subprocess as _sp

        class _Proc:
            returncode = 3
            stdout = ""
            stderr = "librarian compose unavailable: clawd down"

        monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())
        with pytest.raises(RecallTransportError):
            compose_via_cli(
                query="q", requester_role="engineer", requester_agent_id="x",
                task_kind=None, token_budget=4000, cli_command=["python"],
            )

    def test_compose_via_cli_timeout_raises(self, monkeypatch):
        import subprocess as _sp

        def _boom(*a, **k):
            raise _sp.TimeoutExpired(cmd="x", timeout=10)

        monkeypatch.setattr(_sp, "run", _boom)
        with pytest.raises(RecallTransportError):
            compose_via_cli(
                query="q", requester_role="engineer", requester_agent_id="x",
                task_kind=None, token_budget=4000, cli_command=["python"],
            )

    def test_compose_via_cli_builds_argv(self, monkeypatch):
        import subprocess as _sp

        captured = {}

        class _Proc:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def _run(argv, **k):
            captured["argv"] = argv
            return _Proc()

        monkeypatch.setattr(_sp, "run", _run)
        compose_via_cli(
            query="hello", requester_role="engineer", requester_agent_id="hermes_engineer",
            task_kind="code-review", token_budget=2000, cli_command=["mn"],
        )
        argv = captured["argv"]
        assert "librarian" in argv and "compose" in argv
        assert "--role" in argv and "engineer" in argv
        assert "--task-kind" in argv and "code-review" in argv
        assert "--query" in argv and "hello" in argv


# ===========================================================================
# Stale-recall race — the generation-counter fix (review SHOULD-FIX)
# ===========================================================================


class TestPrefetchStaleRecallRace:
    def test_stale_generation_does_not_clobber_fresh_result(self, monkeypatch):
        """Two overlapping prefetch threads: a SLOW turn-N recall is still
        running when turn-N+1 queues a FAST recall. Without the generation
        counter, the slow OLD thread would last-write-win and clobber the fresh
        result. With the fix, the slow thread discards its stale output and the
        FRESH result survives."""
        # Gate the SLOW (turn-N) thread so it cannot finish until we release it.
        slow_release = threading.Event()
        slow_entered = threading.Event()

        def fake_recall(query, *, session_id="", **kw):
            if query == "STALE":
                slow_entered.set()
                # Block until the fresh thread has run and we explicitly release.
                slow_release.wait(timeout=5.0)
                return "stale-result"
            return "fresh-result"

        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        monkeypatch.setattr(p._adapter, "recall", fake_recall)

        # Turn N: queue the slow recall; wait until it is in-flight (blocked).
        p.queue_prefetch("STALE")
        assert slow_entered.wait(timeout=5.0)
        slow_thread = p._prefetch_thread

        # Turn N+1: queue the fast recall — bumps the generation. Join it so the
        # FRESH result is written and cached.
        p.queue_prefetch("FRESH")
        fresh_thread = p._prefetch_thread
        fresh_thread.join(timeout=5.0)
        with p._prefetch_lock:
            assert p._prefetch_result == "fresh-result"

        # Now release the SLOW older-generation thread. It must DISCARD its
        # result rather than clobber the fresh one.
        slow_release.set()
        slow_thread.join(timeout=5.0)
        with p._prefetch_lock:
            assert p._prefetch_result == "fresh-result", "stale thread clobbered fresh result"


# ===========================================================================
# Turn-1 / prior-turn keying semantics (by-design — pinned)
# ===========================================================================


class TestPriorTurnKeying:
    def test_turn_one_recall_is_empty(self, monkeypatch):
        """On turn 1 nothing has been queued yet, so prefetch() returns "" —
        recall is warmed at the END of a turn and consumed at the START of the
        NEXT one (the mem0 queue_prefetch/prefetch split)."""
        monkeypatch.setattr(
            "mnemosyne.adapter.compose_via_cli",
            lambda **kw: _bundle(excerpts=[_excerpt("a fact")]),
        )
        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        # No queue_prefetch has run → turn-1 recall is empty.
        assert p.prefetch("first user message") == ""

    def test_recall_is_keyed_on_previous_message(self, monkeypatch):
        """The query that warms the cache is the PREVIOUS turn's message: the
        result consumed at turn N+1 reflects what was queued at the end of turn
        N, not turn N+1's own (not-yet-seen) message."""
        seen_queries = []

        def fake_recall(query, *, session_id="", **kw):
            seen_queries.append(query)
            return f"recall-for:{query}"

        p = MnemosyneMemoryProvider()
        p.initialize("s1", agent_identity="engineer")
        monkeypatch.setattr(p._adapter, "recall", fake_recall)

        # End of turn N: warm the cache keyed on turn-N's message.
        p.queue_prefetch("message-from-turn-N")
        # Start of turn N+1: consume the cached result. The arg passed to
        # prefetch() is ignored for the cached value — the cache is keyed on the
        # previously-queued (prior-turn) message.
        result = p.prefetch("message-from-turn-N+1")
        assert result == "recall-for:message-from-turn-N"
        assert seen_queries == ["message-from-turn-N"]


# ===========================================================================
# Circuit breaker cycle — trip → cooldown → reset
# ===========================================================================


class TestBreakerCycle:
    def test_trip_cooldown_reset(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(_mnemosyne_pkg.time, "monotonic", lambda: clock["t"])

        p = MnemosyneMemoryProvider()
        # Below threshold: breaker stays closed.
        for _ in range(_mnemosyne_pkg._BREAKER_THRESHOLD - 1):
            p._record_failure()
        assert p._is_breaker_open() is False

        # The threshold-th failure TRIPS the breaker (opens it, arms cooldown).
        p._record_failure()
        assert p._consecutive_failures == _mnemosyne_pkg._BREAKER_THRESHOLD
        assert p._is_breaker_open() is True

        # During COOLDOWN the breaker stays open.
        clock["t"] += _mnemosyne_pkg._BREAKER_COOLDOWN_SECS - 1
        assert p._is_breaker_open() is True

        # After the cooldown elapses it RESETS (closes + zeroes the counter).
        clock["t"] += 2  # now past _breaker_open_until
        assert p._is_breaker_open() is False
        assert p._consecutive_failures == 0

    def test_success_resets_consecutive_failures(self):
        p = MnemosyneMemoryProvider()
        p._record_failure()
        p._record_failure()
        assert p._consecutive_failures == 2
        p._record_success()
        assert p._consecutive_failures == 0


# ===========================================================================
# Control-char coupling — pin the strip set to clawd's CONTROL_CHAR_PATTERN
# ===========================================================================


class TestControlCharCoupling:
    # clawd's contract validator (services/memory_ingest/contract.py:
    # CONTROL_CHAR_PATTERN). The plugin's strip set MUST match this exactly so
    # a write the plugin emits is never 422'd by clawd — and so the coupling
    # cannot silently drift.
    _CLAWD_CONTROL_CHAR_PATTERN = r"[\x00-\x08\x0b\x0c\x0e-\x1f]"

    def test_plugin_strip_set_matches_clawd_pattern(self):
        import re

        clawd_re = re.compile(self._CLAWD_CONTROL_CHAR_PATTERN)
        # Every codepoint 0x00..0x1f: the plugin strips it IFF clawd rejects it.
        plugin_stripped = set(_CONTROL_TRANS.keys())
        for cp in range(0x00, 0x20):
            clawd_rejects = bool(clawd_re.search(chr(cp)))
            assert (cp in plugin_stripped) == clawd_rejects, (
                f"coupling drift at codepoint {cp:#04x}: "
                f"plugin_strips={cp in plugin_stripped} clawd_rejects={clawd_rejects}"
            )
        # Sanity: \n \r \t are preserved by BOTH (newline/CR/tab are allowed).
        for keep in ("\n", "\r", "\t"):
            assert ord(keep) not in plugin_stripped
            assert clawd_re.search(keep) is None
