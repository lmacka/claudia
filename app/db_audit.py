"""SQLite-backed audit / mood / app-feedback persistence (T-NEW-I phase 2).

Replaces the file-based stores:
- /data/audit-sidecars/{id}.json  →  audit_reports table
- /data/context/mood-log.jsonl    →  mood_log table
- /data/app-feedback.md           →  app_feedback table

Module-level functions match the old summariser.write_audit_sidecar /
read_audit_sidecar / append_app_feedback API shape — call sites in main.py
and summariser.py change minimally.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.db import connect, migrate, transaction

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Audit reports (sidecar replacement)
# ---------------------------------------------------------------------------


def save_audit_report(data_root: Path, session_id: str, report: dict) -> None:
    """Insert/replace the audit-report row for a session.

    `report` is the same dict shape that was previously serialised to
    /data/audit-sidecars/{id}.json — the /session/{id}/review template
    reads it back via load_audit_report().
    """
    migrate(data_root)
    payload = json.dumps(report, separators=(",", ":"))
    written_at = datetime.now(UTC).isoformat()
    with connect(data_root) as db, transaction(db):
        db.execute(
            """INSERT INTO audit_reports (session_id, written_at, report_json)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   written_at = excluded.written_at,
                   report_json = excluded.report_json""",
            (session_id, written_at, payload),
        )


def load_audit_report(data_root: Path, session_id: str) -> dict | None:
    """Return the saved report, or None if no audit has been written yet."""
    migrate(data_root)
    with connect(data_root) as db:
        row = db.execute(
            "SELECT report_json FROM audit_reports WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["report_json"])
    except json.JSONDecodeError:
        log.warning("audit_report.bad_json", session_id=session_id)
        return None


# ---------------------------------------------------------------------------
# Mood log
# ---------------------------------------------------------------------------


def record_mood(
    data_root: Path,
    session_id: str,
    regulation_score: int,
    ts: str | None = None,
) -> None:
    """Append a mood-capture row. Idempotency is enforced by the caller via
    state.store.has_event(session_id, 'mood_recorded')."""
    if not 1 <= regulation_score <= 10:
        raise ValueError(f"regulation_score must be 1-10, got {regulation_score}")
    migrate(data_root)
    timestamp = ts or datetime.now(UTC).isoformat()
    with connect(data_root) as db, transaction(db):
        db.execute(
            "INSERT INTO mood_log (session_id, ts, regulation_score) VALUES (?, ?, ?)",
            (session_id, timestamp, regulation_score),
        )


def mood_by_session(data_root: Path) -> dict[str, int]:
    """Map session_id -> last regulation_score recorded for that session."""
    migrate(data_root)
    with connect(data_root) as db:
        rows = db.execute(
            """SELECT session_id, regulation_score
               FROM mood_log m
               WHERE id = (
                   SELECT MAX(id) FROM mood_log
                   WHERE session_id = m.session_id
               )"""
        ).fetchall()
    return {r["session_id"]: r["regulation_score"] for r in rows}


def recent_mood_scores(data_root: Path, limit: int = 20) -> list[int]:
    """Most recent N regulation_score values, oldest-first within the window."""
    migrate(data_root)
    with connect(data_root) as db:
        rows = db.execute(
            """SELECT regulation_score FROM mood_log
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    # rows are newest-first; reverse for oldest-first sparkline rendering.
    return [r["regulation_score"] for r in rows][::-1]


# ---------------------------------------------------------------------------
# App feedback
# ---------------------------------------------------------------------------


def append_app_feedback(
    data_root: Path,
    session_id: str,
    items: list[dict],
) -> int:
    """Insert one row per AppFeedback item. Returns the count inserted.

    `items` is a list of {'quote': str, 'observation': str} dicts (the
    AppFeedback dataclass is converted at the call site to keep this
    module dataclass-agnostic).
    """
    if not items:
        return 0
    migrate(data_root)
    timestamp = datetime.now(UTC).isoformat()
    with connect(data_root) as db, transaction(db):
        db.executemany(
            """INSERT INTO app_feedback (ts, session_id, quote, observation)
               VALUES (?, ?, ?, ?)""",
            [
                (timestamp, session_id, it.get("quote", ""), it.get("observation", ""))
                for it in items
            ],
        )
    return len(items)


def app_feedback_tail(data_root: Path, max_chars: int = 2048) -> str:
    """Render the most recent app-feedback items as markdown for the context pack.

    Mirrors what ContextLoader._app_feedback_tail used to read from the
    monolithic app-feedback.md file.
    """
    migrate(data_root)
    with connect(data_root) as db:
        rows = db.execute(
            """SELECT ts, session_id, quote, observation FROM app_feedback
               ORDER BY id DESC LIMIT 50"""
        ).fetchall()
    if not rows:
        return ""
    # Build oldest-first so the prose reads naturally; cap at max_chars.
    lines: list[str] = []
    for r in reversed(rows):
        lines.append(f"### {r['ts']} — session {r['session_id']}")
        if r["quote"]:
            lines.append(f"> {r['quote']}")
        if r["observation"]:
            lines.append(r["observation"])
        lines.append("")
    rendered = "\n".join(lines).strip()
    if len(rendered) > max_chars:
        rendered = rendered[-max_chars:].lstrip()
    return rendered
