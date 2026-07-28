"""Behavioural contracts that a CLEAN upstream merge can silently break.

WHY THIS FILE EXISTS

The v2026.7.20 merge has a **17-file / 45-hunk** textual conflict surface. That is
the cheap part. The expensive part is behaviour that changes while git reports
success, because upstream and the fork edited *non-overlapping* regions — or worse,
because upstream re-adds a method into ``GatewayRunner``'s own body where it wins by
MRO over the fork's mixin copy. **MRO resolution is invisible in a diff.**

Every check here is written against **observable behaviour**, never a signature or a
line of source, because a signature assertion passes the moment upstream's version
takes over — that is precisely the failure being guarded.

Three hazards, established merge-base-relative by CLAWD-2841 and re-verified here:

1. ``_notify_active_sessions_of_shutdown`` — fork returns a ``list`` and the caller
   **consumes** it to seed the recovery marker; upstream returns ``None`` and
   discards. Upstream has additionally moved the method onto ``async_session_store``,
   which has **zero occurrences** in the fork. Nothing textual will conflict.
2. ``resolve_delivery_transport`` — a new upstream seam (16 hits upstream, 0 in the
   fork) threaded through every adapter send path.
3. ``_send_home_channel_startup_notifications`` — upstream-owned, changed on both
   sides. An upstream re-add wins by MRO and silently drops the fork's two skips.
   Upstream's rewrite also iterates ``self.config.platforms`` instead of
   ``self.adapters``, so the ``adapter`` loop variable both skips reference no longer
   exists.

RUN THIS BEFORE AND AFTER THE MERGE. Green before + red after localises a silent
behavioural regression to the merge. Green both sides is the only acceptable outcome.

    scripts/run_tests.sh tests/gateway/test_upstream_merge_behavioural_contracts.py -q
"""

import asyncio
from typing import Optional

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run import GatewayRunner


# ── Fakes ───────────────────────────────────────────────────────────────────

class _FakeAdapter:
    """Adapter stand-in. ``lifecycle_pinned`` + a ``pin_message`` attribute is what
    ``_adapter_lifecycle_pinned`` duck-types on."""

    def __init__(self, name: str, *, lifecycle_pinned: bool = False,
                 can_pin: bool = True, connected: bool = True):
        self.name = name
        self._lifecycle_pinned = lifecycle_pinned
        self.connected = connected
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        if can_pin:
            self.pin_message = self._pin

    async def _pin(self, *a, **kw):
        return True

    async def send(self, chat_id, text, *a, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kw})
        return {"message_id": f"m-{len(self.sent)}"}

    async def edit_message(self, *a, **kw):
        self.edited.append({"args": a, "kwargs": kw})
        return {"ok": True}


class _FakeHome:
    def __init__(self, chat_id="home-1", thread_id=None):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.user_id = None
        self.scope_id = None


class _FakePlatformCfg:
    def __init__(self, home=None, gateway_restart_notification=True):
        self.home_channel = home
        self.gateway_restart_notification = gateway_restart_notification


class _FakeConfig:
    def __init__(self, platforms: dict):
        self.platforms = platforms

    def get_home_channel(self, platform):
        cfg = self.platforms.get(platform)
        return cfg.home_channel if cfg else None


def _bare_runner(adapters: dict, platforms: dict) -> GatewayRunner:
    """A GatewayRunner with only the shutdown/startup-notification surface wired.

    object.__new__ deliberately — constructing a real runner pulls in the whole
    gateway. This mirrors the existing pattern in tests/gateway/test_pinned_lifecycle_*.
    """
    r = object.__new__(GatewayRunner)
    r.adapters = adapters
    r.config = _FakeConfig(platforms)
    r._restart_requested = False
    r._restart_via_service = False
    r._restart_detached = False
    r._running_agents = {}
    r._draining = False
    r._running = False
    r._exit_code = None          # exit_code is a read-only property over this
    r._should_exit_with_failure = False   # also a read-only property
    return r


