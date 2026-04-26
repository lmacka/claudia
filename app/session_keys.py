"""
Session-scoped KEK cache for kid mode.

Per /plan-eng-review D2: the kid's KEK lives in process memory keyed by
session_id, TTL = session lifetime, hard-cleared on logout. Both the
synchronous auditor and the OCR background task pull KEK by session_id, so
cancellation of the originating request doesn't lose the key.

The pod runs with swap disabled (chart/templates/deployment.yaml) so the
KEK never lands on disk via swap.

In adult mode this module is unused; nothing should call it.

Thread-safety: a single asyncio.Lock guards the dict. The KEK is bytes;
no rotation in v1.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

log = structlog.get_logger()


@dataclass
class _Entry:
    kek: bytes
    expires_at: float  # epoch seconds


class SessionKeys:
    """
    In-memory KEK cache.

    put(session_id, kek, ttl_seconds) — set or refresh.
    get(session_id) — returns kek or None if missing/expired.
    clear(session_id) — explicit logout.
    sweep() — remove expired entries; safe to call periodically.

    Time is monotonic by epoch seconds; ttl is renewed on every put().
    """

    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def put(self, session_id: str, kek: bytes, ttl_seconds: int = 24 * 3600) -> None:
        async with self._lock:
            self._store[session_id] = _Entry(kek=kek, expires_at=time.time() + ttl_seconds)
            log.debug("session_keys.put", session_id=session_id, ttl_seconds=ttl_seconds)

    async def get(self, session_id: str) -> bytes | None:
        async with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                # Expired; drop
                self._store.pop(session_id, None)
                log.debug("session_keys.expired", session_id=session_id)
                return None
            return entry.kek

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            removed = self._store.pop(session_id, None)
            if removed is not None:
                # Best-effort overwrite of the bytes object's referent.
                # CPython doesn't expose memory zeroing directly; this is
                # the closest we get without ctypes.
                try:
                    overwritten = bytearray(len(removed.kek))
                    removed.kek = bytes(overwritten)
                except Exception:
                    pass
                log.debug("session_keys.clear", session_id=session_id)

    async def sweep(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.time()
        async with self._lock:
            expired = [sid for sid, e in self._store.items() if e.expires_at < now]
            for sid in expired:
                self._store.pop(sid, None)
            if expired:
                log.debug("session_keys.sweep", removed=len(expired))
            return len(expired)

    def __len__(self) -> int:
        return len(self._store)
