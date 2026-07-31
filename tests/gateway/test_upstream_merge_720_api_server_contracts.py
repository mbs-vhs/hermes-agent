"""Behavioural contracts for the API server across the v2026.7.20 merge.

WHY THIS FILE EXISTS

``tests/gateway/test_upstream_merge_behavioural_contracts.py`` is the gate for
that merge, but it is a LIFECYCLE/DELIVERY gate — shutdown recovery markers,
startup notices, delivery transport. It never imports
``gateway.platforms.api_server`` and therefore says nothing about the two
surfaces where the v2026.7.20 conflicts are hardest and where a wrong
resolution is silent:

1. **Run concurrency.** The fork carries CLAWD-1923: count IN-FLIGHT runs, never
   retained ``_run_streams`` entries. Its reason is a specific live client — the
   agent-meeting-space bus POSTs ``/v1/runs`` and tracks completion out-of-band,
   never draining ``/events`` — so retained stream queues accumulate for the
   whole TTL window while the runs behind them are long finished. Counting
   queues wedges that client at 429 after ``_max_concurrent_runs`` *completed*
   runs. Upstream independently arrived at the same rule via
   ``active_agent_work_count()`` plus a contextvar reservation. Whether
   upstream's version also closes the wedge is not arguable — it is measurable,
   and this file measures it.

2. **HTTP routing.** Upstream replaced ~35 open-coded ``router.add_*`` calls
   with ``_http_route_table()`` + a ``/p/<profile>/`` mirror loop. The fork's
   routes are not upstream's: ``/v1/runs/{run_id}/clarify`` exists only here.
   A route silently dropped in that swap produces a 404 at runtime and NOTHING
   in the test suite notices — the existing api-server tests build their own
   ``web.Application`` and register routes by hand (see ``_create_app`` in
   ``tests/gateway/test_api_server.py``), so they cannot see ``connect()``'s
   real registration at all.

These are contracts, not change-detectors: each asserts an observable outcome
(a 429 or not; a path reachable or not), so it stays meaningful whether the
implementation is the fork's, upstream's, or a future third one.

    scripts/run_tests.sh tests/gateway/test_upstream_merge_720_api_server_contracts.py -q
"""

import asyncio

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


# ═══════════════════════════════════════════════════════════════════════════
# The fire-and-forget wedge (CLAWD-1923)
# ═══════════════════════════════════════════════════════════════════════════


def test_retained_streams_of_completed_runs_do_not_consume_the_cap():
    """THE contract. A fire-and-forget client must never be wedged at 429.

    Scenario, exactly as it occurs with the agent-meeting-space bus: N runs were
    POSTed to /v1/runs and all N completed. Nobody consumed /events, so N stream
    queues are still retained (they live until drained or until the 300s orphan
    reaper sweeps them). Their tasks are done and popped. Nothing is running.

    The next POST must be admitted. If the cap counts retained queues, this
    returns 429 and the client is dead for the rest of the TTL window.
    """
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 2
    adapter._inflight_agent_runs = 0
    # Two completed runs whose SSE streams were never drained.
    adapter._run_streams = {"done-1": asyncio.Queue(), "done-2": asyncio.Queue()}
    adapter._run_streams_created = {"done-1": 0.0, "done-2": 0.0}
    # Completion pops the task, so the live-task map is empty.
    adapter._active_run_tasks = {}

    assert adapter._concurrency_limited_response() is None, (
        "the concurrency cap counted RETAINED stream queues from runs that have "
        "already finished. A client that POSTs /v1/runs and never drains /events "
        "(the agent-meeting-space bus) is now permanently 429 until the 300s "
        "orphan reaper fires. This is CLAWD-1923."
    )


def test_a_done_task_still_in_the_map_does_not_consume_the_cap():
    """Same wedge, one tick earlier: the task finished but has not been popped.

    ``_active_run_tasks`` is only cleaned in the run's own finally/reaper, so
    there is a window where a completed task is still a dict value. Counting map
    SIZE rather than not-done-ness reintroduces the wedge in that window.
    """
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 1
    adapter._inflight_agent_runs = 0
    adapter._run_streams = {}

    async def _drive():
        async def _already_finished():
            return None

        task = asyncio.create_task(_already_finished())
        await task
        assert task.done()
        adapter._active_run_tasks = {"finished": task}
        return adapter._concurrency_limited_response()

    assert asyncio.run(_drive()) is None, (
        "a COMPLETED task still present in _active_run_tasks consumed a "
        "concurrency slot — the cap must count not-done tasks, not map size"
    )


