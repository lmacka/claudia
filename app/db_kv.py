"""Tiny key-value singleton store.

Used for app-level singletons (setup wizard state, google_enabled toggle,
runtime credential overrides). Sensitive credentials (auth.json,
sessions.json, google_oauth_token.json) stay on disk with mode 0600.

Bytes-level interface: callers pass strings, we store strings. Anything
JSON-shaped is the caller's responsibility to serialise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.db import connect, migrate, transaction


def kv_get(data_root: Path, key: str) -> str | None:
    """Return the stored value, or None if the key is unset."""
    migrate(data_root)
    with connect(data_root) as db:
        row = db.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def kv_set(data_root: Path, key: str, value: str) -> None:
    """Insert or replace the value for `key`. Empty string is allowed."""
    migrate(data_root)
    with connect(data_root) as db, transaction(db):
        db.execute(
            """INSERT INTO kv_store (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )


def kv_delete(data_root: Path, key: str) -> None:
    """Idempotent delete. No error if the key is absent."""
    migrate(data_root)
    with connect(data_root) as db, transaction(db):
        db.execute("DELETE FROM kv_store WHERE key = ?", (key,))


def kv_exists(data_root: Path, key: str) -> bool:
    migrate(data_root)
    with connect(data_root) as db:
        row = db.execute("SELECT 1 FROM kv_store WHERE key = ?", (key,)).fetchone()
    return row is not None
