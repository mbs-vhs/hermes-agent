"""MRO-ownership guard for the lifecycle-notifications mixin (CLAWD-2836).

WHY THIS FILE EXISTS — it guards a failure that is otherwise INVISIBLE.

``gateway/lifecycle_notifications.py`` holds four fork-added methods moved out of
``gateway/run.py`` to shrink the upstream merge conflict surface. Python resolves
attributes by MRO, and **a class's own body always beats a mixin**. So if a future
upstream merge re-adds any of these four names into ``GatewayRunner``'s body, the
upstream version wins and the fork's version is silently shadowed:

  * no exception,
  * no log line,
  * and every existing test still passes, because the tests call
    ``runner._update_pinned_lifecycle_status(...)`` and something answers.

The observable damage lands on the fleet, not in CI: all 11 fleet profiles run
``lifecycle_pinned: true``, so a shadowed pinned-lifecycle method means a status
badge that freezes on a stale green while the gateway is actually down — the
program's named dominant risk (silent degradation) in its purest form.

Precedent: ``tests/gateway/test_kanban_watchers_mixin.py::test_gateway_runner_inherits_mixin``
does the same job for the kanban mixin.

The second half of this file guards the *inverse* error: the three upstream-owned
methods that must NOT have moved. CLAWD-2841 established, merge-base-relative, that
moving them plants exactly the shadow described above — so their staying in
``run.py`` is a contract, not an accident.
"""

import ast
import re
import inspect
from pathlib import Path

import pytest

from gateway.lifecycle_notifications import GatewayLifecycleNotificationsMixin
from gateway.run import GatewayRunner

# The four FORK-ADDED methods that were moved. Each must resolve to the mixin.
MOVED = [
    "_send_post_connect_lifecycle_notifications",
    "_edit_recovery_notifications",
    "_adapter_lifecycle_pinned",
    "_update_pinned_lifecycle_status",
]

# UPSTREAM-OWNED methods that must stay in GatewayRunner's own body. Moving any of
# these is the mistake this test pair exists to catch:
#   _send_restart_notification                 fork body == merge-base (we never
#                                              touched it) -> moving it removes
#                                              zero delta and adds a conflict
#   _send_home_channel_startup_notifications   changed on BOTH sides (79/94/82) ->
#                                              upstream re-add shadows our skips
#   _notify_active_sessions_of_shutdown        fork -> list vs upstream -> None,
#                                              and the fork call site CONSUMES it
MUST_STAY_ON_RUNNER = [
    "_send_restart_notification",
    "_send_home_channel_startup_notifications",
    "_notify_active_sessions_of_shutdown",
]


def _owner(name):
    """The class in GatewayRunner's MRO that actually provides `name`."""
    return next(cls for cls in GatewayRunner.__mro__ if name in cls.__dict__)


def test_gateway_runner_inherits_the_mixin():
    assert GatewayLifecycleNotificationsMixin in GatewayRunner.__mro__


@pytest.mark.parametrize("name", MOVED)
def test_moved_methods_resolve_to_the_mixin_not_a_shadow(name):
    """The load-bearing assertion. If this fails with
    'resolved to GatewayRunner', an upstream merge re-added the method into
    run.py's class body and the fork's version is now dead code — restore the
    single definition in the mixin rather than deleting this test."""
    owner = _owner(name)
    assert owner is GatewayLifecycleNotificationsMixin, (
        f"{name} resolved to {owner.__name__}, expected "
        f"GatewayLifecycleNotificationsMixin. A class body beats a mixin in the "
        f"MRO, so this means the fork's version is silently shadowed."
    )


@pytest.mark.parametrize("name", MOVED)
def test_moved_methods_are_defined_exactly_once_in_the_mro(name):
    """Two definitions means one is dead. Which one is dead depends on MRO order,
    so this catches the shadow even if the ordering later changes."""
    owners = [cls.__name__ for cls in GatewayRunner.__mro__ if name in cls.__dict__]
    assert owners == ["GatewayLifecycleNotificationsMixin"], (
        f"{name} is defined in {owners}; expected exactly one definition, on the mixin"
    )


@pytest.mark.parametrize("name", MUST_STAY_ON_RUNNER)
def test_upstream_owned_methods_stay_on_gateway_runner(name):
    """The inverse guard. These are upstream's methods; the mixin must not own
    them. See MUST_STAY_ON_RUNNER for the per-method reason."""
    owner = _owner(name)
    assert owner is GatewayRunner, (
        f"{name} resolved to {owner.__name__}, expected GatewayRunner. This method "
        f"is upstream-owned — moving it to a mixin means a post-merge upstream "
        f"re-add wins by MRO and silently discards the fork's behaviour."
    )
    assert name not in GatewayLifecycleNotificationsMixin.__dict__


def test_mixin_does_not_import_run_at_module_level():
    """gateway.run imports this module at class-definition time, so a module-level
    import back into gateway.run would be a cycle. The helper access must be a
    lazy, in-method `from gateway import run as _run`."""
    source = inspect.getsource(GatewayLifecycleNotificationsMixin.__module__ and
                               __import__("gateway.lifecycle_notifications",
                                          fromlist=["_"]))
    tree = ast.parse(source)
    for node in tree.body:  # module level only
        if isinstance(node, ast.ImportFrom):
            assert node.module != "gateway.run", (
                "module-level `from gateway.run import ...` creates an import cycle "
                "AND freezes helpers by value so test monkeypatches miss"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "gateway.run", "module-level import of gateway.run is a cycle"


def test_helpers_are_reached_through_the_live_module():
    """Tests monkeypatch run.py's module-level helpers on `gateway.run`. If the
    mixin bound them by value at import time, every such patch would silently
    miss and the tests would pass against the unpatched originals."""
    path = Path(__import__("gateway.lifecycle_notifications", fromlist=["_"]).__file__)
    text = path.read_text(encoding="utf-8")
    assert "from gateway import run as _run" in text
    # Every helper call must be qualified through _run, never bare.
    #
    # The (?<![\w.]) guard is required, not cosmetic: several helper names are
    # SUBSTRINGS of each other — `_restart_notification_pending` is contained in
    # `_planned_restart_notification_pending`, and `_pinned_status_key` shares a
    # prefix with `_read_pinned_status`. A plain substring search reports a
    # correctly-qualified `_run._planned_restart_notification_pending(` call as an
    # unqualified `_restart_notification_pending(` one. (It did, on first run.)
    for helper in (
        "_read_pinned_status",
        "_write_pinned_status",
        "_pinned_status_key",
        "_read_recovery_marker",
        "_clear_recovery_marker",
        "_recovery_notification_pending",
        "_restart_notification_pending",
        "_planned_restart_notification_pending",
        "_clear_planned_restart_notification",
    ):
        bare = re.compile(rf"(?<![\w.]){re.escape(helper)}\(")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not bare.search(line):
                continue
            pytest.fail(
                f"unqualified call to {helper} in the mixin: {stripped!r} — "
                f"must go through _run so gateway.run monkeypatches apply"
            )

    # Positive side: at least one qualified call must actually be present, so the
    # test cannot pass vacuously if the helper calls disappear entirely.
    assert re.search(r"_run\._read_pinned_status\(", text)
    assert re.search(r"_run\._read_recovery_marker\(", text)


def test_logger_name_is_preserved():
    """Extracted log records must keep the `gateway.run` logger name so any
    log-based alerting on these lines keeps matching."""
    module = __import__("gateway.lifecycle_notifications", fromlist=["_"])
    assert module.logger.name == "gateway.run"