def test_live_runs_still_hit_the_cap():
    """The other direction: the cap must still actually cap.

    A test that only proves "never 429" would be satisfied by deleting the
    limiter. Live (not-done) tasks must consume slots.
    """
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 2
    adapter._inflight_agent_runs = 0
    adapter._run_streams = {}

    async def _drive():
        blocker = asyncio.Event()

        async def _live():
            await blocker.wait()

        tasks = [asyncio.create_task(_live()) for _ in range(2)]
        adapter._active_run_tasks = {f"live-{i}": t for i, t in enumerate(tasks)}
        try:
            return adapter._concurrency_limited_response()
        finally:
            blocker.set()
            await asyncio.gather(*tasks)

    resp = asyncio.run(_drive())
    assert resp is not None and resp.status == 429, (
        "two LIVE /v1/runs tasks did not trip a cap of 2 — the limiter is not "
        "limiting"
    )


def test_non_streaming_runs_share_the_same_cap():
    """/v1/chat/completions and /v1/responses count against the same budget."""
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 2
    adapter._inflight_agent_runs = 2
    adapter._run_streams = {}
    adapter._active_run_tasks = {}

    resp = adapter._concurrency_limited_response()
    assert resp is not None and resp.status == 429
    assert resp.headers.get("Retry-After"), "429 must be retryable"


def test_zero_disables_the_cap():
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 0
    adapter._inflight_agent_runs = 9999
    assert adapter._concurrency_limited_response() is None


def _run_under_real_admission(adapter):
    """Call the cap check from inside the REAL admission decorator.

    Deliberately not a hand-rolled stand-in: ``_admit_api_agent_request`` is
    what sets the reservation contextvar in production, and a replica of it in
    the test would keep passing after the real one stopped setting it.
    """
    from gateway.platforms.api_server import _admit_api_agent_request

    @_admit_api_agent_request
    async def _probe(self, request):
        return (self._pending_agent_requests, self._concurrency_limited_response())

    class _FakeRequest:
        headers: dict = {}
        method = "POST"
        path = "/v1/runs"
        remote = "127.0.0.1"
        query: dict = {}

    return asyncio.run(_probe(adapter, _FakeRequest()))


def test_an_admitted_request_does_not_consume_its_own_last_slot():
    """Upstream's reservation contract (v2026.7.20).

    A request is counted in ``_pending_agent_requests`` from admission until it
    reaches agent bookkeeping, so the shutdown drain cannot lose it between its
    first await and _run_agent(). The concurrency check runs INSIDE that window,
    so without discounting its own reservation the very first request would 429
    itself at a cap of 1.
    """
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 1
    adapter._inflight_agent_runs = 0
    adapter._run_streams = {}
    adapter._active_run_tasks = {}

    pending, resp = _run_under_real_admission(adapter)
    assert pending == 1, "admission did not register a pending-work reservation"
    assert resp is None, (
        "a request 429'd itself: its own admission reservation consumed the "
        "only slot"
    )


def test_a_second_admitted_request_does_hit_the_cap():
    """The reservation discount is for the CALLER only, not a blanket -1.

    Two requests admitted concurrently at a cap of 1: the second must be
    refused. If the discount were applied unconditionally the cap would be
    silently off by one for every concurrent caller.
    """
    adapter = _make_adapter()
    adapter._max_concurrent_runs = 1
    adapter._inflight_agent_runs = 0
    adapter._run_streams = {}
    adapter._active_run_tasks = {}
    # A sibling request is already admitted and still pending.
    adapter._pending_agent_requests = 1

    pending, resp = _run_under_real_admission(adapter)
    assert pending == 2, "expected the sibling's reservation plus this caller's"
    assert resp is not None and resp.status == 429, (
        "a second concurrently-admitted request was let through at a cap of 1 — "
        "only the CALLER's own reservation may be discounted"
    )


def test_detached_background_work_stays_visible_to_the_drain():
    """``_reserve_pending_api_work`` is the OTHER reservation (cron fire).

    It exists so work handed to a background task stays counted after the
    handler returns 202. Detaching must not drop the count; the task's done
    callback owns the release.
    """
    from gateway.platforms.api_server import (
        _release_pending_api_work,
        _reserve_pending_api_work,
    )

    adapter = _make_adapter()
    adapter._pending_agent_requests = 0

    with _reserve_pending_api_work(adapter) as reservation:
        assert adapter._pending_agent_requests == 1
        reservation["detached"] = True
    assert adapter._pending_agent_requests == 1, (
        "a DETACHED reservation was released when the handler returned — the "
        "shutdown drain can no longer see the background work it handed off"
    )

    _release_pending_api_work(adapter, reservation)
    assert adapter._pending_agent_requests == 0
    _release_pending_api_work(adapter, reservation)
    assert adapter._pending_agent_requests == 0, "release must be idempotent"


