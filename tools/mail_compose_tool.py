"""Mail compose tool — let Minerva draft a NEW outbound email (CLAWD-1527, Mail v2 P5).

Wraps clawd's ``POST /mail/drafts/compose`` (CLAWD-1525) so the agent can "draft an
email to <recipient> about <intent>" from Telegram, the CLI, or any platform whose
toolset includes this tool. The draft lands in Control (clawd ``email_draft``,
``status='draft'``, ``proposed_by='minerva'``) for Morgan to review, edit, and
approve. **The agent never sends** — approve-before-send is enforced clawd-side; the
send leg is Morgan-session-gated and unreachable from here.

Auth: clawd's mail surface needs BOTH the global bearer (``CLAWD_API_AUTH_TOKEN``)
and the mail agent token (``MAIL_AGENT_TOKEN`` → ``X-Agent-Token``, which resolves
the minerva actor clawd-side). The tool is unavailable (``check_fn`` False) unless
both are configured in the gateway env.

Scope: COMPOSE (a fresh email) only. Replying to an inbound email is already handled
by clawd's background sweep + the Control "Draft reply" button (Mail v2 P2); a
reply-from-agent path needs a thread/message lookup the agent doesn't have here and
is a follow-on.
"""

from __future__ import annotations

import json
import os

import httpx

from tools.registry import registry

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT_SECONDS = 180.0  # the compose endpoint runs an LLM generation


def _base_url() -> str:
    return (os.environ.get("CLAWD_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    bearer = os.environ.get("CLAWD_API_AUTH_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    agent_token = os.environ.get("MAIL_AGENT_TOKEN", "").strip()
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    return headers


def check_mail_compose_requirements() -> bool:
    """Available only when both clawd auth tokens are configured for the gateway."""
    return bool(
        os.environ.get("CLAWD_API_AUTH_TOKEN", "").strip()
        and os.environ.get("MAIL_AGENT_TOKEN", "").strip()
    )


def _parse_recipients(value) -> list[dict[str, str]]:
    """Accept a comma/semicolon-separated string OR a list of emails/dicts."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(email: str, name: str | None = None) -> None:
        email = (email or "").strip().strip("<>").lower()
        if not email or "@" not in email or email in seen:
            return
        seen.add(email)
        entry: dict[str, str] = {"email": email}
        if name and name.strip():
            entry["name"] = name.strip()
        out.append(entry)

    if isinstance(value, str):
        for part in value.replace(";", ",").split(","):
            _add(part)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _add(str(item.get("email", "")), item.get("name"))
            else:
                _add(str(item))
    return out


def mail_compose_tool(
    to: str | list | None = None,
    intent: str = "",
    subject: str | None = None,
    cc: str | list | None = None,
    bcc: str | list | None = None,
) -> str:
    """Draft a new outbound email in Control for Morgan's review. Returns JSON."""
    to_recipients = _parse_recipients(to)
    if not to_recipients:
        return json.dumps({"success": False, "error": "at least one valid 'to' email is required"})
    intent = (intent or "").strip()
    if not intent:
        return json.dumps({"success": False, "error": "'intent' (what the email should say) is required"})

    payload: dict[str, object] = {"to": to_recipients, "intent": intent}
    if subject and subject.strip():
        payload["subject"] = subject.strip()
    cc_recipients = _parse_recipients(cc)
    if cc_recipients:
        payload["cc"] = cc_recipients
    bcc_recipients = _parse_recipients(bcc)
    if bcc_recipients:
        payload["bcc"] = bcc_recipients

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, headers=_headers()) as client:
            resp = client.post(f"{_base_url()}/mail/drafts/compose", json=payload)
    except Exception as exc:  # noqa: BLE001 — surface a clean tool error, never crash the turn
        return json.dumps({"success": False, "error": f"could not reach the mail substrate: {type(exc).__name__}"})

    if resp.status_code != 200:
        detail = ""
        try:
            detail = json.dumps(resp.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        return json.dumps({"success": False, "status": resp.status_code, "error": detail or "compose failed"})

    data = resp.json()
    draft = data.get("draft") or {}
    return json.dumps({
        "success": True,
        "draft_id": data.get("draft_id"),
        "subject": draft.get("subject"),
        "to": [r.get("email") for r in (draft.get("to") or [])],
        "message": "Draft created in Control. Review, edit, and approve it there — nothing is sent until you approve.",
    })


MAIL_COMPOSE_SCHEMA = {
    "name": "mail_compose",
    "description": (
        "Draft a NEW outbound email for Morgan to review in Control. Use when Morgan "
        "asks you to 'draft an email to X about Y', 'write an email to ...', or "
        "'email ... saying ...'. You provide the recipient(s) and the intent (what the "
        "email should accomplish); clawd writes the body as Morgan and saves it as a "
        "draft. NOTHING IS SENT — the draft waits in Control for Morgan to review, edit, "
        "and approve. This is for composing fresh emails; replies to inbound mail are "
        "drafted automatically elsewhere."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address(es), comma-separated for multiple.",
            },
            "intent": {
                "type": "string",
                "description": "What the email should say / accomplish, in plain language. "
                               "clawd turns this into a ready-to-review draft written as Morgan.",
            },
            "subject": {
                "type": "string",
                "description": "Optional subject line. If omitted, clawd proposes one.",
            },
            "cc": {"type": "string", "description": "Optional cc email(s), comma-separated."},
            "bcc": {"type": "string", "description": "Optional bcc email(s), comma-separated."},
        },
        "required": ["to", "intent"],
    },
}


registry.register(
    name="mail_compose",
    toolset="mail",
    schema=MAIL_COMPOSE_SCHEMA,
    handler=lambda args, **kw: mail_compose_tool(
        to=args.get("to"),
        intent=args.get("intent", ""),
        subject=args.get("subject"),
        cc=args.get("cc"),
        bcc=args.get("bcc"),
    ),
    check_fn=check_mail_compose_requirements,
    requires_env=["CLAWD_API_AUTH_TOKEN", "MAIL_AGENT_TOKEN"],
    emoji="📧",
)
