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

# Phase 2: audit sidecars + mood log + app feedback. Replaces the file-based
# stores (audit-sidecars/{id}.json, context/mood-log.jsonl, app-feedback.md).
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS audit_reports (
    session_id TEXT PRIMARY KEY,
    written_at TEXT NOT NULL,
    report_json TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mood_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    regulation_score INTEGER NOT NULL CHECK (regulation_score BETWEEN 1 AND 10),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS mood_log_session_idx ON mood_log(session_id);
CREATE INDEX IF NOT EXISTS mood_log_ts_idx ON mood_log(ts DESC);

CREATE TABLE IF NOT EXISTS app_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT NOT NULL,
    quote TEXT NOT NULL,
    observation TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS app_feedback_ts_idx ON app_feedback(ts DESC);
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

SCHEMA_V3 = """
-- Phase 3: library + people manifests. Per-entity meta.json files become
-- rows; raw bytes / extracted text / notes stay on the filesystem under
-- /data/library/{id}/ and /data/people/{id}/ respectively.
CREATE TABLE IF NOT EXISTS library_docs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    original_date TEXT,
    original_date_source TEXT,
    date_range_end TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mime TEXT NOT NULL DEFAULT '',
    page_count INTEGER,
    extractor TEXT NOT NULL DEFAULT '',
    extracted_chars INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    supersedes TEXT,
    superseded_by TEXT,
    verification TEXT NOT NULL DEFAULT 'ok',
    verification_json TEXT,
    meta_json TEXT NOT NULL  -- full LibraryDocMeta payload for forward-compat
);
CREATE INDEX IF NOT EXISTS library_docs_status_idx ON library_docs(status);
CREATE INDEX IF NOT EXISTS library_docs_created_idx ON library_docs(created_at DESC);
CREATE INDEX IF NOT EXISTS library_docs_kind_idx ON library_docs(kind);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'active',
    last_mentioned TEXT,
    meta_json TEXT NOT NULL  -- full PersonMeta payload
);
CREATE INDEX IF NOT EXISTS people_status_idx ON people(status);
CREATE INDEX IF NOT EXISTS people_name_lower_idx ON people(LOWER(name));
"""

# Phase 4: kv_store for singleton override files.
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CURRENT_VERSION = 4

# Forward-only migrations keyed by version number. Each runs only when the
# current DB version is below the key.
_MIGRATIONS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
}


def _current_version(db: sqlite3.Connection) -> int:
    """Returns 0 if schema_version table missing or empty."""
    try:
        row = db.execute("SELECT MAX(version) AS v FROM _schema_version").fetchone()
        return int(row["v"] or 0)
    except sqlite3.OperationalError:
        return 0


def migrate(data_root: Path) -> None:
    """Idempotent forward-only schema migration. Runs at every boot."""
    data_root.mkdir(parents=True, exist_ok=True)
    with connect(data_root) as db:
        version = _current_version(db)
        if version >= CURRENT_VERSION:
            log.debug("db.migrate.up_to_date", version=version)
            return
        for v in sorted(_MIGRATIONS):
            if v <= version:
                continue
            log.info("db.migrate.applying", from_version=version, to_version=v)
            with transaction(db):
                db.executescript(_MIGRATIONS[v])
                db.execute("INSERT INTO _schema_version (version) VALUES (?)", (v,))
            version = v
        log.info("db.migrate.done", version=CURRENT_VERSION)
