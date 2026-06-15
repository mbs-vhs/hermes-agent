"""Workflow authoring tool — let an agent (or Minerva) stand up + self-correct a
durable workflow from plain conversation (CLAWD-1709, EPIC CLAWD-1707).

Wraps clawd's ``/workflows/*`` authoring surface (the P1 verb layer over the
durable workflow kernel) so a chat agent can author, run, inspect, and
self-correct a workflow WITHOUT writing engine calls — the verbs:

    define  -> register/version a workflow (an ordered step graph of clawd
               registered ``code_ref`` callables)
    run     -> start + enqueue a run of a defined workflow
    revise  -> self-correct a run: propose a corrected step graph; clawd diffs
               old↔new, keeps the unchanged upstream checkpoints (free replay),
               and re-queues only the changed step + everything downstream
    show    -> read one run: status, current step, per-step checkpoints,
               cost/latency, result/error (the replay / time-travel view)
    tail    -> list recent runs (optionally scoped to a workflow name / status)

This is the conversational-authoring PRIMARY path from the build plan: "any agent
(or Minerva) creates a workflow from a direct chat — define/run/revise exposed as
a tool." Step CODE is clawd-side (registered Python callables); the agent composes
existing registered steps into a graph — it does NOT supply executable code (that
is a P3 sandbox concern, unreachable here).

Auth mirrors the other clawd-backed tools (e.g. mail_compose_tool): the tool is
available only when ``CLAWD_API_AUTH_TOKEN`` is configured for the gateway, and
calls clawd on loopback (``CLAWD_BASE_URL``, default 127.0.0.1:8000) with that
bearer. No second auth path; nothing is exposed publicly.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from tools.registry import registry

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT_SECONDS = 30.0


def _base_url() -> str:
    return (os.environ.get("CLAWD_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    bearer = os.environ.get("CLAWD_API_AUTH_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def check_workflow_authoring_requirements() -> bool:
    """Available only when clawd's bearer is configured for the gateway."""
    return bool(os.environ.get("CLAWD_API_AUTH_TOKEN", "").strip())


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra})


def _http_error(resp: httpx.Response) -> str:
    detail: Any = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:  # noqa: BLE001
        detail = resp.text[:200]
    return _err(detail or "request failed", status=resp.status_code)


def _request(method: str, path: str, *, json_body: dict | None = None, params: dict | None = None):
    """One clawd call. Returns (data, error_json). Exactly one is non-None."""
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, headers=_headers()) as client:
            resp = client.request(
                method, f"{_base_url()}{path}", json=json_body, params=params
            )
    except Exception as exc:  # noqa: BLE001 — clean tool error, never crash the turn
        return None, _err(f"could not reach the workflow substrate: {type(exc).__name__}")
    if resp.status_code != 200:
        return None, _http_error(resp)
    return resp.json(), None


def _normalize_steps(steps: Any) -> list[dict[str, Any]] | None:
    """Accept a list of {name, code_ref[, assigned_agent]} dicts (or a JSON string
    of the same). Returns None on an unusable shape."""
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(steps, list) or not steps:
        return None
    out: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict) or "name" not in raw or "code_ref" not in raw:
            return None
        entry: dict[str, Any] = {
            "name": str(raw["name"]),
            "code_ref": str(raw["code_ref"]),
        }
        if raw.get("assigned_agent"):
            entry["assigned_agent"] = str(raw["assigned_agent"])
        out.append(entry)
    return out


