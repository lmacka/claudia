"""Direct tests for app/db_audit.py (T-NEW-I phase 2).

Covers the SQLite-backed audit sidecar / mood log / app feedback
accessors. The session FK is satisfied by seeding via SqliteSessionStore
since FK CASCADE is on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db_audit import (
    app_feedback_tail,
    append_app_feedback,
    load_audit_report,
    mood_by_session,
    recent_mood_scores,
    record_mood,
    save_audit_report,
)
from app.storage import SessionHeader
from app.storage_sqlite import SqliteSessionStore


def _seed(tmp_path: Path, *session_ids: str) -> None:
    store = SqliteSessionStore(tmp_path)
    for sid in session_ids:
        store.create_session(
            SessionHeader(
                session_id=sid,
                created_at="2026-05-03T00:00:00+00:00",
                mode="vent",
                model="claude-sonnet-4-6",
                prompt_sha="",
            )
        )


# ---------------------------------------------------------------------------
# Audit reports
# ---------------------------------------------------------------------------


def test_save_and_load_audit_report(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    save_audit_report(
        tmp_path,
        "s1",
        {
            "session_id": "s1",
            "title": "test session",
            "summary_markdown": "## what happened",
            "current_state_proposed": "doing ok",
            "people_updates": [],
            "app_feedback": [],
        },
    )
    loaded = load_audit_report(tmp_path, "s1")
    assert loaded is not None
    assert loaded["title"] == "test session"
    assert loaded["current_state_proposed"] == "doing ok"


def test_save_audit_report_replaces_existing(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    save_audit_report(tmp_path, "s1", {"session_id": "s1", "title": "v1"})
    save_audit_report(tmp_path, "s1", {"session_id": "s1", "title": "v2"})
    loaded = load_audit_report(tmp_path, "s1")
    assert loaded["title"] == "v2"


def test_load_audit_report_missing_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    assert load_audit_report(tmp_path, "no-such-session") is None


# ---------------------------------------------------------------------------
# Mood log
# ---------------------------------------------------------------------------


def test_record_mood_and_query_by_session(tmp_path: Path) -> None:
    _seed(tmp_path, "s1", "s2")
    record_mood(tmp_path, "s1", 7)
    record_mood(tmp_path, "s2", 3)
    moods = mood_by_session(tmp_path)
    assert moods == {"s1": 7, "s2": 3}


def test_record_mood_keeps_latest_per_session(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    record_mood(tmp_path, "s1", 5)
    record_mood(tmp_path, "s1", 8)  # later record wins for mood_by_session
    moods = mood_by_session(tmp_path)
    assert moods == {"s1": 8}


def test_record_mood_rejects_out_of_range(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    with pytest.raises(ValueError):
        record_mood(tmp_path, "s1", 11)
    with pytest.raises(ValueError):
        record_mood(tmp_path, "s1", 0)


def test_recent_mood_scores_returns_oldest_first_within_window(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    for s in (1, 2, 3, 4, 5):
        record_mood(tmp_path, "s1", s)
    assert recent_mood_scores(tmp_path, limit=3) == [3, 4, 5]
    assert recent_mood_scores(tmp_path, limit=10) == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# App feedback
# ---------------------------------------------------------------------------


def test_append_app_feedback_inserts_rows(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    n = append_app_feedback(
        tmp_path,
        "s1",
        [
            {"quote": "this was useful", "observation": "model recovered well"},
            {"quote": "", "observation": "second item"},
        ],
    )
    assert n == 2
    body = app_feedback_tail(tmp_path)
    assert "session s1" in body
    assert "this was useful" in body
    assert "second item" in body


def test_app_feedback_tail_caps_at_max_chars(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    long_obs = "x" * 1000
    for _ in range(5):
        append_app_feedback(tmp_path, "s1", [{"quote": "", "observation": long_obs}])
    tail = app_feedback_tail(tmp_path, max_chars=2048)
    assert len(tail) <= 2048


def test_app_feedback_tail_empty_when_no_data(tmp_path: Path) -> None:
    _seed(tmp_path, "s1")
    assert app_feedback_tail(tmp_path) == ""
