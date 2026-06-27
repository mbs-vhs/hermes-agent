"""Independent gap-filling tests for the /v1/runs QoL additions (CLAWD-1923).

These complement ``test_api_server_runs_qol.py`` (the implementer's 16) by
pinning real code branches the original file leaves untested or only tests
non-deterministically:

Feature 1 (tool.completed result):
  * the truncation boundary is ``>`` not ``>=`` — a result of *exactly*
    ``_MAX_TOOL_RESULT_CHARS`` must NOT be flagged truncated;
  * a non-string ``result`` is coerced via ``str(...)`` and forwarded.

Feature 2 (per-request overrides):
  * a blank / whitespace-only ``model`` collapses to ``None`` (absent),
    i.e. it must NOT leak an empty override into ``_create_agent`` — the
    regression-relevant "blank behaves like absent" path;
  * ``reasoning_effort`` is normalized (strip + lower-case) before reaching
    ``_create_agent`` and is NOT rejected.

Feature 3 (clarify):
  * the ``clarify_not_pending`` 409 branch — distinct from
    ``clarify_not_active`` — when the run HAS an active clarify session but
    nothing is pending (the original 409 test accepts either code and, with a
    fast mock agent, deterministically hits ``clarify_not_active``);
  * an unknown ``clarify_id`` against an active session → ``clarify_not_pending``
    409 (exercises the real ``resolve_gateway_clarify`` returning False);
  * a successful resolve enqueues a ``clarify.responded`` SSE event carrying
    the response + clarify_id;
  * a malformed JSON body → 400 (after the 404 run-existence check);
  * the clarify session registration is cleaned up on a terminal run path
    (leak check): ``_run_clarify_sessions`` is popped and the per-session
    notify callback is unregistered once the run completes.
"""

import asyncio
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
from tools import clarify_gateway


# ---------------------------------------------------------------------------
# Helpers (kept self-contained; mirror the sibling QoL test file)
# ---------------------------------------------------------------------------


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
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/clarify", adapter._handle_run_clarify)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _simple_agent(final_response: str = "done") -> MagicMock:
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": final_response}
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    return agent


async def _wait_create_called(mock_create, tries: int = 60):
    for _ in range(tries):
        if mock_create.called:
            return True
        await asyncio.sleep(0.05)
    return mock_create.called


async def _wait_status(adapter, run_id, target, tries: int = 80):
    for _ in range(tries):
        if adapter._run_statuses.get(run_id, {}).get("status") == target:
            return True
        await asyncio.sleep(0.05)
    return adapter._run_statuses.get(run_id, {}).get("status") == target


@pytest.fixture
def adapter():
    return _make_adapter()


# ---------------------------------------------------------------------------
# Feature 1 — tool.completed result: boundary + coercion
# ---------------------------------------------------------------------------


class TestToolResultEdges:
    @pytest.mark.asyncio
    async def test_result_exactly_at_cap_not_truncated(self, adapter):
        """len == _MAX_TOOL_RESULT_CHARS uses ``>`` so it is NOT truncated."""
        loop = asyncio.get_running_loop()
        run_id = "run_boundary"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_streams[run_id] = q

        exact = "y" * adapter._MAX_TOOL_RESULT_CHARS
        cb = adapter._make_run_event_callback(run_id, loop)
        cb("tool.completed", "read_file", None, None,
           duration=0.1, is_error=False, result=exact)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert event["event"] == "tool.completed"
        assert len(event["result"]) == adapter._MAX_TOOL_RESULT_CHARS
        assert event["result"] == exact
        assert "result_truncated" not in event

    @pytest.mark.asyncio
    async def test_non_string_result_is_coerced(self, adapter):
        """A non-str result is forwarded via str(...) (isinstance else-branch)."""
        loop = asyncio.get_running_loop()
        run_id = "run_coerce"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_streams[run_id] = q

        payload = {"rows": 3, "ok": True}
        cb = adapter._make_run_event_callback(run_id, loop)
        cb("tool.completed", "query", None, None,
           duration=0.2, is_error=False, result=payload)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert event["result"] == str(payload)
        assert "result_truncated" not in event


# ---------------------------------------------------------------------------
# Feature 2 — override normalization at the _handle_runs boundary
# ---------------------------------------------------------------------------


