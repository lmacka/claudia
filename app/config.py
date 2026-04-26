"""
Runtime configuration for robo-therapist.

Three serve modes per ARCHITECTURE.md § 11 Phase 1:
- local:  InMemoryStore + fixture replies + mock API key. Zero external calls.
- dev:    Real Anthropic API using Haiku (cheap). Local filesystem for /data.
- prod:   Full setup — Anthropic with configured model, /data mounted from NFS.

Env vars (all optional, have sane defaults):
    ROBO_MODE              local | dev | prod   (default: prod)
    ROBO_DATA_ROOT         path to /data        (default: /data)
    ROBO_PROMPTS_DIR       path to prompts      (default: /app/app/prompts)
    ANTHROPIC_API_KEY      required in dev/prod
    BASIC_AUTH_USER        default: liam
    BASIC_AUTH_PASSWORD    required in dev/prod
    ROBO_DEFAULT_MODEL     claude-sonnet-4-6 | claude-opus-4-7 (default sonnet)
    ROBO_DEV_MODEL         model for dev mode (default claude-haiku-4-5-20251001)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["local", "dev", "prod"]


@dataclass(frozen=True)
class Config:
    mode: Mode
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
        return self.mode == "local"

    @property
    def is_dev(self) -> bool:
        return self.mode == "dev"


def load() -> Config:
    mode = os.environ.get("ROBO_MODE", "prod")
    if mode not in ("local", "dev", "prod"):
        raise ValueError(f"ROBO_MODE must be local|dev|prod, got {mode!r}")

    data_root = Path(os.environ.get("ROBO_DATA_ROOT", "/data"))
    prompts_dir = Path(os.environ.get("ROBO_PROMPTS_DIR", "/app/app/prompts"))

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    auth_user = os.environ.get("BASIC_AUTH_USER", "liam")
    auth_pw = os.environ.get("BASIC_AUTH_PASSWORD", "")

    default_model = os.environ.get("ROBO_DEFAULT_MODEL", "claude-sonnet-4-6")
    dev_model = os.environ.get("ROBO_DEV_MODEL", "claude-haiku-4-5-20251001")

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    google_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    # Default redirect matches ARCHITECTURE.md § 10.4
    google_redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://robo.coopernetes.com/oauth/callback"
    )

    if mode in ("dev", "prod"):
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required in dev/prod mode"
            )
        if not auth_pw:
            raise RuntimeError(
                "BASIC_AUTH_PASSWORD is required in dev/prod mode"
            )

    return Config(
        mode=mode,  # type: ignore[arg-type]
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
