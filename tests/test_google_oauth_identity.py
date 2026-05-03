"""Phase C tests — Google OAuth identity flow.

Covers:
  - SCOPES split: IDENTITY_SCOPES vs TOOL_SCOPES (back-compat alias)
  - begin_flow records purpose + scopes correctly
  - exchange_code returns ExchangeResult with purpose intact
  - /oauth/callback identity branch issues login cookie
  - /oauth/callback rejects email mismatch (decision 9 — one-shot lock)
  - /login renders "Sign in with Google" button only when identity is bound
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db_kv, google_auth
from app.runtime_config import (
    KV_GOOGLE_CLIENT_ID,
    KV_GOOGLE_CLIENT_SECRET,
    KV_GOOGLE_IDENTITY_EMAIL,
)

# ---------------------------------------------------------------------------
# Scope split + back-compat
# ---------------------------------------------------------------------------


def test_scopes_split_correctly() -> None:
    assert "openid" in google_auth.IDENTITY_SCOPES
    assert any("userinfo.email" in s for s in google_auth.IDENTITY_SCOPES)
    assert any("userinfo.profile" in s for s in google_auth.IDENTITY_SCOPES)
    assert any("gmail.readonly" in s for s in google_auth.TOOL_SCOPES)
    assert any("calendar.events" in s for s in google_auth.TOOL_SCOPES)
    # Back-compat: SCOPES is the tool list
    assert google_auth.SCOPES == google_auth.TOOL_SCOPES
    # Sets are disjoint
    assert set(google_auth.IDENTITY_SCOPES).isdisjoint(set(google_auth.TOOL_SCOPES))


# ---------------------------------------------------------------------------
# begin_flow + _pending records purpose
# ---------------------------------------------------------------------------


def _gcfg(tmp_path: Path) -> google_auth.GoogleAuthConfig:
    return google_auth.GoogleAuthConfig(
        client_id="cid.apps.googleusercontent.com",
        client_secret="csec",
        redirect_uri="https://claudia.example.com/oauth/callback",
        token_path=tmp_path / ".credentials" / "google_oauth_token.json",
    )


def test_begin_flow_default_purpose_is_tools(tmp_path: Path) -> None:
    """Back-compat: callers that pass no scopes get TOOL_SCOPES."""
    google_auth._pending.clear()
    _, state_token = google_auth.begin_flow(_gcfg(tmp_path))
    assert google_auth._pending[state_token]["purpose"] == "tools"
    assert google_auth._pending[state_token]["scopes"] == google_auth.TOOL_SCOPES


def test_begin_flow_records_identity_purpose(tmp_path: Path) -> None:
    google_auth._pending.clear()
    _, state_token = google_auth.begin_flow(
        _gcfg(tmp_path),
        scopes=google_auth.IDENTITY_SCOPES,
        purpose="identity",
    )
    assert google_auth._pending[state_token]["purpose"] == "identity"
    assert "openid" in google_auth._pending[state_token]["scopes"]


def test_begin_flow_records_both_purpose(tmp_path: Path) -> None:
    google_auth._pending.clear()
    _, state_token = google_auth.begin_flow(
        _gcfg(tmp_path),
        scopes=google_auth.IDENTITY_SCOPES + google_auth.TOOL_SCOPES,
        purpose="both",
    )
    assert google_auth._pending[state_token]["purpose"] == "both"


def test_exchange_code_invalid_state_raises(tmp_path: Path) -> None:
    google_auth._pending.clear()
    with pytest.raises(ValueError, match="invalid or expired"):
        google_auth.exchange_code(_gcfg(tmp_path), code="any", returned_state="not-a-real-state")


# ---------------------------------------------------------------------------
# /oauth/callback identity branch — TestClient with mocked exchange_code
# ---------------------------------------------------------------------------


@pytest.fixture
def adult_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "dev")
    monkeypatch.setenv("CLAUDIA_MODE", "adult")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    monkeypatch.setenv("CLAUDIA_DISPLAY_NAME", "Liam")
    monkeypatch.setenv("CLAUDIA_INGRESS_HOST", "claudia.example.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # Pre-seed Google credentials so the callback doesn't 400 on missing-creds.
    db_kv.kv_set(tmp_path, KV_GOOGLE_CLIENT_ID, "cid.apps.googleusercontent.com")
    db_kv.kv_set(tmp_path, KV_GOOGLE_CLIENT_SECRET, "csec")
    # Adult passphrase set so the wizard isn't intercepting routes.
    from app import auth as auth_mod

    auth_mod.set_passphrase(tmp_path, "longenoughpassword", role="adult")
    db_kv.kv_set(tmp_path, "setup_completed_at", "test-fixture")
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def _mock_exchange_result(email: str, purpose: str = "identity") -> google_auth.ExchangeResult:
    creds = MagicMock()
    return google_auth.ExchangeResult(
        credentials=creds,
        purpose=purpose,
        identity_email=email,
        identity_name="Liam",
    )


def test_oauth_callback_identity_first_bind_issues_cookie(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """First identity callback binds the email and sets adult cookie."""
    with patch.object(google_auth, "exchange_code") as mock_exchange:
        mock_exchange.return_value = _mock_exchange_result("liam@example.com", "identity")
        r = adult_client.get(
            "/oauth/callback",
            params={"code": "auth-code", "state": "any-state"},
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text
    # Email is now bound
    assert db_kv.kv_get(tmp_path, KV_GOOGLE_IDENTITY_EMAIL) == "liam@example.com"
    # Cookie is set
    assert "claudia-adult" in r.cookies


def test_oauth_callback_identity_mismatch_rejected(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """Once an identity is bound, a different email gets a 403."""
    db_kv.kv_set(tmp_path, KV_GOOGLE_IDENTITY_EMAIL, "liam@example.com")
    with patch.object(google_auth, "exchange_code") as mock_exchange:
        mock_exchange.return_value = _mock_exchange_result("intruder@example.com", "identity")
        r = adult_client.get(
            "/oauth/callback",
            params={"code": "auth-code", "state": "any-state"},
            follow_redirects=False,
        )
    assert r.status_code == 403, r.text
    assert "bound to liam@example.com" in r.text
    # No cookie issued
    assert "claudia-adult" not in r.cookies
    # Bound email unchanged
    assert db_kv.kv_get(tmp_path, KV_GOOGLE_IDENTITY_EMAIL) == "liam@example.com"


def test_oauth_callback_identity_match_issues_cookie(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """Repeat identity from the bound email logs the user in (no rebind)."""
    db_kv.kv_set(tmp_path, KV_GOOGLE_IDENTITY_EMAIL, "liam@example.com")
    with patch.object(google_auth, "exchange_code") as mock_exchange:
        mock_exchange.return_value = _mock_exchange_result("liam@example.com", "identity")
        r = adult_client.get(
            "/oauth/callback",
            params={"code": "auth-code", "state": "any-state"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "claudia-adult" in r.cookies


def test_oauth_callback_tool_only_renders_success_template(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """Tool-only callback (no identity scopes) just shows the success page."""
    with patch.object(google_auth, "exchange_code") as mock_exchange:
        # purpose="tools", no identity_email
        result = google_auth.ExchangeResult(
            credentials=MagicMock(), purpose="tools", identity_email=None, identity_name=None
        )
        mock_exchange.return_value = result
        r = adult_client.get(
            "/oauth/callback",
            params={"code": "auth-code", "state": "any-state"},
            follow_redirects=False,
        )
    assert r.status_code == 200
    # Identity not bound
    assert not db_kv.kv_exists(tmp_path, KV_GOOGLE_IDENTITY_EMAIL)


# ---------------------------------------------------------------------------
# /auth/google/start — identity entrypoint
# ---------------------------------------------------------------------------


def test_auth_google_start_redirects_to_google(adult_client: TestClient) -> None:
    """POST /auth/google/start kicks off the identity OAuth flow."""
    r = adult_client.post("/auth/google/start", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.google.com/")
    assert "scope=openid" in r.headers["location"] or "openid" in r.headers["location"]


def test_auth_google_start_400_when_creds_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Google client creds, /auth/google/start should refuse."""
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "dev")
    monkeypatch.setenv("CLAUDIA_MODE", "adult")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    monkeypatch.setenv("CLAUDIA_INGRESS_HOST", "claudia.example.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)

    import app.main as main_module

    with TestClient(main_module.app) as c:
        r = c.post("/auth/google/start", follow_redirects=False)
    assert r.status_code == 400
    assert "credentials" in r.text.lower()


# ---------------------------------------------------------------------------
# /login renders "Sign in with Google" only when identity is bound
# ---------------------------------------------------------------------------


def test_login_page_hides_google_button_when_no_identity(
    adult_client: TestClient,
) -> None:
    """No KV_GOOGLE_IDENTITY_EMAIL → no button."""
    r = adult_client.get("/login")
    assert r.status_code == 200
    assert "Sign in with Google" not in r.text


def test_login_page_shows_google_button_when_identity_bound(
    adult_client: TestClient, tmp_path: Path
) -> None:
    """Identity bound → button visible with the bound email shown."""
    db_kv.kv_set(tmp_path, KV_GOOGLE_IDENTITY_EMAIL, "liam@example.com")
    r = adult_client.get("/login")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text
    assert "liam@example.com" in r.text
