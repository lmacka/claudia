"""Tests for /report PDF generation and the auditor's app-feedback writer."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db_audit import app_feedback_tail
from app.storage import SessionHeader
from app.storage_sqlite import SqliteSessionStore
from app.summariser import AppFeedback, append_app_feedback


def _seed_session(tmp_path: Path, session_id: str) -> None:
    """Create a sessions row so app_feedback's FK can be satisfied."""
    SqliteSessionStore(tmp_path).create_session(
        SessionHeader(
            session_id=session_id,
            created_at="2026-05-03T00:00:00+00:00",
            mode="vent",
            model="claude-sonnet-4-6",
            prompt_sha="",
        )
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".setup_complete").write_text("test fixture\n", encoding="utf-8")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


# ---------------------------------------------------------------------------
# app-feedback file writer
# ---------------------------------------------------------------------------


def test_append_app_feedback_writes_rows(tmp_path: Path) -> None:
    sid = "2026-04-25T01-00-00Z_session_aaaa"
    _seed_session(tmp_path, sid)
    items = [
        AppFeedback(quote="this UI keeps scrolling weird", observation="auto-scroll missing on mobile"),
        AppFeedback(quote="", observation="model was too pushy on the wrap"),
    ]
    count = append_app_feedback(tmp_path, sid, items)
    assert count == 2
    body = app_feedback_tail(tmp_path)
    assert f"session {sid}" in body
    assert "scrolling weird" in body
    assert "auto-scroll missing on mobile" in body
    assert "too pushy on the wrap" in body


def test_append_app_feedback_appends_subsequent_entries(tmp_path: Path) -> None:
    _seed_session(tmp_path, "s1")
    _seed_session(tmp_path, "s2")
    append_app_feedback(tmp_path, "s1", [AppFeedback(quote="a", observation="b")])
    append_app_feedback(tmp_path, "s2", [AppFeedback(quote="c", observation="d")])
    body = app_feedback_tail(tmp_path)
    assert "session s1" in body
    assert "session s2" in body
    # Older entry appears first (oldest-first within the rendered window).
    assert body.index("session s1") < body.index("session s2")


def test_append_app_feedback_empty_list_no_rows(tmp_path: Path) -> None:
    result = append_app_feedback(tmp_path, "s", [])
    assert result is None
    # No DB file is created (no migrate() call on empty input).
    assert not (tmp_path / "claudia.db").exists()
    assert app_feedback_tail(tmp_path) == ""


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------


def test_report_form_renders_with_defaults(client: TestClient) -> None:
    r = client.get("/report")
    assert r.status_code == 200
    body = r.text
    assert "Therapist handover" in body
    assert "start_date" in body
    assert "end_date" in body


def _seed_session_jsonl(
    data_root: Path,
    session_id: str,
    when: datetime,
    messages: list[tuple[str, str]],
) -> None:
    sessions_dir = data_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "header",
                    "session_id": session_id,
                    "created_at": when.isoformat(),
                    "mode": "session",
                    "model": "claude-sonnet-4-6",
                    "prompt_sha": "",
                }
            )
            + "\n"
        )
        for role, content in messages:
            fh.write(json.dumps({"type": "message", "role": role, "content": content}) + "\n")


def test_report_404_when_no_sessions_in_range(client: TestClient) -> None:
    today = date.today()
    r = client.post(
        "/report",
        data={"start_date": today.isoformat(), "end_date": today.isoformat()},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_report_generates_pdf_in_local_mode(client: TestClient) -> None:
    import app.main as main_module

    today = datetime.now(UTC)
    _seed_session_jsonl(
        main_module.state.cfg.data_root,
        f"{today.strftime('%Y-%m-%dT%H-%M-%SZ')}_session_aaaa",
        today,
        [("user", "Day was rough."), ("assistant", "What part?")],
    )
    r = client.post(
        "/report",
        data={
            "start_date": today.date().isoformat(),
            "end_date": today.date().isoformat(),
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"

    # last_export.json should be written.
    last = main_module.state.cfg.data_root / "session-exports" / ".last_export.json"
    assert last.exists()
    rec = json.loads(last.read_text(encoding="utf-8"))
    assert "ts" in rec


def test_report_rejects_inverted_dates(client: TestClient) -> None:
    today = date.today()
    yesterday = today - timedelta(days=1)
    r = client.post(
        "/report",
        data={"start_date": today.isoformat(), "end_date": yesterday.isoformat()},
    )
    assert r.status_code == 400


def test_collect_for_report_includes_spine_and_session_logs(tmp_path: Path) -> None:
    """Regression for the WhatsApp/Jasper attribution fail: the handover bundle
    must include the factual spine and the auditor's session-logs in range."""
    from app.main import _collect_for_report

    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "02_patterns.md").write_text(
        "WhatsApp message to Rhi.", encoding="utf-8"
    )
    (tmp_path / "context" / "05_current_state.md").write_text(
        "Currently: tense.", encoding="utf-8"
    )
    (tmp_path / "session-logs").mkdir()
    (tmp_path / "session-logs" / "2026-04-22_test-session.md").write_text(
        "## What was discussed\nLiam sent Rhi a WhatsApp.", encoding="utf-8"
    )
    (tmp_path / "session-logs" / "2026-01-01_old.md").write_text(
        "out of range", encoding="utf-8"
    )
    (tmp_path / "sessions").mkdir()
    _seed_session_jsonl(
        tmp_path,
        "2026-04-22T10-00-00Z_session_aaaa",
        datetime(2026, 4, 22, 10, 0, tzinfo=UTC),
        [("user", "rough day"), ("assistant", "what part?")],
    )

    bundle = _collect_for_report(tmp_path, date(2026, 4, 20), date(2026, 4, 25))
    assert bundle["session_count"] == 1
    assert "WhatsApp message to Rhi" in bundle["spine"]
    assert "Currently: tense" in bundle["spine"]
    assert "Liam sent Rhi a WhatsApp" in bundle["session_logs"]
    assert "out of range" not in bundle["session_logs"]
    assert "rough day" in bundle["transcripts"]


def test_esc_converts_inline_bold_and_italic() -> None:
    """Regression for `**done**` rendering as literal asterisks in the PDF."""
    from app.main import _esc

    assert _esc("hello **done** there") == "hello <b>done</b> there"
    assert _esc("a *word* in italic") == "a <i>word</i> in italic"
    assert _esc("safe <script>") == "safe &lt;script&gt;"
    # Bold survives HTML escaping.
    assert _esc("a & **b** > c") == "a &amp; <b>b</b> &gt; c"
