"""Fork-only pause/resume contract — restores coverage pruned by upstream v2026.7.30.

WHY THIS FILE EXISTS
--------------------
The v2026.7.30 merge resolved ``tests/gateway/test_platform_reconnect.py`` by
taking a file that dropped 18 of the fork's 38 tests.  The behaviour those 18
guarded is still present in ``gateway/run.py`` / ``gateway/slash_commands.py``
(verified: all 38 fork tests pass against merge source), but on the merge branch
the *tests* are gone, so nothing would catch a future regression.

Two specific holes are re-covered here:

1. ``_handle_platform_command`` — the operator-facing ``/platform`` surface — has
   ZERO tests on the merge branch (the fork had 4).  ``/platform resume`` is the
   documented manual escape hatch out of the "11 gateways stuck paused after a
   WAN blip" state, so an untested escape hatch is the worst kind.

2. The *negative* branches of ``_pause_failed_platform`` / ``_resume_paused_platform``
   (not-queued, not-paused).  The merge keeps only the happy paths.  Without the
   negative branches, a regression that made ``_resume_paused_platform`` return a
   truthy value unconditionally would report "resumed" for a platform it never
   touched.

3. COMPOSITION.  The merge's surviving half-open tests exercise the pause API and
   the watcher *separately* — the watcher tests hand-build
   ``{"next_retry": time.monotonic() - 1, "paused": True}`` rather than calling
   ``_pause_failed_platform``.  That means no existing test would notice if the
   pause API and the watcher stopped agreeing (e.g. a key rename, or a sign
   flip).  ``TestPauseWatcherComposition`` drives the real pause API and then the
   real watcher, so the two halves are pinned together.

The fork-only invariant under all of this: ``gateway/run.py``'s reconnect watcher
deliberately does NOT carry upstream's ``if info.get("paused"): continue``.  A
paused platform is half-open, not dead — see ``_PAUSE_HALFOPEN_PROBE_SEC``.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner


class _StubAdapter(BasePlatformAdapter):
    """Adapter whose connect() outcome the test controls."""

    def __init__(self, *, platform=Platform.TELEGRAM, succeed=True):
        super().__init__(PlatformConfig(enabled=True, token="test"), platform)
        self._succeed = succeed
        self.connect_calls: list[bool] = []

    async def connect(self, *, is_reconnect: bool = False):
        self.connect_calls.append(is_reconnect)
        return self._succeed

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_runner():
    """Minimal GatewayRunner via object.__new__ — mirrors the pattern already
    used by tests/gateway/test_platform_reconnect.py."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.session_store = MagicMock()
    return runner


def _event(content: str):
    ev = MagicMock()
    ev.content = content
    return ev


def _queued(**overrides):
    info = {
        "config": PlatformConfig(enabled=True, token="t"),
        "attempts": 3,
        "next_retry": time.monotonic() + 30,
    }
    info.update(overrides)
    return info


async def _run_watcher_one_pass(runner):
    """Drive exactly one pass of the real reconnect watcher then stop it.

    ``_platform_reconnect_watcher`` sleeps before and between passes; we let the
    first two sleeps through and then clear ``_running`` so the coroutine exits.
    """
    real_sleep = asyncio.sleep
    calls = 0

    async def fake_sleep(_n):
        nonlocal calls
        calls += 1
        if calls > 1:
            runner._running = False
        await real_sleep(0)

    runner._running = True
    with patch("asyncio.sleep", side_effect=fake_sleep):
        await runner._platform_reconnect_watcher()


# ---------------------------------------------------------------------------
# 1. Negative branches of the pause/resume helpers (pruned by upstream)
# ---------------------------------------------------------------------------


class TestPauseResumeNegativeBranches:
    def test_resume_returns_false_when_platform_not_queued(self):
        """`/platform resume` on a platform that was never queued must report
        failure, not a phantom success."""
        runner = _make_runner()
        assert runner._resume_paused_platform(Platform.TELEGRAM) is False

    def test_resume_returns_false_when_queued_but_not_paused(self):
        """A platform that is retrying (not paused) is not 'resumable' — the
        operator must not be told it was resumed."""
        runner = _make_runner()
        runner._failed_platforms[Platform.TELEGRAM] = _queued()
        assert runner._resume_paused_platform(Platform.TELEGRAM) is False
        # and the retry schedule must be left alone
        assert runner._failed_platforms[Platform.TELEGRAM]["attempts"] == 3

    def test_pause_is_a_noop_when_platform_not_queued(self):
        """Pausing something that is not in the retry queue must not fabricate
        a queue entry — a fabricated entry would make the watcher try to
        reconnect a platform that has no config."""
        runner = _make_runner()
        runner._pause_failed_platform(Platform.TELEGRAM, reason="manual")
        assert Platform.TELEGRAM not in runner._failed_platforms

    def test_resume_schedules_an_immediate_retry_not_a_probe_interval(self):
        """Resume means *now*, not "at the next half-open probe".  If resume
        re-armed the slow probe instead, the operator's manual intervention
        would appear to do nothing for up to _PAUSE_HALFOPEN_PROBE_SEC."""
        runner = _make_runner()
        runner._failed_platforms[Platform.TELEGRAM] = _queued(
            paused=True, pause_reason="WAN blip", next_retry=float("inf")
        )
        before = time.monotonic()
        assert runner._resume_paused_platform(Platform.TELEGRAM) is True
        info = runner._failed_platforms[Platform.TELEGRAM]
        # Due immediately — strictly less than one probe interval away.
        assert info["next_retry"] <= before + 1.0, (
            f"resume must schedule an immediate retry; next_retry is "
            f"{info['next_retry'] - before:.1f}s in the future"
        )


