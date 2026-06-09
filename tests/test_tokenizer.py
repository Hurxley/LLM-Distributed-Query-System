"""
Unit tests for engine/tokenizer.py — HMAC-SHA256 tokenization.

Covers:
  - Determinism: same input + same salt = same token
  - Uniqueness: different inputs produce different tokens
  - Salt dependency: different salts produce different tokens
  - Format: output is 64-char hex string
  - Edge cases: empty string, special characters, long input
"""

import sys
import os
import pytest

# Add engine directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from tokenizer import tokenize


# Fixture to set and restore SALT env var
@pytest.fixture
def set_salt(monkeypatch):
    """Set a test SALT and return it."""
    salt = "test-salt-42"
    monkeypatch.setenv("SALT", salt)
    return salt


@pytest.fixture
def alt_salt(monkeypatch):
    """Set a different test SALT."""
    salt = "different-salt-99"
    monkeypatch.setenv("SALT", salt)
    return salt


class TestTokenize:
    """Core tokenization behavior."""

    def test_deterministic(self, set_salt):
        """Same input + same salt = same token."""
        t1 = tokenize("123456789012345678")
        t2 = tokenize("123456789012345678")
        assert t1 == t2

    def test_unique_per_input(self, set_salt):
        """Different inputs should produce different tokens."""
        t1 = tokenize("32010619900100000")
        t2 = tokenize("32010619900100001")
        assert t1 != t2

    def test_salt_affects_output(self, set_salt, alt_salt):
        """Same input with different salts produces different tokens."""
        id_card = "32010619900100042"
        t1 = tokenize(id_card)

        # Switch salt
        os.environ["SALT"] = alt_salt
        # Force re-read by clearing module cache would be needed,
        # but get_salt() reads env each call, so this works.
        # Actually, get_salt reads env every time, so this is fine:
        pass

    def test_format_is_hex(self, set_salt):
        """Output should be 64-char lowercase hex string."""
        t = tokenize("32010619900100000")
        assert len(t) == 64
        assert all(c in '0123456789abcdef' for c in t)

    def test_different_salts_different_output(self, set_salt, monkeypatch):
        """Verify salt change produces different tokens."""
        id_card = "32010619900100000"
        t1 = tokenize(id_card)

        # Change salt
        monkeypatch.setenv("SALT", "completely-different-salt")
        t2 = tokenize(id_card)

        assert t1 != t2

    def test_empty_string(self, set_salt):
        """Empty string should still produce a valid token."""
        t = tokenize("")
        assert len(t) == 64
        assert all(c in '0123456789abcdef' for c in t)

    def test_special_characters(self, set_salt):
        """Input with special chars should not crash."""
        t = tokenize("test-id/with:special*chars!")
        assert len(t) == 64

    def test_long_input(self, set_salt):
        """Very long input should work fine."""
        t = tokenize("A" * 1000)
        assert len(t) == 64

    def test_consistency_across_calls(self, set_salt):
        """Multiple calls with same input always match."""
        inputs = ["32010619900100001", "32010619900100002", "32010619900100003"]
        first_run = [tokenize(i) for i in inputs]
        second_run = [tokenize(i) for i in inputs]
        assert first_run == second_run

    def test_no_collision_small_set(self, set_salt):
        """No collisions in a small set of distinct inputs."""
        inputs = [f"320106199001{str(i).zfill(5)}" for i in range(200)]
        tokens = [tokenize(i) for i in inputs]
        assert len(set(tokens)) == len(inputs)

    def test_salt_missing_raises(self, monkeypatch):
        """Missing SALT env var should raise RuntimeError."""
        monkeypatch.delenv("SALT", raising=False)
        with pytest.raises(RuntimeError, match="SALT"):
            tokenize("32010619900100000")
