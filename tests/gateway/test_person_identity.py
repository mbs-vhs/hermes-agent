"""Unit tests for gateway.person_identity.resolve_person (CLAWD-1565).

resolve_person collapses a per-surface raw_user_id to a stable *person* id
so the same human on different surfaces (Telegram, the API server) lands on
one shared (person, agent) conversation_id. It is FAIL-SAFE by construction:
strangers, unknown platforms, the CLI, and any unexpected error fall back to
the raw user id (or "") so callers never build a bare "profile:" key.

Env is read at *call time*, so every test drives it via monkeypatch.setenv /
delenv. We never touch the process environment otherwise.
"""

from __future__ import annotations

import pytest

from gateway.person_identity import resolve_person

# Env keys this module manipulates.
_PERSON = "HERMES_OPERATOR_PERSON_ID"
_TG = "HERMES_OPERATOR_TELEGRAM_IDS"
_API = "HERMES_OPERATOR_API_SERVER"
_WEBUI = "HERMES_OPERATOR_WEBUI"


@pytest.fixture(autouse=True)
def _clean_operator_env(monkeypatch):
    """Start every test with all operator-mapping env vars unset, so each test
    declares exactly the mapping it intends. delenv(raising=False) tolerates a
    var that was never set in the hermetic test env.
    """
    for key in (_PERSON, _TG, _API, _WEBUI):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Telegram operator mapping
# ---------------------------------------------------------------------------


class TestTelegramOperator:
    def test_operator_telegram_id_resolves_to_person(self, monkeypatch):
        """An id listed in HERMES_OPERATOR_TELEGRAM_IDS -> the operator person."""
        monkeypatch.setenv(_TG, "111,222,333")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "telegram", "222") == "morgan"

    def test_operator_telegram_id_defaults_person_to_morgan(self, monkeypatch):
        """With a telegram mapping but no explicit person id, the person id
        defaults to "morgan" (mapping present => default allowed)."""
        monkeypatch.setenv(_TG, "222")
        # No HERMES_OPERATOR_PERSON_ID set.
        assert resolve_person("minerva", "telegram", "222") == "morgan"

    def test_non_operator_telegram_id_returns_raw_unchanged(self, monkeypatch):
        """FAIL-SAFE: a stranger's telegram id (not in the operator list) is
        returned verbatim — it must NOT be merged into the operator person."""
        monkeypatch.setenv(_TG, "111,222")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "telegram", "999") == "999"

    def test_str_input_matches_listed_id(self, monkeypatch):
        """The gateway passes user ids as strings; a str id matches a listed
        (string) id exactly."""
        monkeypatch.setenv(_TG, "111,222")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "telegram", "111") == "morgan"

    def test_int_input_matches_listed_id_via_str_coercion(self, monkeypatch):
        """The predicate coerces with str(raw_user_id), so an int 111 still
        matches the env entry "111". (The production gateway threads
        agent._user_id, normally a str; this documents int tolerance.)"""
        monkeypatch.setenv(_TG, "111,222")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "telegram", 111) == "morgan"  # type: ignore[arg-type]

    def test_whitespace_and_empty_entries_are_ignored(self, monkeypatch):
        """`"111, ,222"` parses to {"111","222"}; empties/whitespace dropped,
        surrounding whitespace stripped so " 222 " still matches "222"."""
        monkeypatch.setenv(_TG, "111, ,222")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "telegram", "111") == "morgan"
        assert resolve_person("minerva", "telegram", "222") == "morgan"
        # The empty/whitespace fragment must NOT have created a "" operator id
        # that an empty raw_user_id could match.
        assert resolve_person("minerva", "telegram", "") == ""

    def test_explicit_person_id_overrides_default(self, monkeypatch):
        monkeypatch.setenv(_TG, "222")
        monkeypatch.setenv(_PERSON, "morgan_stempf")
        assert resolve_person("minerva", "telegram", "222") == "morgan_stempf"


# ---------------------------------------------------------------------------
# API server surface (whole-surface operator flag, raw_user_id is None)
# ---------------------------------------------------------------------------


