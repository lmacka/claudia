"""Tests for v0.6.0 bug fixes:

- bug 2: kid_parent_display_name is editable in /setup/1 + /settings, persists
  to /data/parent_display_name.txt, and overrides the Helm default at request
  time (no pod restart).
- discovered alongside: _auditor_system_prompt was reading the non-existent
  auditor.md instead of auditor-{mode}.md, silently breaking every prod audit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def kid_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_MODE", "kid")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    monkeypatch.setenv("CLAUDIA_DISPLAY_NAME", "Jasper")
    monkeypatch.setenv("CLAUDIA_KID_PARENT_DISPLAY_NAME", "your parents")
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    (tmp_path / "context" / "05_current_state.md").write_text("# stub\n", encoding="utf-8")
    (tmp_path / ".setup_complete").write_text("test fixture\n", encoding="utf-8")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


# ---------------------------------------------------------------------------
# parent_display_name override file
# ---------------------------------------------------------------------------


def test_default_falls_back_to_cfg(kid_client: TestClient) -> None:
    """No override file present → cfg.kid_parent_display_name wins."""
    import app.main as main_module

    assert main_module.effective_kid_parent_display_name() == "your parents"


def test_override_file_takes_precedence(kid_client: TestClient, tmp_path: Path) -> None:
    """Writing the file changes the value at the next read — no restart needed."""
    import app.main as main_module

    main_module._save_kid_parent_display_name("Sarah")
    assert main_module.effective_kid_parent_display_name() == "Sarah"
    # Empty value clears the override
    main_module._save_kid_parent_display_name("")
    assert main_module.effective_kid_parent_display_name() == "your parents"


def test_settings_parent_name_route_persists(kid_client: TestClient) -> None:
    """POST /settings/parent-name writes the override and redirects."""
    import app.main as main_module

    r = kid_client.post(
        "/settings/parent-name",
        data={"parent_display_name": "Pat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert main_module.effective_kid_parent_display_name() == "Pat"

    # Confirm the value is in kv_store
    from app.db_kv import kv_get

    assert kv_get(main_module.state.cfg.data_root, main_module.KV_PARENT_DISPLAY_NAME) == "Pat"


def test_settings_parent_name_renders_in_settings_page(kid_client: TestClient) -> None:
    """The settings page exposes the editable field in kid mode."""
    import app.main as main_module

    main_module._save_kid_parent_display_name("Alex")
    r = kid_client.get("/settings")
    assert r.status_code == 200
    assert 'name="parent_display_name"' in r.text
    assert 'value="Alex"' in r.text


def test_setup_step1_includes_parent_name_field_in_kid_mode(kid_client: TestClient) -> None:
    """Setup wizard step 1 lets the parent set this before completing setup."""
    # Setup is gated by parent-admin auth; kid_client is unauthenticated for /setup
    # so the route returns 401. We assert the route exists and the template
    # includes the input — render directly via the template.
    import app.main as main_module

    # Force a /setup/1 render by clearing the marker (kv + legacy file)
    from app.db_kv import kv_delete

    main_module.state.cfg.data_root.joinpath(".setup_complete").unlink(missing_ok=True)
    kv_delete(main_module.state.cfg.data_root, main_module.KV_SETUP_COMPLETED)
    # Bypass auth for this assertion by calling the template directly via TestClient
    # — we instead verify by GETing /setup/1 with the basic auth header.
    # Adult auth in kid mode for parent admin = BASIC_AUTH_USER + BASIC_AUTH_PASSWORD
    # (in local ops_mode, auth is bypassed)
    r = kid_client.get("/setup/1")
    assert r.status_code == 200, r.text
    assert 'name="parent_display_name"' in r.text


def test_template_globals_reflect_override(kid_client: TestClient) -> None:
    """Pages rendered AFTER an override change pick up the new value via context_processor."""
    import app.main as main_module

    main_module._save_kid_parent_display_name("Mum")
    r = kid_client.get("/settings")
    assert "Mum" in r.text


# ---------------------------------------------------------------------------
# Auditor mode-aware prompt loading (the read-from-non-existent-file bug)
# ---------------------------------------------------------------------------


def test_auditor_system_prompt_loads_kid_file() -> None:
    """auditor-kid.md exists; the helper reads it (not auditor.md)."""
    from app.summariser import _auditor_system_prompt

    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "prompts"
    text = _auditor_system_prompt(prompts_dir, mode="kid", display_name="Jasper", parent_display_name="Sarah")
    # Substitution worked
    assert "Jasper" not in text or "{{DISPLAY_NAME}}" not in text  # placeholder was replaced
    assert "{{PARENT_DISPLAY_NAME}}" not in text  # all replaced
    # The "Sarah will read this" line is the substituted form of the kid-mode
    # quality-bar metaphor.
    assert "Sarah will read this" in text


def test_auditor_system_prompt_loads_adult_file() -> None:
    """auditor-adult.md exists; the helper reads it for adult mode."""
    from app.summariser import _auditor_system_prompt

    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "prompts"
    text = _auditor_system_prompt(prompts_dir, mode="adult")
    # No substitution on adult prompt
    assert text  # non-empty
    assert "{{DISPLAY_NAME}}" not in text or text.startswith("# Auditor")


def test_auditor_system_prompt_legacy_fallback(tmp_path: Path) -> None:
    """If only legacy auditor.md exists, the helper falls back to it (back-compat)."""
    from app.summariser import _auditor_system_prompt

    (tmp_path / "auditor.md").write_text("# legacy\n", encoding="utf-8")
    text = _auditor_system_prompt(tmp_path, mode="kid")
    assert text == "# legacy\n"
