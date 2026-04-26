"""Unit tests for app/auth.py.

Covers:
- Passphrase set + verify happy path.
- Passphrase wrong rejected.
- Minimum-length enforcement (12 chars per design D5).
- IPRateLimiter sliding-window enforcement.
- KidAuthState file is mode 0600.

v1 dev mode: no encryption, no KEK derivation. The passphrase is just
a password. Encryption-coupled tests return at Step 11.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import auth as auth_mod

# ---------------------------------------------------------------------------
# Passphrase set + verify
# ---------------------------------------------------------------------------


def test_set_and_verify_passphrase(tmp_path: Path) -> None:
    assert not auth_mod.is_passphrase_set(tmp_path)
    auth_mod.set_passphrase(tmp_path, "longenoughpassphrase")
    assert auth_mod.is_passphrase_set(tmp_path)
    assert auth_mod.verify_passphrase(tmp_path, "longenoughpassphrase") is True
    assert auth_mod.verify_passphrase(tmp_path, "wrongpassphrase!") is False


def test_min_length_rejected(tmp_path: Path) -> None:
    """Passphrase < 12 chars rejected per /plan-eng-review D5 + design OQ3."""
    with pytest.raises(ValueError, match="12 characters"):
        auth_mod.set_passphrase(tmp_path, "short")


def test_verify_when_no_passphrase_set(tmp_path: Path) -> None:
    """Verifying any passphrase before setup returns False, not raise."""
    assert auth_mod.verify_passphrase(tmp_path, "anything") is False


def test_passphrase_file_mode(tmp_path: Path) -> None:
    """Passphrase file should be 0600 (user-rw, no group/other)."""
    auth_mod.set_passphrase(tmp_path, "longenoughpassphrase")
    path = auth_mod.kid_auth_path(tmp_path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_set_passphrase_change_works(tmp_path: Path) -> None:
    """Calling set_passphrase a second time replaces the hash."""
    auth_mod.set_passphrase(tmp_path, "first-pass-phrase")
    assert auth_mod.verify_passphrase(tmp_path, "first-pass-phrase")
    auth_mod.set_passphrase(tmp_path, "second-pass-phrase")
    assert auth_mod.verify_passphrase(tmp_path, "second-pass-phrase")
    assert not auth_mod.verify_passphrase(tmp_path, "first-pass-phrase")


# ---------------------------------------------------------------------------
# Session token entropy
# ---------------------------------------------------------------------------


def test_new_kid_session_token_unique() -> None:
    tokens = {auth_mod.new_kid_session_token() for _ in range(20)}
    assert len(tokens) == 20  # all unique
    for t in tokens:
        # token_urlsafe(32) → ~43 char string
        assert len(t) >= 32


# ---------------------------------------------------------------------------
# IPRateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_under_threshold() -> None:
    rl = auth_mod.IPRateLimiter(max_attempts=3, window_seconds=60)
    ip = "192.168.1.1"
    for _ in range(3):
        assert rl.check(ip)
        rl.record(ip)
    # 4th attempt blocked
    assert not rl.check(ip)


def test_rate_limiter_separate_ips() -> None:
    rl = auth_mod.IPRateLimiter(max_attempts=2, window_seconds=60)
    rl.record("a")
    rl.record("a")
    assert not rl.check("a")
    assert rl.check("b")  # different IP unaffected


def test_rate_limiter_reset_on_success() -> None:
    rl = auth_mod.IPRateLimiter(max_attempts=2, window_seconds=60)
    rl.record("a")
    rl.record("a")
    assert not rl.check("a")
    rl.reset("a")
    assert rl.check("a")


def test_rate_limiter_window_expiry() -> None:
    """Old attempts outside the window don't count."""
    rl = auth_mod.IPRateLimiter(max_attempts=2, window_seconds=1)
    rl.record("a")
    rl.record("a")
    assert not rl.check("a")
    time.sleep(1.2)
    assert rl.check("a")