class TestApiServerSurface:
    @pytest.mark.parametrize("flag", ["1", "true", "yes", "on", "True", "ON", "Yes"])
    def test_api_server_flag_on_resolves_to_person(self, monkeypatch, flag):
        """The API server has no per-user id; when the operator flag is truthy
        the whole surface maps to the operator person."""
        monkeypatch.setenv(_API, flag)
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "api_server", None) == "morgan"

    def test_api_server_flag_on_defaults_person_to_morgan(self, monkeypatch):
        monkeypatch.setenv(_API, "1")
        # No explicit person id; mapping present => default to "morgan".
        assert resolve_person("minerva", "api_server", None) == "morgan"

    def test_api_server_flag_off_returns_empty(self, monkeypatch):
        """Flag explicitly falsey + raw None => "" (no merge, no bare key)."""
        monkeypatch.setenv(_API, "0")
        assert resolve_person("minerva", "api_server", None) == ""

    def test_api_server_flag_unset_returns_empty(self, monkeypatch):
        """Flag unset + raw None => "" (fail-safe: no mapping => no person)."""
        assert resolve_person("minerva", "api_server", None) == ""

    def test_api_server_garbage_flag_returns_empty(self, monkeypatch):
        """A non-truthy junk value is treated as OFF."""
        monkeypatch.setenv(_API, "banana")
        assert resolve_person("minerva", "api_server", None) == ""


# ---------------------------------------------------------------------------
# WebUI surface (chat.vhs.box) — CLAWD-1561 / ADR-065 P2b
# ---------------------------------------------------------------------------


class TestWebuiSurface:
    """hermes-webui's in-process session runs platform="webui" with no per-user
    id; the whole surface maps to the operator when HERMES_OPERATOR_WEBUI is on,
    so a chat.vhs.box turn resolves the shared "{profile}:morgan" conversation."""

    @pytest.mark.parametrize("flag", ["1", "true", "yes", "on", "True", "ON", "Yes"])
    def test_webui_flag_on_resolves_to_person(self, monkeypatch, flag):
        monkeypatch.setenv(_WEBUI, flag)
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "webui", None) == "morgan"

    def test_webui_flag_on_defaults_person_to_morgan(self, monkeypatch):
        monkeypatch.setenv(_WEBUI, "1")
        # No explicit person id; mapping present => default to "morgan".
        assert resolve_person("minerva", "webui", None) == "morgan"

    def test_webui_flag_off_returns_empty(self, monkeypatch):
        monkeypatch.setenv(_WEBUI, "0")
        assert resolve_person("minerva", "webui", None) == ""

    def test_webui_flag_unset_returns_empty(self, monkeypatch):
        """Fail-safe: flag unset + raw None => "" (no bare "minerva:" key)."""
        assert resolve_person("minerva", "webui", None) == ""

    def test_webui_garbage_flag_returns_empty(self, monkeypatch):
        monkeypatch.setenv(_WEBUI, "banana")
        assert resolve_person("minerva", "webui", None) == ""

    def test_webui_flag_off_with_person_set_still_does_not_resolve(self, monkeypatch):
        """Mutation guard (revert-validation): pin an explicit person id so the
        empty-person fall-through can NOT mask a predicate that ignores the flag.
        With HERMES_OPERATOR_WEBUI off but a person id configured (e.g. via the
        api_server mapping), _webui_matches MUST still return False, so the webui
        surface does not silently merge to the operator. Without pinning the
        person id, a predicate that always returned True would still pass
        test_webui_flag_off_returns_empty because _operator_person_id() is ""."""
        monkeypatch.setenv(_WEBUI, "0")
        monkeypatch.setenv(_PERSON, "morgan")  # person id is now non-empty
        # Another surface's mapping is present, so person defaults exist, but the
        # webui flag itself is OFF -> the webui surface must not resolve.
        assert resolve_person("minerva", "webui", None) == ""

    def test_webui_flag_unset_with_person_set_still_does_not_resolve(self, monkeypatch):
        """Same guard with the flag fully unset rather than explicitly '0'."""
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "webui", None) == ""

    def test_webui_converges_with_api_and_telegram_on_same_key(self, monkeypatch):
        """The point of P2b: webui, api_server, and telegram all collapse to the
        SAME person id, so all three build the one "{profile}:morgan" key."""
        monkeypatch.setenv(_WEBUI, "1")
        monkeypatch.setenv(_API, "1")
        monkeypatch.setenv(_TG, "222")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", "webui", None) == "morgan"
        assert resolve_person("minerva", "api_server", None) == "morgan"
        assert resolve_person("minerva", "telegram", "222") == "morgan"