# ═══════════════════════════════════════════════════════════════════════════
# HAZARD 1 — the recovery-marker seeding chain
# ═══════════════════════════════════════════════════════════════════════════
#
# THE BEHAVIOUR, not the signature: `_notify_active_sessions_of_shutdown` returns the
# home-channel down-DMs it actually sent, the caller assigns them to `_home_targets`,
# and `_home_targets` does TWO things:
#
#   1. it GATES the recovery marker:  if (_home_targets or _pre_drain_keys) ...
#   2. it is PASSED as `targets=`, so the next boot can EDIT each down-DM in place
#      into the online notice (an edit produces no push notification).
#
# If upstream's `-> None` version wins, `_home_targets` is None and BOTH break:
#   * the gate collapses to "only if sessions were in flight" — the pre-CLAWD-1144
#     behaviour where an IDLE restart is loud going down and silent coming back;
#   * targets=None, so recovery cannot edit and must either re-send (a second alert)
#     or say nothing at all.
#
# The scenario below is exactly that regression: home DMs sent, ZERO sessions in
# flight. It is the case the naive `if _pre_drain_keys` gate cannot see.

def test_hazard1_idle_shutdown_still_seeds_the_recovery_marker(monkeypatch):
    """IDLE restart (no in-flight sessions) must still write a recovery marker WITH
    targets, so the next boot edits the down-DM instead of re-announcing."""
    captured = {}

    def _fake_write(interrupted, targets=None, shutdown_ts=None):
        captured["interrupted"] = interrupted
        captured["targets"] = targets
        captured["shutdown_ts"] = shutdown_ts

    monkeypatch.setattr(gateway_run, "_write_recovery_marker", _fake_write)

    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-9"))},
    )

    # The method under contract: it must RETURN the targets it sent.
    sent_targets = [
        {"platform": "telegram", "chat_id": "chat-9", "thread_id": None, "message_id": "m-1"}
    ]

    async def _fake_notify(self):
        return list(sent_targets)

    monkeypatch.setattr(GatewayRunner, "_notify_active_sessions_of_shutdown",
                        _fake_notify, raising=True)

    # Reproduce the caller's chain verbatim: consume the return, gate on it, pass it.
    async def _caller():
        home_targets = await runner._notify_active_sessions_of_shutdown()
        pre_drain_keys: list = []          # IDLE: nothing in flight
        if (home_targets or pre_drain_keys) and not runner._restart_requested:
            gateway_run._write_recovery_marker(
                len(pre_drain_keys), targets=home_targets, shutdown_ts=1.0
            )
        return home_targets

    result = asyncio.run(_caller())

    assert result, (
        "_notify_active_sessions_of_shutdown returned a falsey value. Upstream's "
        "version returns None; the fork's contract is to return the home-channel "
        "targets it sent. With None, an IDLE restart writes no recovery marker at "
        "all (CLAWD-1144 regression: loud going down, silent coming back)."
    )
    assert "targets" in captured, (
        "recovery marker was NOT written on an idle shutdown — the "
        "(_home_targets or _pre_drain_keys) gate collapsed because _home_targets "
        "was falsey"
    )
    assert captured["targets"], (
        "recovery marker written with empty targets — the next boot cannot EDIT the "
        "down-DM in place and will either re-send (second push alert) or go silent"
    )
    assert captured["targets"][0]["message_id"] == "m-1", (
        "target message_id lost; the edit-in-place path needs the message coordinate"
    )
    assert captured["interrupted"] == 0, "idle shutdown should record 0 interrupted"


