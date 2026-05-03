"""Tests for the three-stage setup wizard + first-run gate + auto-mark."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _build_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "adult"):
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_MODE", mode)
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDIA_DISPLAY_NAME", "Liam" if mode == "adult" else "Jasper")
    monkeypatch.setenv("CLAUDIA_KID_PARENT_DISPLAY_NAME", "Liam")
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    if "app.main" in sys.modules:
        importlib.reload(sys.modules["app.main"])
    import app.main as main_module

    return main_module


# ---------------------------------------------------------------------------
# First-run gate
# ---------------------------------------------------------------------------


def test_home_redirects_to_setup_when_marker_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/setup/1"


def test_home_does_not_redirect_when_marker_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".setup_complete").write_text("done\n", encoding="utf-8")
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Auto-mark on startup for existing deploys
# ---------------------------------------------------------------------------


def test_auto_mark_when_existing_session_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-populate a session log so the lifespan auto-mark logic kicks in.
    logs = tmp_path / "session-logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "2026-04-01_old-session.md").write_text("# Old\n\nbody\n", encoding="utf-8")

    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as _:
        from app.db_kv import kv_exists

        assert kv_exists(tmp_path, main_module.KV_SETUP_COMPLETED)


def test_auto_mark_does_not_fire_on_truly_fresh_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as _:
        from app.db_kv import kv_exists

        # Nothing pre-populated; lifespan should NOT have marked complete.
        assert not kv_exists(tmp_path, main_module.KV_SETUP_COMPLETED)


# ---------------------------------------------------------------------------
# /setup/1 → /setup/2 → /setup/3 → commit
# ---------------------------------------------------------------------------


def test_setup_root_redirects_to_step1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        r = c.get("/setup", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/setup/1"


def test_setup_step1_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        r = c.get("/setup/1")
        assert r.status_code == 200
        assert "basics" in r.text
        assert "preferred name" in r.text
        assert "date of birth" in r.text


def test_setup_full_flow_writes_background_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        # Step 1
        r = c.post(
            "/setup/1",
            data={
                "preferred_name": "Liam",
                "dob": "1985-02-14",
                "country": "AU",
                "region": "Queensland",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/setup/2"

        # Step 2
        r = c.post(
            "/setup/2",
            data={
                "section_who": "Autistic adult, Brisbane",
                "section_stressors": "Co-parent friction",
                "section_never": "No emojis",
                "section_for": "Thinking partner",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/setup/3"

        # Step 2 GET should now show pre-filled textareas.
        r = c.get("/setup/2")
        assert "No emojis" in r.text
        assert "Thinking partner" in r.text

        # Step 3 GET — recap.
        r = c.get("/setup/3")
        assert r.status_code == 200
        assert "Recap" in r.text
        assert "Liam" in r.text

        # Commit.
        r = c.post("/setup/3", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"  # adult mode lands on home

        # Marker written; 01_background.md composed; setup state gone.
        from app.db_kv import kv_exists

        assert kv_exists(tmp_path, main_module.KV_SETUP_COMPLETED)
        bg = (tmp_path / "context" / "01_background.md").read_text(encoding="utf-8")
        assert "Autistic adult, Brisbane" in bg
        assert "Co-parent friction" in bg
        assert "No emojis" in bg
        assert "Thinking partner" in bg
        assert "1985-02-14" in bg
        assert "Queensland" in bg
        assert not (tmp_path / ".setup_state.json").exists()


def test_setup_full_flow_kid_mode_lands_on_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_client(tmp_path, monkeypatch, mode="kid")
    with TestClient(main_module.app) as c:
        c.post("/setup/1", data={"preferred_name": "Jasper", "dob": "2010-09-22", "country": "AU"})
        c.post(
            "/setup/2",
            data={"section_who": "15yo autistic kid", "section_stressors": "", "section_never": "", "section_for": ""},
        )
        r = c.post("/setup/3", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin"


def test_setup_state_persists_between_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_module = _build_client(tmp_path, monkeypatch)
    with TestClient(main_module.app) as c:
        c.post(
            "/setup/1",
            data={"preferred_name": "Test", "dob": "1990-01-01", "country": "AU", "region": "VIC"},
        )
        # Re-GET step 1 — preferred name should be carried.
        r = c.get("/setup/1")
        assert 'value="Test"' in r.text
        assert 'value="1990-01-01"' in r.text
