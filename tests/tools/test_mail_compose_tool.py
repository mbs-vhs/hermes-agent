"""Tests for tools/mail_compose_tool.py — the mail_compose Hermes tool (CLAWD-1527).

Covers the schema contract, recipient parsing, availability gating, header
construction, input validation (no-HTTP-on-bad-input), the happy path + POST
body shape, non-200 handling, fail-soft transport errors, and registration.

HTTP is mocked by patching ``tools.mail_compose_tool.httpx.Client`` so no real
network call is made. The credential env vars are cleared per-test via the
``clear_env`` fixture so a leaked CLAWD token never bleeds into assertions.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.mail_compose_tool import (
    MAIL_COMPOSE_SCHEMA,
    _headers,
    _parse_recipients,
    check_mail_compose_requirements,
    mail_compose_tool,
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Start every test from a known env: no mail tokens, default base URL."""
    monkeypatch.delenv("CLAWD_API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MAIL_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWD_BASE_URL", raising=False)


def _mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _patched_client(resp=None, raise_exc=None):
    """Return a patch() target for httpx.Client whose .post() returns *resp*.

    The real code uses ``with httpx.Client(...) as client: client.post(...)``,
    so the mock must support the context-manager protocol.
    """
    client = MagicMock()
    if raise_exc is not None:
        client.post.side_effect = raise_exc
    else:
        client.post.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    factory = MagicMock(return_value=ctx)
    # Expose the inner client so tests can assert on .post calls.
    factory.client = client
    return factory


# =========================================================================
# 1. Schema contract
# =========================================================================

class TestSchema:
    def test_name_is_mail_compose(self):
        assert MAIL_COMPOSE_SCHEMA["name"] == "mail_compose"

    def test_required_params(self):
        assert MAIL_COMPOSE_SCHEMA["parameters"]["required"] == ["to", "intent"]

    def test_description_mentions_drafting_not_sending(self):
        desc = MAIL_COMPOSE_SCHEMA["description"].lower()
        assert "draft" in desc
        # The agent must understand nothing is sent.
        assert "nothing is sent" in desc or "not sent" in desc or "never sent" in desc

    def test_properties_cover_all_params(self):
        props = MAIL_COMPOSE_SCHEMA["parameters"]["properties"]
        for key in ("to", "intent", "subject", "cc", "bcc"):
            assert key in props


# =========================================================================
# 2. _parse_recipients
# =========================================================================

class TestParseRecipients:
    def test_single_string(self):
        assert _parse_recipients("a@b.com") == [{"email": "a@b.com"}]

    def test_comma_separated(self):
        out = _parse_recipients("a@b.com, c@d.com")
        assert out == [{"email": "a@b.com"}, {"email": "c@d.com"}]

    def test_semicolon_separated(self):
        out = _parse_recipients("a@b.com; c@d.com")
        assert out == [{"email": "a@b.com"}, {"email": "c@d.com"}]

    def test_mixed_comma_and_semicolon(self):
        out = _parse_recipients("a@b.com; c@d.com, e@f.com")
        assert [r["email"] for r in out] == ["a@b.com", "c@d.com", "e@f.com"]

    def test_dedup_case_insensitive(self):
        out = _parse_recipients("A@B.com, a@b.com")
        assert out == [{"email": "a@b.com"}]

    def test_lowercased(self):
        assert _parse_recipients("Alice@Example.COM") == [{"email": "alice@example.com"}]

    def test_list_of_strings(self):
        out = _parse_recipients(["a@b.com", "c@d.com"])
        assert out == [{"email": "a@b.com"}, {"email": "c@d.com"}]

    def test_list_of_dicts_email_and_name(self):
        out = _parse_recipients([{"email": "a@b.com", "name": "Alice"}])
        assert out == [{"email": "a@b.com", "name": "Alice"}]

    def test_invalid_no_at_dropped(self):
        out = _parse_recipients("a@b.com, notanemail, c@d.com")
        assert [r["email"] for r in out] == ["a@b.com", "c@d.com"]

    def test_empty_string_returns_empty(self):
        assert _parse_recipients("") == []

    def test_none_returns_empty(self):
        assert _parse_recipients(None) == []

    def test_strips_angle_brackets(self):
        assert _parse_recipients("<a@b.com>") == [{"email": "a@b.com"}]


# =========================================================================
# 3. check_mail_compose_requirements (env gating)
# =========================================================================

class TestCheckRequirements:
    def test_false_with_neither_token(self):
        assert check_mail_compose_requirements() is False

    def test_false_with_only_bearer(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "bearer")
        assert check_mail_compose_requirements() is False

    def test_false_with_only_agent_token(self, monkeypatch):
        monkeypatch.setenv("MAIL_AGENT_TOKEN", "agent")
        assert check_mail_compose_requirements() is False

    def test_true_with_both(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "bearer")
        monkeypatch.setenv("MAIL_AGENT_TOKEN", "agent")
        assert check_mail_compose_requirements() is True

    def test_false_with_whitespace_only_tokens(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "   ")
        monkeypatch.setenv("MAIL_AGENT_TOKEN", "   ")
        assert check_mail_compose_requirements() is False


