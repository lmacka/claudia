"""
Google OAuth 2.0 Authorization Code + PKCE flow.

Scopes (from ARCHITECTURE.md § 6.2):
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/gmail.compose  (drafts only — app never sends)
  - https://www.googleapis.com/auth/calendar.events

Token is stored on NFS at /data/.credentials/google_oauth_token.json (0600).
Refreshes happen transparently via google-auth.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import structlog
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

log = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]


@dataclass
class GoogleAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    token_path: Path

    def is_complete(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def _client_config(cfg: GoogleAuthConfig) -> dict:
    return {
        "web": {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg.redirect_uri],
        }
    }


# ---------------------------------------------------------------------------
# State storage for PKCE/CSRF — in-memory is fine (single-user, single-replica)
#
# Google's client library auto-generates a PKCE code_verifier on the Flow
# instance when you call authorization_url(). The TOKEN EXCHANGE requires that
# same verifier — which means we must persist it between begin_flow and
# exchange_code, not just the state token.
# ---------------------------------------------------------------------------


_pending: dict[str, str] = {}  # state_token -> code_verifier


def begin_flow(cfg: GoogleAuthConfig) -> tuple[str, str]:
    """Returns (auth_url, state_token). Stashes the code_verifier for later exchange."""
    flow = Flow.from_client_config(
        _client_config(cfg), scopes=SCOPES, redirect_uri=cfg.redirect_uri
    )
    state_token = secrets.token_urlsafe(24)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state_token,
    )
    # flow.code_verifier is populated by authorization_url() when PKCE is active.
    _pending[state_token] = getattr(flow, "code_verifier", "") or ""
    return auth_url, state_token


def exchange_code(
    cfg: GoogleAuthConfig, code: str, returned_state: str
) -> Credentials:
    if returned_state not in _pending:
        raise ValueError("invalid or expired state token")
    code_verifier = _pending.pop(returned_state)
    flow = Flow.from_client_config(
        _client_config(cfg),
        scopes=SCOPES,
        redirect_uri=cfg.redirect_uri,
        state=returned_state,
    )
    if code_verifier:
        # Restore the verifier Google used when generating the code_challenge.
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    _persist(cfg.token_path, creds)
    return creds


def _persist(token_path: Path, creds: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    tmp = token_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(token_path)
    os.chmod(token_path, 0o600)


def load_credentials(cfg: GoogleAuthConfig) -> Credentials | None:
    if not cfg.token_path.exists():
        return None
    try:
        data = json.loads(cfg.token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id") or cfg.client_id,
        client_secret=data.get("client_secret") or cfg.client_secret,
        scopes=data.get("scopes") or SCOPES,
    )
    # google-auth does lazy refresh on valid() check, but we can force now.
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            _persist(cfg.token_path, creds)
    except Exception as e:  # noqa: BLE001
        log.warning("google_auth.refresh_failed", error=str(e))
        return None
    return creds


def status(cfg: GoogleAuthConfig) -> dict:
    """For the UI — 'connected'|'not_connected'|'config_missing'."""
    if not cfg.is_complete():
        return {"state": "config_missing"}
    creds = load_credentials(cfg)
    if creds is None:
        return {"state": "not_connected"}
    return {
        "state": "connected",
        "scopes": creds.scopes or [],
        "expires": creds.expiry.isoformat() if creds.expiry else None,
    }


def revoke(cfg: GoogleAuthConfig) -> None:
    if cfg.token_path.exists():
        cfg.token_path.unlink()
