"""
Cookie-based passphrase auth. Both modes use the same shape; the role
('adult' | 'kid') namespaces the hash file, session store, and cookie.

Adult mode (v0.7.1):
  - Password is set during /setup/1 (no HTTP Basic Auth prompt).
  - /login posts password, gets `claudia-adult` cookie.
  - require_auth on / and /session/* checks the cookie.

Kid mode (unchanged from v0.5):
  - Kid sets passphrase at first /login, gets `claudia-kid` cookie.
  - Parent admin still uses BASIC_AUTH_PASSWORD on /admin/* (separate role).

v1 dev mode: this is access-control only. No at-rest encryption, no KEK
derivation, no break-glass envelope. The passphrase is just a password.
Encryption returns at Step 11 (see docs/build-plan-v1.md).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


Role = Literal["adult", "kid"]
_VALID_ROLES: tuple[Role, ...] = ("adult", "kid")


def _check_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be 'adult' or 'kid', got {role!r}")


# ---------------------------------------------------------------------------
# Passphrase storage (one file per role under /data/.credentials/)
# ---------------------------------------------------------------------------


@dataclass
class AuthState:
    passphrase_hash: str  # Argon2id hash, includes salt + params
    created_at: float
    last_changed_at: float


def auth_path(data_root: Path, role: Role) -> Path:
    _check_role(role)
    return data_root / ".credentials" / f"{role}_auth.json"


def is_passphrase_set(data_root: Path, role: Role = "kid") -> bool:
    return auth_path(data_root, role).exists()


def set_passphrase(data_root: Path, passphrase: str, role: Role = "kid") -> None:
    """Initial passphrase setup OR change (rotates the hash).

    Role defaults to 'kid' for back-compat with the v0.5 kid-only API.
    Adult-mode call sites pass role='adult'.
    """
    if len(passphrase) < 12:
        raise ValueError("passphrase must be at least 12 characters")
    h = _ARGON2.hash(passphrase)
    path = auth_path(data_root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    state = {
        "passphrase_hash": h,
        "created_at": time.time(),
        "last_changed_at": time.time(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(state, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    log.info("auth.passphrase_set", role=role)


def verify_passphrase(data_root: Path, passphrase: str, role: Role = "kid") -> bool:
    """True iff the passphrase matches the stored hash for `role`."""
    path = auth_path(data_root, role)
    if not path.exists():
        return False

    import json as _json

    try:
        state = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        log.error("auth.read_error", role=role, error=str(e))
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
        log.error("auth.invalid_hash", role=role, error=str(e))
        return False


# Back-compat shim — older call sites pass no role and assume kid.
# Kept so /tests/test_auth.py continues to work without rewrites.
def kid_auth_path(data_root: Path) -> Path:
    return auth_path(data_root, "kid")


# ---------------------------------------------------------------------------
# Session cookies (one cookie + session file per role)
# ---------------------------------------------------------------------------

KID_COOKIE_NAME = "claudia-kid"
ADULT_COOKIE_NAME = "claudia-adult"
COOKIE_TTL_SECONDS = 24 * 3600


def cookie_name(role: Role) -> str:
    _check_role(role)
    return ADULT_COOKIE_NAME if role == "adult" else KID_COOKIE_NAME


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


# Back-compat alias
def new_kid_session_token() -> str:
    return new_session_token()


class SessionStore:
    """Persistent session token → display_name map, per role.

    Earlier (pre-v0.5) the active sessions lived in a process-local dict, so
    every pod restart logged the user out. We mirror to disk on every
    mutation, prune expired entries on load, and rehydrate on startup.

    Atomic writes (tempfile + rename) keep the file consistent under crash
    or concurrent access. The file is mode 0600 so only the pod user can
    read it.
    """

    def __init__(self, data_root: Path, role: Role = "kid") -> None:
        _check_role(role)
        self.role = role
        self.path = data_root / ".credentials" / f"{role}_sessions.json"
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        import json as _json

        try:
            raw = _json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as e:
            log.warning("sessions.load_failed", role=self.role, error=str(e))
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
                "display_name": str(entry.get("display_name", self.role)),
                "created_at": float(created_at),
            }

    def _persist(self) -> None:
        import json as _json

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


# Back-compat alias for callers that hardcoded the old name.
KidSessionStore = SessionStore


# ---------------------------------------------------------------------------
# IP rate limiter (unchanged)
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
