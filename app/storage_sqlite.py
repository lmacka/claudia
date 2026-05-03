"""SQLite-backed SessionStore. Mirrors NFSSessionStore semantics.

Sessions are rows in `sessions`. Messages and structured events are
unified into the `events` table with a `kind` column ('message' |
'event'). Insertion order is preserved by the AUTOINCREMENT id; reads
ORDER BY id ASC.

This is a clean rebuild — no JSONL import. The historical JSONL files
on existing deploys remain on disk but are no longer read by the app.
A separate one-shot importer can land later if needed (T-NEW-I doc).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.db import connect, migrate, transaction
from app.storage import Message, SessionHeader, SessionMeta

log = structlog.get_logger()


_HEADER_COLUMNS = (
    "id",
    "created_at",
    "mode",
    "model",
    "prompt_sha",
    "title",
    "status",
    "ended_at",
    "token_total",
    "cost_total_usd",
)


def _row_to_header(row) -> SessionHeader:
    return SessionHeader(
        session_id=row["id"],
        created_at=row["created_at"],
        mode=row["mode"],
        model=row["model"],
        prompt_sha=row["prompt_sha"] or "",
        title=row["title"],
        status=row["status"],
        ended_at=row["ended_at"],
        token_total=row["token_total"],
        cost_total_usd=row["cost_total_usd"],
    )


class SqliteSessionStore:
    """SessionStore backed by /data/claudia.db.

    Migration runs on construction so a fresh deploy gets the schema.
    """

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        migrate(data_root)

    # --- protocol methods --------------------------------------------------

    def create_session(self, header: SessionHeader) -> None:
        with connect(self.data_root) as db, transaction(db):
            db.execute(
                """INSERT INTO sessions
                   (id, created_at, mode, model, prompt_sha, title, status,
                    ended_at, token_total, cost_total_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    header.session_id,
                    header.created_at,
                    header.mode,
                    header.model,
                    header.prompt_sha or "",
                    header.title,
                    header.status,
                    header.ended_at,
                    header.token_total,
                    header.cost_total_usd,
                ),
            )
        log.info("session.created", session_id=header.session_id, mode=header.mode)

    def append_message(self, session_id: str, message: Message) -> None:
        payload = json.dumps(
            {"role": message.role, "content": message.content, "meta": message.meta},
            separators=(",", ":"),
        )
        with connect(self.data_root) as db, transaction(db):
            db.execute(
                "INSERT INTO events (session_id, ts, kind, payload) VALUES (?, ?, 'message', ?)",
                (session_id, message.ts, payload),
            )

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        payload_json = json.dumps(
            {"event_type": event_type, "payload": payload},
            separators=(",", ":"),
        )
        with connect(self.data_root) as db, transaction(db):
            db.execute(
                "INSERT INTO events (session_id, ts, kind, payload) VALUES (?, ?, 'event', ?)",
                (session_id, datetime.now(UTC).isoformat(), payload_json),
            )

    def load_header(self, session_id: str) -> SessionHeader:
        with connect(self.data_root) as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(session_id)
        return _row_to_header(row)

    def update_header(self, session_id: str, **changes: Any) -> None:
        if not changes:
            return
        # Validate column names against the known schema to stop accidental
        # SQL injection via dynamic key construction.
        valid_columns = set(_HEADER_COLUMNS) | {"session_id"}
        sets = []
        values: list[Any] = []
        for k, v in changes.items():
            if k not in valid_columns:
                raise ValueError(f"unknown header column: {k}")
            # SessionHeader uses session_id; the table uses id.
            col = "id" if k == "session_id" else k
            sets.append(f"{col} = ?")
            values.append(v)
        values.append(session_id)
        with connect(self.data_root) as db, transaction(db):
            cur = db.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
                values,
            )
            if cur.rowcount == 0:
                raise FileNotFoundError(session_id)

    def load_messages(self, session_id: str) -> list[Message]:
        with connect(self.data_root) as db:
            # Confirm the session exists first so we raise FileNotFoundError
            # rather than silently returning an empty list for an unknown id.
            row = db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(session_id)
            rows = db.execute(
                "SELECT ts, payload FROM events WHERE session_id = ? AND kind = 'message' ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        out: list[Message] = []
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
            out.append(
                Message(
                    role=p.get("role", ""),
                    content=p.get("content", ""),
                    ts=r["ts"],
                    meta=p.get("meta", {}) or {},
                )
            )
        return out

    def list_sessions(self) -> list[SessionMeta]:
        with connect(self.data_root) as db:
            rows = db.execute(
                """SELECT s.*,
                          COALESCE((SELECT MAX(ts) FROM events e WHERE e.session_id = s.id), s.created_at)
                              AS last_activity
                   FROM sessions s
                   ORDER BY s.created_at DESC"""
            ).fetchall()
        return [
            SessionMeta(
                session_id=r["id"],
                created_at=r["created_at"],
                mode=r["mode"],
                model=r["model"],
                status=r["status"],
                title=r["title"],
                last_activity=r["last_activity"],
            )
            for r in rows
        ]

    def active_session(self) -> SessionMeta | None:
        with connect(self.data_root) as db:
            row = db.execute(
                """SELECT s.*,
                          COALESCE((SELECT MAX(ts) FROM events e WHERE e.session_id = s.id), s.created_at)
                              AS last_activity
                   FROM sessions s
                   WHERE s.status = 'active'
                   ORDER BY s.created_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        return SessionMeta(
            session_id=row["id"],
            created_at=row["created_at"],
            mode=row["mode"],
            model=row["model"],
            status=row["status"],
            title=row["title"],
            last_activity=row["last_activity"],
        )

    def has_event(self, session_id: str, event_type: str) -> bool:
        with connect(self.data_root) as db:
            # Filter on JSON payload; events.kind narrows the scan first.
            row = db.execute(
                """SELECT 1 FROM events
                   WHERE session_id = ? AND kind = 'event'
                     AND json_extract(payload, '$.event_type') = ?
                   LIMIT 1""",
                (session_id, event_type),
            ).fetchone()
        return row is not None