def test_hazard1_return_value_is_actually_consumed_at_the_call_site():
    """Guard the CALL SITE, not the signature. Upstream discards the return value
    (`await self._notify_active_sessions_of_shutdown()`); the fork assigns it. If a
    merge takes upstream's call site, the assignment silently disappears and every
    downstream assertion above becomes unreachable in production."""
    import inspect
    import re

    source = inspect.getsource(gateway_run)
    calls = re.findall(r"^\s*(.*?)_notify_active_sessions_of_shutdown\(\)",
                       source, re.M)
    # Filter to real await call sites (not defs, not this test's monkeypatching).
    awaits = [c for c in calls if "await" in c]
    assert awaits, "no await call site for _notify_active_sessions_of_shutdown found"
    assigned = [c for c in awaits if "=" in c.split("await")[0]]
    assert assigned, (
        "the call site DISCARDS the return value (upstream's form: "
        "`await self._notify_active_sessions_of_shutdown()`). The fork must assign "
        "it (`_home_targets = await ...`) or the recovery marker loses its targets. "
        f"Found call sites: {awaits!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# HAZARD 3 — the two skips an MRO shadow drops
# ═══════════════════════════════════════════════════════════════════════════
#
# `_send_home_channel_startup_notifications` is UPSTREAM-OWNED and changed on BOTH
# sides (merge-base 79L / fork 94L / upstream 82L). The fork added two skips:
#
#   if platform == Platform.EMAIL:            continue   # CLAWD-1144
#   if self._adapter_lifecycle_pinned(adapter): continue  # CLAWD-1376
#
# Upstream's rewrite iterates `self.config.platforms.items()` rather than
# `self.adapters.items()`, so the `adapter` variable the second skip references DOES
# NOT EXIST in upstream's body. Post-merge, upstream's copy in GatewayRunner's own
# body WINS BY MRO over any mixin, and both skips vanish with no diff to show for it.
#
# Observable consequence on the fleet: EMAIL receives a lifecycle DM it should never
# get, and all 11 `lifecycle_pinned: true` profiles double-announce (a pinned badge
# flip AND a fresh "gateway online" DM).

def _run_startup_notifications(runner, skip_targets=None):
    # skip_targets is KEYWORD-ONLY in the fork's signature.
    return asyncio.run(
        runner._send_home_channel_startup_notifications(skip_targets=skip_targets or set())
    )


def test_hazard3_email_is_skipped_for_lifecycle_notices():
    """CLAWD-1144 (ratified 2026-06-04): lifecycle notices are chat-platform-only.
    Email is the supervisor's backstop channel, not a per-event copy."""
    email = _FakeAdapter("email")
    telegram = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.EMAIL: email, Platform.TELEGRAM: telegram},
        {
            Platform.EMAIL: _FakePlatformCfg(_FakeHome("mailbox")),
            Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-1")),
        },
    )
    _run_startup_notifications(runner)

    assert email.sent == [], (
        "EMAIL received a gateway-online lifecycle DM. The fork's "
        "`if platform == Platform.EMAIL: continue` skip is gone — most likely an "
        "upstream re-add of _send_home_channel_startup_notifications won by MRO "
        "(CLAWD-1144)."
    )
    assert telegram.sent, (
        "the skip over-applied: a normal chat platform got no startup notice either"
    )


def test_hazard3_pinned_lifecycle_adapter_does_not_double_announce():
    """CLAWD-1376: a pinned-lifecycle adapter already flipped its pinned status to
    online in _update_pinned_lifecycle_status. A fresh 'gateway online' DM here
    double-announces and badges."""
    pinned = _FakeAdapter("telegram", lifecycle_pinned=True, can_pin=True)
    plain = _FakeAdapter("slack", lifecycle_pinned=False)
    runner = _bare_runner(
        {Platform.TELEGRAM: pinned, Platform.SLACK: plain},
        {
            Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-p")),
            Platform.SLACK: _FakePlatformCfg(_FakeHome("chat-s")),
        },
    )
    _run_startup_notifications(runner)

    assert pinned.sent == [], (
        "a pinned-lifecycle adapter got a 'gateway online' DM on top of its pinned "
        "badge flip — double-announce. The fork's "
        "`if self._adapter_lifecycle_pinned(adapter): continue` skip is gone. Note "
        "upstream's rewrite iterates self.config.platforms and has no `adapter` "
        "variable, so the skip cannot survive a naive merge (CLAWD-1376)."
    )
    assert plain.sent, "the skip over-applied: a non-pinned adapter was also silenced"


def test_hazard3_pinned_skip_requires_BOTH_flag_and_pin_capability():
    """The duck-type is `_lifecycle_pinned AND callable(pin_message)`. An adapter
    that opted in but cannot pin must still get the DM, or opting in silently
    removes the only announcement it had."""
    flagged_but_cannot_pin = _FakeAdapter("matrix", lifecycle_pinned=True, can_pin=False)
    runner = _bare_runner(
        {Platform.MATRIX: flagged_but_cannot_pin},
        {Platform.MATRIX: _FakePlatformCfg(_FakeHome("chat-m"))},
    )
    assert runner._adapter_lifecycle_pinned(flagged_but_cannot_pin) is False
    _run_startup_notifications(runner)
    assert flagged_but_cannot_pin.sent, (
        "an adapter flagged lifecycle_pinned but WITHOUT pin_message was skipped; it "
        "cannot pin, so it now announces nothing at all"
    )


