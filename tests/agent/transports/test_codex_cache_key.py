"""Unit tests for _cap_cache_key in agent/transports/codex.py.

OpenAI/Codex rejects prompt_cache_key strings longer than 64 chars with HTTP
400 string_above_max_length. Long session ids (e.g. council meeting threads
'meeting-...-marketing-<ts>') overflow that limit. _cap_cache_key must:

  * pass keys of length <= 64 through UNCHANGED (no hashing of already-valid
    keys — they remain human-readable cache keys),
  * map any key longer than 64 chars to a stable 64-char SHA256 hex digest,
  * be None-safe (None in -> None out; empty string falls in the passthrough
    branch),
  * NOT prefix-truncate — two distinct long keys that share a 64+ char common
    prefix must map to DIFFERENT capped keys, or per-conversation cache
    isolation breaks (different conversations would collide onto one cache key).

These are pure-function tests: no network, no client, no heavy deps.
"""

import hashlib

from agent.transports.codex import _cap_cache_key


class TestCapCacheKeyPassthrough:
    def test_short_key_passthrough_unchanged(self):
        key = "session-abc-123"
        assert _cap_cache_key(key) is key or _cap_cache_key(key) == key
        assert _cap_cache_key(key) == "session-abc-123"

    def test_exactly_64_chars_passthrough(self):
        """64 is the boundary — <= 64 passes through, so exactly 64 is kept."""
        key = "a" * 64
        result = _cap_cache_key(key)
        assert result == key
        assert len(result) == 64

    def test_empty_string_passthrough(self):
        # Empty string is falsy -> the `not key` branch returns it as-is.
        assert _cap_cache_key("") == ""


class TestCapCacheKeyHashing:
    def test_65_char_key_returns_64_char_string(self):
        key = "b" * 65
        result = _cap_cache_key(key)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_65_char_key_is_not_the_original(self):
        key = "c" * 65
        result = _cap_cache_key(key)
        assert result != key

    def test_long_key_digest_matches_sha256_prefix(self):
        """The digest is the first 64 hex chars of SHA256(key) — stable."""
        key = "meeting-strategy-roundtable-marketing-" + "x" * 40
        assert len(key) > 64  # guard the premise
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:64]
        assert _cap_cache_key(key) == expected

    def test_long_key_is_stable_across_calls(self):
        key = "d" * 100
        assert _cap_cache_key(key) == _cap_cache_key(key)

    def test_capped_long_key_is_within_limit(self):
        # The whole point: a capped long key must itself be <= 64 so the API
        # accepts it.
        key = "e" * 500
        assert len(_cap_cache_key(key)) <= 64


class TestCapCacheKeyNoneSafe:
    def test_none_returns_none(self):
        assert _cap_cache_key(None) is None


class TestCapCacheKeyNoPrefixCollision:
    def test_long_keys_sharing_64_char_prefix_do_not_collide(self):
        """Two distinct long keys sharing a >64-char common prefix must map to
        DIFFERENT capped keys.

        This is the regression the fix exists for: a naive prefix-truncation
        (key[:64]) would map both of these to the SAME 64-char string, merging
        two distinct conversations onto one prompt cache key. SHA256 of the full
        key keeps them distinct.
        """
        common_prefix = "meeting-council-marketing-thread-" + "z" * 40
        assert len(common_prefix) > 64  # guard: prefix alone overflows

        key_a = common_prefix + "-conversation-A"
        key_b = common_prefix + "-conversation-B"

        # Premise check: a prefix-truncation strategy WOULD collide these.
        assert key_a[:64] == key_b[:64]

        capped_a = _cap_cache_key(key_a)
        capped_b = _cap_cache_key(key_b)

        assert capped_a != capped_b, (
            "Long keys sharing a 64-char prefix must produce DIFFERENT capped "
            "keys (no prefix-truncation collision); got identical results"
        )
        # And both are valid 64-char capped keys.
        assert len(capped_a) == 64
        assert len(capped_b) == 64