# =========================================================================
# 4. _headers
# =========================================================================

class TestHeaders:
    def test_content_type_always_present(self):
        assert _headers()["Content-Type"] == "application/json"

    def test_no_auth_headers_when_env_unset(self):
        h = _headers()
        assert "Authorization" not in h
        assert "X-Agent-Token" not in h

    def test_auth_headers_when_env_set(self, monkeypatch):
        monkeypatch.setenv("CLAWD_API_AUTH_TOKEN", "tok123")
        monkeypatch.setenv("MAIL_AGENT_TOKEN", "agent456")
        h = _headers()
        assert h["Authorization"] == "Bearer tok123"
        assert h["X-Agent-Token"] == "agent456"


# =========================================================================
# 5. Validation — no HTTP call on bad input
# =========================================================================

class TestValidation:
    def test_empty_to_returns_failure_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            result = json.loads(mail_compose_tool(to="", intent="say hi"))
        assert result["success"] is False
        factory.assert_not_called()

    def test_to_with_no_valid_email_returns_failure_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            result = json.loads(mail_compose_tool(to="not-an-email", intent="say hi"))
        assert result["success"] is False
        factory.assert_not_called()

    def test_empty_intent_returns_failure_no_http(self):
        factory = _patched_client(_mock_response())
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            result = json.loads(mail_compose_tool(to="a@b.com", intent="   "))
        assert result["success"] is False
        factory.assert_not_called()


# =========================================================================
# 6. Happy path + POST body shape
# =========================================================================

class TestHappyPath:
    def test_success_response_shape(self):
        resp = _mock_response(
            200,
            {
                "draft_id": "d-123",
                "draft": {"subject": "S", "to": [{"email": "a@b.com"}]},
            },
        )
        factory = _patched_client(resp)
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            result = json.loads(mail_compose_tool(to="a@b.com", intent="say hi"))
        assert result["success"] is True
        assert result["draft_id"] == "d-123"
        assert result["subject"] == "S"
        assert result["to"] == ["a@b.com"]
        assert "message" in result and result["message"]

    def test_post_body_minimal(self):
        resp = _mock_response(200, {"draft_id": "d", "draft": {}})
        factory = _patched_client(resp)
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            mail_compose_tool(to="a@b.com", intent="say hi")
        _, kwargs = factory.client.post.call_args
        body = kwargs["json"]
        assert body["to"] == [{"email": "a@b.com"}]
        assert body["intent"] == "say hi"
        assert "subject" not in body
        assert "cc" not in body
        assert "bcc" not in body

    def test_post_body_with_subject_cc_bcc(self):
        resp = _mock_response(200, {"draft_id": "d", "draft": {}})
        factory = _patched_client(resp)
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            mail_compose_tool(
                to="a@b.com",
                intent="say hi",
                subject="Hello",
                cc="c@d.com",
                bcc="e@f.com",
            )
        _, kwargs = factory.client.post.call_args
        body = kwargs["json"]
        assert body["subject"] == "Hello"
        assert body["cc"] == [{"email": "c@d.com"}]
        assert body["bcc"] == [{"email": "e@f.com"}]

    def test_post_url_targets_compose_endpoint(self):
        resp = _mock_response(200, {"draft_id": "d", "draft": {}})
        factory = _patched_client(resp)
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            mail_compose_tool(to="a@b.com", intent="say hi")
        args, _ = factory.client.post.call_args
        assert args[0].endswith("/mail/drafts/compose")


# =========================================================================
# 7. Non-200 handling
# =========================================================================

class TestNon200:
    def test_422_returns_status_and_error(self):
        resp = _mock_response(422, {"detail": {"to": "invalid"}})
        factory = _patched_client(resp)
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            result = json.loads(mail_compose_tool(to="a@b.com", intent="say hi"))
        assert result["success"] is False
        assert result["status"] == 422
        assert "to" in result["error"]


# =========================================================================
# 8. Transport failure — fail-soft, no exception escapes
# =========================================================================

class TestTransportFailure:
    def test_httpx_exception_is_failsoft(self):
        import httpx

        factory = _patched_client(raise_exc=httpx.ConnectError("boom"))
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            raw = mail_compose_tool(to="a@b.com", intent="say hi")
        result = json.loads(raw)
        assert result["success"] is False
        assert "could not reach" in result["error"]

    def test_generic_exception_is_failsoft(self):
        factory = _patched_client(raise_exc=RuntimeError("unexpected"))
        with patch("tools.mail_compose_tool.httpx.Client", factory):
            raw = mail_compose_tool(to="a@b.com", intent="say hi")
        result = json.loads(raw)
        assert result["success"] is False
        assert "could not reach" in result["error"]


# =========================================================================
# 9. Registration
# =========================================================================

class TestRegistration:
    def test_registered_in_registry(self):
        from tools.registry import registry

        entry = registry.get_entry("mail_compose")
        assert entry is not None

    def test_in_hermes_core_tools(self):
        import toolsets

        assert "mail_compose" in toolsets._HERMES_CORE_TOOLS
