"""Tests for the three /v1/runs QoL fork-delta additions (CLAWD-1923).

1. ``tool.completed`` SSE events now carry the tool ``result`` (capped, with a
   ``result_truncated`` flag when the output is too large).
2. Per-request ``reasoning_effort`` / ``verbosity`` / ``model`` overrides on
   ``POST /v1/runs`` (optional; absent → unchanged behavior; invalid → 400).
3. Clarify (agent-asks-mid-turn): a ``clarify.request`` SSE event plus a
   ``POST /v1/runs/{run_id}/clarify`` resolve endpoint, mirroring approval.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    return APIServerAdapter(config)


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    """aiohttp app with the /v1/runs routes (incl. the new clarify route)."""
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


def _make_clarify_agent(question: str, choices):
    """Mock agent whose run_conversation fires the wired clarify_callback.

    Mirrors how the real clarify tool calls ``agent.clarify_callback``;
    ``_run_and_close`` overwrites the mock's attribute with the real callback
    before the run executes.
    """
    captured = {}
    agent = MagicMock()

    def _run(user_message=None, conversation_history=None, task_id=None):
        response = agent.clarify_callback(question, choices)
        captured["response"] = response
        return {"final_response": response}

    agent.run_conversation.side_effect = _run
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    return agent, captured


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# Feature 1 — tool.completed carries (capped) result
# ---------------------------------------------------------------------------


class TestToolResultPayload:
    @pytest.mark.asyncio
    async def test_tool_completed_includes_result(self, adapter):
        loop = asyncio.get_running_loop()
        run_id = "run_tool_result"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_streams[run_id] = q

        cb = adapter._make_run_event_callback(run_id, loop)
        cb("tool.completed", "search", None, None,
           duration=0.5, is_error=False, result="found 3 items")

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert event["event"] == "tool.completed"
        assert event["tool"] == "search"
        assert event["duration"] == 0.5
        assert event["error"] is False
        assert event["result"] == "found 3 items"
        assert "result_truncated" not in event

    @pytest.mark.asyncio
    async def test_tool_completed_truncates_large_result(self, adapter):
        loop = asyncio.get_running_loop()
        run_id = "run_tool_trunc"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_streams[run_id] = q

        big = "x" * (adapter._MAX_TOOL_RESULT_CHARS + 5000)
        cb = adapter._make_run_event_callback(run_id, loop)
        cb("tool.completed", "read_file", None, None,
           duration=0.1, is_error=False, result=big)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert event["result_truncated"] is True
        assert len(event["result"]) == adapter._MAX_TOOL_RESULT_CHARS

    @pytest.mark.asyncio
    async def test_tool_completed_without_result_omits_key(self, adapter):
        loop = asyncio.get_running_loop()
        run_id = "run_tool_noresult"
        q: asyncio.Queue = asyncio.Queue()
        adapter._run_streams[run_id] = q

        cb = adapter._make_run_event_callback(run_id, loop)
        # No result kwarg — must behave exactly as before (no result key).
        cb("tool.completed", "noop", None, None, duration=0.0, is_error=False)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert "result" not in event
        assert "result_truncated" not in event


# ---------------------------------------------------------------------------
# Feature 2 — per-request reasoning_effort / verbosity / model overrides
# ---------------------------------------------------------------------------


class TestPerRequestOverrides:
    @pytest.mark.asyncio
    async def test_overrides_reach_create_agent(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("ok")
                resp = await cli.post("/v1/runs", json={
                    "input": "hi",
                    "model": "gpt-5",
                    "reasoning_effort": "high",
                    "verbosity": "low",
                })
                assert resp.status == 202

                for _ in range(60):
                    if mock_create.called:
                        break
                    await asyncio.sleep(0.05)

                assert mock_create.called
                kwargs = mock_create.call_args.kwargs
                assert kwargs["model"] == "gpt-5"
                assert kwargs["reasoning_effort"] == "high"
                assert kwargs["verbosity"] == "low"

    @pytest.mark.asyncio
    async def test_absent_overrides_pass_none(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("ok")
                resp = await cli.post("/v1/runs", json={"input": "hi"})
                assert resp.status == 202

                for _ in range(60):
                    if mock_create.called:
                        break
                    await asyncio.sleep(0.05)

                assert mock_create.called
                kwargs = mock_create.call_args.kwargs
                assert kwargs["model"] is None
                assert kwargs["reasoning_effort"] is None
                assert kwargs["verbosity"] is None

    @pytest.mark.asyncio
    async def test_invalid_reasoning_effort_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"input": "hi", "reasoning_effort": "turbo"})
            assert resp.status == 400
            data = await resp.json()
            assert data["error"]["code"] == "invalid_reasoning_effort"
        # Invalid request must not allocate a run.
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}

    @pytest.mark.asyncio
    async def test_invalid_verbosity_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs", json={"input": "hi", "verbosity": "loud"})
            assert resp.status == 400
            data = await resp.json()
            assert data["error"]["code"] == "invalid_verbosity"
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}

    def test_create_agent_merges_reasoning_overrides(self, adapter):
        """_create_agent merges overrides into the resolved reasoning_config."""
        captured = {}

        def _fake_aiagent(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from gateway.run import GatewayRunner

        with patch("run_agent.AIAgent", side_effect=_fake_aiagent), \
                patch("gateway.run._resolve_runtime_agent_kwargs", return_value={}), \
                patch("gateway.run._resolve_gateway_model", return_value="default-model"), \
                patch("gateway.run._load_gateway_config", return_value={}), \
                patch("hermes_cli.tools_config._get_platform_tools", return_value=set()), \
                patch.object(adapter, "_ensure_session_db", return_value=None), \
                patch.object(GatewayRunner, "_load_fallback_model", return_value=None), \
                patch.object(GatewayRunner, "_load_reasoning_config",
                             return_value={"enabled": True, "effort": "medium"}):
            adapter._create_agent(model="gpt-5", reasoning_effort="high", verbosity="low")

        assert captured["model"] == "gpt-5"
        assert captured["reasoning_config"] == {
            "enabled": True, "effort": "high", "verbosity": "low",
        }

    def test_create_agent_without_overrides_unchanged(self, adapter):
        """Absent overrides → profile model + reasoning_config untouched."""
        captured = {}

        def _fake_aiagent(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from gateway.run import GatewayRunner

        with patch("run_agent.AIAgent", side_effect=_fake_aiagent), \
                patch("gateway.run._resolve_runtime_agent_kwargs", return_value={}), \
                patch("gateway.run._resolve_gateway_model", return_value="default-model"), \
                patch("gateway.run._load_gateway_config", return_value={}), \
                patch("hermes_cli.tools_config._get_platform_tools", return_value=set()), \
                patch.object(adapter, "_ensure_session_db", return_value=None), \
                patch.object(GatewayRunner, "_load_fallback_model", return_value=None), \
                patch.object(GatewayRunner, "_load_reasoning_config",
                             return_value={"enabled": True, "effort": "medium"}):
            adapter._create_agent()

        assert captured["model"] == "default-model"
        assert captured["reasoning_config"] == {"enabled": True, "effort": "medium"}


# ---------------------------------------------------------------------------
# Feature 3 — clarify (agent-asks-mid-turn)
# ---------------------------------------------------------------------------


async def _drain_for_event(q: asyncio.Queue, event_name: str, max_iters: int = 100):
    for _ in range(max_iters):
        try:
            ev = await asyncio.wait_for(q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if ev and ev.get("event") == event_name:
            return ev
    return None


class TestClarify:
    @pytest.mark.asyncio
    async def test_clarify_request_emitted_and_resolved_with_choices(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, captured = _make_clarify_agent("Pick one", ["A", "B"])
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "go"})
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                q = adapter._run_streams[run_id]
                event = await _drain_for_event(q, "clarify.request")
                assert event is not None
                assert event["question"] == "Pick one"
                assert event["options"] == ["A", "B"]
                assert event["allow_text"] is False
                clarify_id = event["clarify_id"]
                assert clarify_id

                assert adapter._run_statuses[run_id]["status"] == "waiting_for_clarify"

                cresp = await cli.post(
                    f"/v1/runs/{run_id}/clarify",
                    json={"response": "A", "clarify_id": clarify_id},
                )
                assert cresp.status == 200
                cdata = await cresp.json()
                assert cdata["resolved"] is True
                assert cdata["clarify_id"] == clarify_id

                for _ in range(60):
                    if adapter._run_statuses.get(run_id, {}).get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)
                assert captured["response"] == "A"

    @pytest.mark.asyncio
    async def test_clarify_open_ended_resolves_without_clarify_id(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, captured = _make_clarify_agent("Anything to add?", None)
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "go"})
                run_id = (await resp.json())["run_id"]

                q = adapter._run_streams[run_id]
                event = await _drain_for_event(q, "clarify.request")
                assert event is not None
                assert event["options"] is None
                assert event["allow_text"] is True

                # Omit clarify_id → resolved via get_pending_for_session.
                cresp = await cli.post(
                    f"/v1/runs/{run_id}/clarify",
                    json={"response": "yes please"},
                )
                assert cresp.status == 200
                assert (await cresp.json())["resolved"] is True

                for _ in range(60):
                    if adapter._run_statuses.get(run_id, {}).get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)
                assert captured["response"] == "yes please"

    @pytest.mark.asyncio
    async def test_clarify_resolve_calls_resolver_and_resumes(self, adapter):
        """Resolve endpoint calls resolve_gateway_clarify and resets status."""
        app = _create_runs_app(adapter)
        run_id = "run_clarify_unit"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = "sess-1"
        adapter._run_streams[run_id] = asyncio.Queue()

        async with TestClient(TestServer(app)) as cli:
            with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as mock_resolve:
                cresp = await cli.post(
                    f"/v1/runs/{run_id}/clarify",
                    json={"response": "hello", "clarify_id": "cid-9"},
                )

        assert cresp.status == 200
        mock_resolve.assert_called_once_with("cid-9", "hello")
        assert adapter._run_statuses[run_id]["status"] == "running"

    @pytest.mark.asyncio
    async def test_clarify_without_pending_returns_409(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _simple_agent("done")
                resp = await cli.post("/v1/runs", json={"input": "hi"})
                run_id = (await resp.json())["run_id"]

                # No clarify was ever raised by this run.
                cresp = await cli.post(f"/v1/runs/{run_id}/clarify", json={"response": "x"})
                assert cresp.status == 409
                cdata = await cresp.json()
                assert cdata["error"]["code"] in {"clarify_not_active", "clarify_not_pending"}

    @pytest.mark.asyncio
    async def test_clarify_missing_response_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        run_id = "run_clarify_missing"
        adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_clarify"}
        adapter._run_clarify_sessions[run_id] = "sess-1"
        async with TestClient(TestServer(app)) as cli:
            cresp = await cli.post(f"/v1/runs/{run_id}/clarify", json={"clarify_id": "x"})
            assert cresp.status == 400
            assert (await cresp.json())["error"]["code"] == "invalid_clarify_response"

    @pytest.mark.asyncio
    async def test_clarify_run_not_found_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            cresp = await cli.post("/v1/runs/run_nope/clarify", json={"response": "x"})
        assert cresp.status == 404

    @pytest.mark.asyncio
    async def test_clarify_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_any/clarify", json={"response": "x"})
        assert resp.status == 401
