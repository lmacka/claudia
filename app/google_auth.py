"""
Google OAuth 2.0 Authorization Code + PKCE flow.

Two scope sets:

  IDENTITY_SCOPES — sign-in only. Used during /setup/2 when the user picks
    "Sign in with Google" and during /login subsequently. Yields an ID
    token from which we extract email + name; bound one-shot via
    KV_GOOGLE_IDENTITY_EMAIL.

  TOOL_SCOPES — Gmail read + compose-drafts + Calendar events. Requested
    only when the user has enabled Google integration in /settings (or
    step 3 of the wizard). Identity scopes are unioned in too so the
    identity flow keeps working off the same token.

Token is stored on NFS at /data/.credentials/google_oauth_token.json (0600).
Refreshes happen transparently via google-auth.

v0.8 phase C per /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md.
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

IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

TOOL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]

# Back-compat alias — `SCOPES` historically meant "all the tool scopes".
# Anything that imports SCOPES gets the tool list; identity callers must
# request IDENTITY_SCOPES (or the union) explicitly.
SCOPES = TOOL_SCOPES


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


# state_token -> {"verifier": str, "scopes": list[str], "purpose": str}
# `purpose` is "identity" | "tools" | "both"; the callback uses it to
# decide whether to issue a login cookie, register tools, or both.
_pending: dict[str, dict] = {}


def begin_flow(
    cfg: GoogleAuthConfig,
    scopes: list[str] | None = None,
    purpose: str = "tools",
) -> tuple[str, str]:
    """Returns (auth_url, state_token). Stashes verifier + scopes + purpose
    so the callback can resume correctly.

    `scopes` defaults to TOOL_SCOPES (back-compat with the v0.7 callers).
    The wizard-side identity flow passes IDENTITY_SCOPES; the
    /settings-side tool-enable flow passes TOOL_SCOPES + IDENTITY_SCOPES
    (so the same token serves both).
    """
    requested_scopes = list(scopes) if scopes is not None else TOOL_SCOPES
    flow = Flow.from_client_config(
        _client_config(cfg), scopes=requested_scopes, redirect_uri=cfg.redirect_uri
    )
    state_token = secrets.token_urlsafe(24)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state_token,
    )
    _pending[state_token] = {
        "verifier": getattr(flow, "code_verifier", "") or "",
        "scopes": requested_scopes,
        "purpose": purpose,
    }
    return auth_url, state_token


@dataclass
class ExchangeResult:
    """What the callback gets back from a successful exchange."""

    credentials: Credentials
    purpose: str  # "identity" | "tools" | "both"
    identity_email: str | None  # populated when openid scope was granted
    identity_name: str | None


def exchange_code(
    cfg: GoogleAuthConfig, code: str, returned_state: str
) -> ExchangeResult:
    """Swap the auth code for tokens. If identity scopes were requested,
    decode the ID token and return the email + name alongside the
    credentials. Caller (the /oauth/callback handler) decides whether to
    issue a login cookie based on the recorded purpose."""
    pending = _pending.pop(returned_state, None)
    if pending is None:
        raise ValueError("invalid or expired state token")
    code_verifier = pending["verifier"]
    requested_scopes = pending["scopes"]
    purpose = pending.get("purpose", "tools")

    flow = Flow.from_client_config(
        _client_config(cfg),
        scopes=requested_scopes,
        redirect_uri=cfg.redirect_uri,
        state=returned_state,
    )
    if code_verifier:
        # Restore the verifier Google used when generating the code_challenge.
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    _persist(cfg.token_path, creds)

    identity_email: str | None = None
    identity_name: str | None = None
    if "openid" in requested_scopes:
        identity_email, identity_name = _decode_id_token(creds, cfg.client_id)

    return ExchangeResult(
        credentials=creds,
        purpose=purpose,
        identity_email=identity_email,
        identity_name=identity_name,
    )


def _decode_id_token(creds: Credentials, client_id: str) -> tuple[str | None, str | None]:
    """Verify + decode the ID token attached to the credentials. Returns
    (email, name) on success, (None, None) on any failure — the caller
    should treat absence as 'identity not bound'."""
    id_token_value = getattr(creds, "id_token", None)
    if not id_token_value:
        return (None, None)
    try:
        from google.oauth2 import id_token as google_id_token

        info = google_id_token.verify_oauth2_token(
            id_token_value, GoogleRequest(), client_id
        )
        return (info.get("email"), info.get("name"))
    except Exception as e:  # noqa: BLE001
        log.warning("google_auth.id_token_decode_failed", error=str(e))
        return (None, None)


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
