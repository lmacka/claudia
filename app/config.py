"""
Runtime configuration for claudia.

Three serve modes:
- local:  InMemoryStore + fixture replies + mock API key. Zero external calls.
- dev:    Real Anthropic API using Haiku (cheap). Local filesystem for /data.
- prod:   Full setup — Anthropic with configured model, /data mounted from PVC.

Env vars (all optional, have sane defaults):
    CLAUDIA_OPS_MODE          local | dev | prod   (default: prod)
    CLAUDIA_DATA_ROOT         path to /data        (default: /data)
    CLAUDIA_PROMPTS_DIR       path to prompts      (default: /app/app/prompts)
    ANTHROPIC_API_KEY         required in dev/prod
    BASIC_AUTH_USER           default: liam
    BASIC_AUTH_PASSWORD       required in dev/prod
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
    ops_mode: OpsMode
    data_root: Path
    prompts_dir: Path
    anthropic_api_key: str  # empty in local mode
    basic_auth_user: str
    basic_auth_password: str  # empty in local mode
    default_model: str
    dev_model: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    @property
    def is_local(self) -> bool:
        return self.ops_mode == "local"

    @property
    def is_dev(self) -> bool:
        return self.ops_mode == "dev"


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

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    google_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    # Default redirect host inferred from CLAUDIA_INGRESS_HOST (set by the chart);
    # explicit override always wins.
    ingress_host = os.environ.get("CLAUDIA_INGRESS_HOST", "claudia.example.com")
    google_redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", f"https://{ingress_host}/oauth/callback"
    )

    if ops_mode in ("dev", "prod"):
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required in dev/prod mode"
            )
        if not auth_pw:
            raise RuntimeError(
                "BASIC_AUTH_PASSWORD is required in dev/prod mode"
            )

    return Config(
        ops_mode=ops_mode,  # type: ignore[arg-type]
        data_root=data_root,
        prompts_dir=prompts_dir,
        anthropic_api_key=api_key,
        basic_auth_user=auth_user,
        basic_auth_password=auth_pw,
        default_model=default_model,
        dev_model=dev_model,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
    )
