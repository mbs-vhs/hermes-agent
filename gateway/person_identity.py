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
- ``HERMES_OPERATOR_WEBUI`` — truthy flag marking the hermes-webui browser
  surface (``chat.vhs.box``) as operator-only. Like the API server it carries no
  per-user id (the in-process webui session runs ``platform="webui"`` with
  ``user_id=None``), so the whole surface maps to the operator when set. This is
  what lets a chat.vhs.box turn resolve the same ``"{profile}:morgan"``
  conversation the voice / Telegram / api surfaces use, so Phase S recent-context
  seeding (CLAWD-1542) reaches it (CLAWD-1561 / ADR-065 P2b).

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
    if (os.getenv("HERMES_OPERATOR_WEBUI") or "").strip().lower() in _TRUTHY:
        return "morgan"
    return ""


def _operator_telegram_ids() -> set[str]:
    """Parse ``HERMES_OPERATOR_TELEGRAM_IDS`` into a stripped set of ids."""
    raw = os.getenv("HERMES_OPERATOR_TELEGRAM_IDS") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _operator_api_server() -> bool:
    """Whether the API server is an operator-only surface."""
    return (os.getenv("HERMES_OPERATOR_API_SERVER") or "").strip().lower() in _TRUTHY


def _operator_webui() -> bool:
    """Whether the hermes-webui browser surface is an operator-only surface."""
    return (os.getenv("HERMES_OPERATOR_WEBUI") or "").strip().lower() in _TRUTHY


# Platform -> predicate mapping. Each predicate decides whether the given
# raw_user_id on that platform belongs to the operator person. Adding a new
# operator surface later is a one-line addition here.
def _telegram_matches(raw_user_id: str | None) -> bool:
    return str(raw_user_id) in _operator_telegram_ids()


def _api_server_matches(raw_user_id: str | None) -> bool:
    # The API server has no per-user id; the whole surface is operator-only
    # when the flag is set.
    return _operator_api_server()


def _webui_matches(raw_user_id: str | None) -> bool:
    # hermes-webui's in-process session carries no per-user id (platform="webui",
    # user_id=None); the whole surface is operator-only when the flag is set.
    return _operator_webui()


_OPERATOR_PREDICATES = {
    "telegram": _telegram_matches,
    "api_server": _api_server_matches,
    "webui": _webui_matches,
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
