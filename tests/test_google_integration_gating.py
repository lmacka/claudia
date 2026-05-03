"""T-NEW-F: Gmail + Calendar tools are gated at the registry.

The schema used to claim `kid.safety.write_tools_disabled: const true`,
but the code registered the Google tools unconditionally. This test
locks the new shape: kid mode never registers Google tools regardless
of any env vars, and adult mode is off by default — opt-in via
`adult.integrations.google.enabled`.
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
    mode: str,
    google_enabled: bool,
    with_oauth_secrets: bool,
) -> config_module.Config:
    """Construct a Config directly so we can vary each axis independently."""
    return config_module.Config(
        mode=mode,  # type: ignore[arg-type]
        ops_mode="local",
        data_root=tmp_path,
        prompts_dir=tmp_path / "prompts",
        anthropic_api_key="",
        basic_auth_user="liam",
        basic_auth_password="",
        default_model="claude-sonnet-4-6",
        dev_model="claude-haiku-4-5",
        classifier_model="claude-haiku-4-5",
        display_name="Liam",
        country="AU",
        ingress_host="claudia.example.com",
        kid_parent_display_name="your parent",
        google_client_id="cid" if with_oauth_secrets else "",
        google_client_secret="csec" if with_oauth_secrets else "",
        google_redirect_uri="https://claudia.example.com/oauth/callback",
        adult_integrations_google_enabled=google_enabled,
    )


@pytest.fixture(autouse=True)
def _seed_state(tmp_path: Path):
    """Tool registry helpers read state.library / state.people directly."""
    from app.library import Library
    from app.people import People

    main_module.state.library = Library(tmp_path / "library")
    main_module.state.people = People(tmp_path / "people")
    yield


def test_kid_mode_never_registers_google_tools_even_with_secrets(tmp_path: Path) -> None:
    """Kid mode + OAuth secrets present + flag on → still no Google tools."""
    cfg = _build_cfg(tmp_path, mode="kid", google_enabled=True, with_oauth_secrets=True)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(reg.names()), (
        f"kid mode must not register Google tools; got {reg.names() & GOOGLE_TOOL_NAMES}"
    )


def test_adult_mode_default_no_google_tools(tmp_path: Path) -> None:
    """Adult mode, flag NOT set, secrets present → tools off (default-off posture)."""
    cfg = _build_cfg(tmp_path, mode="adult", google_enabled=False, with_oauth_secrets=True)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(reg.names())


def test_adult_mode_flag_on_secrets_missing_no_google_tools(tmp_path: Path) -> None:
    """Adult mode + flag on but no OAuth secrets → tools still not registered."""
    cfg = _build_cfg(tmp_path, mode="adult", google_enabled=True, with_oauth_secrets=False)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(reg.names())


def test_adult_mode_flag_on_with_secrets_registers_google_tools(tmp_path: Path) -> None:
    """Adult mode + flag on + secrets present → tools registered."""
    cfg = _build_cfg(tmp_path, mode="adult", google_enabled=True, with_oauth_secrets=True)
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.issubset(reg.names()), (
        f"missing Google tools: {GOOGLE_TOOL_NAMES - reg.names()}"
    )


def test_google_enabled_helper_kid_always_false(tmp_path: Path) -> None:
    """Helper truth table — used by /connect-gmail + /oauth/callback gates."""
    kid_on = _build_cfg(tmp_path, mode="kid", google_enabled=True, with_oauth_secrets=True)
    kid_off = _build_cfg(tmp_path, mode="kid", google_enabled=False, with_oauth_secrets=True)
    adult_on = _build_cfg(tmp_path, mode="adult", google_enabled=True, with_oauth_secrets=True)
    adult_off = _build_cfg(tmp_path, mode="adult", google_enabled=False, with_oauth_secrets=True)
    main_module.state.cfg = kid_on
    assert main_module._google_enabled(kid_on) is False
    main_module.state.cfg = kid_off
    assert main_module._google_enabled(kid_off) is False
    main_module.state.cfg = adult_on
    assert main_module._google_enabled(adult_on) is True
    main_module.state.cfg = adult_off
    assert main_module._google_enabled(adult_off) is False


# ---------------------------------------------------------------------------
# UI toggle (file-backed override of the env var)
# ---------------------------------------------------------------------------


def test_file_override_enables_when_env_off(tmp_path: Path) -> None:
    """Adult mode + env off + file says true → tools registered."""
    cfg = _build_cfg(tmp_path, mode="adult", google_enabled=False, with_oauth_secrets=True)
    main_module.state.cfg = cfg
    main_module._save_google_enabled(True)
    assert main_module._google_enabled(cfg) is True
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.issubset(reg.names())


def test_file_override_disables_when_env_on(tmp_path: Path) -> None:
    """Adult mode + env on + file says false → tools not registered."""
    cfg = _build_cfg(tmp_path, mode="adult", google_enabled=True, with_oauth_secrets=True)
    main_module.state.cfg = cfg
    main_module._save_google_enabled(False)
    assert main_module._google_enabled(cfg) is False
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(reg.names())


def test_kid_mode_ignores_file_override(tmp_path: Path) -> None:
    """Even if the override file says true, kid mode never honours it."""
    cfg = _build_cfg(tmp_path, mode="kid", google_enabled=False, with_oauth_secrets=True)
    main_module.state.cfg = cfg
    main_module._save_google_enabled(True)
    assert main_module._google_enabled(cfg) is False
    reg = main_module._build_tool_registry(cfg)
    assert GOOGLE_TOOL_NAMES.isdisjoint(reg.names())