def test_shutdown_drain_sees_the_same_work_the_cap_sees():
    """``active_agent_work_count`` is the single accounting of live agent work.

    The drain and the cap disagreeing is how a request slips through a shutdown.
    Retained stream queues must be invisible to BOTH: they are transport state
    and can outlive the run they belonged to.
    """
    adapter = _make_adapter()
    adapter._inflight_agent_runs = 0
    adapter._pending_agent_requests = 0
    adapter._active_run_tasks = {}
    adapter._run_streams = {"orphan": asyncio.Queue()}

    assert adapter.active_agent_work_count() == 0, (
        "the shutdown drain counts a retained SSE queue as live agent work; it "
        "would refuse to finish draining until the orphan reaper fires"
    )


# ═══════════════════════════════════════════════════════════════════════════
# HTTP route registration
# ═══════════════════════════════════════════════════════════════════════════
#
# Upstream's v2026.7.20 refactor moved every route into _http_route_table(). The
# risk is silent omission, so pin the full set the fork must serve — including
# the fork-only clarify route, which upstream has no reason to carry.

_REQUIRED_ROUTES = {
    ("GET", "/health"),
    ("GET", "/health/detailed"),
    ("GET", "/v1/health"),
    ("GET", "/v1/models"),
    ("GET", "/v1/capabilities"),
    ("GET", "/v1/skills"),
    ("GET", "/v1/toolsets"),
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions"),
    ("GET", "/api/sessions/{session_id}"),
    ("PATCH", "/api/sessions/{session_id}"),
    ("DELETE", "/api/sessions/{session_id}"),
    ("GET", "/api/sessions/{session_id}/messages"),
    ("POST", "/api/sessions/{session_id}/fork"),
    ("POST", "/api/sessions/{session_id}/chat"),
    ("POST", "/api/sessions/{session_id}/chat/stream"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/responses/{response_id}"),
    ("DELETE", "/v1/responses/{response_id}"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs/{job_id}"),
    ("PATCH", "/api/jobs/{job_id}"),
    ("DELETE", "/api/jobs/{job_id}"),
    ("POST", "/api/jobs/{job_id}/pause"),
    ("POST", "/api/jobs/{job_id}/resume"),
    ("POST", "/api/jobs/{job_id}/run"),
    ("POST", "/v1/runs"),
    ("GET", "/v1/runs/{run_id}"),
    ("GET", "/v1/runs/{run_id}/events"),
    ("POST", "/v1/runs/{run_id}/approval"),
    # FORK-ONLY. Upstream has no clarify surface; a "take upstream's route
    # table" resolution drops it and /v1/runs/{run_id}/clarify starts 404ing
    # while _handle_run_clarify sits in the file looking perfectly healthy.
    ("POST", "/v1/runs/{run_id}/clarify"),
    ("POST", "/v1/runs/{run_id}/stop"),
}


def _route_pairs(adapter) -> set:
    return {(method, path) for method, path, _handler in adapter._http_route_table()}


def test_every_required_route_is_in_the_table():
    missing = _REQUIRED_ROUTES - _route_pairs(_make_adapter())
    assert not missing, (
        f"routes dropped from _http_route_table(): {sorted(missing)}. Each is a "
        f"404 at runtime with a live handler still present in api_server.py."
    )


def test_every_route_resolves_to_a_real_bound_handler():
    """A table row pointing at a missing attribute would fail only at connect()."""
    adapter = _make_adapter()
    for method, path, handler in adapter._http_route_table():
        assert callable(handler), f"{method} {path} handler is not callable"
        assert getattr(handler, "__self__", None) is adapter, (
            f"{method} {path} handler is not bound to the adapter: {handler!r}"
        )


def test_the_clarify_route_is_wired_to_the_clarify_handler():
    """Route→handler identity, not just presence: a copy-paste that points
    /clarify at _handle_run_approval registers fine and is silently wrong."""
    adapter = _make_adapter()
    table = {
        (m, p): h for m, p, h in adapter._http_route_table()
    }
    handler = table.get(("POST", "/v1/runs/{run_id}/clarify"))
    assert handler is not None, "the fork's clarify route is not registered"
    assert handler == adapter._handle_run_clarify, (
        f"the clarify route points at {handler!r}, not _handle_run_clarify"
    )


@pytest.mark.parametrize("method,path", sorted(_REQUIRED_ROUTES))
def test_connect_registers_native_and_profile_mirrored_routes(method, path):
    """``connect()`` registers each row twice: native and /p/<profile>/ mirror.

    Asserted against a real ``web.Application`` router built from the same table
    ``connect()`` uses, so the multiplex mirror cannot quietly stop covering a
    route the fork added.
    """
    from aiohttp import web

    adapter = _make_adapter()
    app = web.Application()
    for m, p, h in adapter._http_route_table():
        app.router.add_route(m, p, h)
        app.router.add_route(m, f"/p/{{profile}}{p}", h)

    registered = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.resource is not None
    }
    assert (method, path) in registered
    assert (method, f"/p/{{profile}}{path}") in registered, (
        f"{method} {path} has no /p/<profile>/ multiplex mirror"
    )
