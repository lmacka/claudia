"""Tests for /session/{id}/review (read-only memory-diff cards)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    from app.db_kv import kv_set as _kv_set
    _kv_set(tmp_path, "setup_completed_at", "test fixture")
    if "app.main" in sys.modules:
        importlib.reload(sys.modules["app.main"])
    import app.main as main_module

    return main_module


def _create_session(client: TestClient) -> str:
    r = client.get("/session/new", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("/", 2)[2]


def test_review_404_for_unknown_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_app(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        r = c.get("/session/does-not-exist/review")
        assert r.status_code == 404


def test_review_renders_empty_state_without_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_app(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        sid = _create_session(c)
        r = c.get(f"/session/{sid}/review")
        assert r.status_code == 200
        assert "No review available" in r.text


def test_review_renders_diff_cards_from_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_app(tmp_path, monkeypatch)
    from app.claude import Usage
    from app.summariser import (
        AppFeedback,
        AuditorReport,
        PeopleUpdate,
        write_audit_sidecar,
    )

    with TestClient(main_module.app) as c:
        sid = _create_session(c)

        report = AuditorReport(
            title="Test session",
            summary_markdown="## What was discussed\n\nstuff happened\n\n## Patterns I noticed\n\n- x",
            current_state_proposed="# Top of mind\n\n- The big thing\n",
            current_state_rationale="Picked up a recurring concern.",
            app_feedback=[AppFeedback(quote="ui jank", observation="scrolling weird")],
            people_updates=[
                PeopleUpdate(action="add", name="Sofia", relationship="english class friend"),
                PeopleUpdate(action="update", id="rhi-001", append_note="late-night pattern"),
            ],
            usage=Usage(),
        )
        write_audit_sidecar(tmp_path, sid, report)

        r = c.get(f"/session/{sid}/review")
        assert r.status_code == 200
        # current_state card
        assert "Top of mind right now" in r.text
        assert "Picked up a recurring concern." in r.text
        # people_updates cards
        assert "Sofia" in r.text
        assert "english class friend" in r.text
        assert "rhi-001" in r.text or "About rhi-001" in r.text
        assert "late-night pattern" in r.text
        # app_feedback card
        assert "Note for the developer" in r.text
        assert "ui jank" in r.text
        # session log expandable
        assert "Session log (full)" in r.text


def test_review_running_state_when_audit_scheduled_no_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_app(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        sid = _create_session(c)
        # Simulate: audit scheduled but not applied yet.
        main_module.state.store.append_event(sid, "audit_scheduled", {})
        r = c.get(f"/session/{sid}/review")
        assert r.status_code == 200
        assert "Still thinking" in r.text


def test_review_failed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _build_app(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        sid = _create_session(c)
        main_module.state.store.append_event(sid, "auditor_failed", {"error": "boom"})
        r = c.get(f"/session/{sid}/review")
        assert r.status_code == 200
        assert "Couldn" in r.text  # "Couldn't generate"
