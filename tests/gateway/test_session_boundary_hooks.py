"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_fires_finalize_hook(mock_invoke_hook):
    """Regression test for #14981.

    When ``_session_expiry_watcher`` sweeps a session that has aged past
    its reset policy (idle timeout, scheduled reset), it must fire
    ``on_session_finalize`` so plugin providers get the same final-pass
    extraction opportunity they'd get from /new or CLI shutdown.  Before
    the fix, the expiry path evicted the agent but silently skipped the
    hook.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    # The watcher starts with `await asyncio.sleep(0.2)` and loops while
    # `self._running`.  Patch sleep so the 60s initial delay is instant, and
    # make the expiry hook invocation flip `_running` false so the loop
    # exits cleanly after one pass.
    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    # Look for the finalize call targeting the expired session.
    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0] and c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1].get("session_id") for c in finalize_calls}
    assert "sess-expired" in session_ids, (
        f"on_session_finalize was not fired during idle expiry; "
        f"got session_ids={session_ids} (regression of #14981)"
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_emits_session_end(mock_invoke_hook):
    """Regression test for #28746.

    The gateway-level ``session:end`` event must fire from
    ``_session_expiry_watcher`` so external hook subscribers (not just
    plugin ``on_session_finalize`` handlers) see the close.  Before the
    fix, only ``on_session_finalize`` fired and any ``~/.hermes/hooks/``
    subscriber to ``session:end`` would silently miss every
    idle-expiry-driven close — leaving stale state forever.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=_make_source(),
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    # Flip _running off as soon as the gateway-level session:end emit fires
    # so the watcher loop exits cleanly after one pass.  Returning a
    # plain (non-awaitable) None for the mock is fine because the actual
    # production caller awaits it through the AsyncMock plumbing.
    async def _emit_and_stop(event_name, ctx):
        if event_name == "session:end":
            runner._running = False
        return None

    runner.hooks.emit.side_effect = _emit_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    # Find the session:end emit and assert it carried the expired session_id
    # and the idle_expiry reason.
    session_end_calls = [
        c for c in runner.hooks.emit.call_args_list
        if c[0] and c[0][0] == "session:end"
    ]
    assert session_end_calls, (
        "session:end was not emitted from idle-expiry watcher "
        "(regression of #28746)"
    )
    ctx = session_end_calls[0][0][1]
    assert ctx.get("session_id") == "sess-expired", (
        f"session:end emitted with wrong session_id: {ctx!r}"
    )
    assert ctx.get("session_key") == session_key
    assert ctx.get("reason") == "idle_expiry"
    assert ctx.get("platform") == "telegram"


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_idle_expiry_clears_conversation_scoped_state(mock_invoke_hook):
    """Upstream v2026.7.20's #58403 test, adapted to the fork's runner.

    Expiry finalization used to carry a hand-copied pop-list of per-session
    dicts, and the list drifted every time a dict was added — #58403 was
    "finalization forgot ``_last_resolved_model``", so a resumed session could
    serve a model cached before it went idle. The v2026.7.20 merge replaced the
    pop-list with ``_clear_conversation_scope()`` (the boundary funnel driven by
    ``_CONVERSATION_SCOPED_STATE``), which is what this pins.

    Adapted, not copied: upstream's version builds a runner with no ``hooks``
    and no ``origin``, because upstream's expiry path emits no ``session:end``.
    The fork's does (#28746), and the two behaviours now coexist — so the setup
    has to satisfy both or this test would pass for the wrong reason.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    other_key = "agent:main:telegram:dm:other"

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=_make_source(),
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    runner._session_model_overrides = {session_key: "gpt-5-mini", other_key: "keep-me"}
    runner._pending_model_notes = {session_key: "note", other_key: "keep-me"}
    runner._last_resolved_model = {session_key: "gpt-5", other_key: "keep-me"}

    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    async def _emit_and_stop(event_name, ctx):
        if event_name == "session:end":
            runner._running = False
        return None

    runner.hooks.emit.side_effect = _emit_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    assert session_key not in runner._last_resolved_model, (
        "expiry finalization left _last_resolved_model behind — a resumed "
        "session can serve a stale cached model (#58403). The boundary funnel "
        "_clear_conversation_scope() should have dropped it."
    )
    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._pending_model_notes
    # The funnel is per-session-key: an unrelated session must be untouched.
    assert runner._last_resolved_model.get(other_key) == "keep-me"
    assert runner._session_model_overrides.get(other_key) == "keep-me"
    assert runner._pending_model_notes.get(other_key) == "keep-me"


class _StopAfterEmit(Exception):
    """Sentinel raised from the first call *after* the boundary-emit block."""


def _make_emit_runner(session_entry: SessionEntry):
    """Runner wired just far enough to reach the boundary-emit block.

    The predecessor of these tests re-implemented the emit fragment inline
    and asserted against its own copy, so it passed no matter what
    ``_handle_message_with_agent`` actually did — deleting the production
    emit outright would not have turned it red.  This drives the real
    method instead and stops it, via ``_StopAfterEmit``, at the first call
    that follows the block (``build_session_context``), which keeps the
    agent/tool plumbing out of scope without faking the code under test.
    """
    runner = _make_runner()

    # ``async_session_store`` is a read-only property returning the real
    # AsyncSessionStore facade, so stub the synchronous store it offloads to
    # rather than replacing the facade.
    runner.session_store.get_or_create_session.return_value = session_entry
    runner._recover_telegram_topic_thread_id = MagicMock(return_value=None)
    runner._cache_session_source = MagicMock()
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._record_telegram_topic_binding = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._evict_cached_agent = MagicMock()
    return runner


async def _drive_to_emit(runner):
    """Run ``_handle_message_with_agent`` through the boundary-emit block."""
    from gateway.run import GatewayRunner

    with patch(
        "gateway.run.build_session_context", side_effect=_StopAfterEmit
    ):
        with pytest.raises(_StopAfterEmit):
            await GatewayRunner._handle_message_with_agent(
                runner,
                _make_event("hello"),
                _make_source(),
                _quick_key="qk",
                run_generation=1,
            )


def _auto_reset_entry(session_key: str, **overrides) -> SessionEntry:
    # created_at and updated_at come from ONE timestamp because production
    # does (``SessionStore._get_or_create_session_impl`` passes the same
    # ``now`` to both).  Two separate ``datetime.now()`` calls would make
    # them differ by microseconds and silently switch off the
    # ``created_at == updated_at`` half of the is-new-session test.
    now = datetime.now()
    kwargs = dict(
        session_key=session_key,
        session_id="sess-new",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        was_auto_reset=True,
        auto_reset_reason="idle",
        prev_session_id="sess-old-prior",
    )
    kwargs.update(overrides)
    return SessionEntry(**kwargs)


@pytest.mark.asyncio
async def test_auto_reset_emits_session_end_for_prior_session():
    """Regression test for #28746 (auto-reset path), rekeyed by CLAWD-3534.

    When ``SessionStore.get_or_create_session`` rolls a stale session over
    to a fresh ``session_id`` (idle/daily/suspended auto-reset, NOT an
    explicit /new), the new ``SessionEntry`` carries ``prev_session_id``.
    The emit pass in ``_handle_message_with_agent`` must fire
    ``session:end`` for that prior id before ``session:start`` for the new
    one.  Before CLAWD-3534 this was keyed off the fork-local transient
    ``auto_reset_prior_session_id``; it is now keyed off upstream's
    persisted field.
    """
    source = _make_source()
    session_key = build_session_key(source)
    fresh_entry = _auto_reset_entry(session_key)
    runner = _make_emit_runner(fresh_entry)

    await _drive_to_emit(runner)

    event_sequence = [c[0][0] for c in runner.hooks.emit.call_args_list]
    assert event_sequence == ["session:end", "session:start"], (
        f"Expected session:end before session:start; got {event_sequence}"
    )
    end_ctx = runner.hooks.emit.call_args_list[0][0][1]
    assert end_ctx["session_id"] == "sess-old-prior"
    assert end_ctx["reason"] == "auto_reset"
    assert end_ctx["session_key"] == session_key
    assert end_ctx["platform"] == "telegram"


@pytest.mark.asyncio
async def test_auto_reset_emit_does_not_clear_prev_session_id():
    """CLAWD-3534: the emit must not consume upstream's field.

    ``prev_session_id`` has a second consumer:
    ``build_channel_continuity_note`` reads it to point Slack/Discord at the
    prior same-channel session.  The retired fork field was cleared after
    one read; doing that
    to ``prev_session_id`` would silently drop the continuity hint, so the
    exactly-once marker is a separate field and this pins that.
    """
    source = _make_source()
    fresh_entry = _auto_reset_entry(build_session_key(source))
    runner = _make_emit_runner(fresh_entry)

    await _drive_to_emit(runner)

    assert fresh_entry.prev_session_id == "sess-old-prior", (
        "session:end emit cleared prev_session_id — the Slack/Discord "
        "continuity hint reads that field and would go silent"
    )
    assert fresh_entry.session_end_emitted_for == "sess-old-prior", (
        "emit did not record the already-emitted marker"
    )


@pytest.mark.asyncio
async def test_auto_reset_session_end_fires_exactly_once():
    """CLAWD-3534: re-entering the block must not re-fire session:end.

    ``prev_session_id`` persists for the life of the entry, unlike the
    transient field it replaced, so something has to make the close
    idempotent.  This drives the state where that is not academic: the
    /model path (``slash_commands.py``) consumes ``was_auto_reset`` without
    emitting anything, so the entry keeps ``auto_reset_reason`` forever —
    ``run.py`` clears it only inside ``if _was_auto_reset:``, which never
    runs on that path.  Every later turn that reaches the emit block —
    ``_is_new_session`` still gates entry, see the sibling test's docstring
    for what masks that today — therefore re-classifies as an auto-reset
    boundary, and the marker is the only thing between that and a duplicate
    close on each one.
    """
    source = _make_source()
    fresh_entry = _flag_consumed_elsewhere_entry(build_session_key(source))
    runner = _make_emit_runner(fresh_entry)

    await _drive_to_emit(runner)
    first_pass = [c[0][0] for c in runner.hooks.emit.call_args_list]
    assert first_pass == ["session:end", "session:start"]
    assert fresh_entry.session_end_emitted_for == "sess-old-prior"

    # Second turn on the same entry.  Nothing is mutated between passes —
    # the entry re-classifies on its own, which is the point.
    assert fresh_entry.auto_reset_reason == "idle", (
        "precondition lost: the entry no longer re-classifies, so the "
        "assertion below would hold without the marker doing anything"
    )
    runner.hooks.emit.reset_mock()
    await _drive_to_emit(runner)

    assert "session:start" in [
        c[0][0] for c in runner.hooks.emit.call_args_list
    ], "second pass never reached the emit block; the assertion below is vacuous"

    second_pass = [c[0][0] for c in runner.hooks.emit.call_args_list]
    assert "session:end" not in second_pass, (
        f"session:end re-fired for an already-closed prior session; "
        f"got {second_pass}"
    )


def _flag_consumed_elsewhere_entry(session_key: str) -> SessionEntry:
    """An auto-reset entry whose ``was_auto_reset`` was eaten by /model.

    ``gateway/slash_commands.py:2229`` consumes the flag so the model
    override it is about to store survives the next turn's cleanup
    (#48031).  That path emits no ``session:end``, and it leaves
    ``auto_reset_reason`` set because ``run.py`` clears the reason only
    inside ``if _was_auto_reset:``.
    """
    return _auto_reset_entry(session_key, was_auto_reset=False)


@pytest.mark.asyncio
async def test_close_survives_flag_consumed_by_slash_command():
    """CLAWD-3534: an idle reset followed by /model must not lose the close.

    Sequence: a session idles past its reset policy, and the user's next
    inbound is ``/model ...``.  The slash path calls
    ``get_or_create_session`` — firing the auto-reset, so the close for the
    prior id is owed — then consumes ``was_auto_reset`` without emitting
    anything.  If the boundary classifier read only that flag, the close
    would be unreachable to this block forever.

    Currently masked in production by ``updated_at`` being bumped
    unconditionally on the healthy path (so the next turn is not a "new
    session" at all).  The mask disappears with upstream's
    ``touch_activity=False``, already on upstream/main, which preserves
    ``updated_at`` — so this is pinned now rather than after that merge.
    """
    source = _make_source()
    entry = _flag_consumed_elsewhere_entry(build_session_key(source))
    assert entry.was_auto_reset is False and entry.auto_reset_reason == "idle"

    runner = _make_emit_runner(entry)
    await _drive_to_emit(runner)

    events = [c[0][0] for c in runner.hooks.emit.call_args_list]
    assert events == ["session:end", "session:start"], (
        f"close for the prior session was dropped because another path had "
        f"already consumed was_auto_reset; got {events}"
    )
    assert runner.hooks.emit.call_args_list[0][0][1]["session_id"] == (
        "sess-old-prior"
    )


@pytest.mark.asyncio
async def test_explicit_reset_never_emits_auto_reset_close():
    """CLAWD-3534 tripwire: /new must not be reported as an auto-reset.

    The retired field was written at one call site the fork controlled.
    ``prev_session_id`` is upstream's, and upstream already threads it into
    lineage columns, so an upstream release that records a parent id on the
    entry ``reset_session`` builds would — if the close were keyed on that id
    alone — turn every explicit /new into a spurious reason="auto_reset"
    close on all 11 live gateways.

    This pins the classifier rather than the current write sites: the entry
    below is shaped like ``reset_session``'s output but *also* carries a
    prev_session_id, which is precisely the state no test would otherwise
    cover.
    """
    source = _make_source()
    entry = _auto_reset_entry(
        build_session_key(source),
        was_auto_reset=False,
        auto_reset_reason=None,
        is_fresh_reset=True,
        prev_session_id="sess-old-explicitly-reset",
    )
    runner = _make_emit_runner(entry)

    await _drive_to_emit(runner)

    events = [c[0][0] for c in runner.hooks.emit.call_args_list]
    assert events == ["session:start"], (
        f"an explicit /new emitted a session boundary close; got {events}"
    )


def test_reset_session_leaves_prev_session_id_unset(tmp_path, monkeypatch):
    """The write-site half of the tripwire above, against the real store.

    If upstream ever starts recording a parent id here, this fails and points
    at ``test_explicit_reset_never_emits_auto_reset_close`` as the guard that
    is now load-bearing.
    """
    import hermes_state

    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(
        sessions_dir=tmp_path / "sessions", config=GatewayConfig()
    )

    source = _make_source()
    first = store.get_or_create_session(source)
    reset = store.reset_session(first.session_key)

    assert reset is not None
    assert reset.is_fresh_reset is True
    assert reset.prev_session_id is None, (
        "reset_session now records a prior session id; the session:end emit "
        "in run.py classifies boundaries by was_auto_reset, so verify that "
        "guard still holds before relying on this"
    )


def test_session_entry_has_session_end_emitted_for_field():
    """The dataclass exposes the exactly-once marker the emit needs
    (#28746, CLAWD-3534)."""
    entry = SessionEntry(
        session_key="k",
        session_id="s",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    # default value is None
    assert entry.session_end_emitted_for is None
    # field is writable
    entry.session_end_emitted_for = "prior"
    assert entry.session_end_emitted_for == "prior"


def test_session_end_marker_survives_persistence_roundtrip():
    """CLAWD-3534: the marker is PERSISTED, unlike the field it replaces.

    The retired ``auto_reset_prior_session_id`` was never written to
    sessions.json, so a restart between the auto-reset and the next turn
    dropped the pending close outright.  Both halves now round-trip, so the
    close still fires after a restart and does not re-fire once the marker
    has been saved.

    Stated precisely, because this is a real behaviour change and not a
    strict improvement: if the process dies between the emit and the next
    save, the marker is lost while ``prev_session_id`` survives, so the
    close can fire twice.  That trades at-most-once-with-silent-loss for
    at-least-once.

    Do not read that as "a duplicate is harmless".  The one subscriber
    installed in this mesh (the clawd-substrate-ingest hook) ignores the
    ``session_id`` in the context and closes every row in its state file, so
    a duplicate lands on whatever session is live by then.  What makes the
    trade defensible is that the fleet ALREADY receives repeated closes for
    one session_id today — the expiry watcher emits reason="idle_expiry"
    and this block then emits reason="auto_reset" for the same id — so
    duplicate-tolerance is a property subscribers need regardless, while a
    silently dropped close leaves state stuck forever with nothing to
    reconcile it.
    """
    entry = SessionEntry(
        session_key="k",
        session_id="s",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        prev_session_id="sess-old-prior",
        session_end_emitted_for="sess-old-prior",
    )

    restored = SessionEntry.from_dict(entry.to_dict())

    assert restored.prev_session_id == "sess-old-prior"
    assert restored.session_end_emitted_for == "sess-old-prior"
