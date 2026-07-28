"""Behavioural contracts that a CLEAN upstream merge can silently break.

WHY THIS FILE EXISTS

The v2026.7.20 merge has a **17-file / 45-hunk** textual conflict surface. That is
the cheap part. The expensive part is behaviour that changes while git reports
success, because upstream and the fork edited *non-overlapping* regions — or worse,
because upstream re-adds a method into ``GatewayRunner``'s own body where it wins by
MRO over the fork's mixin copy. **MRO resolution is invisible in a diff.**

Checks here are written against **observable behaviour** wherever the behaviour is
reachable, because a signature assertion passes the moment upstream's version takes
over — precisely the failure being guarded.

ONE test is deliberately structural rather than behavioural:
``test_hazard1_every_call_site_consumes_the_return_value``. Whether a caller ASSIGNS
an awaited result or discards it is not observable from outside the function — both
run identically — so it is checked by walking the AST. AST, not a source regex: a
regex cannot distinguish code from comments, and review demonstrated that bypass.

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
from gateway.platforms.base import SendResult
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
        # Returns a SendResult-SHAPED object, not a dict. Production reads
        # `getattr(result, "message_id", None)` — a dict made message_id come back
        # None and the edit-in-place coordinate was silently lost. Caught only once
        # the test started driving the real method instead of a replica.
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kw})
        return SendResult(success=True, message_id=f"m-{len(self.sent)}")

    async def edit_message(self, *a, **kw):
        self.edited.append({"args": a, "kwargs": kw})
        return SendResult(success=True, message_id="edited")


class _FakeHome:
    def __init__(self, chat_id="home-1", thread_id=None):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.user_id = None
        self.scope_id = None


class _FakePlatformCfg:
    # `enabled` is required by upstream's post-merge delivery seam:
    # gateway/delivery.py::resolve_delivery_transport gates on
    # `native_config.enabled` before returning a transport. The fork's pre-merge
    # lifecycle path never consulted it, so this attribute is the fixture
    # modelling a NEW delivery precondition introduced by the merge.
    def __init__(self, home=None, gateway_restart_notification=True, enabled=True):
        self.home_channel = home
        self.gateway_restart_notification = gateway_restart_notification
        self.enabled = enabled


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

def test_hazard1_real_method_returns_the_home_targets_it_sent():
    """Drive the REAL _notify_active_sessions_of_shutdown and assert its RETURN.

    An earlier version of this test monkeypatched both the method AND
    _write_recovery_marker, then re-implemented the caller inside the test — so it
    asserted against its own replica and survived every behavioural mutation of the
    thing it claimed to guard, including `return None` (the hazard itself) and the
    exact CLAWD-1144 gate collapse its docstring named. It was a name-existence
    check wearing a contract docstring. Independent review caught that; this is the
    replacement.

    Nothing is monkeypatched here. The runner is IDLE (no in-flight sessions) with
    one home channel configured, which is precisely the CLAWD-1144 scenario: the
    down-DM is the only thing that happened, so the returned targets are the only
    thing that can seed the recovery marker. `return None` fails this immediately.
    """
    adapter = _FakeAdapter("telegram")
    runner = _bare_runner(
        {Platform.TELEGRAM: adapter},
        {Platform.TELEGRAM: _FakePlatformCfg(_FakeHome("chat-9"))},
    )
    runner.session_store = None
    runner._restart_command_source = None
    runner._cached_session_sources = {}

    targets = asyncio.run(runner._notify_active_sessions_of_shutdown())

    assert isinstance(targets, list), (
        f"_notify_active_sessions_of_shutdown returned {type(targets).__name__}, not a "
        f"list. Upstream's version returns None; the fork's contract is to return the "
        f"home-channel targets it sent, because the caller feeds them to the recovery "
        f"marker so the next boot can EDIT the down-DM in place (CLAWD-1144)."
    )
    assert adapter.sent, "no shutdown down-DM was sent to the configured home channel"
    assert targets, (
        "the down-DM WAS sent but the method returned an empty list — the recovery "
        "marker will have no targets, so the next boot cannot edit the down-DM and "
        "must either re-send (a second push alert) or go silent"
    )
    coord = targets[0]
    assert coord.get("message_id"), (
        f"target is missing message_id, which the edit-in-place path needs: {coord!r}"
    )
    assert str(coord.get("chat_id")) == "chat-9"


def test_hazard1_every_call_site_consumes_the_return_value():
    """Guard the CALL SITE via AST, not a source regex.

    Upstream discards the value (`await self._notify_active_sessions_of_shutdown()`);
    the fork assigns it. A regex over `inspect.getsource` cannot tell code from
    comments — review demonstrated the bypass: discard the value and leave
    `# was: _home_targets = await self...()` nearby, and a regex-based check passes.
    A regex also only required SOME call site to assign, so a second discarding call
    site slipped through. This walks the AST and requires EVERY await of the method
    to be consumed.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gateway_run))
    consumed, discarded = [], []

    for node in ast.walk(tree):
        # A discarded await is an Expr whose value is an Await of this attribute.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await):
            call = node.value.value
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_notify_active_sessions_of_shutdown"):
                discarded.append(node.lineno)
        # A consumed await is an Assign/AnnAssign whose value is such an Await.
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Await):
            call = node.value.value
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_notify_active_sessions_of_shutdown"):
                consumed.append(node.lineno)

    assert consumed, (
        "no call site ASSIGNS the result of _notify_active_sessions_of_shutdown. "
        "The fork must consume it (`_home_targets = await ...`) or the recovery "
        "marker loses its targets."
    )
    assert not discarded, (
        f"call site(s) at line(s) {discarded} DISCARD the return value (upstream's "
        f"form). Every await of this method must be consumed, or the recovery marker "
        f"silently loses its targets on that path."
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
    Email is the supervisor's backstop channel, not a per-event copy.

    THIS IS A WORKING GUARD, verified by mutation: deleting the
    `if platform == Platform.EMAIL: continue` skip from
    _send_home_channel_startup_notifications turns it RED, on both sides of the
    v2026.7.20 merge, and so does substituting upstream's verbatim method body (the
    literal MRO-shadow scenario).

    A previous revision wrongly labelled this NOT EXECUTABLE. That came from a bad
    mutation: a `count=1` regex for the EMAIL skip matched the FIRST occurrence in
    gateway/run.py — the shutdown-side skip in _notify_active_sessions_of_shutdown
    (~L5583) — not the startup-side skip this test guards (~L14343). It removed code
    the test never covered, the test correctly stayed green, and that was misread as
    vacuity. Target mutations by AST span, never by regex ordinal.

    The same revision claimed EMAIL was suppressed "incidentally" because
    `_non_conversational_metadata` returns None for it. Also wrong: upstream's send
    block has an `else: await transport.adapter.send(...)` fallback, so a None
    metadata result does not suppress delivery. There is no incidental suppression —
    this skip is the only thing keeping EMAIL quiet.
    """
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

    # POSITIVE CONTROL: without this, a wholesale delivery breakage (every send
    # failing) would leave the assertion above green for the wrong reason. A second
    # platform with a valid config MUST still receive its notice.
    control = _FakeAdapter("telegram")
    runner.adapters[Platform.TELEGRAM] = control
    runner.config.platforms[Platform.TELEGRAM] = _FakePlatformCfg(_FakeHome("chat-ctl"))
    _run_startup_notifications(runner)
    assert control.sent, (
        "control platform received nothing either — delivery is broken wholesale, so "
        "the assertion above proves nothing"
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

    # POSITIVE CONTROL: without this, a wholesale delivery breakage (every send
    # failing) would leave the assertion above green for the wrong reason. A second
    # platform with a valid config MUST still receive its notice.
    control = _FakeAdapter("telegram")
    runner.adapters[Platform.TELEGRAM] = control
    runner.config.platforms[Platform.TELEGRAM] = _FakePlatformCfg(_FakeHome("chat-ctl"))
    _run_startup_notifications(runner)
    assert control.sent, (
        "control platform received nothing either — delivery is broken wholesale, so "
        "the assertion above proves nothing"
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

    # POSITIVE CONTROL: without this, a wholesale delivery breakage (every send
    # failing) would leave the assertion above green for the wrong reason. A second
    # platform with a valid config MUST still receive its notice.
    control = _FakeAdapter("telegram")
    runner.adapters[Platform.TELEGRAM] = control
    runner.config.platforms[Platform.TELEGRAM] = _FakePlatformCfg(_FakeHome("chat-ctl"))
    _run_startup_notifications(runner)
    assert control.sent, (
        "control platform received nothing either — delivery is broken wholesale, so "
        "the assertion above proves nothing"
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
