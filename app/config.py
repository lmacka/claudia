"""
Runtime configuration for claudia.

Single-user app. One Helm release per user; the user customises it via the
in-app /setup wizard on first visit.

Env vars (all optional, have sane defaults):
    CLAUDIA_OPS_MODE          local | dev | prod   (default: prod)
    CLAUDIA_DATA_ROOT         path to /data        (default: /data)
    CLAUDIA_PROMPTS_DIR       path to prompts      (default: /app/app/prompts)
    CLAUDIA_DISPLAY_NAME      e.g. "Liam"
    CLAUDIA_COUNTRY           default: AU
    CLAUDIA_INGRESS_HOST      e.g. claudia.example.com
    CLAUDIA_INTEGRATIONS_GOOGLE_ENABLED  "true"/"1"/"yes" to enable
                              Gmail+Calendar tool registration. Default: false.
    ANTHROPIC_API_KEY         optional bootstrap; user enters in /setup/1.
    BASIC_AUTH_USER           default: liam (cookie-auth display label)
    BASIC_AUTH_PASSWORD       optional bootstrap; user picks in /setup/2.
    CLAUDIA_DEFAULT_MODEL     claude-sonnet-4-6 | claude-opus-4-7 (default sonnet)
    CLAUDIA_DEV_MODEL         model for dev mode (default claude-haiku-4-5)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

OpsMode = Literal["local", "dev", "prod"]


@dataclass(frozen=True)
class Config:
    """Operational configuration loaded from env vars at boot.

    Credentials (anthropic_api_key, google_client_id/secret/redirect_uri,
    basic_auth_password) are **boot-time fallbacks only**. The runtime
    config layer (app/runtime_config.py) reads env first, kv_store second;
    user edits made via /setup or /settings persist to kv_store. Use
    runtime_config_mod.get_anthropic_key(cfg.data_root) etc. instead of
    reading these fields directly.
    """

    ops_mode: OpsMode
    data_root: Path
    prompts_dir: Path
    anthropic_api_key: str  # boot-time fallback; runtime_config is the canonical reader
    basic_auth_user: str
    basic_auth_password: str  # optional boot-time fallback
    default_model: str
    dev_model: str
    display_name: str
    country: str
    ingress_host: str
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    integrations_google_enabled: bool = False

    @property
    def is_local(self) -> bool:
        return self.ops_mode == "local"


def load() -> Config:
    ops_mode = os.environ.get("CLAUDIA_OPS_MODE", "prod")
    if ops_mode not in ("local", "dev", "prod"):
        raise ValueError(f"CLAUDIA_OPS_MODE must be local|dev|prod, got {ops_mode!r}")

    data_root = Path(os.environ.get("CLAUDIA_DATA_ROOT", "/data"))
    prompts_dir = Path(os.environ.get("CLAUDIA_PROMPTS_DIR", "/app/app/prompts"))

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    auth_user = os.environ.get("BASIC_AUTH_USER", "liam")
    auth_pw = os.environ.get("BASIC_AUTH_PASSWORD", "")

    default_model = os.environ.get("CLAUDIA_DEFAULT_MODEL", "claude-sonnet-4-6")
    dev_model = os.environ.get("CLAUDIA_DEV_MODEL", "claude-haiku-4-5-20251001")

    display_name = os.environ.get("CLAUDIA_DISPLAY_NAME", "")
    country = os.environ.get("CLAUDIA_COUNTRY", "AU")
    ingress_host = os.environ.get("CLAUDIA_INGRESS_HOST", "claudia.example.com")

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    google_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    google_redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", f"https://{ingress_host}/oauth/callback"
    )
    google_enabled_raw = os.environ.get("CLAUDIA_INTEGRATIONS_GOOGLE_ENABLED", "")
    google_enabled = google_enabled_raw.strip().lower() in ("1", "true", "yes")

    return Config(
        ops_mode=ops_mode,  # type: ignore[arg-type]
        data_root=data_root,
        prompts_dir=prompts_dir,
        anthropic_api_key=api_key,
        basic_auth_user=auth_user,
        basic_auth_password=auth_pw,
        default_model=default_model,
        dev_model=dev_model,
        display_name=display_name,
        country=country,
        ingress_host=ingress_host,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
        integrations_google_enabled=google_enabled,
    )
