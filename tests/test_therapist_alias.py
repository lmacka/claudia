"""Tests for the therapist-alias + additional-instructions plumbing.

v0.8 phase B per /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md:
  - Decision 2: alias-only — bot says "you can call me X" once per session
    if customised. "Claudia" stays as canonical product name everywhere
    else.
  - Decision 3: single "additional instructions" textarea appended to
    companion-adult.md at assemble time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db_kv
from app.context import ContextLoader
from app.runtime_config import KV_ADDITIONAL_INSTRUCTIONS, KV_THERAPIST_ALIAS

# ---------------------------------------------------------------------------
# _opener_alias_directive — once-per-session intro
# ---------------------------------------------------------------------------


def test_alias_directive_empty_when_unset(tmp_path: Path) -> None:
    from app.main import _opener_alias_directive

    assert _opener_alias_directive(tmp_path) == ""


def test_alias_directive_empty_when_default(tmp_path: Path) -> None:
    """'claudia' is the default — no directive needed; the prompt already
    self-references that name."""
    from app.main import _opener_alias_directive

    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "claudia")
    assert _opener_alias_directive(tmp_path) == ""
    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "Claudia")  # case-insensitive
    assert _opener_alias_directive(tmp_path) == ""


def test_alias_directive_renders_with_phrase(tmp_path: Path) -> None:
    from app.main import _opener_alias_directive

    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "Sage")
    out = _opener_alias_directive(tmp_path)
    assert "Sage" in out
    assert "you can call me Sage" in out
    assert "session opener only" in out
    assert "Do not repeat" in out


def test_alias_directive_strips_whitespace(tmp_path: Path) -> None:
    from app.main import _opener_alias_directive

    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "  Sage  ")
    out = _opener_alias_directive(tmp_path)
    assert "you can call me Sage" in out


# ---------------------------------------------------------------------------
# ContextLoader.additional_instructions_provider — appended to block 1
# ---------------------------------------------------------------------------


def _adult_loader_with_instructions(tmp_path: Path, instructions: str) -> ContextLoader:
    """Build an adult ContextLoader with a fixed additional-instructions
    provider returning `instructions`. Companion prompt is read from the
    real prompts dir so we exercise the assemble path end-to-end."""
    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "prompts"
    return ContextLoader(
        data_root=tmp_path,
        prompts_dir=prompts_dir,
        mode="adult",
        display_name="Liam",
        additional_instructions_provider=lambda: instructions,
    )


def test_additional_instructions_appended_to_companion(tmp_path: Path) -> None:
    loader = _adult_loader_with_instructions(
        tmp_path, "Don't moralise. Push back when I'm catastrophising."
    )
    blocks = loader.assemble()
    assert "Additional instructions from the user" in blocks.block1
    assert "Don't moralise" in blocks.block1


def test_additional_instructions_skipped_when_empty(tmp_path: Path) -> None:
    loader = _adult_loader_with_instructions(tmp_path, "")
    blocks = loader.assemble()
    assert "Additional instructions from the user" not in blocks.block1


def test_additional_instructions_skipped_in_kid_mode(tmp_path: Path) -> None:
    """Kid prompts are parent-controlled at deploy time; the in-app
    /settings additional-instructions should never bleed into kid mode."""
    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "prompts"
    loader = ContextLoader(
        data_root=tmp_path,
        prompts_dir=prompts_dir,
        mode="kid",
        display_name="Jasper",
        additional_instructions_provider=lambda: "should not appear",
    )
    blocks = loader.assemble()
    assert "should not appear" not in blocks.block1


def test_additional_instructions_provider_exception_safe(tmp_path: Path) -> None:
    """If the provider raises, the assemble shouldn't bring the whole
    request down — the user-tunable extras are non-essential."""

    def boom():
        raise RuntimeError("kv unreachable")

    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "prompts"
    loader = ContextLoader(
        data_root=tmp_path,
        prompts_dir=prompts_dir,
        mode="adult",
        display_name="Liam",
        additional_instructions_provider=boom,
    )
    blocks = loader.assemble()
    assert "Additional instructions" not in blocks.block1


# ---------------------------------------------------------------------------
# /settings POST handlers — round-trip persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def adult_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_MODE", "adult")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    monkeypatch.setenv("CLAUDIA_DISPLAY_NAME", "Liam")
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    from app.db_kv import kv_set as _kv_set
    _kv_set(tmp_path, "setup_completed_at", "test fixture")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def test_settings_therapist_name_round_trip(adult_client: TestClient, tmp_path: Path) -> None:
    r = adult_client.post(
        "/settings/therapist-name",
        data={"therapist_alias": "Sage"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    import app.main as main_module

    assert db_kv.kv_get(main_module.state.cfg.data_root, KV_THERAPIST_ALIAS) == "Sage"

    # Clear by submitting empty
    r = adult_client.post(
        "/settings/therapist-name",
        data={"therapist_alias": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert not db_kv.kv_exists(main_module.state.cfg.data_root, KV_THERAPIST_ALIAS)


def test_settings_therapist_name_caps_length(adult_client: TestClient) -> None:
    long_name = "x" * 100
    adult_client.post(
        "/settings/therapist-name",
        data={"therapist_alias": long_name},
        follow_redirects=False,
    )
    import app.main as main_module

    stored = db_kv.kv_get(main_module.state.cfg.data_root, KV_THERAPIST_ALIAS)
    assert stored is not None and len(stored) == 40


def test_settings_additional_instructions_round_trip(
    adult_client: TestClient,
) -> None:
    r = adult_client.post(
        "/settings/additional-instructions",
        data={"additional_instructions": "Be more direct. Skip the validation."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    import app.main as main_module

    stored = db_kv.kv_get(main_module.state.cfg.data_root, KV_ADDITIONAL_INSTRUCTIONS)
    assert stored == "Be more direct. Skip the validation."


def test_settings_renders_alias_and_instructions_pre_filled(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """The /settings page reads from kv via the context_processor."""
    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "Atlas")
    db_kv.kv_set(tmp_path, KV_ADDITIONAL_INSTRUCTIONS, "no emojis")
    r = adult_client.get("/settings")
    assert r.status_code == 200
    assert 'value="Atlas"' in r.text
    assert "no emojis" in r.text
