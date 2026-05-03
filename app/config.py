"""
Runtime configuration for claudia.

Two ortho dimensions:
- persona mode: adult | kid (Helm release setting; immutable post-install)
- ops mode:     local | dev | prod (operational only, never user-visible)

Env vars (all optional, have sane defaults):
    CLAUDIA_MODE              adult | kid          (default: adult)
    CLAUDIA_OPS_MODE          local | dev | prod   (default: prod)
    CLAUDIA_DATA_ROOT         path to /data        (default: /data)
    CLAUDIA_PROMPTS_DIR       path to prompts      (default: /app/app/prompts)
    CLAUDIA_DISPLAY_NAME      e.g. "Liam" / "Jasper"
    CLAUDIA_COUNTRY           default: AU
    CLAUDIA_INGRESS_HOST      e.g. claudia.example.com
    CLAUDIA_KID_PARENT_DISPLAY_NAME  what claudia calls the deployer ("your dad")
    CLAUDIA_ADULT_INTEGRATIONS_GOOGLE_ENABLED  "true"/"1"/"yes" to enable
                              Gmail+Calendar tool registration in adult mode.
                              Default: false. Ignored in kid mode (always off).
    ANTHROPIC_API_KEY         required in dev/prod
    BASIC_AUTH_USER           default: liam (adult mode chat user OR parent-admin)
    BASIC_AUTH_PASSWORD       adult: chat password; kid: parent-admin password
    CLAUDIA_DEFAULT_MODEL     claude-sonnet-4-6 | claude-opus-4-7 (default sonnet)
    CLAUDIA_DEV_MODEL         model for dev mode (default claude-haiku-4-5)
    CLAUDIA_MODEL_CLASSIFIER  haiku model for safety classifier (kid mode only)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

OpsMode = Literal["local", "dev", "prod"]
PersonaMode = Literal["adult", "kid"]


@dataclass(frozen=True)
class Config:
    """Operational configuration loaded from env vars at boot.

    Credentials (anthropic_api_key, google_client_id/secret/redirect_uri,
    basic_auth_password) are **boot-time fallbacks only** in v0.8+. The
    runtime config layer (app/runtime_config.py) reads env first, kv_store
    second; user edits made via /setup or /settings persist to kv_store.
    Use runtime_config_mod.get_anthropic_key(cfg.data_root) etc. instead of
    reading these fields directly. The fields stay here so existing test
    fixtures + the lifespan auto-import know what env names to look at.
    """

    mode: PersonaMode
    ops_mode: OpsMode
    data_root: Path
    prompts_dir: Path
    anthropic_api_key: str  # boot-time fallback; runtime_config is the canonical reader
    basic_auth_user: str
    basic_auth_password: str  # required in kid mode (parent admin); empty otherwise
    default_model: str
    dev_model: str
    classifier_model: str
    display_name: str
    country: str
    ingress_host: str
    kid_parent_display_name: str
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    adult_integrations_google_enabled: bool = False

    @property
    def is_local(self) -> bool:
        return self.ops_mode == "local"

    @property
    def is_dev(self) -> bool:
        return self.ops_mode == "dev"

    @property
    def is_kid(self) -> bool:
        return self.mode == "kid"

    @property
    def is_adult(self) -> bool:
        return self.mode == "adult"


def load() -> Config:
    mode = os.environ.get("CLAUDIA_MODE", "adult")
    if mode not in ("adult", "kid"):
        raise ValueError(f"CLAUDIA_MODE must be adult|kid, got {mode!r}")

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
    classifier_model = os.environ.get("CLAUDIA_MODEL_CLASSIFIER", "claude-haiku-4-5-20251001")

    display_name = os.environ.get("CLAUDIA_DISPLAY_NAME", "")
    country = os.environ.get("CLAUDIA_COUNTRY", "AU")
    ingress_host = os.environ.get("CLAUDIA_INGRESS_HOST", "claudia.example.com")
    kid_parent_display_name = os.environ.get("CLAUDIA_KID_PARENT_DISPLAY_NAME", "your parents")

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    google_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    google_redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", f"https://{ingress_host}/oauth/callback"
    )
    google_enabled_raw = os.environ.get("CLAUDIA_ADULT_INTEGRATIONS_GOOGLE_ENABLED", "")
    adult_google_enabled = google_enabled_raw.strip().lower() in ("1", "true", "yes")

    if ops_mode in ("dev", "prod"):
        # ANTHROPIC_API_KEY no longer required at boot — v0.8 captures it via
        # /setup/1 and stores in kv_store (see app/runtime_config.py). The
        # app boots without it; routes that need it (chat, auditor, /report)
        # raise a clear assertion if both env and kv are empty.
        # BASIC_AUTH_PASSWORD still required in kid mode for the parent
        # admin (/admin/*); kid mode setup is operator-side, not in-app.
        if mode == "kid" and not auth_pw:
            raise RuntimeError("BASIC_AUTH_PASSWORD is required in kid mode (parent admin)")

    return Config(
        mode=mode,  # type: ignore[arg-type]
        ops_mode=ops_mode,  # type: ignore[arg-type]
        data_root=data_root,
        prompts_dir=prompts_dir,
        anthropic_api_key=api_key,
        basic_auth_user=auth_user,
        basic_auth_password=auth_pw,
        default_model=default_model,
        dev_model=dev_model,
        classifier_model=classifier_model,
        display_name=display_name,
        country=country,
        ingress_host=ingress_host,
        kid_parent_display_name=kid_parent_display_name,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
        adult_integrations_google_enabled=adult_google_enabled,
    )