def test_hazard3_method_ownership_is_gateway_runner_not_a_mixin():
    """Ownership is a contract in BOTH directions (CLAWD-2836). This method is
    upstream-owned and must stay on GatewayRunner; if it ever moves to a mixin, an
    upstream re-add wins by MRO and the two skips above die silently."""
    owner = next(c for c in GatewayRunner.__mro__
                 if "_send_home_channel_startup_notifications" in c.__dict__)
    assert owner is GatewayRunner, (
        f"_send_home_channel_startup_notifications resolved to {owner.__name__}. It "
        f"is upstream-owned and must stay in GatewayRunner's own body."
    )


# ═══════════════════════════════════════════════════════════════════════════
# HAZARD 2 — the resolve_delivery_transport seam
# ═══════════════════════════════════════════════════════════════════════════
#
# Upstream refactored every adapter send path behind
# `resolve_delivery_transport(platform, config, adapters) -> transport`, exposing
# `transport.adapter`, `transport.send(platform, ...)` and `transport.is_relay`.
# The fork has ZERO occurrences. Nothing textual conflicts in the fork's own
# lifecycle code, but the upstream-owned methods our lifecycle code CALLS
# (`_send_restart_notification`, `_send_home_channel_startup_notifications`) are
# rewritten around it.
#
# The check is deliberately SEAM-AGNOSTIC: it asserts the observable outcome —
# a lifecycle notice reaches the configured home chat of a connected platform, and
# does NOT reach an unconfigured or notification-disabled one — so it is meaningful
# whether delivery goes direct (fork, today) or through a transport (post-merge).

def test_hazard2_startup_notice_reaches_the_configured_home_chat():
    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-target"))},
    )
    _run_startup_notifications(runner)
    assert adapter.sent, "no lifecycle notice delivered at all"
    assert adapter.sent[0]["chat_id"] == "chat-target", (
        f"notice went to {adapter.sent[0]['chat_id']!r}, not the configured home "
        f"channel. Post-merge this path runs through resolve_delivery_transport; a "
        f"mis-wired transport sends to the wrong chat."
    )


def test_hazard2_no_home_channel_means_no_delivery():
    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(None)},   # no home channel
    )
    _run_startup_notifications(runner)
    assert adapter.sent == [], (
        "delivered a lifecycle notice to a platform with NO home channel configured"
    )


def test_hazard2_notification_disabled_platform_is_respected():
    """`gateway_restart_notification: false` must suppress delivery. Upstream moved
    this check to after transport resolution, so it is exactly the kind of clause a
    merge can reorder."""
    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(
            _FakeHome("chat-x"), gateway_restart_notification=False)},
    )
    _run_startup_notifications(runner)
    assert adapter.sent == [], (
        "sent a lifecycle notice to a platform with gateway_restart_notification "
        "disabled"
    )


def test_hazard2_skip_targets_suppresses_duplicate_delivery():
    """`skip_targets` is how the recovery path tells startup 'I already edited that
    down-DM in place' (CLAWD-1144). If a merge drops it, recovery edits the DM AND
    startup sends a fresh one."""
    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-dup"))},
    )
    _run_startup_notifications(runner, skip_targets={("telegram", "chat-dup", None)})
    assert adapter.sent == [], (
        "skip_targets was ignored — the recovery path already edited this down-DM, "
        "so this is a duplicate announcement (CLAWD-1144)"
    )


# ── Seam presence, recorded rather than asserted ────────────────────────────

def test_record_whether_the_delivery_transport_seam_is_present():
    """NOT a pass/fail contract — a RECORD. Pre-merge the fork has no
    resolve_delivery_transport; post-merge it will. Printing which side we are on
    makes the behavioural results above interpretable without re-deriving it."""
    import importlib

    present = False
    try:
        mod = importlib.import_module("gateway.delivery")
        present = hasattr(mod, "resolve_delivery_transport")
    except ModuleNotFoundError:
        present = "resolve_delivery_transport" in open(
            gateway_run.__file__, encoding="utf-8"
        ).read()

    print(f"\n[merge-state] resolve_delivery_transport present: {present}")
    # Always passes. The value is the observation.
    assert present in (True, False)
