"""SQLite connection + schema migrations for claudia.

Per docs/storage-decision.md: SQLite holds the structured artifacts
(sessions, events, audit sidecars, mood log, app feedback, library +
people manifests, profile/setup/parent-name/kid-auth singletons). Blobs
(library/*/raw.{ext}, kid-attach staging) stay on the filesystem.

Single file at /data/claudia.db, WAL journal mode, FK constraints on.

Schema migrations are idempotent forward-only — run on every boot via
migrate(). Each migration is a numbered .sql block; we track the
applied version in a `_schema_version` row so reboots are fast.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_sha TEXT NOT NULL DEFAULT '',
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    ended_at TEXT,
    token_total INTEGER NOT NULL DEFAULT 0,
    cost_total_usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS sessions_status_idx ON sessions(status);
CREATE INDEX IF NOT EXISTS sessions_created_idx ON sessions(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS events_session_idx ON events(session_id, id);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind);

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _db_path(data_root: Path) -> Path:
    return data_root / "claudia.db"


@contextmanager
def connect(data_root: Path) -> Iterator[sqlite3.Connection]:
    """Yield an open connection with PRAGMA already set."""
    db = sqlite3.connect(_db_path(data_root), timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Auto-commit on success, rollback on exception. Uses Python's sqlite3
    implicit-transaction handling (deferred mode is default)."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

CURRENT_VERSION = 1


def _current_version(db: sqlite3.Connection) -> int:
    """Returns 0 if schema_version table missing or empty."""
    try:
        row = db.execute("SELECT MAX(version) AS v FROM _schema_version").fetchone()
        return int(row["v"] or 0)
    except sqlite3.OperationalError:
        return 0


def migrate(data_root: Path) -> None:
    """Idempotent schema migration. Runs at every boot."""
    data_root.mkdir(parents=True, exist_ok=True)
    with connect(data_root) as db:
        version = _current_version(db)
        if version >= CURRENT_VERSION:
            log.debug("db.migrate.up_to_date", version=version)
            return
        log.info("db.migrate.applying", from_version=version, to_version=CURRENT_VERSION)
        with transaction(db):
            db.executescript(SCHEMA_V1)
            db.execute(
                "INSERT INTO _schema_version (version) VALUES (?)",
                (CURRENT_VERSION,),
            )
        log.info("db.migrate.done", version=CURRENT_VERSION)
