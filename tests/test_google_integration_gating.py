"""Gmail + Calendar tools are gated at the registry.

Tools register only when:
  - Google OAuth credentials are configured (env or kv), AND
  - the integrations.google.enabled toggle is on (env or file override).

Either condition false → no Google tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config as config_module
from app import main as main_module

GOOGLE_TOOL_NAMES = {
    "search_gmail",
    "get_gmail_thread",
    "get_gmail_message",
    "save_gmail_attachment",
    "create_gmail_draft",
    "list_calendar_events",
    "create_calendar_event",
    "update_calendar_event",
}


def _build_cfg(
    tmp_path: Path,
    *,
    google_enabled: bool,
    with_oauth_secrets: bool,
) -> config_module.Config:
    return config_module.Config(
        ops_mode="local",
        data_root=tmp_path,
        prompts_dir=tmp_path / "prompts",
        anthropic_api_key="",
        basic_auth_user="liam",
        basic_auth_password="",
        default_model="claude-sonnet-4-6",
        dev_model="claude-haiku-4-5",
        display_name="Liam",
        country="AU",
        ingress_host="claudia.example.com",
        google_client_id="cid" if with_oauth_secrets else "",
        google_client_secret="csec" if with_oauth_secrets else "",
        google_redirect_uri="https://claudia.example.com/oauth/callback",
        integrations_google_enabled=google_enabled,
    )


@pytest.fixture(autouse=True)
def _seed_state(tmp_path: Path):
    """Tool registry helpers read state.library / state.people directly."""
    from app.library import Library
    from app.people import People

    main_module.state.library = Library(tmp_path / "library")
    main_module.state.people = People(tmp_path / "people")
    yield


def _seed_oauth_credentials_in_kv(data_root: Path) -> None:
    """Google credentials are read by `_google_cfg` via
    `runtime_config.get_google_creds()`, which checks env then kv_store."""
    from app import db_kv
    from app.runtime_config import KV_GOOGLE_CLIENT_ID, KV_GOOGLE_CLIENT_SECRET

    db_kv.kv_set(data_root, KV_GOOGLE_CLIENT_ID, "cid")
    db_kv.kv_set(data_root, KV_GOOGLE_CLIENT_SECRET, "csec")


def test_default_no_google_tools(tmp_path: Path) -> None:
    """Flag NOT set, secrets present → tools off (default-off posture)."""
    _seed_oauth_credentials_in_kv(tmp_path)
    cfg = _build_cfg(tmp_path, google_enabled=False, with_oauth_secrets=True)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(set(reg.names()))


def test_flag_on_secrets_missing_no_google_tools(tmp_path: Path) -> None:
    """Flag on but no OAuth secrets → tools still not registered."""
    cfg = _build_cfg(tmp_path, google_enabled=True, with_oauth_secrets=False)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(set(reg.names()))


def test_flag_on_with_secrets_registers_google_tools(tmp_path: Path) -> None:
    """Flag on + secrets present → tools registered."""
    _seed_oauth_credentials_in_kv(tmp_path)
    cfg = _build_cfg(tmp_path, google_enabled=True, with_oauth_secrets=True)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.issubset(set(reg.names())), (
        f"missing Google tools: {GOOGLE_TOOL_NAMES - set(reg.names())}"
    )


# ---------------------------------------------------------------------------
# UI toggle (file-backed override of the env var)
# ---------------------------------------------------------------------------


def test_file_override_enables_when_env_off(tmp_path: Path) -> None:
    """Env off + file says true → tools registered."""
    _seed_oauth_credentials_in_kv(tmp_path)
    cfg = _build_cfg(tmp_path, google_enabled=False, with_oauth_secrets=True)
    main_module.state.cfg = cfg
    main_module._save_google_enabled(True)
    assert main_module._google_enabled(cfg) is True
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.issubset(set(reg.names()))


def test_file_override_disables_when_env_on(tmp_path: Path) -> None:
    """Env on + file says false → tools not registered."""
    _seed_oauth_credentials_in_kv(tmp_path)
    cfg = _build_cfg(tmp_path, google_enabled=True, with_oauth_secrets=True)
    main_module.state.cfg = cfg
    main_module._save_google_enabled(False)
    assert main_module._google_enabled(cfg) is False
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(set(reg.names()))