class TestOverrideNormalization:
    @pytest.mark.asyncio
    async def test_blank_model_collapses_to_none(self, adapter):
        """A whitespace-only model must reach _create_agent as None (absent),
        not as an empty-string override."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("ok")
                resp = await cli.post("/v1/runs", json={"input": "hi", "model": "   "})
                assert resp.status == 202
                assert await _wait_create_called(mock_create)
                kwargs = mock_create.call_args.kwargs
                assert kwargs["model"] is None

    @pytest.mark.asyncio
    async def test_reasoning_effort_is_normalized_not_rejected(self, adapter):
        """' High ' is stripped+lowercased to 'high', accepted, and reaches
        _create_agent as 'high'."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("ok")
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hi", "reasoning_effort": " High "},
                )
                assert resp.status == 202
                assert await _wait_create_called(mock_create)
                kwargs = mock_create.call_args.kwargs
                assert kwargs["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Feature 3 — clarify: the under-covered branches
# ---------------------------------------------------------------------------


class TestClarifyBranches:
    @pytest.mark.asyncio
    async def test_active_session_no_pending_returns_clarify_not_pending(self, adapter):
        """Active clarify session but nothing pending → 409 clarify_not_pending
        (the branch distinct from clarify_not_active)."""
        app = _create_runs_app(adapter)
        run_id = "run_no_pending"
        session_key = "sess-no-pending"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = session_key
        # Guard: ensure no stale entry exists for this session key.
        assert clarify_gateway.get_pending_for_session(session_key) is None

        async with TestClient(TestServer(app)) as cli:
            cresp = await cli.post(f"/v1/runs/{run_id}/clarify", json={"response": "x"})
            assert cresp.status == 409
            cdata = await cresp.json()
        assert cdata["error"]["code"] == "clarify_not_pending"

    @pytest.mark.asyncio
    async def test_unknown_clarify_id_returns_clarify_not_pending(self, adapter):
        """clarify_id given but resolve_gateway_clarify (real) returns False
        because the id was never registered → 409 clarify_not_pending."""
        app = _create_runs_app(adapter)
        run_id = "run_unknown_cid"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = "sess-unknown-cid"

        async with TestClient(TestServer(app)) as cli:
            cresp = await cli.post(
                f"/v1/runs/{run_id}/clarify",
                json={"response": "x", "clarify_id": "never-registered"},
            )
            assert cresp.status == 409
            cdata = await cresp.json()
        assert cdata["error"]["code"] == "clarify_not_pending"

    @pytest.mark.asyncio
    async def test_resolve_enqueues_clarify_responded_event(self, adapter):
        """A successful resolve pushes a clarify.responded SSE event carrying
        the response + clarify_id, and flips status to running."""
        app = _create_runs_app(adapter)
        run_id = "run_resp_event"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = "sess-resp"
        adapter._run_streams[run_id] = q

        async with TestClient(TestServer(app)) as cli:
            with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True):
                cresp = await cli.post(
                    f"/v1/runs/{run_id}/clarify",
                    json={"response": "the answer", "clarify_id": "cid-42"},
                )
        assert cresp.status == 200
        assert adapter._run_statuses[run_id]["status"] == "running"

        # Drain the queue and locate the clarify.responded event.
        seen = None
        while not q.empty():
            ev = q.get_nowait()
            if ev and ev.get("event") == "clarify.responded":
                seen = ev
                break
        assert seen is not None
        assert seen["clarify_id"] == "cid-42"
        assert seen["response"] == "the answer"
        assert seen["run_id"] == run_id

    @pytest.mark.asyncio
    async def test_malformed_json_body_returns_400(self, adapter):
        """Non-JSON body → 400 'Invalid JSON' (after the 404 existence check)."""
        app = _create_runs_app(adapter)
        run_id = "run_bad_json"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = "sess-bad-json"

        async with TestClient(TestServer(app)) as cli:
            cresp = await cli.post(
                f"/v1/runs/{run_id}/clarify",
                data="not-json-at-all",
                headers={"Content-Type": "application/json"},
            )
        assert cresp.status == 400

    @pytest.mark.asyncio
    async def test_clarify_session_cleaned_up_after_run(self, adapter):
        """Leak check: a completed run pops _run_clarify_sessions and
        unregisters its per-session clarify notify callback."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("done")
                resp = await cli.post("/v1/runs", json={"input": "hi"})
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                # With no session_id / X-Hermes-Session-Key, the clarify
                # session key derives to run_id.
                assert await _wait_status(adapter, run_id, "completed")

        assert run_id not in adapter._run_clarify_sessions
        assert clarify_gateway.get_notify(run_id) is None
