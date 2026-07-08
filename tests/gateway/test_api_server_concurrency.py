"""Tests for the /v1/runs concurrency-admission branch in ``_handle_runs``.

The admission check counts IN-FLIGHT runs (tasks in ``_active_run_tasks``
whose ``.done()`` is False), NOT retained result streams
(``len(self._run_streams)``). This matters because a completed run keeps its
``_run_streams`` entry until its SSE stream is consumed or the 300s orphan
reaper sweeps it, while its task is popped from ``_active_run_tasks`` in
``_run_and_close``'s ``finally``. A fire-and-forget client (the
agent-meeting-space bus) that never drains the stream would otherwise wedge
the gateway at 429 after ``_max_concurrent_runs`` *completed* runs.

These tests seed the internal dicts directly with fake task objects so the
admission branch is exercised in isolation — no real agent runs are started.
"""

import json

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTask:
    """Minimal stand-in for an asyncio.Task exposing only ``.done()``.

    The admission check only ever calls ``.done()`` on the values of
    ``_active_run_tasks``, so a real Task is unnecessary (and would require
    a running coroutine). ``done`` controls whether this counts as in-flight.
    """

    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    return APIServerAdapter(config)


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


def _mock_create_agent(adapter):
    """Patch ``_create_agent`` so an admitted run does no real work."""
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "ok"}
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0
    return patch.object(adapter, "_create_agent", return_value=mock_agent)


@pytest.fixture
def adapter():
    return _make_adapter()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrencyAdmission:
    @pytest.mark.asyncio
    async def test_max_inflight_runs_returns_429(self, adapter):
        """With _max_concurrent_runs not-done tasks present, a new run is rejected."""
        limit = adapter._max_concurrent_runs
        for i in range(limit):
            adapter._active_run_tasks[f"run_inflight_{i}"] = _FakeTask(done=False)

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"input": "hello"})
            # The 429 path returns before the server reads the request body;
            # under pytest-asyncio the keep-alive connection then closes, so
            # read the full body once before inspecting status (a second
            # deferred read would hit the closed connection). The production
            # response is well-formed — verified by a standalone repro.
            body = await resp.read()

        assert resp.status == 429
        data = json.loads(body)
        assert data["error"]["code"] == "rate_limit_exceeded"
        assert "Too many concurrent runs" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_just_under_limit_is_admitted(self, adapter):
        """With limit-1 in-flight tasks, a new run is admitted (boundary check)."""
        limit = adapter._max_concurrent_runs
        for i in range(limit - 1):
            adapter._active_run_tasks[f"run_inflight_{i}"] = _FakeTask(done=False)

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with _mock_create_agent(adapter):
                resp = await cli.post("/v1/runs", json={"input": "hello"})

        assert resp.status == 202

    @pytest.mark.asyncio
    async def test_completed_unconsumed_streams_do_not_count(self, adapter):
        """KEY regression test (the production wedge).

        Simulate many COMPLETED fire-and-forget runs: their _run_streams
        entries are retained (never drained), but their tasks were popped
        from _active_run_tasks in _run_and_close's finally. A new run MUST
        be admitted even though len(_run_streams) >= _max_concurrent_runs.

        Under the OLD check (len(self._run_streams) >= limit) this would
        return 429 and wedge the gateway. Under the fix it is admitted.
        """
        limit = adapter._max_concurrent_runs
        # Far more retained streams than the limit; tasks are absent
        # (popped on completion) — i.e. zero in-flight runs.
        for i in range(limit * 3):
            adapter._run_streams[f"run_done_{i}"] = MagicMock()  # stand-in queue
            adapter._run_streams_created[f"run_done_{i}"] = 0.0
        assert len(adapter._run_streams) >= limit
        assert adapter._active_run_tasks == {}

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with _mock_create_agent(adapter):
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                body = await resp.read()

        assert resp.status == 202, "completed-but-unconsumed runs must not count"
        data = json.loads(body)
        assert data["status"] == "started"

    @pytest.mark.asyncio
    async def test_done_tasks_in_active_dict_do_not_count(self, adapter):
        """A done task still present in _active_run_tasks must not count (the
        ``not t.done()`` guard). Even with limit+ DONE tasks plus retained
        streams, a new run is admitted because zero tasks are in-flight."""
        limit = adapter._max_concurrent_runs
        for i in range(limit + 2):
            adapter._active_run_tasks[f"run_done_{i}"] = _FakeTask(done=True)
            adapter._run_streams[f"run_done_{i}"] = MagicMock()
            adapter._run_streams_created[f"run_done_{i}"] = 0.0

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with _mock_create_agent(adapter):
                resp = await cli.post("/v1/runs", json={"input": "hello"})

        assert resp.status == 202

    @pytest.mark.asyncio
    async def test_mixed_done_and_inflight_counts_only_inflight(self, adapter):
        """With limit-1 in-flight + several done tasks, a new run is admitted;
        the done tasks are ignored. Adding one more in-flight would reach the
        limit (verified separately) — here we confirm done entries don't tip it."""
        limit = adapter._max_concurrent_runs
        for i in range(limit - 1):
            adapter._active_run_tasks[f"run_inflight_{i}"] = _FakeTask(done=False)
        for i in range(limit):
            adapter._active_run_tasks[f"run_done_{i}"] = _FakeTask(done=True)

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with _mock_create_agent(adapter):
                resp = await cli.post("/v1/runs", json={"input": "hello"})

        assert resp.status == 202