# ---------------------------------------------------------------------------
# 2. The operator-facing /platform command (zero coverage on the merge branch)
# ---------------------------------------------------------------------------


class TestPlatformCommandSurface:
    @pytest.mark.asyncio
    async def test_resume_command_unpauses_and_says_so(self):
        runner = _make_runner()
        runner._failed_platforms[Platform.WHATSAPP] = _queued(
            attempts=10, next_retry=float("inf"), paused=True, pause_reason="x"
        )
        out = await runner._handle_platform_command(_event("/platform resume whatsapp"))
        assert "resum" in out.lower(), out
        assert runner._failed_platforms[Platform.WHATSAPP]["paused"] is False

    @pytest.mark.asyncio
    async def test_pause_command_pauses_a_queued_platform(self):
        runner = _make_runner()
        runner._failed_platforms[Platform.WHATSAPP] = _queued()
        out = await runner._handle_platform_command(_event("/platform pause whatsapp"))
        assert "paused" in out.lower(), out
        assert runner._failed_platforms[Platform.WHATSAPP]["paused"] is True

    @pytest.mark.asyncio
    async def test_resume_command_on_unqueued_platform_does_not_claim_success(self):
        runner = _make_runner()
        out = await runner._handle_platform_command(_event("/platform resume whatsapp"))
        assert "nothing to resume" in out.lower(), out

    @pytest.mark.asyncio
    async def test_unknown_platform_name_is_rejected(self):
        runner = _make_runner()
        out = await runner._handle_platform_command(
            _event("/platform pause notarealplatform")
        )
        assert "Unknown platform" in out, out

    @pytest.mark.asyncio
    async def test_list_surfaces_paused_platforms_with_the_resume_hint(self):
        """The stuck-paused state must be *visible* and must tell the operator
        how to get out of it — that is the whole recovery affordance."""
        runner = _make_runner()
        runner.adapters[Platform.DISCORD] = _StubAdapter(platform=Platform.DISCORD)
        runner._failed_platforms[Platform.WHATSAPP] = _queued(
            attempts=10, next_retry=float("inf"), paused=True, pause_reason="not paired"
        )
        out = await runner._handle_platform_command(_event("/platform list"))
        assert "discord" in out
        assert "whatsapp" in out
        assert "PAUSED" in out
        assert "not paired" in out
        assert "/platform resume whatsapp" in out, (
            "a paused platform must carry its own recovery instruction"
        )


# ---------------------------------------------------------------------------
# 3. Composition: the pause API and the watcher must agree
# ---------------------------------------------------------------------------


class TestPauseWatcherComposition:
    def test_pause_arms_next_retry_at_exactly_one_probe_interval(self):
        """Pins the re-arm to the fork-only ``_PAUSE_HALFOPEN_PROBE_SEC``
        constant, not merely to "finite".  The merge's surviving test asserts
        only ``!= float("inf")``, which a 1-year interval would satisfy while
        being operationally identical to never retrying."""
        runner = _make_runner()
        runner._failed_platforms[Platform.TELEGRAM] = _queued()
        before = time.monotonic()
        runner._pause_failed_platform(Platform.TELEGRAM, reason="manual")
        after = time.monotonic()
        armed = runner._failed_platforms[Platform.TELEGRAM]["next_retry"]
        probe = gateway_run._PAUSE_HALFOPEN_PROBE_SEC
        assert before + probe <= armed <= after + probe, (
            f"next_retry must be one _PAUSE_HALFOPEN_PROBE_SEC ({probe}s) out; "
            f"got {armed - before:.1f}s"
        )

    @pytest.mark.asyncio
    async def test_platform_paused_through_the_real_api_still_self_heals(self, monkeypatch):
        """END-TO-END re-arm: pause via the real ``_pause_failed_platform`` (not a
        hand-built dict), let the probe interval elapse, and require the real
        watcher to probe and recover it with no operator action.

        This is the recorded regression — "11 gateways stuck paused after a WAN
        blip".  Upstream v2026.7.30 skips paused platforms outright; the fork
        deliberately does not.
        """
        runner = _make_runner()
        runner._sync_voice_mode_state_to_adapter = MagicMock()
        runner._failed_platforms[Platform.TELEGRAM] = _queued(attempts=10)

        # Collapse the probe interval so "the interval elapsed" is true
        # immediately.  The real constant's *value* is pinned by the test above.
        monkeypatch.setattr(gateway_run, "_PAUSE_HALFOPEN_PROBE_SEC", 0)
        runner._pause_failed_platform(Platform.TELEGRAM, reason="WAN blip")
        assert runner._failed_platforms[Platform.TELEGRAM]["paused"] is True

        adapter = _StubAdapter(succeed=True)
        with patch.object(runner, "_create_adapter", return_value=adapter):
            with patch("gateway.run.build_channel_directory", create=True):
                await _run_watcher_one_pass(runner)

        assert adapter.connect_calls, (
            "watcher never probed the paused platform — the half-open re-arm is "
            "gone (upstream's `if info.get('paused'): continue` is back?)"
        )
        assert adapter.connect_calls[0] is True, (
            "half-open probe must connect with is_reconnect=True so the "
            "platform's offline update queue is preserved"
        )
        assert Platform.TELEGRAM not in runner._failed_platforms, (
            "a successful half-open probe must clear the failed/paused entry"
        )
        assert Platform.TELEGRAM in runner.adapters
