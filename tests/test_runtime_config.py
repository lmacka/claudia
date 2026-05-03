"""Tests for app/runtime_config.py — credential precedence + auto-import.

Locked-in behaviour from the v0.8 plan:
  - ENV/Secret always wins (decision 8 in
    /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md)
  - kv_store is the fallback when env is empty
  - "" is returned when neither source has a value
  - auto_import_env_secrets is idempotent: only writes when kv key is unset
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import db_kv
from app.runtime_config import (
    KV_ANTHROPIC_API_KEY,
    KV_GOOGLE_CLIENT_ID,
    KV_GOOGLE_CLIENT_SECRET,
    KV_THERAPIST_ALIAS,
    auto_import_env_secrets,
    get_additional_instructions,
    get_anthropic_key,
    get_default_model,
    get_field_source,
    get_google_creds,
    get_therapist_alias,
)

# ---------------------------------------------------------------------------
# Precedence: env > kv > none
# ---------------------------------------------------------------------------


def test_anthropic_key_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    db_kv.kv_set(tmp_path, KV_ANTHROPIC_API_KEY, "sk-ant-from-kv")
    assert get_anthropic_key(tmp_path) == "sk-ant-from-env"


def test_anthropic_key_kv_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db_kv.kv_set(tmp_path, KV_ANTHROPIC_API_KEY, "sk-ant-from-kv")
    assert get_anthropic_key(tmp_path) == "sk-ant-from-kv"


def test_anthropic_key_empty_when_neither_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_anthropic_key(tmp_path) == ""


def test_anthropic_key_env_empty_string_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ANTHROPIC_API_KEY env (Helm Secret unset / blanked) should
    NOT win over a populated kv. Tests the .strip() guard in `_read`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    db_kv.kv_set(tmp_path, KV_ANTHROPIC_API_KEY, "sk-ant-real")
    assert get_anthropic_key(tmp_path) == "sk-ant-real"


def test_google_creds_partial_kv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each Google field is independently sourced. Env client_id + kv
    client_secret should produce a hybrid tuple."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")
    db_kv.kv_set(tmp_path, KV_GOOGLE_CLIENT_SECRET, "kv-secret")
    cid, secret, redirect = get_google_creds(tmp_path)
    assert cid == "env-cid"
    assert secret == "kv-secret"
    assert redirect == ""


def test_kv_only_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """therapist_alias / additional_instructions / default_model have no env
    counterpart. Only kv writes show up."""
    db_kv.kv_set(tmp_path, KV_THERAPIST_ALIAS, "Sage")
    assert get_therapist_alias(tmp_path) == "Sage"
    assert get_additional_instructions(tmp_path) == ""
    # default_model with explicit fallback
    assert get_default_model(tmp_path, fallback="claude-sonnet-4-6") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# get_field_source — what /settings uses to grey-out fields
# ---------------------------------------------------------------------------


def test_field_source_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert get_field_source(tmp_path, "anthropic_api_key") == "env"


def test_field_source_kv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db_kv.kv_set(tmp_path, KV_ANTHROPIC_API_KEY, "sk-ant-from-kv")
    assert get_field_source(tmp_path, "anthropic_api_key") == "kv"


def test_field_source_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_field_source(tmp_path, "anthropic_api_key") == "none"


def test_field_source_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown field"):
        get_field_source(tmp_path, "made_up_field")


# ---------------------------------------------------------------------------
# auto_import_env_secrets — idempotency + ENV-stays-authoritative
# ---------------------------------------------------------------------------


def test_auto_import_copies_env_to_empty_kv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-imported")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "oauth-cid")
    imported = auto_import_env_secrets(tmp_path)
    assert "anthropic_api_key" in imported
    assert "google_client_id" in imported
    assert db_kv.kv_get(tmp_path, KV_ANTHROPIC_API_KEY) == "sk-ant-imported"
    assert db_kv.kv_get(tmp_path, KV_GOOGLE_CLIENT_ID) == "oauth-cid"


def test_auto_import_skips_when_kv_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user has manually set kv via /settings (which only happens
    when env is empty per decision 8), don't overwrite it on later boot."""
    db_kv.kv_set(tmp_path, KV_ANTHROPIC_API_KEY, "sk-ant-existing")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    imported = auto_import_env_secrets(tmp_path)
    assert "anthropic_api_key" not in imported
    assert db_kv.kv_get(tmp_path, KV_ANTHROPIC_API_KEY) == "sk-ant-existing"


def test_auto_import_idempotent_on_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    first = auto_import_env_secrets(tmp_path)
    second = auto_import_env_secrets(tmp_path)
    assert "anthropic_api_key" in first
    assert "anthropic_api_key" not in second  # already imported


def test_auto_import_skips_when_env_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    imported = auto_import_env_secrets(tmp_path)
    assert imported == {}
    # kv stays unset
    assert not db_kv.kv_exists(tmp_path, KV_ANTHROPIC_API_KEY)


def test_auto_import_only_handles_env_backed_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """therapist_alias / additional_instructions are kv-only and never get
    auto-imported because they have no env counterpart."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    imported = auto_import_env_secrets(tmp_path)
    assert "therapist_alias" not in imported
    assert "additional_instructions" not in imported
