"""Fork-added gateway lifecycle-notification methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (CLAWD-2836, epic CLAWD-2832). These
are the recovery-notification (CLAWD-1019 / CLAWD-1144) and pinned-lifecycle
(CLAWD-1376) clusters. They use only ``self`` state plus run.py's module-level
helpers, so they live on a mixin ``GatewayRunner`` inherits — the ``self._*`` call
sites resolve identically via the MRO, making this a behaviour-neutral move.

WHY THIS MOVE EXISTS. Upstream is rewriting ``gateway/run.py`` aggressively
(measured 2026-07-27: ours 20,373 lines, upstream 24,741). Our lifecycle delta was
scattered across that file, so every upstream merge had to reconcile it hunk by
hunk. Collapsing it into one file upstream never touches turns ~30 conflict sites
into zero for this cluster.

WHAT DELIBERATELY DID **NOT** MOVE, and why it matters more than what did
(CLAWD-2841 verdict, re-derived merge-base-relative):

  * ``_send_restart_notification`` — UPSTREAM-OWNED. The fork's body is
    byte-identical to the merge-base; we never touched it. It contributes zero
    fork delta, so moving it would remove nothing while adding a 71-line deletion
    hunk against a block upstream is actively rewriting. It would *increase* the
    conflict surface this file exists to shrink.
  * ``_send_home_channel_startup_notifications`` — UPSTREAM-OWNED **and** changed
    on both sides (merge-base 79L, fork 94L, upstream 82L). Post-merge, upstream
    re-adds its copy into ``GatewayRunner``'s own body, which **wins by MRO**, and
    the fork's version — carrying the CLAWD-1376 ``_adapter_lifecycle_pinned`` skip
    and the CLAWD-1144 ``Platform.EMAIL`` skip — would be silently shadowed.
    Result: double-announce + badge on all 11 ``lifecycle_pinned: true`` fleet
    profiles, behind a fully green suite.
  * ``_notify_active_sessions_of_shutdown`` — UPSTREAM-OWNED, and the highest-risk
    merge point in the program: fork returns ``list`` (246L) and the call site
    consumes it; upstream returns ``None`` (198L) and discards it. Upstream has
    additionally moved it onto ``async_session_store``, which does not exist in the
    fork at all.

The general rule, and the re-derivation that enforces it:

    MB=$(git merge-base upstream/main HEAD)
    git show "$MB:gateway/run.py" | rg -q "def <method>" && echo "UPSTREAM-OWNED -> STAYS"

**Anything present at the merge-base stays in run.py.** A mixin can only safely
own code upstream does not have, because a class body always beats a mixin in the
MRO — so an upstream re-add silently wins and the failure is invisible to tests.
``tests/gateway/test_lifecycle_notifications_mixin.py`` exists to make exactly
that failure loud.

HOW THE FREE HELPERS ARE REACHED. run.py's module-level helpers
(``_read_pinned_status``, ``_read_recovery_marker``, …) stay in run.py and are
called as ``_run.<helper>()`` after a **lazy, in-method**
``from gateway import run as _run``. Both properties are load-bearing:

  * *Through the module, not by value* — tests do
    ``monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)`` and patch the
    helpers themselves. A ``from gateway.run import _read_pinned_status`` would
    bind the original at import time and every patch would silently miss.
  * *Lazy* — ``gateway.run`` imports this module at class-definition time, so a
    module-level import back into ``gateway.run`` would be a cycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from gateway.config import Platform

# Match the logger run.py uses (``logging.getLogger(__name__)`` where
# ``__name__ == "gateway.run"``) so extracted log records keep their original
# logger name — log-based alerting on these lines must not break.
logger = logging.getLogger("gateway.run")


class GatewayLifecycleNotificationsMixin:
    """Recovery-notification + pinned-lifecycle methods for ``GatewayRunner``."""

    async def _send_post_connect_lifecycle_notifications(self) -> None:
        """Fire post-connect lifecycle notifications once adapters are up.

        Two mutually-exclusive paths, either of which clears the external-restart
        recovery marker so "gateway online" fires at most once:
          - in-band ``/restart``: notify the requester + home channels.
          - external restart (systemd / SIGTERM / ``--replace``) that interrupted
            in-flight work: announce "gateway online" via the recovery marker,
            which the ``/restart`` path never writes (CLAWD-1019).

        A third, independent path handles upstream's non-chat *planned* restart
        (terminal / SIGUSR1 / service) tracked by ``.restart_pending.json``:
        broadcast a generic "gateway is back" to configured home channels and
        clear the planned-restart marker (see ``_planned_restart_notification_*``).

        Extracted from ``start()`` so the if/elif wiring is unit-testable against
        real code rather than a replica.
        """
        # Reach run.py's module-level helpers through the LIVE module rather
        # than by value: tests monkeypatch them on gateway.run, and a by-value
        # import would freeze the originals so patches silently miss. Lazy and
        # in-method, so no import cycle forms (gateway.run imports this module
        # at class-definition time).
        from gateway import run as _run

        # CLAWD-1376: badge-free pinned lifecycle. For adapters in pinned mode,
        # the gateway-online transition is an in-place edit of the pinned status
        # message (no fresh send, no badge). This runs FIRST and independently of
        # the /restart and recovery branches below — those legacy paths skip
        # pinned-mode adapters (see _send_home_channel_startup_notifications and
        # _edit_recovery_notifications), so the home channel never gets both a
        # pin edit and a fresh "gateway online" DM.
        await self._update_pinned_lifecycle_status("online")

        # Notify the chat that initiated /restart that the gateway is back.
        restart_notification_pending = _run._restart_notification_pending()
        # Upstream's non-chat planned restart uses a distinct marker
        # (.restart_pending.json). Capture it BEFORE _send_restart_notification()
        # unlinks the /restart marker, so both the _booted_from_restart guard and
        # the planned-restart home broadcast below still see the pre-restart state.
        planned_restart_notification_pending = _run._planned_restart_notification_pending()
        # One-shot signal consumed by _is_stale_restart_redelivery: a missing
        # dedup marker only suppresses a /restart when we KNOW we just came out
        # of a restart cycle.
        if restart_notification_pending or planned_restart_notification_pending:
            self._booted_from_restart = True
        delivered_restart_target = await self._send_restart_notification()

        # Broadcast a lightweight "gateway is back" message to configured home
        # channels when resuming from /restart. If the /restart requester already
        # received a direct completion notice in the same chat, skip the generic
        # broadcast there to avoid duplicates while still allowing a home-channel
        # fallback when the direct send fails.
        if restart_notification_pending or delivered_restart_target is not None:
            skip_home_targets = (
                {delivered_restart_target} if delivered_restart_target else None
            )
            await self._send_home_channel_startup_notifications(
                skip_targets=skip_home_targets,
            )
            # A /restart supersedes any same-cycle external-recovery marker;
            # drop it so it can't double-fire on the next boot. (CLAWD-1019)
            _run._clear_recovery_marker()
        elif _run._recovery_notification_pending():
            # External (systemd / SIGTERM / `--replace`) restart leaves no
            # .restart_notify.json marker, so the branch above is skipped and
            # the recovery would otherwise be silent. Announce "gateway online"
            # — preferring an IN-PLACE EDIT of the recorded down-DMs (an edit
            # produces no push notification, so the chat list flips to "online"
            # without a second alert — CLAWD-1144) with a fresh silent send as
            # fallback for targets that couldn't be edited — then clear the
            # marker so it fires exactly once per restart. (CLAWD-1019)
            edited = await self._edit_recovery_notifications()
            await self._send_home_channel_startup_notifications(
                skip_targets=edited or None,
            )
            _run._clear_recovery_marker()

        # Non-chat planned restart (upstream .restart_pending.json:
        # terminal/SIGUSR1/service paths). Chat-originated /restart already has a
        # precise reply target in .restart_notify.json and is handled above, so
        # this only broadcasts the generic "gateway is back" to configured home
        # channels, then clears the planned-restart marker so it fires once.
        if planned_restart_notification_pending:
            try:
                await self._send_home_channel_startup_notifications(
                    skip_targets=None,
                )
            finally:
                _run._clear_planned_restart_notification()

    async def _edit_recovery_notifications(self) -> set[tuple[str, str, Optional[str]]]:
        """Edit the recorded shutdown down-DMs in place into the online notice.

        Reads the recovery marker's ``targets`` (written by the shutdown path
        with the message ids of the home-channel "shutting down" DMs) and edits
        each into the "gateway online" message. An edit produces NO push
        notification, so recovery flips the chat list to "online" without a
        second alert — one silent tray entry per restart, total (CLAWD-1144).

        Returns the successfully edited targets in the same key shape as the
        startup-notification dedup set so the caller can skip fresh sends for
        them. Targets without a message_id, failed edits, and pre-CLAWD-1144
        markers (no ``targets``) are simply not returned — the caller's
        fallback fresh send covers those. Best-effort throughout.
        """
        # Reach run.py's module-level helpers through the LIVE module rather
        # than by value: tests monkeypatch them on gateway.run, and a by-value
        # import would freeze the originals so patches silently miss. Lazy and
        # in-method, so no import cycle forms (gateway.run imports this module
        # at class-definition time).
        from gateway import run as _run

        edited: set[tuple[str, str, Optional[str]]] = set()
        marker = _run._read_recovery_marker()
        if not marker:
            return edited
        targets = marker.get("targets")
        if not isinstance(targets, list) or not targets:
            return edited

        message = "♻️ Gateway online — Hermes is back and ready."
        ts = marker.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            down_secs = max(0, int(time.time() - ts))
            message = (
                f"♻️ Gateway online — Hermes is back and ready (was down ~{down_secs}s)."
            )

        for target in targets:
            if not isinstance(target, dict):
                continue
            platform_str = target.get("platform")
            chat_id = target.get("chat_id")
            message_id = target.get("message_id")
            thread_id = target.get("thread_id")
            if not platform_str or not chat_id or not message_id:
                continue
            try:
                platform = Platform(platform_str)
            except ValueError:
                continue
            adapter = self.adapters.get(platform)
            if adapter is None:
                continue

            # CLAWD-1376: pinned-lifecycle adapters own their online transition
            # via the pinned status edit; skip the legacy down-DM recovery edit.
            if self._adapter_lifecycle_pinned(adapter):
                continue

            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Recovery edit suppressed: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue

            try:
                result = await adapter.edit_message(
                    str(chat_id), str(message_id), message, finalize=True,
                )
                if result is not None and getattr(result, "success", True) is False:
                    logger.debug(
                        "Recovery edit failed for %s:%s (msg %s): %s — falling back to send",
                        platform.value,
                        chat_id,
                        message_id,
                        getattr(result, "error", "edit returned success=False"),
                    )
                    continue
                edited.add(
                    (platform.value, str(chat_id), str(thread_id) if thread_id else None)
                )
                logger.info(
                    "Edited shutdown notice into online notice for %s:%s",
                    platform.value,
                    chat_id,
                )
            except Exception as exc:
                logger.debug(
                    "Recovery edit failed for %s:%s (msg %s): %s — falling back to send",
                    platform_str,
                    chat_id,
                    message_id,
                    exc,
                )
        return edited

    def _adapter_lifecycle_pinned(self, adapter) -> bool:
        """True when this adapter is in badge-free pinned-lifecycle mode and
        can pin (CLAWD-1376). Requires both the opt-in flag and a ``pin_message``
        capability so only adapters that actually support pinning take the path.
        """
        return bool(
            getattr(adapter, "_lifecycle_pinned", False)
            and callable(getattr(adapter, "pin_message", None))
        )

    async def _update_pinned_lifecycle_status(self, state: str) -> bool:
        """Reflect a gateway lifecycle transition in the pinned status message.

        CLAWD-1376: each gateway keeps ONE pinned status message per home
        channel and EDITS it in place on every online/offline transition — an
        edit produces no push notification, and the one-time create+pin is done
        with ``disable_notification=True``, so the operator's chat list flips
        state with ZERO badges. If the stored message can't be edited (operator
        unpinned/deleted it, or first run), a fresh status message is sent
        silently and re-pinned.

        ``state`` is ``"online"`` or ``"offline"``. Returns True when at least
        one home channel's pinned status was updated, so the caller can skip the
        legacy CLAWD-1144 send/edit-down-DM path for those platforms.

        Best-effort throughout — a failure here must never block start/stop.
        """
        # Reach run.py's module-level helpers through the LIVE module rather
        # than by value: tests monkeypatch them on gateway.run, and a by-value
        # import would freeze the originals so patches silently miss. Lazy and
        # in-method, so no import cycle forms (gateway.run imports this module
        # at class-definition time).
        from gateway import run as _run

        message = (
            "🟢 Gateway online — Hermes is connected and ready."
            if state == "online"
            else "🔴 Gateway offline — Hermes is restarting; back shortly."
        )
        store = _run._read_pinned_status()
        updated = False

        for platform, adapter in list(self.adapters.items()):
            if not self._adapter_lifecycle_pinned(adapter):
                continue
            if platform == Platform.EMAIL:
                continue

            home = self.config.get_home_channel(platform)
            if not home or not home.chat_id:
                continue

            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Pinned lifecycle status suppressed: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue

            chat_id = str(home.chat_id)
            thread_id = str(home.thread_id) if home.thread_id else None
            key = _run._pinned_status_key(platform.value, chat_id, thread_id)
            # notify=False force-silences the create/recreate send in EVERY
            # notification mode (incl. "all"), so the badge-free invariant holds
            # regardless of the adapter's configured mode (CLAWD-1376).
            metadata: Dict[str, Any] = {"notify": False}
            if home.thread_id:
                metadata["thread_id"] = home.thread_id

            try:
                cached_id = store.get(key)
                if cached_id:
                    result = await adapter.edit_message(
                        chat_id, str(cached_id), message, finalize=True,
                        metadata=metadata,
                    )
                    if result is not None and getattr(result, "success", True):
                        store[key] = str(
                            getattr(result, "message_id", None) or cached_id
                        )
                        updated = True
                        logger.info(
                            "Edited pinned lifecycle status (%s) for %s:%s",
                            state, platform.value, chat_id,
                        )
                        continue
                    # Edit failed (unpinned/deleted/too old) — drop the stale id
                    # and recreate below.
                    logger.debug(
                        "Pinned lifecycle status edit failed for %s:%s — recreating",
                        platform.value, chat_id,
                    )

                # First run, or the stored message is gone: send a fresh status
                # message silently and pin it silently. metadata.notify=False
                # force-silences the send in every mode (incl. "all"); the pin
                # is explicitly silent. Recreate fires only when the edit FAILS
                # (deleted, or unpinned+too-old). A bare unpin where the message
                # is still editable is intentionally NOT auto-re-pinned — that
                # would require a pin-state probe; the status text still updates
                # in place via the edit, just without re-pinning.
                send_result = await adapter.send(chat_id, message, metadata=metadata)
                new_id = (
                    getattr(send_result, "message_id", None)
                    if send_result is not None
                    and getattr(send_result, "success", True)
                    else None
                )
                if not new_id:
                    logger.debug(
                        "Pinned lifecycle status send failed for %s:%s",
                        platform.value, chat_id,
                    )
                    store.pop(key, None)
                    continue
                await adapter.pin_message(
                    chat_id, str(new_id), disable_notification=True,
                )
                store[key] = str(new_id)
                updated = True
                logger.info(
                    "Created+pinned lifecycle status (%s) for %s:%s",
                    state, platform.value, chat_id,
                )
            except Exception as exc:
                logger.debug(
                    "Pinned lifecycle status update failed for %s:%s: %s",
                    platform.value, chat_id, exc,
                )

        if updated:
            _run._write_pinned_status(store)
        return updated
