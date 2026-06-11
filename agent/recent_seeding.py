"""Recent cross-surface context seeding (ADR-065 / CLAWD-1542 Phase S).

The mesh keeps a *shared, per-(person, agent) conversation* (the
``_shared_conversation_id`` derived in ``agent_init``), distinct from the
per-surface transcript/session key.  Turns that happen on one surface
(Telegram, voice, CLI, …) are appended to that shared conversation in clawd;
at the *start* of a turn on any surface we read the most recent shared turns
back and inject them into the **current user message** so the agent has
cross-surface continuity without merging per-surface transcripts.

Two seams:

* :func:`read_recent_seed` — blocking, on the critical path, HARD-capped by a
  short read timeout.  Fails OPEN: any timeout / connection error / non-200 /
  parse error returns an empty string and never raises.  The result is fenced
  and meant to be appended to the *user message* (never the system prompt) so
  Anthropic prompt-cache prefixes stay byte-stable.

* :func:`append_turn_async` — fire-and-forget in a daemon thread.  POSTs the
  user turn then the assistant turn (chronological).  Best-effort: the store is
  reconstructable, so a dropped append is acceptable and all errors are
  swallowed.  Never blocks the turn's return path.

The whole feature is gated by ``HERMES_RECENT_SEEDING_ENABLED`` (default OFF).
When off, both entry points are inert and behaviour is byte-identical to before.
The clawd base URL + bearer token reuse the existing ``CLAWD_BASE_URL`` /
``CLAWD_API_AUTH_TOKEN`` names (same as the mnemosyne provider).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# Defaults — overridable via env.
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_LIMIT = 8
_DEFAULT_READ_TIMEOUT = 1.5
_DEFAULT_WRITE_TIMEOUT = 2.0


def seeding_enabled() -> bool:
    """Master gate. Default OFF — the feature is fully inert when unset."""
    return os.environ.get("HERMES_RECENT_SEEDING_ENABLED", "").strip().lower() in _TRUTHY


def _base_url() -> str:
    return (os.environ.get("CLAWD_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CLAWD_API_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_limit() -> int:
    try:
        return int(os.environ.get("HERMES_RECENT_SEEDING_LIMIT", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _read_timeout() -> float:
    try:
        return float(os.environ.get("HERMES_RECENT_SEEDING_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_READ_TIMEOUT


def _write_timeout() -> float:
    try:
        return float(os.environ.get("HERMES_RECENT_SEEDING_WRITE_TIMEOUT", _DEFAULT_WRITE_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_WRITE_TIMEOUT


def format_seed_block(turns: List[dict]) -> str:
    """Render recent shared turns into a fenced context block.

    Mirrors the fencing style of ``build_memory_context_block`` so injected
    content is unambiguously framed as reference data, not new user input.
    Empty / malformed input yields an empty string (inject nothing).
    """
    lines: List[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "") or "").strip()
        content = str(turn.get("content", "") or "").strip()
        if not role or not content:
            continue
        lines.append(f"{role}: {content}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "<recent-shared-context>\n"
        "[System note: The following is recent conversation shared across your "
        "surfaces (e.g. chat, voice), NOT new user input. Treat as authoritative "
        "reference data for continuity.]\n\n"
        f"{body}\n"
        "</recent-shared-context>"
    )


def read_recent_seed(conversation_id: str) -> str:
    """Synchronously fetch + format recent shared turns.  Fails OPEN.

    Returns a fenced block string, or ``""`` on disabled / empty id / any
    error (timeout, connection, non-200, parse).  Never raises, never hangs
    longer than the read timeout.  Caller appends the (non-empty) result to the
    current user message.
    """
    if not seeding_enabled() or not conversation_id:
        return ""
    try:
        import httpx

        url = f"{_base_url()}/conversation-turns/{conversation_id}/recent"
        with httpx.Client(timeout=_read_timeout(), headers=_auth_headers()) as client:
            resp = client.get(url, params={"limit": _read_limit()})
        if resp.status_code != 200:
            logger.debug("recent-seed read non-200 (%s); failing open", resp.status_code)
            return ""
        data = resp.json()
        turns = data.get("turns") if isinstance(data, dict) else None
        if not isinstance(turns, list):
            return ""
        return format_seed_block(turns)
    except Exception as exc:  # noqa: BLE001 — fail open on ANY error.
        logger.debug("recent-seed read failed (%s); failing open", exc)
        return ""


def _post_turn(client: "object", base_url: str, conversation_id: str,
               role: str, content: str) -> None:
    url = f"{base_url}/conversation-turns/{conversation_id}"
    client.post(url, json={"role": role, "content": content, "timestamp": None})  # type: ignore[attr-defined]


def _append_worker(conversation_id: str, user_text: str, assistant_text: str) -> None:
    try:
        import httpx

        base_url = _base_url()
        with httpx.Client(timeout=_write_timeout(), headers=_auth_headers()) as client:
            # Chronological order: user turn first, then assistant turn.
            _post_turn(client, base_url, conversation_id, "user", user_text)
            _post_turn(client, base_url, conversation_id, "assistant", assistant_text)
    except Exception as exc:  # noqa: BLE001 — best-effort; a dropped append is acceptable.
        logger.debug("recent-seed append failed (%s); ignoring", exc)


def append_turn_async(
    conversation_id: str,
    user_message: Optional[str],
    assistant_response: Optional[str],
) -> Optional[threading.Thread]:
    """Fire-and-forget append of a completed turn to the shared conversation.

    POSTs user then assistant (chronological) in a daemon thread so it never
    blocks the turn's return path.  No-op (returns ``None``) when disabled, the
    id is empty, or either side of the exchange is missing.  The caller is
    responsible for the ``interrupted`` gate (don't call on interrupted turns).
    """
    if not seeding_enabled() or not conversation_id:
        return None
    user_text = (user_message or "").strip() if isinstance(user_message, str) else ""
    assistant_text = (assistant_response or "").strip() if isinstance(assistant_response, str) else ""
    if not user_text or not assistant_text:
        return None
    thread = threading.Thread(
        target=_append_worker,
        args=(conversation_id, user_text, assistant_text),
        daemon=True,
        name="recent-seed-append",
    )
    thread.start()
    return thread


__all__ = [
    "seeding_enabled",
    "format_seed_block",
    "read_recent_seed",
    "append_turn_async",
]
