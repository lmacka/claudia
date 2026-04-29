"""
Two-role auth for kid mode.

Adult mode: Basic auth on every route, single password
(BASIC_AUTH_PASSWORD). Same as today.

Kid mode (per /plan-eng-review D5):
  - Kid logs in at GET /login, posts passphrase, gets session cookie
    `claudia-kid` for routes under /. Cookie expires after 24h sliding,
    renewed on activity.
  - Parent admin logs in via Basic auth at /admin/* (separate from kid).
    Same BASIC_AUTH_PASSWORD env var.
  - First-time kid login flow: if no kid passphrase has been set yet, the
    /login page shows a "set passphrase" form (the wireframe's
    kid-firstchat). Kid sets it, server stores Argon2id hash + a verifier
    sentinel.
  - IP rate limit: 5 attempts per 15 min per IP.

v1 dev mode: kid mode is access-control only. No at-rest encryption,
no KEK derivation, no break-glass envelope. The passphrase is just a
password. Encryption returns at Step 11 (see docs/build-plan-v1.md).
"""

from __future__ import annotations

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


_KID_AUTH_FILE = ".credentials/kid_auth.json"


@dataclass
class KidAuthState:
    passphrase_hash: str  # Argon2id hash, includes salt + params
    created_at: float
    last_changed_at: float


def kid_auth_path(data_root: Path) -> Path:
    return data_root / _KID_AUTH_FILE


def is_passphrase_set(data_root: Path) -> bool:
    return kid_auth_path(data_root).exists()


def set_passphrase(data_root: Path, passphrase: str) -> None:
    """
    Initial kid passphrase setup OR change.

    Writes Argon2id hash to /data/.credentials/kid_auth.json. The file
    is created with mode 0600. Caller is responsible for KEK regeneration
    + re-wrapping of any existing encrypted data (passphrase change flow).
    """
    if len(passphrase) < 12:
        raise ValueError("passphrase must be at least 12 characters")
    h = _ARGON2.hash(passphrase)
    path = kid_auth_path(data_root)
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
    log.info("kid_auth.passphrase_set")


def verify_passphrase(data_root: Path, passphrase: str) -> bool:
    """Returns True iff passphrase matches the stored hash."""
    path = kid_auth_path(data_root)
    if not path.exists():
        return False

    import json as _json

    try:
        state = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        log.error("kid_auth.read_error", error=str(e))
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
        log.error("kid_auth.invalid_hash", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Kid session cookie
# ---------------------------------------------------------------------------

KID_COOKIE_NAME = "claudia-kid"
KID_COOKIE_TTL_SECONDS = 24 * 3600


def new_kid_session_token() -> str:
    """Cryptographically random session token for the kid cookie."""
    return secrets.token_urlsafe(32)


_KID_SESSIONS_FILE = ".credentials/kid_sessions.json"


class KidSessionStore:
    """Persistent kid session token → display_name map.

    Earlier in v0.5 the active sessions lived in a process-local dict, so
    every pod restart logged the kid out even though their cookie still had
    23 hours of life. Now we mirror the dict to ``/data/.credentials/
    kid_sessions.json`` on every mutation, prune expired entries on load,
    and rehydrate on startup.

    Atomic writes (tempfile + rename) keep the file consistent under crash
    or concurrent access. The file is mode 0600 so only the pod user can
    read it. Adult mode never instantiates this.
    """

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / _KID_SESSIONS_FILE
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        import json as _json

        try:
            raw = _json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as e:
            log.warning("kid_sessions.load_failed", error=str(e))
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
            if now - created_at > KID_COOKIE_TTL_SECONDS:
                continue
            self._sessions[token] = {
                "display_name": str(entry.get("display_name", "kid")),
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
        """Returns the kid's display_name if the token is valid, else None.

        Tokens past KID_COOKIE_TTL_SECONDS are pruned lazily on lookup.
        """
        entry = self._sessions.get(token)
        if entry is None:
            return None
        if time.time() - entry["created_at"] > KID_COOKIE_TTL_SECONDS:
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
    """
    Sliding-window rate limit. By default 5 attempts per 15 minutes per IP.
    Adequate for a single-tenant family tool; revisit if shared-IP NAT
    scenarios bite (note in /plan-eng-review D5).
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 15 * 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._buckets: dict[str, _Bucket] = {}

    def check(self, ip: str) -> bool:
        """Returns True if the request is allowed."""
        now = time.time()
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = _Bucket(attempts=[])
            self._buckets[ip] = bucket
        # Drop attempts older than window
        cutoff = now - self.window_seconds
        bucket.attempts = [t for t in bucket.attempts if t >= cutoff]
        return len(bucket.attempts) < self.max_attempts

    def record(self, ip: str) -> None:
        """Record one failed attempt. Call only on auth failure."""
        now = time.time()
        bucket = self._buckets.setdefault(ip, _Bucket(attempts=[]))
        bucket.attempts.append(now)

    def reset(self, ip: str) -> None:
        """Clear on successful auth so subsequent typos don't lock out."""
        self._buckets.pop(ip, None)
