"""Tests for tools/workflow_authoring_tool.py — the workflow_authoring tool (CLAWD-1709).

Covers the schema contract, env gating, header construction, step/input
normalization, per-verb request shape (define/run/revise/show/tail), validation
that bad input never makes an HTTP call, non-200 + transport fail-soft handling,
and registration (registry + Hermes core-tools list).

HTTP is mocked by patching ``tools.workflow_authoring_tool.httpx.Client`` so no
real network call is made. The tool calls ``client.request(method, url, ...)``
(not ``.post``), so the mock returns the response from ``.request``.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.workflow_authoring_tool import (
    WORKFLOW_AUTHORING_SCHEMA,
    _coerce_input,
    _headers,
    _normalize_steps,
    check_workflow_authoring_requirements,
    workflow_authoring_tool,
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("CLAWD_API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAWD_BASE_URL", raising=False)


def _mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _patched_client(resp=None, raise_exc=None):
    """Patch target for httpx.Client whose .request() returns *resp*.

    The real code uses ``with httpx.Client(...) as client: client.request(...)``.
    """
    client = MagicMock()
    if raise_exc is not None:
        client.request.side_effect = raise_exc
    else:
        client.request.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    factory = MagicMock(return_value=ctx)
    factory.client = client
    return factory


# =========================================================================
# 1. Schema contract
# =========================================================================
class TestSchema:
    def test_name(self):
        assert WORKFLOW_AUTHORING_SCHEMA["name"] == "workflow_authoring"

    def test_action_required(self):
        assert WORKFLOW_AUTHORING_SCHEMA["parameters"]["required"] == ["action"]

    def test_action_enum_lists_all_verbs(self):
        enum = WORKFLOW_AUTHORING_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert set(enum) == {
            "define", "run", "revise", "show", "tail",
            "schedule", "trigger", "emit", "schedules", "triggers",
        }

    def test_properties_cover_all_params(self):
        props = WORKFLOW_AUTHORING_SCHEMA["parameters"]["properties"]
        for key in (
            "action", "name", "steps", "input", "run_id", "version", "status", "limit",
            "cron_expr", "event_pattern", "event", "payload",
        ):
            assert key in props


# =========================================================================
# 2. Env gating
# =========================================================================
class TestCheckRequirements:
    def test_false_without_token(self):
        assert check_workflow_authoring_requirements() is False

    def test_true_with_token(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "bearer")
        assert check_workflow_authoring_requirements() is True

    def test_false_with_whitespace_token(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "   ")
        assert check_workflow_authoring_requirements() is False


# =========================================================================
# 3. _headers
# =========================================================================
class TestHeaders:
    def test_content_type_always_present(self):
        assert _headers()["Content-Type"] == "application/json"

    def test_no_auth_header_when_unset(self):
        assert "Authorization" not in _headers()

    def test_bearer_when_set(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "tok123")
        assert _headers()["Authorization"] == "Bearer tok123"


# =========================================================================
# 4. _normalize_steps / _coerce_input
# =========================================================================
class TestNormalizeSteps:
    def test_list_of_dicts(self):
        out = _normalize_steps([{"name": "a", "code_ref": "m.a"}])
        assert out == [{"name": "a", "code_ref": "m.a"}]

    def test_json_string(self):
        out = _normalize_steps('[{"name": "a", "code_ref": "m.a"}]')
        assert out == [{"name": "a", "code_ref": "m.a"}]

    def test_assigned_agent_kept(self):
        out = _normalize_steps([{"name": "a", "code_ref": "m.a", "assigned_agent": "elon"}])
        assert out == [{"name": "a", "code_ref": "m.a", "assigned_agent": "elon"}]

    def test_missing_code_ref_is_none(self):
        assert _normalize_steps([{"name": "a"}]) is None

    def test_empty_list_is_none(self):
        assert _normalize_steps([]) is None

    def test_bad_json_is_none(self):
        assert _normalize_steps("not json") is None


class TestCoerceInput:
    def test_dict_passthrough(self):
        assert _coerce_input({"x": 1}) == {"x": 1}

    def test_json_object_string(self):
        assert _coerce_input('{"x": 1}') == {"x": 1}

    def test_none_and_empty(self):
        assert _coerce_input(None) is None
        assert _coerce_input("") is None

    def test_non_object_string_is_none(self):
        assert _coerce_input("[1, 2]") is None


# =========================================================================
# 5. Validation — no HTTP on bad input
# =========================================================================
class TestValidation:
    def test_unknown_action_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="frobnicate"))
        assert result["success"] is False
        factory.assert_not_called()

    def test_define_missing_name_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="define", steps=[{"name": "a", "code_ref": "m.a"}]))
        assert result["success"] is False
        factory.assert_not_called()

    def test_define_bad_steps_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="define", name="w", steps=[{"name": "a"}]))
        assert result["success"] is False
        factory.assert_not_called()

    def test_run_missing_name_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="run"))
        assert result["success"] is False
        factory.assert_not_called()

    def test_revise_missing_run_id_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="revise", steps=[{"name": "a", "code_ref": "m.a"}]))
        assert result["success"] is False
        factory.assert_not_called()

    def test_show_missing_run_id_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="show"))
        assert result["success"] is False
        factory.assert_not_called()


# =========================================================================
# 6. Per-verb request shape (happy path)
# =========================================================================
class TestDefine:
    def test_post_to_define_with_graph(self):
        resp = _mock_response(200, {"def_id": "d1", "name": "enrich", "version": 1})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="define", name="enrich",
                steps=[{"name": "double", "code_ref": "demo.double"}],
            ))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/define")
        assert kwargs["json"]["name"] == "enrich"
        assert kwargs["json"]["steps"] == [{"name": "double", "code_ref": "demo.double"}]
        assert result["success"] is True
        assert result["version"] == 1


class TestRun:
    def test_post_to_run_with_input(self):
        resp = _mock_response(200, {"run_id": "r1", "name": "enrich"})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="run", name="enrich", input={"x": 21}))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/run")
        assert kwargs["json"] == {"name": "enrich", "input": {"x": 21}}
        assert result["run_id"] == "r1"

    def test_run_without_input_omits_key(self):
        resp = _mock_response(200, {"run_id": "r1", "name": "enrich"})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            workflow_authoring_tool(action="run", name="enrich")
        _, kwargs = factory.client.request.call_args
        assert "input" not in kwargs["json"]


class TestRevise:
    def test_post_to_revise_endpoint(self):
        resp = _mock_response(200, {
            "version": 2, "dirty_steps": ["scale", "format"], "unchanged_steps": ["double"],
        })
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="revise", run_id="r1",
                steps=[{"name": "scale", "code_ref": "demo.scale_fixed"}],
            ))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/r1/revise")
        assert result["new_version"] == 2
        assert result["dirty_steps"] == ["scale", "format"]
        assert result["unchanged_steps"] == ["double"]


class TestShow:
    def test_get_run_endpoint_and_step_projection(self):
        resp = _mock_response(200, {"run": {
            "run_id": "r1", "name": "enrich", "def_version": 2, "status": "done",
            "current_step": "format", "result": {"value": 142}, "error": None,
            "steps": [
                {"step_name": "double", "status": "done", "code_ref": "demo.double",
                 "attempt": 1, "latency_ms": 3, "cost_usd": None},
            ],
        }})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="show", run_id="r1"))
        args, _ = factory.client.request.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/workflows/runs/r1")
        assert result["run"]["status"] == "done"
        assert result["run"]["steps"][0]["step_name"] == "double"


class TestTail:
    def test_get_runs_with_scope_params(self):
        resp = _mock_response(200, {"count": 1, "runs": [
            {"run_id": "r1", "name": "enrich", "status": "done",
             "current_step": "format", "def_version": 2},
        ]})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="tail", name="enrich", status="done", limit=5))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/workflows/runs")
        assert kwargs["params"] == {"name": "enrich", "status": "done", "limit": 5}
        assert result["count"] == 1
        assert result["runs"][0]["run_id"] == "r1"


# =========================================================================
# 6b. Ignition verbs (CLAWD-1710): schedule / trigger / emit / lists
# =========================================================================
class TestSchedule:
    def test_post_to_schedule_with_cron_and_input(self):
        resp = _mock_response(200, {
            "schedule_id": "s1", "name": "enrich", "cron_expr": "0 6 * * *",
            "next_run_at": "2026-06-16T06:00:00-06:00",
        })
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="schedule", name="enrich", cron_expr="0 6 * * *", input={"x": 1},
            ))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/schedule")
        assert kwargs["json"] == {"name": "enrich", "cron_expr": "0 6 * * *", "input": {"x": 1}}
        assert result["success"] is True
        assert result["schedule_id"] == "s1"

    def test_missing_name_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="schedule", cron_expr="* * * * *"))
        assert result["success"] is False
        factory.assert_not_called()

    def test_missing_cron_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="schedule", name="enrich"))
        assert result["success"] is False
        factory.assert_not_called()


class TestTrigger:
    def test_post_to_trigger_with_input_template(self):
        resp = _mock_response(200, {
            "trigger_id": "t1", "name": "enrich", "event_pattern": "plane_*",
        })
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="trigger", name="enrich", event_pattern="plane_*", input={"base": 1},
            ))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/trigger")
        assert kwargs["json"] == {
            "name": "enrich", "event_pattern": "plane_*", "input_template": {"base": 1},
        }
        assert result["trigger_id"] == "t1"

    def test_missing_event_pattern_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="trigger", name="enrich"))
        assert result["success"] is False
        factory.assert_not_called()


class TestEmit:
    def test_post_to_events_with_payload(self):
        resp = _mock_response(200, {
            "event": "council_ruling", "count": 1,
            "started": [{"name": "council_execution", "run_id": "r9"}],
        })
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="emit", event="council_ruling", payload={"meeting_id": "m1"},
            ))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/workflows/events")
        assert kwargs["json"] == {"event": "council_ruling", "payload": {"meeting_id": "m1"}}
        assert result["count"] == 1
        assert result["started"][0]["run_id"] == "r9"

    def test_missing_event_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="emit"))
        assert result["success"] is False
        factory.assert_not_called()


class TestLists:
    def test_schedules_get_with_optional_name(self):
        resp = _mock_response(200, {"count": 1, "schedules": [
            {"schedule_id": "s1", "name": "enrich", "cron_expr": "0 6 * * *",
             "enabled": True, "next_run_at": "2026-06-16T06:00:00-06:00"},
        ]})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="schedules", name="enrich"))
        args, kwargs = factory.client.request.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/workflows/schedules")
        assert kwargs["params"] == {"name": "enrich"}
        assert result["schedules"][0]["schedule_id"] == "s1"

    def test_triggers_get(self):
        resp = _mock_response(200, {"count": 1, "triggers": [
            {"trigger_id": "t1", "name": "council_execution",
             "event_pattern": "council_ruling", "enabled": True},
        ]})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="triggers"))
        args, _ = factory.client.request.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/workflows/triggers")
        assert result["triggers"][0]["event_pattern"] == "council_ruling"


# =========================================================================
# 7. Non-200 + transport fail-soft
# =========================================================================
class TestNon200:
    def test_404_returns_status_and_error(self):
        resp = _mock_response(404, {"detail": "no workflow run r9"})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="show", run_id="r9"))
        assert result["success"] is False
        assert result["status"] == 404
        assert "no workflow run" in result["error"]

    def test_409_running_run_revise_conflict(self):
        resp = _mock_response(409, {"detail": "run r1 is currently running"})
        factory = _patched_client(resp)
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(
                action="revise", run_id="r1",
                steps=[{"name": "a", "code_ref": "m.a"}],
            ))
        assert result["success"] is False
        assert result["status"] == 409


class TestTransportFailure:
    def test_httpx_exception_is_failsoft(self):
        import httpx

        factory = _patched_client(raise_exc=httpx.ConnectError("boom"))
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="run", name="enrich"))
        assert result["success"] is False
        assert "could not reach" in result["error"]

    def test_generic_exception_is_failsoft(self):
        factory = _patched_client(raise_exc=RuntimeError("unexpected"))
        with patch("tools.workflow_authoring_tool.httpx.Client", factory):
            result = json.loads(workflow_authoring_tool(action="run", name="enrich"))
        assert result["success"] is False
        assert "could not reach" in result["error"]


# =========================================================================
# 8. Registration
# =========================================================================
class TestRegistration:
    def test_registered_in_registry(self):
        from tools.registry import registry

        assert registry.get_entry("workflow_authoring") is not None

    def test_in_hermes_core_tools(self):
        import toolsets

        assert "workflow_authoring" in toolsets._HERMES_CORE_TOOLS
