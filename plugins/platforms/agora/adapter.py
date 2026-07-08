"""Agora virtual platform adapter (Hermes plugin).

Agora is not a chat-network gateway with an inbound stream. It is Minerva's
clawd-backed SvelteKit console. This plugin exposes a Hermes messaging target
named ``agora`` whose outbound path appends assistant turns to clawd's canonical
conversation thread:

    POST /chat/conversation/{cid}/turn

The default cid is ``minerva:morgan`` so ``send_message(target="agora", ...)``
lands in the Minerva<->Morgan Agora thread. Explicit cids such as
``agora:minerva:morgan`` also work.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by tests via monkeypatch
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

DEFAULT_CLAWD_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CID = "minerva:morgan"
DEFAULT_PRIVACY_CLASS = "work_videotape"
MAX_MESSAGE_LENGTH = 20000

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUE_VALUES


def _clean_base_url(value: str | None) -> str:
    return (value or DEFAULT_CLAWD_BASE_URL).strip().rstrip("/") or DEFAULT_CLAWD_BASE_URL


def _configured_token(extra: Dict[str, Any] | None = None) -> str:
    extra = extra or {}
    return str(extra.get("token") or os.getenv("CLAWD_API_AUTH_TOKEN", "")).strip()


def _configured_cid(extra: Dict[str, Any] | None = None, chat_id: str | None = None) -> str:
    extra = extra or {}
    return (
        (chat_id or "").strip()
        or str(extra.get("default_cid") or "").strip()
        or os.getenv("AGORA_HOME_CHANNEL", "").strip()
        or DEFAULT_CID
    )


def _build_headers(token: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def check_requirements() -> bool:
    """Agora only needs httpx plus either a clawd token or explicit enablement."""
    if not HTTPX_AVAILABLE:
        return False
    return bool(_configured_token() or _truthy(os.getenv("AGORA_ENABLED", "")))


def validate_config(config) -> bool:
    """Validate that the virtual target is intentionally enabled."""
    if not HTTPX_AVAILABLE:
        return False
    extra = getattr(config, "extra", {}) or {}
    return bool(_configured_token(extra) or extra.get("enabled_without_token") is True)


def is_connected(config) -> bool:
    """Return whether the virtual target has enough config to attempt a write."""
    return validate_config(config)


class AgoraAdapter(BasePlatformAdapter):
    """Outbound-only adapter for the Agora virtual messaging target."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("agora"))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is forwarded by the gateway's reconnect loop (v0.18+);
        # Agora is outbound-only and stateless per connect, so the flag is
        # accepted for interface parity with the other platform adapters and
        # otherwise ignored — connect is idempotent (validate + mark connected).
        if not validate_config(self.config):
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        result = await _standalone_send(
            self.config,
            chat_id,
            content,
            thread_id=(metadata or {}).get("thread_id") if metadata else None,
        )
        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id"))
        return SendResult(success=False, error=result.get("error", "Agora send failed"))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}


def _env_enablement() -> dict | None:
    """Seed Agora config from Minerva/clawd environment variables.

    ``CLAWD_API_AUTH_TOKEN`` is the normal enablement signal in Minerva. For
    local unauthenticated development, ``AGORA_ENABLED=true`` also enables the
    platform without a token.
    """
    token = _configured_token()
    explicit_enabled = _truthy(os.getenv("AGORA_ENABLED", ""))
    if not token and not explicit_enabled:
        return None

    cid = os.getenv("AGORA_HOME_CHANNEL", "").strip() or DEFAULT_CID
    seed: dict[str, Any] = {
        "base_url": _clean_base_url(os.getenv("CLAWD_BASE_URL")),
        "default_cid": cid,
        "home_channel": {
            "chat_id": cid,
            "name": os.getenv("AGORA_HOME_CHANNEL_NAME", "Agora / Minerva").strip() or "Agora / Minerva",
        },
    }
    if token:
        seed["token"] = token
    else:
        seed["enabled_without_token"] = True
    privacy_class = os.getenv("AGORA_PRIVACY_CLASS", "").strip()
    if privacy_class:
        seed["privacy_class"] = privacy_class
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Append an assistant turn to clawd's cid-keyed conversation endpoint."""
    if not HTTPX_AVAILABLE:
        return {"error": "agora standalone send: httpx not installed"}

    extra = getattr(pconfig, "extra", {}) or {}
    cid = _configured_cid(extra, chat_id)
    if not cid or ":" not in cid:
        return {"error": "agora standalone send: cid must look like 'agent:person'"}

    content = (message or "")[:MAX_MESSAGE_LENGTH]
    if media_files:
        content = f"{content}\n\n[Agora virtual target omitted {len(media_files)} media attachment(s).]".strip()
    if force_document:
        content = content.replace("[[as_document]]", "").strip()

    token = _configured_token(extra)
    base_url = _clean_base_url(extra.get("base_url") or os.getenv("CLAWD_BASE_URL"))
    privacy_class = str(extra.get("privacy_class") or os.getenv("AGORA_PRIVACY_CLASS", "") or DEFAULT_PRIVACY_CLASS)
    url = f"{base_url}/chat/conversation/{quote(cid, safe='')}/turn"
    payload = {
        "role": "assistant",
        "content": content,
        "privacy_class": privacy_class,
        "idempotency_key": f"hermes-agora:{uuid.uuid4().hex}",
    }

    try:
        assert httpx is not None
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=_build_headers(token))
        if resp.status_code >= 300:
            return {"error": f"agora HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
        except Exception:
            data = {}
        msg_id = str(data.get("id") or data.get("message_id") or uuid.uuid4().hex[:12])
        return {"success": True, "platform": "agora", "chat_id": cid, "message_id": msg_id}
    except Exception as e:
        return {"error": f"agora standalone send failed: {e}"}


def register(ctx) -> None:
    """Plugin entry point called by Hermes plugin discovery."""
    ctx.register_platform(
        name="agora",
        label="Agora",
        adapter_factory=lambda cfg: AgoraAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint="Set CLAWD_API_AUTH_TOKEN in the Minerva profile environment.",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="AGORA_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🏛️",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are communicating through Agora, Minerva's clawd-backed operator console. "
            "Outbound messages are persisted as assistant turns in the canonical conversation thread."
        ),
    )
