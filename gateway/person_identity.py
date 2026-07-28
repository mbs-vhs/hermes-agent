"""Resolve a stable *person* identity for cross-surface conversation continuity.

Hermes scopes its cross-surface ``conversation_id`` as ``"{profile}:{person}"``
(CLAWD-1542) so the same human talking to the same agent on different surfaces
(Telegram, the API server, ...) lands on one shared conversation. The raw
gateway ``user_id`` is *per-surface* — a Telegram numeric id has nothing to do
with the API-server caller — so using it verbatim would never merge those
surfaces, and would also leak distinct stranger ids into the shared space.

This module is the single source of truth for collapsing a per-surface
``raw_user_id`` to a stable person id, driven by per-profile operator mapping
read from the environment at call time:

- ``HERMES_OPERATOR_PERSON_ID`` — the canonical person id (e.g. ``"morgan"``).
  Defaults to ``"morgan"`` *only* when some operator mapping is configured for
  this profile; otherwise empty (no mapping → no merge).
- ``HERMES_OPERATOR_TELEGRAM_IDS`` — comma-separated Telegram user ids that map
  to the operator person.
- ``HERMES_OPERATOR_API_SERVER`` — truthy flag marking the API server as an
  operator-only surface (its caller has no per-user id).
.. note::
   ``HERMES_OPERATOR_WEBUI`` was removed from the operator-predicate map on
   2026-07-28 (CLAWD-2803) along with the ``"webui"`` entry in
   ``_OPERATOR_PREDICATES``. It existed solely for the hermes-webui browser
   surface at ``chat.vhs.box``, which was decommissioned.

   **It DID have producers** — an earlier version of this note claimed "no
   producer anywhere", which was wrong: it is set in
   ``~/.hermes/profiles/minerva/.env`` and in the contained fleet gateway's
   ``/home/hermes-minerva/.hermes/profiles/minerva/.env``. The search behind
   that claim covered only systemd units and ``/etc/chat-ui/.env`` (the latter
   already deleted, so it could only come back empty) and never looked at
   profile ``.env`` files.

   Removing the branch is nonetheless safe, for a different reason than
   originally stated: ``_operator_person_id()`` short-circuits on the explicit
   ``HERMES_OPERATOR_PERSON_ID``, which the minerva profile sets, so the WEBUI
   fallback could never change its result; and ``_webui_matches`` was reachable
   only via ``platform="webui"``, which only hermes-webui ever emitted. A
   ``platform="webui"`` call now falls through to the fail-safe below and
   returns ``""``.

   The two ``.env`` lines are now orphan writers-without-readers. They are NOT
   edited here — ``~/.hermes/profiles/*/.env`` is an HR7 stop-condition surface
   needing per-change operator approval. Tracked separately.

   Scope note: this removed the ``"webui"`` key from the OPERATOR predicate map
   only. Upstream Hermes still ships a live ``"webui"`` entry in
   ``PLATFORM_HINTS`` (``agent/prompt_builder.py``) — deliberately untouched.
   Control's ``/chat`` embeds agora (CLAWD-1971), which resolves the canonical
   thread through its own path.

FAIL-SAFE by construction: strangers, unknown platforms, and the CLI never
merge. Any unexpected error, or a matched rule with an empty person id, falls
back to the raw user id (or ``""``), so we never emit a bare ``"profile:"`` key.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _operator_person_id() -> str:
    """Return the configured operator person id, defaulting to ``"morgan"``
    only when some operator mapping is set for this profile."""
    explicit = (os.getenv("HERMES_OPERATOR_PERSON_ID") or "").strip()
    if explicit:
        return explicit
    # No explicit person id: default to "morgan" only if *some* operator
    # mapping exists, otherwise stay empty (unconfigured profile => no merge).
    if (os.getenv("HERMES_OPERATOR_TELEGRAM_IDS") or "").strip():
        return "morgan"
    if (os.getenv("HERMES_OPERATOR_API_SERVER") or "").strip().lower() in _TRUTHY:
        return "morgan"
    return ""


def _operator_telegram_ids() -> set[str]:
    """Parse ``HERMES_OPERATOR_TELEGRAM_IDS`` into a stripped set of ids."""
    raw = os.getenv("HERMES_OPERATOR_TELEGRAM_IDS") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _operator_api_server() -> bool:
    """Whether the API server is an operator-only surface."""
    return (os.getenv("HERMES_OPERATOR_API_SERVER") or "").strip().lower() in _TRUTHY


# Platform -> predicate mapping. Each predicate decides whether the given
# raw_user_id on that platform belongs to the operator person. Adding a new
# operator surface later is a one-line addition here.
def _telegram_matches(raw_user_id: str | None) -> bool:
    return str(raw_user_id) in _operator_telegram_ids()


def _api_server_matches(raw_user_id: str | None) -> bool:
    # The API server has no per-user id; the whole surface is operator-only
    # when the flag is set.
    return _operator_api_server()


# CLAWD-2803: the "webui" predicate was removed with chat.vhs.box. An unknown
# platform hits the fail-safe in resolve_person and returns "".
_OPERATOR_PREDICATES = {
    "telegram": _telegram_matches,
    "api_server": _api_server_matches,
}


def resolve_person(profile: str, platform: str, raw_user_id: str | None) -> str:
    """Collapse a per-surface ``raw_user_id`` to a stable person id.

    Returns the configured operator person id when ``platform``/``raw_user_id``
    match a configured operator mapping; otherwise returns ``raw_user_id`` (or
    ``""``). Fail-safe: any exception or an empty operator person id on a
    matched rule falls back to ``raw_user_id or ""`` so callers never build a
    bare ``"profile:"`` conversation key.
    """
    try:
        predicate = _OPERATOR_PREDICATES.get(platform)
        if predicate is not None and predicate(raw_user_id):
            person = _operator_person_id()
            if person:
                return person
            # Matched an operator surface but no person id configured:
            # fall through to the raw id rather than emitting "profile:".
        return raw_user_id or ""
    except Exception as exc:  # noqa: BLE001 — fail-safe to the raw id
        logger.debug("person_identity: resolve_person failed: %s", exc)
        return raw_user_id or ""
