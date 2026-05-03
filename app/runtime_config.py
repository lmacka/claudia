"""Runtime credential + override accessors.

Single read path for things the user can set at runtime (Anthropic API key,
Google OAuth client id/secret, therapist alias, additional companion
instructions, default-model override). Each accessor consults env first
(ENV/Secret-mounted values are authoritative), falls back to kv_store, and
returns "" / None when neither source is set.

The auto-import below copies any non-empty env-mounted credentials into
kv_store on first boot so /settings can render their values back to the user
even when the secret is what's actually in use. ENV stays the writer; kv is
the read mirror until/unless the chart stops mounting the Secret.

Per the v0.8 plan in /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md:
  - decision 8: ENV/Secret always wins; UI fields go read-only when env is set
  - decision 4: lifespan auto-imports env-var secrets to kv_store on first boot
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import structlog

from app.db_kv import kv_exists, kv_get, kv_set

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# kv_store keys
# ---------------------------------------------------------------------------

KV_ANTHROPIC_API_KEY = "anthropic_api_key"
KV_GOOGLE_CLIENT_ID = "google_oauth_client_id"
KV_GOOGLE_CLIENT_SECRET = "google_oauth_client_secret"
KV_GOOGLE_REDIRECT_URI = "google_oauth_redirect_uri"
KV_GOOGLE_IDENTITY_EMAIL = "google_identity_email"
KV_THERAPIST_ALIAS = "therapist_alias"
KV_ADDITIONAL_INSTRUCTIONS = "additional_instructions"
KV_DEFAULT_MODEL_OVERRIDE = "default_model"


# Per-field source tracking. Used by /settings to render disabled inputs
# with a "set by Helm Secret — edit chart to change" tooltip when env wins.
FieldSource = Literal["env", "kv", "none"]


# Mapping from accessor name → (env var name, kv key).
# Centralises the precedence so /settings can introspect.
_FIELD_MAP: dict[str, tuple[str, str]] = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY", KV_ANTHROPIC_API_KEY),
    "google_client_id": ("GOOGLE_OAUTH_CLIENT_ID", KV_GOOGLE_CLIENT_ID),
    "google_client_secret": ("GOOGLE_OAUTH_CLIENT_SECRET", KV_GOOGLE_CLIENT_SECRET),
    "google_redirect_uri": ("GOOGLE_OAUTH_REDIRECT_URI", KV_GOOGLE_REDIRECT_URI),
    # therapist_alias / additional_instructions / default_model are kv-only
    # — no env var seeds them. get_field_source returns "kv" or "none" for
    # these.
    "therapist_alias": ("", KV_THERAPIST_ALIAS),
    "additional_instructions": ("", KV_ADDITIONAL_INSTRUCTIONS),
    "default_model": ("", KV_DEFAULT_MODEL_OVERRIDE),
}


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def _read(data_root: Path, field: str) -> str:
    """Env wins; kv fallback; "" if neither."""
    env_name, kv_key = _FIELD_MAP[field]
    if env_name:
        v = os.environ.get(env_name, "").strip()
        if v:
            return v
    try:
        v = kv_get(data_root, kv_key)
    except (OSError, AttributeError):
        return ""
    return (v or "").strip()


def get_anthropic_key(data_root: Path) -> str:
    return _read(data_root, "anthropic_api_key")


def get_google_creds(data_root: Path) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri). Empty strings if unset."""
    return (
        _read(data_root, "google_client_id"),
        _read(data_root, "google_client_secret"),
        _read(data_root, "google_redirect_uri"),
    )


def get_therapist_alias(data_root: Path) -> str:
    """User's preferred name for the bot. Default 'claudia' applied at the
    call site (we return empty string here so callers can detect 'unset')."""
    return _read(data_root, "therapist_alias")


def get_additional_instructions(data_root: Path) -> str:
    """Free-form instructions appended to the companion system prompt."""
    return _read(data_root, "additional_instructions")


def get_default_model(data_root: Path, fallback: str = "") -> str:
    """Model override set in /setup/3 or /settings. Falls back to the
    constructor-time default (cfg.default_model from env) when unset."""
    v = _read(data_root, "default_model")
    return v or fallback


def get_field_source(data_root: Path, field: str) -> FieldSource:
    """Where this field's current value comes from. Used by /settings to
    decide whether the input renders editable or disabled-with-tooltip."""
    if field not in _FIELD_MAP:
        raise ValueError(f"unknown field: {field!r}")
    env_name, kv_key = _FIELD_MAP[field]
    if env_name and os.environ.get(env_name, "").strip():
        return "env"
    try:
        if kv_exists(data_root, kv_key):
            v = kv_get(data_root, kv_key)
            if v and v.strip():
                return "kv"
    except (OSError, AttributeError):
        pass
    return "none"


# ---------------------------------------------------------------------------
# Boot-time env → kv import
# ---------------------------------------------------------------------------


def auto_import_env_secrets(data_root: Path) -> dict[str, str]:
    """Copy any non-empty env-mounted credentials into kv_store on first boot.

    Idempotent: only writes when the kv key is unset. Returns a dict of
    {field_name: source} for keys that were imported (for logging /
    debugging). ENV stays authoritative for reads — this just gives
    /settings something to display when env is the source.
    """
    imported: dict[str, str] = {}
    for field, (env_name, kv_key) in _FIELD_MAP.items():
        if not env_name:
            continue
        env_value = os.environ.get(env_name, "").strip()
        if not env_value:
            continue
        try:
            if kv_exists(data_root, kv_key):
                continue
            kv_set(data_root, kv_key, env_value)
            imported[field] = env_name
        except OSError as e:
            log.warning("runtime_config.import_failed", field=field, error=str(e))
    if imported:
        log.info("runtime_config.env_imported_to_kv", fields=sorted(imported.keys()))
    return imported
