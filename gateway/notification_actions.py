"""Server-side registry for Telegram notification action buttons.

Telegram callback payloads are capped at 64 bytes.  This module keeps the
button payload tiny (``na:<short_id>:<verb>``) and stores the real notification
context server-side so callbacks can be authenticated, expired, and made
idempotent without leaking source data into Telegram.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from utils import atomic_replace


DEFAULT_NOTIFICATION_ACTIONS: dict[str, str] = {
    "finished": "✅ Mark finished",
    "snooze": "⏰ Snooze",
    "help_me": "🆘 Help me",
}
TERMINAL_STATUSES = {"finished", "snoozed", "help_requested"}


@dataclass
class NotificationActionEntry:
    short_id: str
    notification_id: str
    source_type: str | None = None
    source_id: str | None = None
    chat_id: str | None = None
    user_id: str | None = None
    thread_id: str | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "open"
    actions: dict[str, Any] = field(default_factory=dict)
    snooze_until: float | None = None
    help_handle: str | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotificationActionEntry":
        return cls(
            short_id=str(data.get("short_id") or ""),
            notification_id=str(data.get("notification_id") or ""),
            source_type=_optional_str(data.get("source_type")),
            source_id=_optional_str(data.get("source_id")),
            chat_id=_optional_str(data.get("chat_id")),
            user_id=_optional_str(data.get("user_id")),
            thread_id=_optional_str(data.get("thread_id")),
            created_at=float(data.get("created_at") or time.time()),
            expires_at=float(data.get("expires_at") or 0.0),
            status=str(data.get("status") or "open"),
            actions=dict(data.get("actions") or {}),
            snooze_until=_optional_float(data.get("snooze_until")),
            help_handle=_optional_str(data.get("help_handle")),
            resolved_at=_optional_float(data.get("resolved_at")),
            resolved_by=_optional_str(data.get("resolved_by")),
        )

    def is_expired(self, now: float | None = None) -> bool:
        return bool(self.expires_at and self.expires_at <= (time.time() if now is None else now))

    def callback_data(self, verb: str) -> str:
        data = f"na:{self.short_id}:{verb}"
        if len(data.encode("utf-8")) > 64:
            raise ValueError("notification action callback_data exceeds Telegram's 64-byte cap")
        return data


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_action_map(raw: Any) -> dict[str, Any]:
    if raw is True or raw is None:
        return {verb: {"label": label} for verb, label in DEFAULT_NOTIFICATION_ACTIONS.items()}
    if isinstance(raw, (list, tuple, set)):
        actions: dict[str, Any] = {}
        for verb in raw:
            normalized = str(verb or "").strip()
            if normalized in DEFAULT_NOTIFICATION_ACTIONS:
                actions[normalized] = {"label": DEFAULT_NOTIFICATION_ACTIONS[normalized]}
        return actions or {verb: {"label": label} for verb, label in DEFAULT_NOTIFICATION_ACTIONS.items()}
    if isinstance(raw, Mapping):
        actions = {}
        for verb, spec in raw.items():
            normalized = str(verb or "").strip()
            if not normalized:
                continue
            if isinstance(spec, Mapping):
                spec_dict = dict(spec)
            elif isinstance(spec, str):
                spec_dict = {"label": spec}
            elif spec is False:
                continue
            else:
                spec_dict = {}
            spec_dict.setdefault("label", DEFAULT_NOTIFICATION_ACTIONS.get(normalized, normalized.replace("_", " ").title()))
            actions[normalized] = spec_dict
        return actions
    return {}


class NotificationActionStore:
    """Small JSON-backed store for inline notification action state."""

    def __init__(self, path: str | Path | None = None, *, default_ttl_seconds: int = 7 * 24 * 60 * 60):
        self.path = Path(path) if path is not None else None
        self.default_ttl_seconds = int(default_ttl_seconds)
        self._entries: dict[str, NotificationActionEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        entries = raw.get("entries", raw if isinstance(raw, dict) else {})
        if not isinstance(entries, Mapping):
            return
        for key, value in entries.items():
            if isinstance(value, Mapping):
                entry = NotificationActionEntry.from_dict(value)
                if not entry.short_id:
                    entry.short_id = str(key)
                self._entries[entry.short_id] = entry

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": {key: asdict(entry) for key, entry in sorted(self._entries.items())},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        atomic_replace(str(tmp), str(self.path))

    def register(
        self,
        *,
        notification_id: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        chat_id: str | int | None = None,
        user_id: str | int | None = None,
        thread_id: str | int | None = None,
        ttl_seconds: int | float | None = None,
        actions: Any = None,
        now: float | None = None,
    ) -> NotificationActionEntry:
        created_at = time.time() if now is None else float(now)
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        action_map = _normalize_action_map(actions)
        if not action_map:
            raise ValueError("notification action registry requires at least one action")
        for _ in range(10):
            short_id = secrets.token_urlsafe(8).rstrip("=")
            if short_id not in self._entries:
                break
        else:  # pragma: no cover - practically unreachable
            raise RuntimeError("could not allocate unique notification action id")
        entry = NotificationActionEntry(
            short_id=short_id,
            notification_id=str(notification_id or short_id),
            source_type=_optional_str(source_type),
            source_id=_optional_str(source_id),
            chat_id=_optional_str(chat_id),
            user_id=_optional_str(user_id),
            thread_id=_optional_str(thread_id),
            created_at=created_at,
            expires_at=created_at + ttl if ttl > 0 else 0.0,
            actions=action_map,
        )
        # Prove every callback stays under Telegram's hard cap before exposing it.
        for verb in action_map:
            entry.callback_data(verb)
        self._entries[short_id] = entry
        self._save()
        return entry

    def get(self, short_id: str) -> NotificationActionEntry | None:
        return self._entries.get(str(short_id or ""))

    def verify_context(
        self,
        entry: NotificationActionEntry,
        *,
        chat_id: str | int | None,
        user_id: str | int | None,
        thread_id: str | int | None,
    ) -> bool:
        if entry.chat_id is not None and entry.chat_id != _optional_str(chat_id):
            return False
        if entry.user_id is not None and entry.user_id != _optional_str(user_id):
            return False
        if entry.thread_id is not None and entry.thread_id != _optional_str(thread_id):
            return False
        return True

    def mark_expired(self, entry: NotificationActionEntry) -> None:
        if entry.status == "open":
            entry.status = "expired"
            self._save()

    def finish(self, entry: NotificationActionEntry, *, user_id: str | int | None = None, now: float | None = None) -> tuple[bool, str]:
        if entry.status == "finished":
            return False, "already finished"
        if entry.status in TERMINAL_STATUSES:
            return False, f"already {entry.status.replace('_', ' ')}"
        entry.status = "finished"
        entry.resolved_at = time.time() if now is None else float(now)
        entry.resolved_by = _optional_str(user_id)
        self._save()
        return True, "finished"

    def snooze(
        self,
        entry: NotificationActionEntry,
        *,
        seconds: int | float = 60 * 60,
        user_id: str | int | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        if entry.status in TERMINAL_STATUSES:
            return False, f"already {entry.status.replace('_', ' ')}"
        base = time.time() if now is None else float(now)
        entry.status = "snoozed"
        entry.snooze_until = base + float(seconds)
        entry.resolved_by = _optional_str(user_id)
        self._save()
        return True, "snoozed"

    def request_help(
        self,
        entry: NotificationActionEntry,
        *,
        user_id: str | int | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        if entry.status == "help_requested":
            return False, entry.help_handle or f"help:{entry.short_id}"
        if entry.status in TERMINAL_STATUSES:
            return False, f"already {entry.status.replace('_', ' ')}"
        entry.status = "help_requested"
        entry.resolved_at = time.time() if now is None else float(now)
        entry.resolved_by = _optional_str(user_id)
        entry.help_handle = entry.help_handle or f"help:{entry.short_id}"
        self._save()
        return True, entry.help_handle

    def set_help_handle(self, entry: NotificationActionEntry, handle: str) -> None:
        entry.help_handle = str(handle)
        self._save()

    def prune(self, *, now: float | None = None, grace_seconds: int = 24 * 60 * 60) -> int:
        current = time.time() if now is None else float(now)
        before = len(self._entries)
        self._entries = {
            key: entry
            for key, entry in self._entries.items()
            if not (entry.expires_at and entry.expires_at + grace_seconds < current)
        }
        removed = before - len(self._entries)
        if removed:
            self._save()
        return removed