def _coerce_input(value: Any) -> dict[str, Any] | None:
    """Accept an input dict or a JSON-object string; None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def workflow_authoring_tool(
    action: str,
    name: str | None = None,
    steps: Any = None,
    input: Any = None,
    run_id: str | None = None,
    version: int | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> str:
    """Author / run / inspect / self-correct a durable clawd workflow. Returns JSON."""
    action = (action or "").strip().lower()

    if action == "define":
        if not name or not str(name).strip():
            return _err("'name' is required for define")
        graph = _normalize_steps(steps)
        if graph is None:
            return _err(
                "'steps' must be a non-empty list of {name, code_ref[, assigned_agent]} "
                "objects (each code_ref must be a clawd-registered step)"
            )
        data, error = _request(
            "POST", "/workflows/define", json_body={"name": str(name).strip(), "steps": graph}
        )
        if error:
            return error
        return json.dumps({
            "success": True,
            "action": "define",
            "name": data.get("name"),
            "version": data.get("version"),
            "def_id": data.get("def_id"),
            "message": f"Defined workflow {data.get('name')!r} v{data.get('version')}.",
        })

    if action == "run":
        if not name or not str(name).strip():
            return _err("'name' is required for run")
        body: dict[str, Any] = {"name": str(name).strip()}
        coerced = _coerce_input(input)
        if coerced is not None:
            body["input"] = coerced
        if version is not None:
            body["version"] = int(version)
        data, error = _request("POST", "/workflows/run", json_body=body)
        if error:
            return error
        return json.dumps({
            "success": True,
            "action": "run",
            "run_id": data.get("run_id"),
            "name": data.get("name"),
            "message": (
                f"Started run {data.get('run_id')} of {data.get('name')!r}. "
                "Use action='show' with this run_id to follow it."
            ),
        })

    if action == "revise":
        if not run_id or not str(run_id).strip():
            return _err("'run_id' is required for revise")
        graph = _normalize_steps(steps)
        if graph is None:
            return _err(
                "'steps' must be a non-empty list of {name, code_ref[, assigned_agent]} "
                "objects — the corrected step graph"
            )
        data, error = _request(
            "POST", f"/workflows/{str(run_id).strip()}/revise", json_body={"steps": graph}
        )
        if error:
            return error
        return json.dumps({
            "success": True,
            "action": "revise",
            "new_version": data.get("version"),
            "dirty_steps": data.get("dirty_steps"),
            "unchanged_steps": data.get("unchanged_steps"),
            "message": (
                f"Revised to v{data.get('version')}: steps {data.get('dirty_steps')} re-run; "
                f"steps {data.get('unchanged_steps')} replay from cache."
            ),
        })

    if action == "show":
        if not run_id or not str(run_id).strip():
            return _err("'run_id' is required for show")
        data, error = _request("GET", f"/workflows/runs/{str(run_id).strip()}")
        if error:
            return error
        run = data.get("run") or {}
        return json.dumps({
            "success": True,
            "action": "show",
            "run": {
                "run_id": run.get("run_id"),
                "name": run.get("name"),
                "def_version": run.get("def_version"),
                "status": run.get("status"),
                "current_step": run.get("current_step"),
                "result": run.get("result"),
                "error": run.get("error"),
                "steps": [
                    {
                        "step_name": s.get("step_name"),
                        "attempt": s.get("attempt"),
                        "status": s.get("status"),
                        "code_ref": s.get("code_ref"),
                        "latency_ms": s.get("latency_ms"),
                        "cost_usd": s.get("cost_usd"),
                    }
                    for s in (run.get("steps") or [])
                ],
            },
        })

    if action == "tail":
        params: dict[str, Any] = {}
        if name and str(name).strip():
            params["name"] = str(name).strip()
        if status and str(status).strip():
            params["status"] = str(status).strip()
        if limit is not None:
            params["limit"] = int(limit)
        data, error = _request("GET", "/workflows/runs", params=params or None)
        if error:
            return error
        return json.dumps({
            "success": True,
            "action": "tail",
            "count": data.get("count"),
            "runs": [
                {
                    "run_id": r.get("run_id"),
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "current_step": r.get("current_step"),
                    "def_version": r.get("def_version"),
                }
                for r in (data.get("runs") or [])
            ],
        })

    return _err(
        f"unknown action {action!r}; expected one of: define, run, revise, show, tail"
    )


WORKFLOW_AUTHORING_SCHEMA = {
    "name": "workflow_authoring",
    "description": (
        "Author, run, inspect, and self-correct a DURABLE workflow on clawd. Use when "
        "Morgan asks you to 'set up a workflow', 'run that workflow', 'fix the broken "
        "step and re-run', or 'show me how run X is going'. A workflow is an ordered "
        "list of steps; each step is a clawd-registered code_ref (you compose existing "
        "registered steps — you do NOT write code). Verbs (the 'action' arg):\n"
        "- define: register a workflow. Provide 'name' and 'steps' (a list of "
        "{name, code_ref} objects).\n"
        "- run: start a run. Provide 'name' (and optional 'input' object).\n"
        "- revise: self-correct a run. Provide 'run_id' and the corrected 'steps'. "
        "clawd keeps the unchanged upstream steps' results (free replay) and re-runs "
        "only the changed step and everything after it.\n"
        "- show: inspect one run. Provide 'run_id'. Returns status, current step, each "
        "step's checkpoint (status/cost/latency), and the result or error.\n"
        "- tail: list recent runs. Optional 'name' and/or 'status' to scope.\n"
        "Runs execute durably in the background — after 'run' or 'revise', use 'show' "
        "to follow progress to done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["define", "run", "revise", "show", "tail"],
                "description": "Which verb to perform.",
            },
            "name": {
                "type": "string",
                "description": "Workflow name (for define / run, and to scope tail).",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Ordered step graph (for define / revise): a list of "
                    "{name, code_ref, assigned_agent?} objects. Each code_ref must be a "
                    "step registered in clawd."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "code_ref": {"type": "string"},
                        "assigned_agent": {"type": "string"},
                    },
                    "required": ["name", "code_ref"],
                },
            },
            "input": {
                "type": "object",
                "description": "Run input payload (for run) — the first step's input.",
            },
            "run_id": {
                "type": "string",
                "description": "The run to revise / show.",
            },
            "version": {
                "type": "integer",
                "description": "Optional: pin a definition version for run (default: latest).",
            },
            "status": {
                "type": "string",
                "description": "Optional: scope tail to a run status (queued/running/done/failed/...).",
            },
            "limit": {
                "type": "integer",
                "description": "Optional: max runs to return for tail (default 20).",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="workflow_authoring",
    toolset="workflow",
    schema=WORKFLOW_AUTHORING_SCHEMA,
    handler=lambda args, **kw: workflow_authoring_tool(
        action=args.get("action", ""),
        name=args.get("name"),
        steps=args.get("steps"),
        input=args.get("input"),
        run_id=args.get("run_id"),
        version=args.get("version"),
        status=args.get("status"),
        limit=args.get("limit"),
    ),
    check_fn=check_workflow_authoring_requirements,
    requires_env=["CLAWD_API_AUTH_TOKEN"],
    emoji="🛠️",
)