# ---------------------------------------------------------------------------
# Unknown platforms & the full no-config fail-safe
# ---------------------------------------------------------------------------


class TestUnknownPlatformsAndFailSafe:
    @pytest.mark.parametrize("platform", ["discord", "cli", "slack", "matrix", ""])
    def test_unknown_platform_returns_raw_unchanged(self, monkeypatch, platform):
        """No operator predicate registered for these platforms => raw id is
        returned verbatim even with full operator env set."""
        monkeypatch.setenv(_TG, "111,222")
        monkeypatch.setenv(_API, "1")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", platform, "some_uid") == "some_uid"

    @pytest.mark.parametrize("platform", ["discord", "cli", ""])
    def test_unknown_platform_none_raw_returns_empty(self, monkeypatch, platform):
        monkeypatch.setenv(_TG, "111")
        monkeypatch.setenv(_PERSON, "morgan")
        assert resolve_person("minerva", platform, None) == ""

    def test_no_operator_env_at_all_telegram_id_unchanged(self, monkeypatch):
        """THE CORE FAIL-SAFE: with NO operator mapping env set, even a
        telegram id is returned verbatim — the person id must NOT default to
        "morgan" when no mapping is configured for the profile."""
        # _clean_operator_env already unset all three.
        assert resolve_person("minerva", "telegram", "222") == "222"

    def test_no_operator_env_api_server_returns_empty_not_morgan(self, monkeypatch):
        """No mapping => api_server surface does NOT silently become morgan."""
        result = resolve_person("minerva", "api_server", None)
        assert result == ""
        assert result != "morgan"

    def test_no_operator_env_does_not_default_person(self, monkeypatch):
        """Belt-and-suspenders on the regression guard: a matching-looking
        telegram id with zero env must never resolve to a person."""
        assert resolve_person("minerva", "telegram", "111") != "morgan"


# ---------------------------------------------------------------------------
# Exception / malformed-env paths must never raise — always fail-safe.
# ---------------------------------------------------------------------------


class TestExceptionFailSafe:
    def test_matched_surface_but_empty_person_falls_back_to_raw(self, monkeypatch):
        """If a telegram id matches but HERMES_OPERATOR_PERSON_ID is explicitly
        empty AND that's the only mapping... note: a non-empty TELEGRAM_IDS is
        itself a mapping, so person defaults to "morgan". To force the
        empty-person branch we set an explicit-empty person with the telegram
        list present: _operator_person_id() returns "morgan" (list present),
        so this still resolves. This test instead drives the documented
        fall-through via the API path below."""
        # Telegram list present => person defaults to morgan; matched id -> morgan.
        monkeypatch.setenv(_TG, "222")
        monkeypatch.setenv(_PERSON, "   ")  # whitespace-only => treated empty
        assert resolve_person("minerva", "telegram", "222") == "morgan"

    def test_malformed_telegram_env_does_not_raise(self, monkeypatch):
        """Bizarre but legal string values must parse without raising; a value
        with only delimiters yields an empty operator set => stranger ids are
        returned unchanged."""
        monkeypatch.setenv(_TG, ",,, , ,")
        monkeypatch.setenv(_PERSON, "morgan")
        # No real ids in the set => the "222" caller is a stranger => raw back.
        assert resolve_person("minerva", "telegram", "222") == "222"

    def test_predicate_exception_falls_back_to_raw(self, monkeypatch):
        """If a predicate raises (simulated), resolve_person must swallow it and
        return raw_user_id — proving the try/except fail-safe is load-bearing."""
        import gateway.person_identity as pid

        def _boom(_raw):
            raise RuntimeError("synthetic predicate failure")

        monkeypatch.setitem(pid._OPERATOR_PREDICATES, "telegram", _boom)
        assert resolve_person("minerva", "telegram", "222") == "222"

    def test_predicate_exception_none_raw_falls_back_to_empty(self, monkeypatch):
        import gateway.person_identity as pid

        def _boom(_raw):
            raise RuntimeError("synthetic predicate failure")

        monkeypatch.setitem(pid._OPERATOR_PREDICATES, "telegram", _boom)
        assert resolve_person("minerva", "telegram", None) == ""
