"""Tests for /settings + theme cookie roundtrip."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    from app.db_kv import kv_set as _kv_set
    _kv_set(tmp_path, "setup_completed_at", "test fixture")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def test_settings_renders_with_default_theme(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert "theme-sage" in r.text  # default
    assert "blush" in r.text  # other swatches present
    assert "high contrast" in r.text


def test_settings_post_sets_cookie_and_persists(client: TestClient) -> None:
    r = client.post("/settings/theme", data={"theme": "lavender"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    cookie = r.cookies.get("claudia_theme")
    assert cookie == "lavender"

    # Subsequent /settings request should reflect the choice in the page.
    r2 = client.get("/settings")
    assert r2.status_code == 200
    assert "theme-lavender" in r2.text


def test_settings_rejects_unknown_theme_falls_back_to_sage(client: TestClient) -> None:
    r = client.post("/settings/theme", data={"theme": "neon-explosion"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.cookies.get("claudia_theme") == "sage"


def test_settings_theme_applies_to_other_pages(client: TestClient) -> None:
    client.post("/settings/theme", data={"theme": "amber"})
    r = client.get("/")
    assert r.status_code == 200
    assert "theme-amber" in r.text
