"""
Cookie-based passphrase auth.

The user sets their password in /setup/2 (or via Helm Secret env override).
/login posts password, gets `claudia` cookie. require_auth on /, /session/*,
/library, /people, /settings, /report checks the cookie.
"""

from __future__ import annotations

import json as _json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

log = structlog.get_logger()


# Argon2id parameters — calibrated for ~250ms on a modern x86 core. The
# slowness is the brute-force defense (paired with the IP rate limit).
_ARGON2 = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


# ---------------------------------------------------------------------------
# Passphrase storage
# ---------------------------------------------------------------------------


@dataclass
class AuthState:
    passphrase_hash: str  # Argon2id hash, includes salt + params
    created_at: float
    last_changed_at: float


def auth_path(data_root: Path) -> Path:
    return data_root / ".credentials" / "auth.json"


def is_passphrase_set(data_root: Path) -> bool:
    return auth_path(data_root).exists()


def set_passphrase(data_root: Path, passphrase: str) -> None:
    """Initial passphrase setup OR change (rotates the hash)."""
    if len(passphrase) < 12:
        raise ValueError("passphrase must be at least 12 characters")
    h = _ARGON2.hash(passphrase)
    path = auth_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "passphrase_hash": h,
        "created_at": time.time(),
        "last_changed_at": time.time(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(state, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    log.info("auth.passphrase_set")


def verify_passphrase(data_root: Path, passphrase: str) -> bool:
    """True iff the passphrase matches the stored hash."""
    path = auth_path(data_root)
    if not path.exists():
        return False
    try:
        state = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        log.error("auth.read_error", error=str(e))
        return False

    stored_hash = state.get("passphrase_hash", "")
    if not stored_hash:
        return False

    try:
        _ARGON2.verify(stored_hash, passphrase)
        return True
    except VerifyMismatchError:
        return False
    except InvalidHashError as e:
        log.error("auth.invalid_hash", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

COOKIE_NAME = "claudia"
COOKIE_TTL_SECONDS = 24 * 3600


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


class SessionStore:
    """Persistent session token → display_name map.

    Mirrors to disk on every mutation, prunes expired entries on load,
    rehydrates on startup. Atomic writes (tempfile + rename) keep the file
    consistent under crash or concurrent access. The file is mode 0600.
    """

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / ".credentials" / "sessions.json"
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = _json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as e:
            log.warning("sessions.load_failed", error=str(e))
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for token, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            created_at = entry.get("created_at")
            if not isinstance(created_at, (int, float)):
                continue
            if now - created_at > COOKIE_TTL_SECONDS:
                continue
            self._sessions[token] = {
                "display_name": str(entry.get("display_name", "")),
                "created_at": float(created_at),
            }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(self._sessions), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def add(self, token: str, display_name: str) -> None:
        self._sessions[token] = {
            "display_name": display_name,
            "created_at": time.time(),
        }
        self._persist()

    def get(self, token: str) -> str | None:
        entry = self._sessions.get(token)
        if entry is None:
            return None
        if time.time() - entry["created_at"] > COOKIE_TTL_SECONDS:
            self.remove(token)
            return None
        return entry["display_name"]

    def remove(self, token: str) -> None:
        if self._sessions.pop(token, None) is not None:
            self._persist()

    def __contains__(self, token: str) -> bool:
        return self.get(token) is not None


# ---------------------------------------------------------------------------
# IP rate limiter
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    attempts: list[float]


class IPRateLimiter:
    """Sliding-window rate limit. Default 5 attempts per 15 minutes per IP."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 15 * 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._buckets: dict[str, _Bucket] = {}

    def check(self, ip: str) -> bool:
        now = time.time()
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = _Bucket(attempts=[])
            self._buckets[ip] = bucket
        cutoff = now - self.window_seconds
        bucket.attempts = [t for t in bucket.attempts if t >= cutoff]
        return len(bucket.attempts) < self.max_attempts

    def record(self, ip: str) -> None:
        now = time.time()
        bucket = self._buckets.setdefault(ip, _Bucket(attempts=[]))
        bucket.attempts.append(now)

    def reset(self, ip: str) -> None:
        self._buckets.pop(ip, None)
