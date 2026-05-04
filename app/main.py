"""
FastAPI app for claudia (simplified build, lifted from robo-therapist).

Single-user, LAN-only, basic-auth. Single ops_mode, single model (Sonnet 4.6).
Removed: tripwire/safety, session timers, modes/model toggle, bridge status,
cost governor, /summary review page, commitments YAML, prometheus metrics,
clean/dev cookie, prompt SHA tracking, per-session PDF export.

Routes:
  GET  /                          home
  GET  /session/new               creates and redirects (no picker)
  GET  /session/<id>              chat view
  GET  /session/<id>/messages-poll  HTMX poll for opener
  POST /session/<id>/message      user turn
  POST /session/<id>/end          ends session, schedules audit
  POST /session/<id>/mood         records regulation score
  GET  /report                    handover report form
  POST /report                    generates one-page PDF
  GET  /connect-gmail / /oauth/callback
  POST /upload, POST /session/<id>/paste
  GET  /healthz, /readyz
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth as auth_mod
from app import claude as claude_mod
from app import config as config_module
from app import google_auth, library_pipeline, safety
from app import runtime_config as runtime_config_mod
from app import setup_autodraft as setup_autodraft_mod
from app import summariser as summariser_mod
from app import tool_loop as tool_loop_mod
from app.claude import SONNET, Usage
from app.context import TZ as BRISBANE_TZ
from app.context import ContextLoader
from app.extractors import build_registry, make_vision_callables
from app.google_auth import GoogleAuthConfig
from app.library import Library
from app.library_stream import StatusBus
from app.people import People
from app.storage import (
    Message,
    SessionHeader,
    SessionStore,
    new_session_id,
)
from app.storage_sqlite import SqliteSessionStore
from app.tools import ToolRegistry
from app.tools.calendar import (
    create_calendar_event_spec,
    list_calendar_events_spec,
    update_calendar_event_spec,
)
from app.tools.documents import (
    LIST_DOCUMENTS_SPEC,
    READ_DOCUMENT_SPEC,
    SEARCH_DOCUMENTS_SPEC,
)
from app.tools.gmail import (
    create_gmail_draft_spec,
    get_gmail_message_spec,
    get_gmail_thread_spec,
    save_gmail_attachment_spec,
    search_gmail_spec,
)
from app.tools.people import (
    list_people_spec,
    lookup_person_spec,
    search_people_spec,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Theme picker (cookie-persisted; same set in adult and kid mode).
# ---------------------------------------------------------------------------

VALID_THEMES = ("sage", "blush", "lavender", "amber", "contrast")
THEME_COOKIE_NAME = "claudia_theme"


def _theme_context(request: Request) -> dict:
    """Jinja2Templates context_processor: makes `theme` available everywhere."""
    raw = request.cookies.get(THEME_COOKIE_NAME, "sage")
    return {"theme": raw if raw in VALID_THEMES else "sage"}


# ---------------------------------------------------------------------------
# Parent display name (kid mode) — DB-backed override of the Helm default.
# Editable via /setup/1 + /settings; reads on every request so a change
# applies immediately without a pod restart.
# ---------------------------------------------------------------------------

KV_PARENT_DISPLAY_NAME = "parent_display_name"


def effective_kid_parent_display_name() -> str:
    """kv_store override → cfg default. Set via /setup/1 and /settings."""
    from app.db_kv import kv_get

    try:
        value = kv_get(state.cfg.data_root, KV_PARENT_DISPLAY_NAME)
        if value and value.strip():
            return value.strip()
    except (OSError, AttributeError):
        pass
    return state.cfg.kid_parent_display_name


def _save_kid_parent_display_name(value: str) -> None:
    """Persist a new parent display name. Empty string clears the override."""
    from app.db_kv import kv_delete, kv_set

    value = value.strip()
    if not value:
        kv_delete(state.cfg.data_root, KV_PARENT_DISPLAY_NAME)
        return
    kv_set(state.cfg.data_root, KV_PARENT_DISPLAY_NAME, value)


def _parent_name_context(request: Request) -> dict:
    """Jinja2Templates context_processor: per-request parent display name."""
    return {"parent_display_name": effective_kid_parent_display_name()}


def _google_enabled_context(request: Request) -> dict:
    """Jinja2Templates context_processor: per-request Google integration state."""
    return {"google_enabled": _google_enabled(state.cfg)}


def _runtime_overrides_context(request: Request) -> dict:
    """Jinja2Templates context_processor: per-request therapist alias +
    additional instructions. Pre-fills /settings inputs with current values.
    Empty string when neither env nor kv has set them."""
    return {
        "therapist_alias": runtime_config_mod.get_therapist_alias(state.cfg.data_root),
        "additional_instructions": runtime_config_mod.get_additional_instructions(state.cfg.data_root),
    }


# ---------------------------------------------------------------------------
# Google integration toggle (adult mode) — file-backed override of the
# CLAUDIA_ADULT_INTEGRATIONS_GOOGLE_ENABLED env var. Lets the user flip
# Gmail/Calendar tools on/off from /settings without redeploying.
# Kid mode physically cannot reach this — _google_enabled checks
# cfg.is_adult first.
# ---------------------------------------------------------------------------

KV_GOOGLE_ENABLED = "google_enabled"


def effective_google_enabled(cfg: config_module.Config | None = None) -> bool:
    """kv_store override → cfg default. Adult mode only — kid caller short-circuits.

    cfg is optional; when omitted, reads state.cfg (request-time path).
    Tests pass cfg explicitly to avoid global-state coupling.
    """
    from app.db_kv import kv_get

    cfg = cfg if cfg is not None else state.cfg
    try:
        value = kv_get(cfg.data_root, KV_GOOGLE_ENABLED)
        if value is not None:
            return value.strip().lower() in ("1", "true", "yes")
    except (OSError, AttributeError):
        pass
    return cfg.adult_integrations_google_enabled


def _save_google_enabled(value: bool) -> None:
    """Persist the override. Always writes — explicit on or off."""
    from app.db_kv import kv_set

    kv_set(state.cfg.data_root, KV_GOOGLE_ENABLED, "true" if value else "false")


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------


class AppState:
    cfg: config_module.Config
    store: SessionStore
    loader: ContextLoader
    templates: Jinja2Templates
    app_root: Path
    rate_limiter: auth_mod.IPRateLimiter
    kid_session_store: auth_mod.SessionStore  # token -> kid display_name, persisted
    adult_session_store: auth_mod.SessionStore  # token -> adult display_name, persisted
    library: Library
    extractor_registry: Any
    status_bus: StatusBus
    people: People
    # Tracks asyncio.create_task spawns (auditor + opener-seed). Holding
    # references prevents GC; tests await this set to verify completion.
    background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Track an asyncio.create_task so it doesn't get GC'd mid-flight."""
    task = asyncio.create_task(coro)
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)
    return task


state = AppState()


def _google_cfg(cfg: config_module.Config) -> GoogleAuthConfig:
    """Build GoogleAuthConfig from the runtime config layer.

    Reads client_id/secret/redirect via runtime_config so /settings UI edits
    or fresh-from-/setup values land here. Env-mounted Helm Secrets still win
    (per decision 8) — runtime_config handles that precedence internally.
    Computed default for redirect_uri (when neither env nor kv sets it):
    https://{cfg.ingress_host}/oauth/callback.
    """
    client_id, client_secret, redirect_uri = runtime_config_mod.get_google_creds(cfg.data_root)
    if not redirect_uri:
        redirect_uri = f"https://{cfg.ingress_host}/oauth/callback"
    return GoogleAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token_path=cfg.data_root / ".credentials" / "google_oauth_token.json",
    )


def _google_enabled(cfg: config_module.Config) -> bool:
    """Single source of truth: Google integrations are adult-mode-only and opt-in.

    Kid mode is short-circuited unconditionally. Adult mode honours the
    file-backed override (set via /settings) if present, otherwise falls
    back to cfg.adult_integrations_google_enabled (env var).
    """
    if not cfg.is_adult:
        return False
    return effective_google_enabled(cfg)


def _build_tool_registry(cfg: config_module.Config, session_id: str | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(READ_DOCUMENT_SPEC(state.library, cfg.data_root))
    reg.register(LIST_DOCUMENTS_SPEC(state.library))
    reg.register(SEARCH_DOCUMENTS_SPEC(state.library))
    reg.register(list_people_spec(state.people))
    reg.register(lookup_person_spec(state.people, state.library))
    reg.register(search_people_spec(state.people))

    # Google integrations: adult-mode only, opt-in. Kid mode physically
    # cannot get Gmail/Calendar tools — the gate is here at the registry,
    # not at the prompt. See T-NEW-F.
    if _google_enabled(cfg):
        gcfg = _google_cfg(cfg)
        if gcfg.is_complete():
            reg.register(search_gmail_spec(gcfg))
            reg.register(get_gmail_thread_spec(gcfg))
            reg.register(get_gmail_message_spec(gcfg))
            reg.register(save_gmail_attachment_spec(gcfg, cfg.data_root))
            reg.register(create_gmail_draft_spec(gcfg))
            reg.register(list_calendar_events_spec(gcfg))
            reg.register(create_calendar_event_spec(gcfg))
            reg.register(update_calendar_event_spec(gcfg))
    return reg


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config_module.load()
    state.cfg = cfg
    state.app_root = Path(__file__).resolve().parent

    for sub in ("sessions", "session-logs", "uploads", "context", "session-exports", "library", "people", ".locks"):
        (cfg.data_root / sub).mkdir(parents=True, exist_ok=True)

    # Storage (T-NEW-I): SqliteSessionStore in every mode. Tests get a fresh
    # /data/claudia.db under tmp_path so there's no global-state contamination.
    # Existing JSONL files on prod are left untouched but no longer read.
    state.store = SqliteSessionStore(cfg.data_root)
    # ContextLoader's people_md_provider + library_index_provider are wired
    # below once state.people / state.library are constructed; until then
    # they return "".
    state.loader = ContextLoader(
        cfg.data_root,
        cfg.prompts_dir,
        mode=cfg.mode,
        display_name=cfg.display_name,
        kid_parent_display_name_provider=effective_kid_parent_display_name,
        people_md_provider=lambda: state.people.render_people_md() if hasattr(state, "people") else "",
        additional_instructions_provider=lambda: runtime_config_mod.get_additional_instructions(cfg.data_root),
        library_index_provider=lambda: state.library.render_index_md() if hasattr(state, "library") else "",
    )
    state.rate_limiter = auth_mod.IPRateLimiter()
    state.kid_session_store = auth_mod.SessionStore(cfg.data_root, role="kid")
    state.adult_session_store = auth_mod.SessionStore(cfg.data_root, role="adult")

    # T-NEW-RC (v0.8 phase A): mirror env-mounted credentials into kv_store
    # so /settings can render their values back even when the Helm Secret is
    # what's authoritatively in use. ENV stays the source of truth for reads.
    runtime_config_mod.auto_import_env_secrets(cfg.data_root)

    state.library = Library(cfg.data_root / "library")
    # Vision callables need a Claude client. Local mode = no API; dev/prod
    # may have an API key from env (cfg.anthropic_api_key) or kv_store
    # (set via /setup/1). get_client returns None if both are empty —
    # callers handle the None case (e.g. extractor falls back to no OCR).
    boot_claude = (
        None
        if cfg.is_local
        else claude_mod.get_client(runtime_config_mod.get_anthropic_key(cfg.data_root))
    )
    if boot_claude is not None:
        transcribe, spot_check = make_vision_callables(boot_claude, model=SONNET)
    else:
        transcribe, spot_check = None, None
    state.extractor_registry = build_registry(transcribe=transcribe, spot_check=spot_check)
    state.status_bus = StatusBus()
    state.status_bus.attach_loop(asyncio.get_running_loop())
    state.people = People(cfg.data_root / "people")

    # Block 2 renders the library index dynamically — no INDEX.md write
    # at startup needed.

    import time as _time

    state.templates = Jinja2Templates(
        directory=str(state.app_root / "templates"),
        context_processors=[
            _theme_context,
            _parent_name_context,
            _google_enabled_context,
            _runtime_overrides_context,
        ],
    )
    state.templates.env.globals["asset_version"] = str(int(_time.time()))
    state.templates.env.globals["claudia_mode"] = cfg.mode
    state.templates.env.globals["display_name"] = cfg.display_name
    # parent_display_name is supplied dynamically per-request by
    # _parent_name_context so /setup edits apply without a pod restart.
    state.templates.env.globals["crisis_footer_text"] = safety.CRISIS_FOOTER_TEXT
    state.templates.env.globals["au_hotlines"] = safety.AU_HOTLINES

    # Setup-complete marker. Existing live deploys (sessions, library, or
    # legacy .setup_complete file) get auto-marked so /setup doesn't bounce
    # them after the v0.7.0 storage migration. Fresh installs go through
    # the wizard.
    if not _setup_complete():
        legacy_marker = cfg.data_root / ".setup_complete"
        looks_used = (
            legacy_marker.exists()
            or any((cfg.data_root / "sessions").glob("*.jsonl"))
            or any((cfg.data_root / "session-logs").glob("*.md"))
            or (cfg.data_root / "library" / "manifest.json").exists()
            or (cfg.data_root / "context" / "01_background.md").exists()
        )
        if looks_used:
            try:
                _mark_setup_complete("auto-marked at v0.7.0 boot; legacy data on disk")
                log.info("setup.auto_marked_existing_deploy")
            except OSError as e:
                log.warning("setup.auto_mark_failed", error=str(e))

    log.info(
        "app.startup",
        mode=cfg.mode,
        ops_mode=cfg.ops_mode,
        data_root=str(cfg.data_root),
    )
    yield
    log.info("app.shutdown")


app = FastAPI(title="claudia", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
#
# Adult mode: Basic auth on every route, single password.
# Kid mode:  Kid cookie (`claudia-kid`) on / and /session/*; Basic auth on
#            /admin/* (separate password = parent admin). See
#            /plan-eng-review D5 for the hardening posture.
#
# `require_auth` is the kid-or-adult auth dep used on everything that's
# NOT /admin. `require_parent_admin` is the basic-auth dep on /admin.

# Adult-mode auth is cookie-based (set during /setup/1). The only remaining
# HTTP Basic Auth surface is the kid-mode parent admin (/admin/* +
# /library + /people in kid mode). Adult deploys never see a Basic Auth
# prompt on any user-facing route.
_admin_basic_auth = HTTPBasic(realm="claudia-admin", auto_error=False)


def _check_basic_credentials(credentials: HTTPBasicCredentials) -> bool:
    cfg = state.cfg
    user_ok = secrets.compare_digest(credentials.username, cfg.basic_auth_user)
    pw_ok = secrets.compare_digest(credentials.password, cfg.basic_auth_password)
    return user_ok and pw_ok


def _client_ip(request: Request) -> str:
    # X-Forwarded-For from the cluster ingress; fall back to direct.
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff:
        return xff
    if request.client:
        return request.client.host
    return "unknown"


def _adult_cookie_display_name(request: Request) -> str | None:
    """Return the adult display_name if a valid adult cookie is present."""
    cookie = request.cookies.get(auth_mod.ADULT_COOKIE_NAME, "")
    if not cookie:
        return None
    return state.adult_session_store.get(cookie)


def _kid_cookie_display_name(request: Request) -> str | None:
    cookie = request.cookies.get(auth_mod.KID_COOKIE_NAME, "")
    if not cookie:
        return None
    return state.kid_session_store.get(cookie)


def require_auth(request: Request) -> str:
    """Cookie-based auth for the user-facing surface (/, /session/*, etc.).

    Both modes use the same shape: a passphrase set during /setup or first
    /login, then a `claudia-adult` or `claudia-kid` session cookie. No HTTP
    Basic Auth on user-facing routes any more.
    """
    cfg = state.cfg
    if cfg.is_local:
        return "liam"

    role: auth_mod.Role = "kid" if cfg.is_kid else "adult"
    display = _kid_cookie_display_name(request) if cfg.is_kid else _adult_cookie_display_name(request)
    if display is not None:
        return display
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"{role}-not-logged-in",
        headers={"Location": "/login"},
    )


def require_setup_auth(
    request: Request,
    admin_credentials: HTTPBasicCredentials | None = Depends(_admin_basic_auth),
) -> str:
    """Auth for /setup/*.

    Adult mode:
      - If no passphrase has been set yet (fresh install): allow anyone to
        run the wizard. The wizard's password step writes the passphrase.
      - Otherwise: require a valid adult cookie (i.e., already logged in).
    Kid mode:
      - Always parent-admin basic auth (BASIC_AUTH_PASSWORD).
    """
    cfg = state.cfg
    if cfg.is_local:
        return "liam"
    if cfg.is_kid:
        if admin_credentials is not None and _check_basic_credentials(admin_credentials):
            return admin_credentials.username
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="setup is parent-admin only in kid mode",
            headers={"WWW-Authenticate": 'Basic realm="claudia-admin"'},
        )
    # Adult mode: open during initial bootstrap, cookie-gated thereafter.
    if not auth_mod.is_passphrase_set(cfg.data_root, role="adult"):
        return "setup"
    display = _adult_cookie_display_name(request)
    if display is not None:
        return display
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="adult-not-logged-in",
        headers={"Location": "/login"},
    )


def require_parent_admin(
    credentials: HTTPBasicCredentials | None = Depends(_admin_basic_auth),
) -> str:
    """
    Auth for /admin/* — parent admin password.
    Only meaningful in kid mode; in adult mode the routes 404 anyway.
    """
    cfg = state.cfg
    if cfg.is_local:
        return "parent"
    if not cfg.is_kid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="admin routes are only available in kid mode",
        )
    if credentials is None or not _check_basic_credentials(credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="claudia-admin"'},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Stub kept so the existing Prometheus ServiceMonitor scrape doesn't 404.
    All app metrics were removed in the simplify pass — Liam reads pod logs."""
    return "# claudia metrics removed\n"


@app.get("/readyz", response_class=PlainTextResponse)
async def readyz() -> Response:
    healthcheck_path = state.cfg.data_root / ".healthcheck"
    try:
        healthcheck_path.write_text(datetime.now(UTC).isoformat())
    except OSError as e:
        log.error("readyz.write_failed", error=str(e))
        return PlainTextResponse("fail", status_code=503)
    return PlainTextResponse("ready")


# ---------------------------------------------------------------------------
# Auth: redirect kid-not-logged-in to /login
# ---------------------------------------------------------------------------


from fastapi.exceptions import HTTPException as _HTTPExc  # noqa: E402


@app.exception_handler(_HTTPExc)
async def _login_redirect(request: Request, exc: _HTTPExc) -> Response:
    """Cookie-auth 401s redirect to /login instead of showing the raw error."""
    if (
        exc.status_code == 401
        and exc.detail in ("kid-not-logged-in", "adult-not-logged-in")
        and not request.url.path.startswith("/login")
    ):
        return RedirectResponse(url="/login", status_code=303)
    headers = exc.headers or {}
    return Response(
        content=str(exc.detail),
        status_code=exc.status_code,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# /login (kid mode only) and /logout
# ---------------------------------------------------------------------------


def _login_role() -> auth_mod.Role:
    """Which role the current /login flow is for. Per-mode singleton."""
    return "kid" if state.cfg.is_kid else "adult"


def _login_session_store() -> auth_mod.SessionStore:
    return state.kid_session_store if state.cfg.is_kid else state.adult_session_store


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    cfg = state.cfg
    role = _login_role()
    # Already logged in? Bounce home.
    existing = (
        _kid_cookie_display_name(request) if role == "kid" else _adult_cookie_display_name(request)
    )
    if existing is not None:
        return RedirectResponse(url="/", status_code=303)

    password_set = auth_mod.is_passphrase_set(cfg.data_root, role=role)
    # Adult mode: only bounce to /setup if setup itself isn't complete.
    # Setup-complete-but-no-password is a valid state when the user authed
    # via Google during the wizard; in that case render /login with the
    # Google button as the only option.
    if role == "adult" and not password_set and not _setup_complete():
        return RedirectResponse(url="/setup", status_code=303)

    # Show "Sign in with Google" option only when:
    #   - adult mode
    #   - Google credentials are configured (env or kv)
    #   - an identity email has already been bound (one-shot lock per
    #     decision 9 — first identity to complete setup owns the deploy)
    google_identity_email: str | None = None
    if role == "adult" and _google_creds_present(cfg):
        google_identity_email = _google_identity_bound_email(cfg)

    return state.templates.TemplateResponse(
        request,
        "login.html",
        {
            "role": role,
            "is_first_time": not password_set,
            "password_set": password_set,
            "display_name": cfg.display_name,
            "google_identity_email": google_identity_email,
        },
    )


@app.post("/login")
async def login_submit(request: Request) -> Response:
    cfg = state.cfg
    role = _login_role()

    ip = _client_ip(request)
    if not state.rate_limiter.check(ip):
        log.warning("auth.rate_limited", role=role, ip=ip)
        return Response(
            content="Too many attempts. Try again in a few minutes.",
            status_code=429,
        )

    form = await request.form()
    passphrase = str(form.get("passphrase", ""))
    confirm = str(form.get("confirm", ""))

    is_first_time = not auth_mod.is_passphrase_set(cfg.data_root, role=role)

    if is_first_time and role == "kid":
        # Kid first-time: setup flow runs HERE (parent has not pre-set the
        # kid passphrase; the kid sets their own at first login).
        if passphrase != confirm:
            return state.templates.TemplateResponse(
                request,
                "login.html",
                {
                    "role": role,
                    "is_first_time": True,
                    "display_name": cfg.display_name,
                    "error": "The two passwords didn't match.",
                },
                status_code=400,
            )
        try:
            auth_mod.set_passphrase(cfg.data_root, passphrase, role="kid")
        except ValueError as e:
            return state.templates.TemplateResponse(
                request,
                "login.html",
                {
                    "role": role,
                    "is_first_time": True,
                    "display_name": cfg.display_name,
                    "error": str(e),
                },
                status_code=400,
            )

    if is_first_time and role == "adult":
        # Adult mode with no password set: either setup is incomplete (bounce
        # to wizard) or the user authed via Google during setup and never
        # picked a password (refuse password attempt; they must use Google).
        if not _setup_complete():
            return RedirectResponse(url="/setup", status_code=303)
        google_identity_email: str | None = None
        if _google_creds_present(cfg):
            google_identity_email = _google_identity_bound_email(cfg)
        return state.templates.TemplateResponse(
            request,
            "login.html",
            {
                "role": role,
                "is_first_time": True,
                "password_set": False,
                "display_name": cfg.display_name,
                "google_identity_email": google_identity_email,
                "error": "No password set on this deploy — sign in with Google.",
            },
            status_code=401,
        )

    if not auth_mod.verify_passphrase(cfg.data_root, passphrase, role=role):
        state.rate_limiter.record(ip)
        log.warning("auth.verify_failed", role=role, ip=ip)
        return state.templates.TemplateResponse(
            request,
            "login.html",
            {
                "role": role,
                "is_first_time": False,
                "display_name": cfg.display_name,
                "error": "That password didn't match. Try again.",
            },
            status_code=401,
        )

    state.rate_limiter.reset(ip)
    return _issue_login_cookie(role, redirect_to="/")


def _issue_login_cookie(role: auth_mod.Role, redirect_to: str) -> Response:
    """Mint a session token, persist it, and set the response cookie."""
    cfg = state.cfg
    token = auth_mod.new_session_token()
    _login_session_store().add(token, cfg.display_name or role)
    log.info("auth.login_success", role=role, display_name=cfg.display_name)

    resp = RedirectResponse(url=redirect_to, status_code=303)
    resp.set_cookie(
        key=auth_mod.cookie_name(role),
        value=token,
        max_age=auth_mod.COOKIE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/logout")
async def logout(request: Request) -> Response:
    cfg = state.cfg
    role: auth_mod.Role = "kid" if cfg.is_kid else "adult"
    cookie = request.cookies.get(auth_mod.cookie_name(role), "")
    if cookie:
        _login_session_store().remove(cookie)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth_mod.cookie_name(role), path="/")
    return resp


# ---------------------------------------------------------------------------
# /settings — theme picker. Same UI in adult + kid; cookie-persisted.
# ---------------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    google_creds_present = state.cfg.is_adult and _google_creds_present(state.cfg)
    google_tools_connected = False
    if google_creds_present and _google_enabled(state.cfg):
        try:
            google_tools_connected = google_auth.status(_google_cfg(state.cfg)).get("state") == "connected"
        except Exception:
            google_tools_connected = False
    return state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "valid_themes": VALID_THEMES,
            "google_creds_present": google_creds_present,
            "google_tools_connected": google_tools_connected,
        },
    )


@app.post("/settings/parent-name")
async def settings_parent_name(request: Request, _: str = Depends(require_auth)) -> Response:
    """Persist the parent's display name. Kid mode only."""
    if not state.cfg.is_kid:
        raise HTTPException(404, "parent name is kid-mode only")
    form = await request.form()
    value = str(form.get("parent_display_name", "")).strip()
    _save_kid_parent_display_name(value)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/google-integration")
async def settings_google_integration(
    request: Request, _: str = Depends(require_auth)
) -> Response:
    """Toggle Gmail + Calendar tool registration. Adult mode only."""
    if not state.cfg.is_adult:
        raise HTTPException(404, "Google integration is adult-mode only")
    form = await request.form()
    enabled = str(form.get("enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    _save_google_enabled(enabled)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/therapist-name")
async def settings_therapist_name(
    request: Request, _: str = Depends(require_auth)
) -> Response:
    """Persist therapist alias (the bot's preferred name in copy). Adult only.

    v0.8 phase B: writes to kv_store via runtime_config. Empty string clears
    the override (bot reverts to default 'claudia' framing). 40-char cap
    matches the input maxlength.
    """
    if not state.cfg.is_adult:
        raise HTTPException(404, "therapist name is adult-mode only")
    from app.db_kv import kv_delete, kv_set

    form = await request.form()
    value = str(form.get("therapist_alias", "")).strip()[:40]
    if value:
        kv_set(state.cfg.data_root, runtime_config_mod.KV_THERAPIST_ALIAS, value)
    else:
        kv_delete(state.cfg.data_root, runtime_config_mod.KV_THERAPIST_ALIAS)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/additional-instructions")
async def settings_additional_instructions(
    request: Request, _: str = Depends(require_auth)
) -> Response:
    """Persist free-form additional companion instructions. Adult only.

    Appended to companion-adult.md at assemble time via ContextLoader's
    additional_instructions_provider (Phase B). Empty string clears.
    """
    if not state.cfg.is_adult:
        raise HTTPException(404, "additional instructions is adult-mode only")
    from app.db_kv import kv_delete, kv_set

    form = await request.form()
    value = str(form.get("additional_instructions", "")).strip()
    if value:
        kv_set(state.cfg.data_root, runtime_config_mod.KV_ADDITIONAL_INSTRUCTIONS, value)
    else:
        kv_delete(state.cfg.data_root, runtime_config_mod.KV_ADDITIONAL_INSTRUCTIONS)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/theme")
async def settings_theme(request: Request, _: str = Depends(require_auth)) -> Response:
    form = await request.form()
    theme = str(form.get("theme", "")).strip()
    if theme not in VALID_THEMES:
        theme = "sage"
    resp = RedirectResponse(url="/settings", status_code=303)
    # 1-year expiry, host-scoped, no Secure flag (works on http during local
    # dev too; ingress provides TLS in production).
    resp.set_cookie(
        THEME_COOKIE_NAME,
        theme,
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="Lax",
        httponly=False,  # template reads it; nothing secret in it
    )
    return resp


# ---------------------------------------------------------------------------
# /help — public crisis-help page (no auth, by design — safety reach matters
# more than access control).
# ---------------------------------------------------------------------------


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    """AU hotline directory. Reachable via the kid-chat ··· menu and direct URL.

    Deliberately unauthed: a kid who's lost their password should still hit
    the hotline numbers without bouncing through /login.
    """
    back = request.query_params.get("back")
    return state.templates.TemplateResponse(
        request,
        "help.html",
        {"back": back},
    )


# ---------------------------------------------------------------------------
# /admin/* (kid mode only) — parent admin
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    _: str = Depends(require_parent_admin),
) -> HTMLResponse:
    cfg = state.cfg
    sessions = state.store.list_sessions()
    return state.templates.TemplateResponse(
        request,
        "admin/home.html",
        {
            "display_name": cfg.display_name,
            "session_count": len(sessions),
        },
    )


@app.get("/admin/review", response_class=HTMLResponse)
async def admin_review(
    request: Request,
    _: str = Depends(require_parent_admin),
) -> HTMLResponse:
    cfg = state.cfg
    return state.templates.TemplateResponse(
        request,
        "admin/review.html",
        {
            "display_name": cfg.display_name,
            "summary": None,
            "themes": [],
            "flag": "none",
            "people_added": [],
        },
    )


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /setup — first-run wizard. Three stages. Writes /data/.setup_complete on
# finish. / redirects here when the marker is missing.
# ---------------------------------------------------------------------------


KV_SETUP_STATE = "setup_wizard_state"
KV_SETUP_COMPLETED = "setup_completed_at"


def _setup_state_load() -> dict:
    """Working state during the wizard. Persists to kv_store."""
    from app.db_kv import kv_get

    raw = kv_get(state.cfg.data_root, KV_SETUP_STATE)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _setup_state_save(updates: dict) -> None:
    from app.db_kv import kv_set

    cur = _setup_state_load()
    cur.update(updates)
    kv_set(state.cfg.data_root, KV_SETUP_STATE, json.dumps(cur))


def _setup_state_clear() -> None:
    from app.db_kv import kv_delete

    kv_delete(state.cfg.data_root, KV_SETUP_STATE)


def _setup_complete() -> bool:
    """True once the wizard has been finished (or auto-marked at boot)."""
    from app.db_kv import kv_exists

    return kv_exists(state.cfg.data_root, KV_SETUP_COMPLETED)


def _mark_setup_complete(note: str = "") -> None:
    from app.db_kv import kv_set

    kv_set(state.cfg.data_root, KV_SETUP_COMPLETED, note or datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# 5-step wizard (v0.8 phase D)
# ---------------------------------------------------------------------------
#
# Step 1: Anthropic API key (with live validation)
# Step 2: Auth method (password OR Google OAuth identity)
# Step 3: Profile + model + custom prompt + (disabled) kid-mode toggle
# Step 4: Library import (inline upload, auto-draft profile)
# Step 5: Recap + theme + therapist name → commit
#
# Adult mode only goes through this; kid mode setup is operator-side
# (parent ssh + Helm), so kid users never hit /setup/*.
#
# Per /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md.

_AVAILABLE_MODELS = [
    ("claude-sonnet-4-6", "Sonnet 4.6", "Default. Best balance of quality + cost."),
    ("claude-opus-4-7", "Opus 4.7", "Most capable. ~5× the cost of Sonnet."),
    ("claude-haiku-4-5", "Haiku 4.5", "Fastest, cheapest. Smaller model — less nuanced."),
]


def _setup_first_incomplete_step() -> int:
    """Sticky-resume: pick the first incomplete step. Used by setup_root.

    A step is "complete" iff the precondition for moving on has been
    satisfied (key set, auth done, profile filled, etc). If everything's
    done, return 5 (the recap) so the user can finalise.
    """
    cfg = state.cfg
    # Step 1: Anthropic API key (env or kv); local mode skips the check.
    if not cfg.is_local and not runtime_config_mod.get_anthropic_key(cfg.data_root):
        return 1
    # Step 2: adult passphrase OR Google identity bound; local skips.
    if cfg.is_adult and not cfg.is_local:
        from app.db_kv import kv_exists

        password_done = auth_mod.is_passphrase_set(cfg.data_root, role="adult")
        google_done = kv_exists(cfg.data_root, runtime_config_mod.KV_GOOGLE_IDENTITY_EMAIL)
        if not (password_done or google_done):
            return 2
    state_data = _setup_state_load()
    # Step 3: profile basics (DOB used as the canary)
    if not state_data.get("dob"):
        return 3
    # Step 4: library not blocking; auto-draft button optional. Skip past
    # only if at least one of the four textareas is filled.
    profile_filled = any(
        state_data.get(k) for k in ("section_who", "section_stressors", "section_never", "section_for")
    )
    if not profile_filled:
        return 4
    return 5


@app.get("/setup", response_class=HTMLResponse)
@app.get("/setup/", response_class=HTMLResponse)
async def setup_root(_: str = Depends(require_setup_auth)) -> Response:
    target = _setup_first_incomplete_step()
    return RedirectResponse(url=f"/setup/{target}", status_code=303)


def _setup_step_context(step: int, extra: dict | None = None) -> dict:
    """Common Jinja context for any wizard step. Each step's GET adds its
    own extras on top."""
    state_data = _setup_state_load()
    cfg = state.cfg
    ctx = {
        "step": step,
        "data": state_data,
        "cfg_display_name": cfg.display_name,
        "cfg_country": cfg.country,
        "cfg_parent_display_name": effective_kid_parent_display_name(),
        "adult_passphrase_set": (
            cfg.is_local or auth_mod.is_passphrase_set(cfg.data_root, role="adult")
        ),
    }
    if extra:
        ctx.update(extra)
    return ctx


# ---- Step 1: Anthropic API key -------------------------------------------


@app.get("/setup/1", response_class=HTMLResponse)
async def setup_step1(request: Request, _: str = Depends(require_setup_auth)) -> HTMLResponse:
    """Capture the Anthropic API key. Skipped automatically (303 → /setup/2)
    if env-set, since the user can't change it from the UI when the source
    is the Helm Secret (decision 8)."""
    cfg = state.cfg
    key_source = runtime_config_mod.get_field_source(cfg.data_root, "anthropic_api_key")
    # Auto-skip: env-set or already in kv (no point re-prompting).
    if key_source != "none":
        return RedirectResponse(url="/setup/2", status_code=303)
    return state.templates.TemplateResponse(
        request, "setup/step1.html", _setup_step_context(1)
    )


@app.post("/setup/1")
async def setup_step1_submit(request: Request, _: str = Depends(require_setup_auth)) -> Response:
    """Validate the key with a tiny live Anthropic call before persisting.
    Blocks until success per decision 5."""
    form = await request.form()
    api_key = str(form.get("anthropic_api_key", "")).strip()
    ok, error = await asyncio.to_thread(claude_mod.validate_api_key, api_key)
    if not ok:
        return state.templates.TemplateResponse(
            request,
            "setup/step1.html",
            _setup_step_context(1, {"error": error, "submitted_key_prefix": api_key[:10]}),
            status_code=400,
        )
    from app.db_kv import kv_set

    kv_set(state.cfg.data_root, runtime_config_mod.KV_ANTHROPIC_API_KEY, api_key)
    log.info("setup.step1.api_key_validated_and_saved")
    return RedirectResponse(url="/setup/2", status_code=303)


# ---- Step 2: Auth method --------------------------------------------------


@app.get("/setup/2", response_class=HTMLResponse)
async def setup_step2(request: Request, _: str = Depends(require_setup_auth)) -> HTMLResponse:
    """Auth method choice: password vs Google OAuth identity.
    Local mode skips this entirely (no auth in local)."""
    cfg = state.cfg
    if cfg.is_local:
        return RedirectResponse(url="/setup/3", status_code=303)
    # Already authed via either method? Skip ahead.
    from app.db_kv import kv_exists

    if auth_mod.is_passphrase_set(cfg.data_root, role="adult") or kv_exists(
        cfg.data_root, runtime_config_mod.KV_GOOGLE_IDENTITY_EMAIL
    ):
        return RedirectResponse(url="/setup/3", status_code=303)
    google_creds_present = _google_creds_present(cfg)
    return state.templates.TemplateResponse(
        request,
        "setup/step2.html",
        _setup_step_context(2, {"google_creds_present": google_creds_present}),
    )


@app.post("/setup/2")
async def setup_step2_submit(request: Request, _: str = Depends(require_setup_auth)) -> Response:
    """Password path. Google path posts to /auth/google/start directly,
    which means /setup/2 POST only handles the password form."""
    cfg = state.cfg
    form = await request.form()
    passphrase = str(form.get("passphrase", ""))
    confirm = str(form.get("passphrase_confirm", ""))
    if passphrase != confirm:
        return state.templates.TemplateResponse(
            request,
            "setup/step2.html",
            _setup_step_context(
                2,
                {
                    "google_creds_present": _google_creds_present(cfg),
                    "error": "The two passwords didn't match.",
                },
            ),
            status_code=400,
        )
    try:
        auth_mod.set_passphrase(cfg.data_root, passphrase, role="adult")
    except ValueError as e:
        return state.templates.TemplateResponse(
            request,
            "setup/step2.html",
            _setup_step_context(
                2,
                {
                    "google_creds_present": _google_creds_present(cfg),
                    "error": str(e),
                },
            ),
            status_code=400,
        )
    return _issue_login_cookie("adult", redirect_to="/setup/3")


# ---- Step 3: Profile + model + prompt + kid-toggle ------------------------


@app.get("/setup/3", response_class=HTMLResponse)
async def setup_step3(request: Request, _: str = Depends(require_setup_auth)) -> HTMLResponse:
    return state.templates.TemplateResponse(
        request,
        "setup/step3.html",
        _setup_step_context(
            3,
            {
                "available_models": _AVAILABLE_MODELS,
                "current_model": runtime_config_mod.get_default_model(
                    state.cfg.data_root, state.cfg.default_model
                ),
                "current_instructions": runtime_config_mod.get_additional_instructions(
                    state.cfg.data_root
                ),
            },
        ),
    )


@app.post("/setup/3")
async def setup_step3_submit(request: Request, _: str = Depends(require_setup_auth)) -> Response:
    form = await request.form()
    parent_name = str(form.get("parent_display_name", "")).strip()
    model = str(form.get("default_model", "")).strip()
    instructions = str(form.get("additional_instructions", "")).strip()

    _setup_state_save(
        {
            "preferred_name": str(form.get("preferred_name", "")).strip(),
            "dob": str(form.get("dob", "")).strip(),
            "country": str(form.get("country", "")).strip() or "AU",
            "region": str(form.get("region", "")).strip(),
            "parent_display_name": parent_name,
        }
    )
    if state.cfg.is_kid and parent_name:
        _save_kid_parent_display_name(parent_name)

    from app.db_kv import kv_delete, kv_set

    valid_models = {m[0] for m in _AVAILABLE_MODELS}
    if model in valid_models:
        kv_set(state.cfg.data_root, runtime_config_mod.KV_DEFAULT_MODEL_OVERRIDE, model)
    if instructions:
        kv_set(state.cfg.data_root, runtime_config_mod.KV_ADDITIONAL_INSTRUCTIONS, instructions)
    else:
        kv_delete(state.cfg.data_root, runtime_config_mod.KV_ADDITIONAL_INSTRUCTIONS)

    return RedirectResponse(url="/setup/4", status_code=303)


# ---- Step 4: Library import (inline) -------------------------------------


@app.get("/setup/4", response_class=HTMLResponse)
async def setup_step4(request: Request, _: str = Depends(require_setup_auth)) -> HTMLResponse:
    state_data = _setup_state_load()
    docs = state.library.list_active() if hasattr(state, "library") else []
    can_autodraft = (
        bool(docs)
        and not state.cfg.is_local
        and bool(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
    )
    autodraft_error = state_data.pop("autodraft_error", None) if isinstance(state_data, dict) else None
    return state.templates.TemplateResponse(
        request,
        "setup/step4.html",
        _setup_step_context(
            4,
            {
                "docs": docs,
                "can_autodraft": can_autodraft,
                "autodraft_error": autodraft_error,
            },
        ),
    )


@app.post("/setup/4/upload")
async def setup_step4_upload(
    request: Request, _: str = Depends(require_setup_auth)
) -> Response:
    """Inline-upload entrypoint for step 4. Same processing as
    /library/upload but redirects back to /setup/4 instead of /library
    (or the streaming progress page)."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(400, "no file in request")
    raw_title = (form.get("title") or "").strip() or upload.filename or "Untitled"
    body = await upload.read()
    if len(body) > _LIBRARY_MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"upload exceeds {_LIBRARY_MAX_UPLOAD_BYTES} bytes")
    if not body:
        raise HTTPException(400, "empty upload")
    doc_id = state.library.mint_doc_id(raw_title)
    asyncio.create_task(
        library_pipeline.process_doc_creation_async(
            state.library,
            state.extractor_registry,
            state.status_bus,
            doc_id=doc_id,
            title=raw_title,
            original_bytes=body,
            filename=upload.filename,
            mime=upload.content_type or "application/octet-stream",
            source="upload",
            tags=[],
        )
    )
    return RedirectResponse(url="/setup/4", status_code=303)


@app.post("/setup/4/auto-draft")
async def setup_step4_auto_draft(_: str = Depends(require_setup_auth)) -> Response:
    """Run the LLM pass over uploaded library docs, populate the four
    setup textareas with the suggestions, redirect back to /setup/4."""
    claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
    if state.cfg.is_local or claude is None:
        raise HTTPException(404, "auto-draft requires API access (dev/prod mode)")
    docs = state.library.list_active()
    if not docs:
        return RedirectResponse(url="/setup/4", status_code=303)
    try:
        suggestions = await asyncio.to_thread(
            setup_autodraft_mod.auto_draft_profile,
            claude,
            runtime_config_mod.get_default_model(state.cfg.data_root, state.cfg.default_model),
            state.library,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("setup.autodraft.failed", error=str(e))
        _setup_state_save({"autodraft_error": f"Auto-draft failed: {e}"})
        return RedirectResponse(url="/setup/4", status_code=303)
    if suggestions:
        _setup_state_save(suggestions)
        log.info("setup.autodraft.applied", sections=sorted(suggestions.keys()))
    return RedirectResponse(url="/setup/4", status_code=303)


@app.post("/setup/4")
async def setup_step4_submit(request: Request, _: str = Depends(require_setup_auth)) -> Response:
    form = await request.form()
    _setup_state_save(
        {
            "section_who": str(form.get("section_who", "")).strip(),
            "section_stressors": str(form.get("section_stressors", "")).strip(),
            "section_never": str(form.get("section_never", "")).strip(),
            "section_for": str(form.get("section_for", "")).strip(),
        }
    )
    return RedirectResponse(url="/setup/5", status_code=303)


# ---- Step 5: Recap + theme + therapist name → commit ---------------------


@app.get("/setup/5", response_class=HTMLResponse)
async def setup_step5(request: Request, _: str = Depends(require_setup_auth)) -> HTMLResponse:
    docs = state.library.list_active() if hasattr(state, "library") else []
    return state.templates.TemplateResponse(
        request,
        "setup/step5.html",
        _setup_step_context(
            5,
            {
                "docs": docs,
                "current_alias": runtime_config_mod.get_therapist_alias(state.cfg.data_root),
                "valid_themes": VALID_THEMES,
            },
        ),
    )


@app.post("/setup/5")
async def setup_step5_submit(request: Request, _: str = Depends(require_setup_auth)) -> Response:
    """Final commit: persist alias + theme, write 01_background.md, mark complete."""
    cfg = state.cfg
    state_data = _setup_state_load()

    form = await request.form()
    alias = str(form.get("therapist_alias", "")).strip()[:40]
    theme = str(form.get("theme", "")).strip()
    from app.db_kv import kv_delete, kv_set

    if alias and alias.lower() != "claudia":
        kv_set(cfg.data_root, runtime_config_mod.KV_THERAPIST_ALIAS, alias)
    else:
        kv_delete(cfg.data_root, runtime_config_mod.KV_THERAPIST_ALIAS)

    # Compose 01_background.md from the four step-4 textareas + step-3
    # profile fields. Same composer as before; just moved here.
    target_label = cfg.display_name or state_data.get("preferred_name") or "the user"
    if cfg.is_kid:
        target_label = cfg.display_name or "this kid"
    sections: list[str] = [f"# About {target_label}\n"]
    if state_data.get("section_who"):
        sections.append("## Who they are right now\n\n" + state_data["section_who"].strip() + "\n")
    if state_data.get("section_stressors"):
        sections.append("## Active stressors right now\n\n" + state_data["section_stressors"].strip() + "\n")
    if state_data.get("section_never"):
        sections.append("## What claudia should never do\n\n" + state_data["section_never"].strip() + "\n")
    if state_data.get("section_for"):
        sections.append("## What claudia is for\n\n" + state_data["section_for"].strip() + "\n")
    if state_data.get("dob"):
        sections.append(
            "## Date of birth (authoritative)\n\n"
            f"{state_data['dob']}\n\n"
            "This date is the ground truth for the user's age and DOB. "
            "Any year, age, or birthday mentioned in prose elsewhere — in diagnostic reports, journals, "
            "uploaded documents — is informational background, not the source of truth. "
            "When the user asks how old they are or when their birthday is, calculate from this date.\n"
        )
    if state_data.get("country") or state_data.get("region"):
        loc = ", ".join(x for x in [state_data.get("region", ""), state_data.get("country", "")] if x)
        sections.append(f"## Location\n\n{loc}\n")
    body = "\n".join(sections).strip() + "\n"
    bg_path = cfg.data_root / "context" / "01_background.md"
    bg_path.parent.mkdir(parents=True, exist_ok=True)
    bg_path.write_text(body, encoding="utf-8")

    _mark_setup_complete()
    _setup_state_clear()
    log.info("setup.completed", data_root=str(cfg.data_root))

    target = "/admin" if cfg.is_kid else "/"
    resp: Response
    if cfg.is_adult and auth_mod.is_passphrase_set(cfg.data_root, role="adult"):
        # Auto-login (in case password path was used + cookie expired between
        # step 2 and step 5 — rare but possible during long wizard sessions).
        resp = _issue_login_cookie("adult", redirect_to=target)
    else:
        resp = RedirectResponse(url=target, status_code=303)
    # Persist the chosen theme as a 1-year cookie regardless of mode.
    if theme in VALID_THEMES:
        resp.set_cookie(
            THEME_COOKIE_NAME,
            theme,
            max_age=60 * 60 * 24 * 365,
            path="/",
            samesite="Lax",
            httponly=False,
        )
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _: str = Depends(require_auth)) -> Response:
    # First-run gate. If the parent (or adult) hasn't run the wizard yet,
    # bounce here. Existing live deploys are auto-marked complete on startup.
    if not _setup_complete():
        return RedirectResponse(url="/setup", status_code=303)
    sessions = state.store.list_sessions()
    active = state.store.active_session()
    mood_line = _mood_sparkline(state.cfg.data_root)
    mood_by_session = _mood_by_session(state.cfg.data_root)
    session_rows = [
        {
            "session_id": s.session_id,
            "label": s.title or _brisbane_label(s.created_at),
            "mood": mood_by_session.get(s.session_id),
        }
        for s in sessions
    ]
    active_row = None
    if active:
        active_row = {
            "session_id": active.session_id,
            "label": _brisbane_label(active.created_at),
        }
    return state.templates.TemplateResponse(
        request,
        "home.html",
        {
            "sessions": session_rows,
            "active": active_row,
            "mood_sparkline": mood_line,
        },
    )


def _brisbane_label(iso_ts: str) -> str:
    """Render an ISO 8601 timestamp as a friendly Brisbane (AEST, UTC+10) string."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(BRISBANE_TZ).strftime("%a %d %b %H:%M")
    except (ValueError, TypeError, AttributeError):
        return (iso_ts or "")[:16]


def _mood_by_session(data_root: Path) -> dict[str, int]:
    """Map session_id -> last regulation_score recorded for that session."""
    from app.db_audit import mood_by_session

    return mood_by_session(data_root)


def _mood_sparkline(data_root: Path) -> str | None:
    from app.db_audit import recent_mood_scores

    scores = recent_mood_scores(data_root, limit=20)
    if not scores:
        return None
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[min(7, max(0, (s - 1) * 7 // 9))] for s in scores) + f"  ({scores[-1]}/10)"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.get("/session/new")
async def session_new(_: str = Depends(require_auth)) -> RedirectResponse:
    """No picker. Create a session and redirect to it."""
    if state.store.active_session():
        return RedirectResponse("/", status_code=303)

    blocks = state.loader.assemble()
    session_id = new_session_id("session")
    header = SessionHeader(
        session_id=session_id,
        created_at=datetime.now(UTC).isoformat(),
        mode="session",
        model=SONNET,
        prompt_sha="",
    )
    state.store.create_session(header)
    state.store.append_event(session_id, "session_started", {"model": SONNET})

    if not state.cfg.is_local:
        # asyncio.create_task instead of BackgroundTasks so the redirect
        # response can flush before the worker thread runs the slow opener
        # — see the rationale in session_end.
        _spawn_background(_seed_opener_safe_async(session_id, SONNET, blocks))

    return RedirectResponse(f"/session/{session_id}", status_code=303)


async def _seed_opener_safe_async(session_id: str, model: str, blocks) -> None:
    """Async wrapper for the sync opener-seed."""
    try:
        await asyncio.to_thread(_seed_opener_safe, session_id, model, blocks)
    except Exception:  # noqa: BLE001
        log.exception("session.opener_async_wrapper_failed", session_id=session_id)


def _seed_opener_safe(session_id: str, model: str, blocks) -> None:
    try:
        _seed_opener(session_id, model, blocks)
    except Exception as e:  # noqa: BLE001
        log.warning("session.opener_failed", session_id=session_id, error=str(e))
        try:
            state.store.append_event(session_id, "opener_failed", {"error": str(e)})
        except Exception:  # noqa: BLE001
            pass


def _opener_alias_directive(data_root: Path) -> str:
    """If the user set a non-default therapist alias, return a one-line bot
    directive to prepend to the synthetic-user 'Begin the session.' content.
    Drives the once-per-session 'you can call me X' intro per the v0.8 plan
    (decision 2: alias-only, no full rename cascade).

    Returns empty string when no alias is set or the alias is the default
    'claudia' (case-insensitive) — no directive needed in either case.
    """
    alias = runtime_config_mod.get_therapist_alias(data_root).strip()
    if not alias or alias.lower() == "claudia":
        return ""
    return (
        f"[bot directive — session opener only: the user's preferred name for "
        f"you in this deploy is '{alias}'. Include the exact phrase "
        f"'you can call me {alias}' once in this opener so they see you "
        f"remembered. Do not repeat the phrase in subsequent turns.] "
    )


def _seed_opener(session_id: str, model: str, blocks) -> None:
    claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
    assert claude is not None, "opener invoked without an Anthropic API key"
    synthetic_user = _opener_alias_directive(state.cfg.data_root) + "Begin the session."
    state.store.append_message(
        session_id,
        Message(
            role="user",
            content="",
            meta={
                "blocks": [{"type": "text", "text": synthetic_user}],
                "is_synthetic_opener": True,
            },
        ),
    )
    history = [{"role": "user", "content": synthetic_user}]
    tools_reg = _build_tool_registry(state.cfg, session_id=session_id)
    result = tool_loop_mod.run_tool_loop(
        claude=claude,
        model=model,
        blocks=blocks,
        history=history,
        tools=tools_reg,
    )
    _persist_turns_to_store(
        session_id=session_id,
        local_mode=False,
        loop_result=result,
        fallback_text=result.text or "What's on your mind?",
    )


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_view(session_id: str, request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    messages = state.store.load_messages(session_id)
    opener_expected = not state.cfg.is_local
    return state.templates.TemplateResponse(
        request,
        "session_chat.html",
        {
            "header": header,
            "messages": messages,
            "opener_expected": opener_expected,
        },
    )


def _has_visible_messages(session_id: str) -> bool:
    try:
        for m in state.store.load_messages(session_id):
            if m.content and m.content.strip():
                return True
    except (FileNotFoundError, ValueError):
        pass
    return False


@app.get("/session/{session_id}/messages-poll", response_class=HTMLResponse)
async def session_messages_poll(
    session_id: str, request: Request, _: str = Depends(require_auth)
) -> HTMLResponse:
    try:
        state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    poll_url = f"/session/{session_id}/messages-poll"
    if _has_visible_messages(session_id):
        messages = state.store.load_messages(session_id)
        return state.templates.TemplateResponse(
            request,
            "fragments/messages_list.html",
            {"messages": messages, "polling": False, "poll_url": poll_url},
        )
    if _event_present(session_id, "opener_failed"):
        return state.templates.TemplateResponse(
            request,
            "fragments/messages_list.html",
            {"messages": [], "polling": False, "opener_failed": True, "poll_url": poll_url},
        )
    return state.templates.TemplateResponse(
        request,
        "fragments/messages_list.html",
        {"messages": [], "polling": True, "poll_url": poll_url},
    )


@app.post("/session/{session_id}/message", response_class=HTMLResponse)
async def session_message(
    session_id: str,
    request: Request,
    _: str = Depends(require_auth),
) -> HTMLResponse:
    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    if header.status != "active":
        raise HTTPException(410, "Session is ended")

    form = await request.form()
    user_text = str(form.get("content", "")).strip()
    frame_tag = str(form.get("frame", "")).strip()
    if not user_text:
        return HTMLResponse("", status_code=204)

    # Kid-mode safety floor: run regex tripwire + Haiku classifier on every
    # incoming message before the companion is allowed to reply. Per Premise
    # 3 + values.schema.json, this is non-disableable in kid mode.
    safety_result = safety.screen_message(
        user_text,
        api_key=state.cfg.anthropic_api_key,
        classifier_model=state.cfg.classifier_model,
        enabled=state.cfg.is_kid,
    )
    if safety_result.flagged:
        log.info(
            "safety.flagged",
            session_id=session_id,
            category=safety_result.category,
            prominence=safety_result.prominence,
            regex=safety_result.flagged_regex,
            classifier=safety_result.flagged_classifier,
        )
        state.store.append_event(
            session_id,
            "safety_flag",
            {
                "category": safety_result.category,
                "prominence": safety_result.prominence,
                "explanation": safety_result.explanation,
                "regex": safety_result.flagged_regex,
                "classifier": safety_result.flagged_classifier,
            },
        )

    state.store.append_message(session_id, Message(role="user", content=user_text))
    if frame_tag:
        state.store.append_event(session_id, "frame_tag", {"tag": frame_tag})
    blocks = state.loader.assemble(frame_tag=frame_tag)

    if state.cfg.is_local:
        reply_text = f"[local mock reply] heard: {user_text[:120]!r}"
        result = None
    else:
        claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
        assert claude is not None, "session message invoked without an Anthropic API key"
        history: list[dict] = []
        for m in state.store.load_messages(session_id):
            if m.role not in ("user", "assistant"):
                continue
            raw_blocks = m.meta.get("blocks") if m.meta else None
            if raw_blocks:
                history.append({"role": m.role, "content": raw_blocks})
            else:
                history.append({"role": m.role, "content": m.content})
        tools_reg = _build_tool_registry(state.cfg, session_id=session_id)
        result = await asyncio.to_thread(
            tool_loop_mod.run_tool_loop,
            claude,
            header.model or SONNET,
            blocks,
            history,
            tools_reg,
        )
        reply_text = result.text or "(no response)"
        for tc in result.tool_calls:
            state.store.append_event(
                session_id,
                "tool_call",
                {"name": tc.name, "arguments": tc.arguments, "summary": tc.result_summary[:200]},
            )

    assistant_msg = _persist_turns_to_store(
        session_id=session_id,
        local_mode=state.cfg.is_local,
        loop_result=result if not state.cfg.is_local else None,
        fallback_text=reply_text,
    )

    return state.templates.TemplateResponse(
        request,
        "fragments/message_pair.html",
        {
            "user_message": Message(role="user", content=user_text),
            "assistant_message": assistant_msg,
            "session_ended": False,
            "header": header,
        },
    )


@app.get("/session/{session_id}/review", response_class=HTMLResponse)
async def session_review(
    session_id: str, request: Request, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Adult-mode memory-diff review. Read-only for v0.5.0 — surfaces the
    auditor's report as cards so the user can see what claudia learnt.
    Interactive keep/edit/discard is a v0.5.x follow-up."""
    if not state.cfg.is_adult:
        raise HTTPException(404, "review is adult-mode only")
    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")

    sidecar = summariser_mod.read_audit_sidecar(state.cfg.data_root, session_id)
    audit_running = (
        _event_present(session_id, "audit_scheduled")
        and not _event_present(session_id, "audit_applied")
        and not _event_present(session_id, "auditor_failed")
    )
    audit_failed = _event_present(session_id, "auditor_failed")

    # Build display-ready diff cards.
    cards: list[dict] = []
    if sidecar:
        if (sidecar.get("current_state_proposed") or "").strip():
            cards.append({
                "verb": "update",
                "title": "Top of mind right now",
                "rationale": sidecar.get("current_state_rationale") or "",
                "body": sidecar["current_state_proposed"],
                "kind": "current_state",
            })
        for u in sidecar.get("people_updates") or []:
            verb = {"add": "add", "update": "update", "touch": "update"}.get(u.get("action"), "update")
            who = u.get("name") or u.get("id") or "(person)"
            descr_parts: list[str] = []
            if u.get("relationship"):
                descr_parts.append(u["relationship"])
            if u.get("summary"):
                descr_parts.append(u["summary"])
            if u.get("important_context"):
                descr_parts.append("• " + " • ".join(u["important_context"]))
            if u.get("append_note"):
                descr_parts.append(u["append_note"])
            cards.append({
                "verb": verb,
                "title": ("New: " if u.get("action") == "add" else "About ") + who,
                "rationale": "",
                "body": "\n".join(descr_parts) if descr_parts else "(no detail)",
                "kind": "person",
            })
        for f in sidecar.get("app_feedback") or []:
            quote = (f.get("quote") or "").strip()
            obs = (f.get("observation") or "").strip()
            cards.append({
                "verb": "add",
                "title": "Note for the developer",
                "rationale": "",
                "body": (f"> {quote}\n\n{obs}" if quote else obs) or "(empty)",
                "kind": "app_feedback",
            })

    return state.templates.TemplateResponse(
        request,
        "session_review.html",
        {
            "header": header,
            "sidecar": sidecar,
            "cards": cards,
            "audit_running": audit_running,
            "audit_failed": audit_failed,
        },
    )


@app.post("/session/{session_id}/end")
async def session_end(
    session_id: str,
    _: str = Depends(require_auth),
) -> RedirectResponse:
    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404)
    if header.status != "active":
        return RedirectResponse("/", status_code=303)
    state.store.update_header(
        session_id,
        status="ended",
        ended_at=datetime.now(UTC).isoformat(),
    )
    state.store.append_event(session_id, "session_ended", {})
    log.info("session.ended", session_id=session_id)
    # Spawn audit on the event loop (not as a Starlette BackgroundTask) so the
    # response is fully flushed BEFORE the worker thread is taken — Starlette
    # holds the connection until BackgroundTasks complete, which let Envoy
    # see "upstream connection termination" on slow auditor runs (qa-protocol
    # bug 3 from v0.6.0). The sync auditor + Anthropic call still runs in a
    # worker thread via _run_audit_and_apply_async.
    _spawn_background(_run_audit_and_apply_async(session_id))
    return RedirectResponse(f"/session/{session_id}", status_code=303)


@app.post("/session/{session_id}/mood", response_class=HTMLResponse)
async def session_mood(
    session_id: str, request: Request, _: str = Depends(require_auth)
) -> Response:
    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    form = await request.form()
    try:
        score = int(str(form.get("regulation_score") or "").strip())
    except (ValueError, TypeError):
        raise HTTPException(400, "regulation_score must be an integer 1-10")
    if not (1 <= score <= 10):
        raise HTTPException(400, "regulation_score must be 1-10")

    if not _event_present(session_id, "mood_recorded"):
        session_ts = header.ended_at or header.created_at
        _append_mood(state.cfg.data_root, session_id, regulation_score=score, ts=session_ts)
        state.store.append_event(session_id, "mood_recorded", {"score": score})

    # Return a tiny acknowledgement that swaps in for the form.
    return HTMLResponse(
        f'<section id="msg-form" class="mood-end-panel">'
        f'<p>Recorded <strong>{score}/10</strong>.</p>'
        f'<a href="/" role="button" class="primary">Done</a>'
        f"</section>"
    )


def _append_mood(data_root: Path, session_id: str, regulation_score: int, ts: str | None = None) -> None:
    from app.db_audit import record_mood

    record_mood(data_root, session_id, regulation_score, ts=ts)


# ---------------------------------------------------------------------------
# Auditor (background)
# ---------------------------------------------------------------------------


def _event_present(session_id: str, event_type: str) -> bool:
    """Encapsulates store-specific lookup. SessionStore.has_event is the
    Protocol method; this wrapper exists so callers don't import the store types."""
    return state.store.has_event(session_id, event_type)


async def _run_audit_and_apply_async(session_id: str) -> None:
    """Async wrapper: run the sync auditor in a worker thread, log on failure."""
    try:
        await asyncio.to_thread(_run_audit_and_apply, session_id)
    except Exception:  # noqa: BLE001
        log.exception("audit.async_wrapper_failed", session_id=session_id)


def _run_audit_and_apply(session_id: str) -> None:
    """Background-task: run auditor, write session-log + current_state + app-feedback."""
    if _event_present(session_id, "audit_applied"):
        return
    state.store.append_event(session_id, "audit_scheduled", {})
    try:
        header = state.store.load_header(session_id)
    except (FileNotFoundError, ValueError) as e:
        log.error("audit.header_missing", session_id=session_id, error=str(e))
        return

    ctx_dir = state.cfg.data_root / "context"
    current_state = (
        (ctx_dir / "05_current_state.md").read_text(encoding="utf-8")
        if (ctx_dir / "05_current_state.md").exists()
        else ""
    )
    logs_dir = state.cfg.data_root / "session-logs"
    log_tails = ""
    if logs_dir.exists():
        files = sorted(logs_dir.glob("*.md"), reverse=True)[:3]
        parts = []
        for f in files:
            try:
                parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')[-2048:]}")
            except OSError:
                continue
        log_tails = "\n\n".join(parts)

    try:
        messages = state.store.load_messages(session_id)
    except (FileNotFoundError, ValueError) as e:
        log.error("audit.messages_missing", session_id=session_id, error=str(e))
        return

    inp = summariser_mod.SummariserInput(
        session_id=session_id,
        messages=messages,
        current_state=current_state,
        recent_session_logs=log_tails,
    )
    try:
        if state.cfg.is_local:
            report = summariser_mod.mock_auditor_report(inp)
        else:
            claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
            assert claude is not None, "auditor invoked without an Anthropic API key"
            report = summariser_mod.run_auditor(
                claude,
                state.cfg.prompts_dir,
                header.model or SONNET,
                inp,
                mode=state.cfg.mode,
                display_name=state.cfg.display_name,
                parent_display_name=effective_kid_parent_display_name() if state.cfg.is_kid else "",
            )
    except summariser_mod.AuditorError as e:
        log.error("audit.failed", session_id=session_id, error=str(e))
        state.store.append_event(session_id, "auditor_failed", {"error": str(e)})
        return

    # Persist + apply
    if report.summary_markdown.strip():
        try:
            path = summariser_mod.write_session_log(
                state.cfg.data_root, session_id, report.title, report.summary_markdown
            )
            state.store.append_event(session_id, "session_log_written", {"path": str(path)})
        except OSError as e:
            log.error("audit.session_log_failed", session_id=session_id, error=str(e))

    if report.current_state_proposed.strip():
        try:
            summariser_mod.write_current_state(state.cfg.data_root, report.current_state_proposed)
            state.store.append_event(session_id, "current_state_updated", {})
        except OSError as e:
            log.error("audit.current_state_failed", session_id=session_id, error=str(e))

    if report.app_feedback:
        try:
            path = summariser_mod.append_app_feedback(state.cfg.data_root, session_id, report.app_feedback)
            if path:
                state.store.append_event(
                    session_id, "app_feedback_recorded", {"path": str(path), "count": len(report.app_feedback)}
                )
        except OSError as e:
            log.error("audit.app_feedback_failed", session_id=session_id, error=str(e))

    if report.people_updates:
        applied = summariser_mod.apply_people_updates(state.people, report.people_updates)
        if applied:
            state.store.append_event(
                session_id, "people_updates_applied", {"count": len(applied), "items": applied}
            )

    # Persist a structured sidecar so /session/<id>/review can render the
    # memory-diff cards. Independent of session-log markdown (which is prose).
    try:
        summariser_mod.write_audit_sidecar(state.cfg.data_root, session_id, report)
    except OSError as e:
        log.warning("audit.sidecar_failed", session_id=session_id, error=str(e))

    state.store.append_event(session_id, "audit_applied", {})


# ---------------------------------------------------------------------------
# Report (therapist handover)
# ---------------------------------------------------------------------------


@app.get("/report", response_class=HTMLResponse)
async def report_form(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    if not state.cfg.is_adult:
        raise HTTPException(404, "report is adult-mode only")
    last_export_ts = _read_last_export_ts(state.cfg.data_root)
    today = date.today()
    default_start = last_export_ts.date() if last_export_ts else (today - timedelta(days=30))
    return state.templates.TemplateResponse(
        request,
        "report.html",
        {
            "default_start": default_start.isoformat(),
            "default_end": today.isoformat(),
            "last_export": last_export_ts.isoformat() if last_export_ts else None,
        },
    )


@app.post("/report")
async def report_submit(request: Request, _: str = Depends(require_auth)) -> Response:
    if not state.cfg.is_adult:
        raise HTTPException(404, "report is adult-mode only")
    from fastapi.responses import FileResponse

    form = await request.form()
    try:
        start = date.fromisoformat(str(form.get("start_date", "")).strip())
        end = date.fromisoformat(str(form.get("end_date", "")).strip())
    except ValueError:
        raise HTTPException(400, "Invalid date — use YYYY-MM-DD")
    if end < start:
        raise HTTPException(400, "end_date must be on or after start_date")

    bundle = _collect_for_report(state.cfg.data_root, start, end)
    if not bundle["transcripts"]:
        raise HTTPException(404, "No sessions found in that date range")

    if state.cfg.is_local:
        markdown = _mock_handover_markdown(start, end, bundle["session_count"], bundle["mood_entries"])
    else:
        claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(state.cfg.data_root))
        assert claude is not None, "/report invoked without an Anthropic API key"
        markdown = await asyncio.to_thread(
            _run_handover_call,
            claude,
            state.cfg.prompts_dir,
            start,
            end,
            bundle,
        )

    pdf_path = _render_handover_pdf(state.cfg.data_root, start, end, markdown)
    _write_last_export_ts(state.cfg.data_root, datetime.now(UTC))
    return FileResponse(path=str(pdf_path), filename=pdf_path.name, media_type="application/pdf")


def _read_last_export_ts(data_root: Path) -> datetime | None:
    p = data_root / "session-exports" / ".last_export.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        return datetime.fromisoformat(rec["ts"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _write_last_export_ts(data_root: Path, ts: datetime) -> None:
    p = data_root / "session-exports" / ".last_export.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": ts.isoformat()}), encoding="utf-8")


def _collect_for_report(data_root: Path, start: date, end: date) -> dict:
    """Return a bundle of inputs for the handover call:
        transcripts, session_count, mood_entries, spine, session_logs

    `spine` is the factual ground truth (current_state + patterns + relationship
    map + background) — needed so the handover model can attribute actions
    correctly. Without it, the model has no idea who's who and confabulates.

    `session_logs` is the auditor's already-summarised version of each session
    in range — preferred over raw transcripts for narrative facts, and disambiguates
    referents the raw chat assumes both speakers share.
    """
    sessions_dir = data_root / "sessions"
    transcripts_blocks: list[str] = []
    count = 0
    if sessions_dir.exists():
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if ".bak-" in path.name:
                continue
            try:
                created: datetime | None = None
                lines: list[str] = []
                with path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        try:
                            rec = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        rtype = rec.get("type")
                        if rtype == "header" and not created:
                            ts = rec.get("created_at")
                            if ts:
                                try:
                                    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                except ValueError:
                                    created = None
                        elif rtype == "message":
                            role = rec.get("role")
                            content = (rec.get("content") or "").strip()
                            if role not in ("user", "assistant") or not content:
                                continue
                            meta = rec.get("meta") or {}
                            if meta.get("is_synthetic_opener"):
                                continue
                            label = "LIAM" if role == "user" else "COMPANION"
                            lines.append(f"{label}: {content}")
                if created is None:
                    continue
                d = created.date()
                if d < start or d > end:
                    continue
                count += 1
                transcripts_blocks.append(
                    f"## Session {path.stem} ({d.isoformat()})\n\n" + "\n\n".join(lines)
                )
            except OSError:
                continue

    # Mood entries in range
    mood_entries: list[dict] = []
    mood_path = data_root / "context" / "mood-log.jsonl"
    if mood_path.exists():
        try:
            for line in mood_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts", "")
                try:
                    d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
                if start <= d <= end:
                    mood_entries.append(rec)
        except OSError:
            pass

    # Factual spine — ground truth for who's who and what happened previously.
    ctx_dir = data_root / "context"
    spine_files = [
        ("01_background.md", "Background"),
        ("02_patterns.md", "Patterns"),
        ("04_relationship_map.md", "Relationship map"),
        ("05_current_state.md", "Current state"),
    ]
    spine_parts: list[str] = []
    for fname, label in spine_files:
        p = ctx_dir / fname
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    spine_parts.append(f"## {label} ({fname})\n\n{content}")
            except OSError:
                continue
    spine = "\n\n---\n\n".join(spine_parts)

    # Session-log markdown files whose YYYY-MM-DD prefix falls in range.
    logs_dir = data_root / "session-logs"
    log_blocks: list[str] = []
    if logs_dir.exists():
        for p in sorted(logs_dir.glob("*.md")):
            stem = p.stem
            try:
                log_date = date.fromisoformat(stem[:10])
            except ValueError:
                continue
            if log_date < start or log_date > end:
                continue
            try:
                log_blocks.append(f"## {stem}\n\n{p.read_text(encoding='utf-8').strip()}")
            except OSError:
                continue
    session_logs = "\n\n---\n\n".join(log_blocks)

    return {
        "transcripts": "\n\n---\n\n".join(transcripts_blocks),
        "session_count": count,
        "mood_entries": mood_entries,
        "spine": spine,
        "session_logs": session_logs,
    }


def _run_handover_call(
    claude: claude_mod.ClaudeClient,
    prompts_dir: Path,
    start: date,
    end: date,
    bundle: dict,
) -> str:
    system = (prompts_dir / "handover.md").read_text(encoding="utf-8")
    mood_summary = _summarise_mood(bundle["mood_entries"])
    user_msg = (
        f"# Date range\n{start.isoformat()} to {end.isoformat()}\n\n"
        f"# Mood log entries (in range)\n{mood_summary}\n\n"
        "# FACTUAL SPINE — ground truth\n"
        "These files are the source of truth for who's who and what happened. "
        "When attributing actions, defer to this spine. Never guess.\n\n"
        f"{bundle['spine']}\n\n"
        "---\n\n"
        "# Session-log summaries (auditor-written, in range)\n"
        "Already-summarised version of each session. Use these as your structured base.\n\n"
        f"{bundle['session_logs'] or '(no session-logs in range)'}\n\n"
        "---\n\n"
        "# Raw session transcripts (in range)\n"
        "Verbatim chat. Useful for quotes and for what *Liam said in the moment*. "
        "Treat referents as ambiguous when not made explicit — defer to the spine.\n\n"
        f"{bundle['transcripts']}\n\n"
        "Produce the handover markdown per your system prompt. One A4 page."
    )
    raw = claude._c.messages.create(  # noqa: SLF001
        model=SONNET,
        max_tokens=2048,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user_msg}],
    )
    parts = [b.text for b in raw.content if hasattr(b, "text")]
    return "\n\n".join(parts).strip()


def _summarise_mood(entries: list[dict]) -> str:
    if not entries:
        return "(no mood entries in range)"
    scores = [int(e["regulation_score"]) for e in entries if isinstance(e.get("regulation_score"), int)]
    if not scores:
        return "(entries present but no valid scores)"
    return (
        f"n={len(scores)}, first={scores[0]}, last={scores[-1]}, "
        f"min={min(scores)}, max={max(scores)}, mean={sum(scores)/len(scores):.1f}"
    )


def _mock_handover_markdown(start: date, end: date, n: int, mood_entries: list[dict]) -> str:
    return (
        f"# Handover — {start.isoformat()} to {end.isoformat()}\n\n"
        f"**Sessions:** {n}  ·  **Mood:** {_summarise_mood(mood_entries)}\n\n"
        "## Themes\n- (mock — no model in local mode)\n\n"
        "## Notable events\n- (mock)\n\n"
        "## Action items / commitments named\n- (mock)\n\n"
        "## Risk / safety notes\n(none)\n\n"
        "## Open questions for next appointment\n- (mock)\n"
    )


def _render_handover_pdf(data_root: Path, start: date, end: date, markdown: str) -> Path:
    """Render the markdown string to a one-page A4 PDF using ReportLab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    out_dir = data_root / "session-exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"handover_{start.isoformat()}_to_{end.isoformat()}.pdf"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        title="Handover note",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=6, spaceAfter=2)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=12)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=0)

    flow = []
    for kind, content in _md_blocks(markdown):
        if kind == "h1":
            flow.append(Paragraph(_esc(content), h1))
            flow.append(HRFlowable(color="#999", thickness=0.5, spaceAfter=4))
        elif kind == "h2":
            flow.append(Paragraph(_esc(content), h2))
        elif kind == "bullet":
            flow.append(Paragraph(f"• {_esc(content)}", bullet))
        elif kind == "para":
            flow.append(Paragraph(_esc(content), body))
        elif kind == "blank":
            flow.append(Spacer(1, 3))
    doc.build(flow)
    return out_path


def _esc(s: str) -> str:
    """Escape for ReportLab Paragraph, then convert markdown **bold** and
    *italic* into the inline HTML ReportLab understands (`<b>`/`<i>`).
    Order: HTML-escape first so the user content can't smuggle tags."""
    import re

    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", s)
    return s


def _md_blocks(text: str):
    buf: list[str] = []

    def flush():
        if buf:
            yield ("para", " ".join(buf).strip())
            buf.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            yield from flush()
            yield ("blank", "")
        elif s.startswith("# "):
            yield from flush()
            yield ("h1", s[2:])
        elif s.startswith("## "):
            yield from flush()
            yield ("h2", s[3:])
        elif s.startswith("- ") or s.startswith("* "):
            yield from flush()
            yield ("bullet", s[2:])
        else:
            buf.append(s)
    yield from flush()


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------


def _google_creds_present(cfg: config_module.Config) -> bool:
    """True iff client_id + client_secret are available (env or kv).
    Identity flow can run even when /settings tools toggle is off,
    as long as credentials are configured."""
    cid, secret, _redirect = runtime_config_mod.get_google_creds(cfg.data_root)
    return bool(cid and secret) and cfg.is_adult


def _google_identity_bound_email(cfg: config_module.Config) -> str | None:
    """The one-shot-locked email from runtime_config.
    Returns None if no identity has been bound yet."""
    from app.db_kv import kv_get

    return kv_get(cfg.data_root, runtime_config_mod.KV_GOOGLE_IDENTITY_EMAIL) or None


@app.get("/connect-gmail", response_class=HTMLResponse)
async def connect_gmail(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    """Tool-side OAuth entrypoint. Adult-mode + Google-tools-enabled only.
    Requests identity scopes alongside tool scopes so the same token serves
    both purposes (decision 1 of v0.8 plan)."""
    if not _google_enabled(state.cfg):
        raise HTTPException(status_code=404, detail="Google integrations not enabled")
    gcfg = _google_cfg(state.cfg)
    if not gcfg.is_complete():
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "config_missing"}
        )
    stat = google_auth.status(gcfg)
    if stat["state"] == "connected":
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "connected", "scopes": stat.get("scopes", [])}
        )
    # Request both scope sets so the resulting token covers identity AND tools.
    auth_url, _s = google_auth.begin_flow(
        gcfg,
        scopes=google_auth.IDENTITY_SCOPES + google_auth.TOOL_SCOPES,
        purpose="both",
    )
    return state.templates.TemplateResponse(
        request, "connect_gmail.html", {"variant": "connect", "auth_url": auth_url}
    )


@app.get("/connect-gmail/disconnect")
async def connect_gmail_disconnect(_: str = Depends(require_auth)) -> RedirectResponse:
    if not _google_enabled(state.cfg):
        raise HTTPException(status_code=404, detail="Google integrations not enabled")
    google_auth.revoke(_google_cfg(state.cfg))
    return RedirectResponse("/", status_code=303)


@app.post("/auth/google/start")
async def auth_google_start(request: Request) -> Response:
    """Identity-only OAuth entrypoint. Used by /setup/2 and /login when the
    user picks 'Sign in with Google'. Adult-mode only.

    Unauthenticated by design — the user is mid-login. The identity binding
    (one-shot lock per decision 9) is enforced in the callback.
    """
    cfg = state.cfg
    if not cfg.is_adult:
        raise HTTPException(status_code=404, detail="Google sign-in is adult-mode only")
    if cfg.is_local:
        raise HTTPException(status_code=404, detail="Google sign-in unavailable in local mode")
    if not _google_creds_present(cfg):
        raise HTTPException(
            status_code=400,
            detail="Google OAuth credentials are not configured. Complete /setup/2 with the password path or add credentials in /settings.",
        )
    gcfg = _google_cfg(cfg)
    auth_url, _s = google_auth.begin_flow(
        gcfg,
        scopes=google_auth.IDENTITY_SCOPES,
        purpose="identity",
    )
    return RedirectResponse(url=auth_url, status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> Response:
    """Handles BOTH identity and tool callbacks.

    Branches on the `purpose` recorded by begin_flow at the matching state
    token. Identity callbacks bind the email one-shot (decision 9) and
    issue an adult login cookie. Tool callbacks just persist the token and
    show the success template. Both-purpose callbacks do both.

    Unauthenticated for identity flows (the user is mid-login). Tool flows
    are reachable only when the user is already logged in, so no extra
    auth check is needed here — the OAuth state token is the binding."""
    cfg = state.cfg
    if not cfg.is_adult:
        raise HTTPException(status_code=404, detail="Google integrations are adult-mode only")
    if not _google_creds_present(cfg):
        raise HTTPException(status_code=400, detail="Google credentials missing")

    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "callback_error", "error": error}, status_code=400
        )
    if not code or not returned_state:
        return state.templates.TemplateResponse(
            request,
            "connect_gmail.html",
            {"variant": "callback_error", "error": "Missing code or state"},
            status_code=400,
        )
    gcfg = _google_cfg(cfg)
    try:
        result = google_auth.exchange_code(gcfg, code, returned_state)
    except Exception as e:  # noqa: BLE001
        log.exception("oauth.exchange_failed")
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "callback_error", "error": str(e)}, status_code=500
        )

    # Identity branch: bind the email if not yet bound; reject mismatch.
    issued_login = False
    if result.purpose in ("identity", "both") and result.identity_email:
        from app.db_kv import kv_get, kv_set

        bound = kv_get(cfg.data_root, runtime_config_mod.KV_GOOGLE_IDENTITY_EMAIL)
        if not bound:
            kv_set(cfg.data_root, runtime_config_mod.KV_GOOGLE_IDENTITY_EMAIL, result.identity_email)
            log.info("oauth.identity_bound", email=result.identity_email)
            issued_login = True
        elif bound == result.identity_email:
            issued_login = True
        else:
            log.warning(
                "oauth.identity_mismatch",
                bound_email=bound,
                attempted_email=result.identity_email,
            )
            return state.templates.TemplateResponse(
                request,
                "connect_gmail.html",
                {
                    "variant": "callback_error",
                    "error": (
                        f"This deploy is bound to {bound}; can't sign in as "
                        f"{result.identity_email}. To rebind, an operator must clear "
                        "google_identity_email from the kv_store via kubectl exec."
                    ),
                },
                status_code=403,
            )

    if issued_login:
        # Identity flow lands the user back at /setup/3 (mid-wizard) or /
        # (post-setup login). Use _setup_complete to decide.
        target = "/" if _setup_complete() else "/setup/3"
        return _issue_login_cookie("adult", redirect_to=target)

    # Tool-only callback (the existing /connect-gmail flow path).
    return state.templates.TemplateResponse(request, "connect_gmail.html", {"variant": "callback_ok"})


# ---------------------------------------------------------------------------
# Legacy /upload + /session/{id}/paste — thin shims that forward to the
# unified library pipeline. Marker shape preserved as `[uploaded: <doc_id>]`
# / `[pasted: <doc_id>]` per spec; session_chat.html JS reads j.doc_id.
# ---------------------------------------------------------------------------

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _safe_filename(name: str) -> str:
    import re

    name = name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = name.strip("-.")
    return name[:120] or "upload"


@app.post("/upload")
async def upload(request: Request, _: str = Depends(require_auth)) -> Response:
    from fastapi import UploadFile

    form = await request.form()
    upload_obj = form.get("file")
    if not hasattr(upload_obj, "filename") or not upload_obj.filename:  # type: ignore[union-attr]
        return Response(
            content=json.dumps({"error": "no file field"}),
            status_code=400,
            media_type="application/json",
        )
    file: UploadFile = upload_obj  # type: ignore[assignment]
    content_type = file.content_type or "application/octet-stream"
    body = await file.read()
    if len(body) > _MAX_UPLOAD_BYTES:
        return Response(
            content=json.dumps({"error": f"file too large (max {_MAX_UPLOAD_BYTES} bytes)"}),
            status_code=413,
            media_type="application/json",
        )
    if not body:
        return Response(
            content=json.dumps({"error": "empty upload"}),
            status_code=400,
            media_type="application/json",
        )

    title = (file.filename or "Upload").rsplit(".", 1)[0] or "Upload"
    try:
        doc_id = library_pipeline.process_doc_creation(
            state.library,
            state.extractor_registry,
            title=title,
            original_bytes=body,
            filename=file.filename,
            mime=content_type,
            source="upload",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("upload.pipeline_failed")
        return Response(
            content=json.dumps({"error": f"extraction failed: {e!s}"}),
            status_code=500,
            media_type="application/json",
        )
    return Response(
        content=json.dumps(
            {
                "doc_id": doc_id,
                "size": len(body),
                "mime": content_type,
            }
        ),
        media_type="application/json",
    )


@app.post("/session/{session_id}/paste")
async def session_paste(session_id: str, request: Request, _: str = Depends(require_auth)) -> Response:
    try:
        state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404)
    form = await request.form()
    content = str(form.get("content", "")).strip()
    if not content:
        return Response(status_code=400, content="empty paste")
    label = _safe_filename(str(form.get("label", "paste")))
    title = label.replace("-", " ").strip() or "Pasted note"

    try:
        doc_id = library_pipeline.process_doc_creation(
            state.library,
            state.extractor_registry,
            title=title,
            original_bytes=content.encode("utf-8"),
            filename=f"{label}.txt",
            mime="text/plain",
            source="paste",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("paste.pipeline_failed")
        return Response(
            content=json.dumps({"error": f"extraction failed: {e!s}"}),
            status_code=500,
            media_type="application/json",
        )

    state.store.append_event(session_id, "paste_saved", {"doc_id": doc_id})
    return Response(
        content=json.dumps({"doc_id": doc_id}),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Kid-mode OCR-discard attachment (Step 8).
#
# Variant-C: kid uploads a screenshot, the server runs vision OCR
# synchronously, the original file is deleted, the transcript is rendered as
# a tool-card in chat, and then the companion replies. No library entry; no
# residual image. The companion uses list_people / lookup_person tools to
# spot unknown names from the OCR text and asks the kid in dialogue.
# ---------------------------------------------------------------------------

_KID_ATTACH_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — kid screenshots, not big PDFs.
_KID_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}


@app.post("/session/{session_id}/kid-attach", response_class=HTMLResponse)
async def session_kid_attach(
    session_id: str,
    request: Request,
    _: str = Depends(require_auth),
) -> HTMLResponse:
    """Kid-only image attachment with sync OCR + immediate file deletion."""
    cfg = state.cfg
    if not cfg.is_kid:
        raise HTTPException(404, "kid-attach is kid-mode only")

    try:
        header = state.store.load_header(session_id)
    except FileNotFoundError:
        raise HTTPException(404, "Session not found")
    if header.status != "active":
        raise HTTPException(410, "Session is ended")

    form = await request.form()
    upload_obj = form.get("file")
    if not hasattr(upload_obj, "filename") or not upload_obj.filename:  # type: ignore[union-attr]
        raise HTTPException(400, "no file field")
    user_text = str(form.get("content", "")).strip()

    from fastapi import UploadFile

    file: UploadFile = upload_obj  # type: ignore[assignment]
    filename = _safe_filename(file.filename or "image.png")
    suffix = Path(filename).suffix.lower() or ".png"
    if suffix not in _KID_ATTACH_EXTS:
        raise HTTPException(415, f"unsupported image type: {suffix}")

    body = await file.read()
    if len(body) > _KID_ATTACH_MAX_BYTES:
        raise HTTPException(413, f"image too large (max {_KID_ATTACH_MAX_BYTES // (1024*1024)} MB)")
    if not body:
        raise HTTPException(400, "empty upload")

    # Stage the file under the session dir; we will delete it after OCR.
    staging_dir = cfg.data_root / "kid-attach-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_name = f"{session_id}-{secrets.token_hex(4)}-{filename}"
    staged = staging_dir / staged_name
    staged.write_bytes(body)

    # Synchronous OCR via the existing ImageExtractor.
    extractor = state.extractor_registry.pick(staged, file.content_type or "image/png")
    if extractor is None or extractor.kind != "image":
        try:
            staged.unlink(missing_ok=True)
        finally:
            pass
        raise HTTPException(500, "no image extractor available")

    ocr_text = ""
    ocr_failed = False
    try:
        if cfg.is_local:
            ocr_text = f"[local-mock OCR of {filename}]"
        else:
            result = extractor.extract(staged)
            ocr_text = (result.extracted_md or "").strip()
    except Exception as e:  # noqa: BLE001
        log.exception("kid_attach.ocr_failed", filename=filename, error=str(e))
        ocr_failed = True
    finally:
        # OCR-DISCARD: kill the original. The transcript is what the chat keeps.
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    # Append the visible turns to the chat log.
    user_marker = f"[attached: {filename}]"
    user_visible = f"{user_marker}\n\n{user_text}" if user_text else user_marker
    state.store.append_message(
        session_id,
        Message(
            role="user",
            content=user_visible,
            meta={"kid_attach": True, "filename": filename},
        ),
    )
    state.store.append_message(
        session_id,
        Message(
            role="system_event",
            content=ocr_text if not ocr_failed else "",
            meta={
                "kind": "kid_attach_ocr",
                "filename": filename,
                "failed": ocr_failed,
            },
        ),
    )
    state.store.append_event(
        session_id,
        "kid_attach",
        {"filename": filename, "ocr_chars": len(ocr_text), "failed": ocr_failed},
    )

    # Build companion turn. The OCR text is folded into the user message we
    # send to the model so the model has the screenshot's content in context.
    if ocr_failed:
        synth_user = (
            f"[I attached an image: {filename}, but the OCR step failed. "
            f"Tell me you couldn't read it and ask me to type out the relevant part.]\n\n"
            f"{user_text}"
        ).strip()
    else:
        synth_user = (
            f"[I attached an image: {filename}. Here's what you read from it (vision OCR — original image discarded):]\n\n"
            f"{ocr_text or '(no text was found in the image)'}\n\n"
            f"{user_text or '(no other text from me — figure out what I want from context)'}"
        )

    blocks = state.loader.assemble(frame_tag="")
    if cfg.is_local:
        reply_text = f"[local mock reply] OCR'd {filename} ({len(ocr_text)} chars). user said: {user_text!r}"
        loop_result = None
    else:
        claude = claude_mod.get_client(runtime_config_mod.get_anthropic_key(cfg.data_root))
        assert claude is not None, "kid-attach invoked without an Anthropic API key"
        history: list[dict] = []
        prior = state.store.load_messages(session_id)
        # Drop the two messages we just appended (user + system_event); we
        # rebuild the user turn with the synthesised OCR-folded version.
        for m in prior[:-2]:
            if m.role not in ("user", "assistant"):
                continue
            raw_blocks = m.meta.get("blocks") if m.meta else None
            if raw_blocks:
                history.append({"role": m.role, "content": raw_blocks})
            else:
                history.append({"role": m.role, "content": m.content})
        history.append({"role": "user", "content": synth_user})
        tools_reg = _build_tool_registry(cfg, session_id=session_id)
        loop_result = await asyncio.to_thread(
            tool_loop_mod.run_tool_loop,
            claude,
            header.model or SONNET,
            blocks,
            history,
            tools_reg,
        )
        reply_text = loop_result.text or "(no response)"
        for tc in loop_result.tool_calls:
            state.store.append_event(
                session_id,
                "tool_call",
                {"name": tc.name, "arguments": tc.arguments, "summary": tc.result_summary[:200]},
            )

    assistant_msg = _persist_turns_to_store(
        session_id=session_id,
        local_mode=cfg.is_local,
        loop_result=loop_result if not cfg.is_local else None,
        fallback_text=reply_text,
    )

    return state.templates.TemplateResponse(
        request,
        "fragments/kid_attach_pair.html",
        {
            "filename": filename,
            "user_text": user_text,
            "ocr_text": ocr_text,
            "ocr_failed": ocr_failed,
            "assistant_message": assistant_msg,
            "header": header,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist_turns_to_store(
    session_id: str, local_mode: bool, loop_result, fallback_text: str
) -> Message:
    if local_mode or loop_result is None:
        msg = Message(role="assistant", content=fallback_text)
        state.store.append_message(session_id, msg)
        return msg

    turns_list = loop_result.turns or []
    if not turns_list:
        msg = Message(role="assistant", content=fallback_text)
        state.store.append_message(session_id, msg)
        return msg

    def _extract_text(blocks: list) -> str:
        parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n\n".join(p for p in parts if p).strip()

    final_msg: Message | None = None
    for t in turns_list:
        if t.role == "assistant":
            text = _extract_text(t.blocks)
            msg = Message(role=t.role, content=text, meta={"blocks": t.blocks})
        else:
            msg = Message(role=t.role, content="", meta={"blocks": t.blocks})
        state.store.append_message(session_id, msg)
        if t.role == "assistant":
            final_msg = msg
    if final_msg is None:
        final_msg = Message(role="assistant", content=fallback_text)
        state.store.append_message(session_id, final_msg)
    return final_msg


# ---------------------------------------------------------------------------
# /library/* — unified library management (commit D of Step 3)
# ---------------------------------------------------------------------------


_LIBRARY_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB; matches legacy /upload cap


def require_library_access(
    request: Request,
    admin_credentials: HTTPBasicCredentials | None = Depends(_admin_basic_auth),
) -> str:
    """
    Library + people access shape:
      - local mode: pass.
      - kid mode: parent-admin basic auth (kid doesn't see /library or /people).
      - adult mode: same cookie auth as the rest of the user surface.
    """
    cfg = state.cfg
    if cfg.is_local:
        return "liam"
    if cfg.is_kid:
        if admin_credentials is not None and _check_basic_credentials(admin_credentials):
            return admin_credentials.username
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="library is parent-admin only in kid mode",
            headers={"WWW-Authenticate": 'Basic realm="claudia-admin"'},
        )
    # Adult mode: cookie auth (the v0.7.1 refactor; no more HTTP Basic).
    display = _adult_cookie_display_name(request)
    if display is not None:
        return display
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="adult-not-logged-in",
        headers={"Location": "/login"},
    )


@app.get("/library", response_class=HTMLResponse)
async def library_index(
    request: Request, _: str = Depends(require_library_access)
) -> HTMLResponse:
    active = state.library.list_active()
    archived = state.library.list_archived()
    return state.templates.TemplateResponse(
        request,
        "library.html",
        {"active": active, "archived": archived},
    )


def _is_async_request(request: Request) -> bool:
    """JS-aware client (HTMX) wants the async streaming path."""
    return request.headers.get("hx-request", "").lower() == "true"


@app.post("/library/upload")
async def library_upload(
    request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(400, "no file in request")
    raw_title = (form.get("title") or "").strip() or upload.filename or "Untitled"
    raw_tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
    body = await upload.read()
    if len(body) > _LIBRARY_MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"upload exceeds {_LIBRARY_MAX_UPLOAD_BYTES} bytes")
    if not body:
        raise HTTPException(400, "empty upload")

    if _is_async_request(request):
        doc_id = state.library.mint_doc_id(raw_title)
        asyncio.create_task(
            library_pipeline.process_doc_creation_async(
                state.library,
                state.extractor_registry,
                state.status_bus,
                doc_id=doc_id,
                title=raw_title,
                original_bytes=body,
                filename=upload.filename,
                mime=upload.content_type or "application/octet-stream",
                source="upload",
                tags=raw_tags,
            )
        )
        return RedirectResponse(url=f"/library/{doc_id}/stream", status_code=303)

    doc_id = library_pipeline.process_doc_creation(
        state.library,
        state.extractor_registry,
        title=raw_title,
        original_bytes=body,
        filename=upload.filename,
        mime=upload.content_type or "application/octet-stream",
        source="upload",
        tags=raw_tags,
    )
    return RedirectResponse(url=f"/library#{doc_id}", status_code=303)


@app.post("/library/paste")
async def library_paste(
    request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    text = (form.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty paste")
    raw_title = (form.get("title") or "").strip()
    raw_tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
    if not raw_title:
        # Title heuristic: first non-empty line, max 80 chars.
        from app.extractors import TextExtractor

        raw_title = TextExtractor.title_from_text(text, fallback="Pasted note")

    body = text.encode("utf-8")

    if _is_async_request(request):
        doc_id = state.library.mint_doc_id(raw_title)
        asyncio.create_task(
            library_pipeline.process_doc_creation_async(
                state.library,
                state.extractor_registry,
                state.status_bus,
                doc_id=doc_id,
                title=raw_title,
                original_bytes=body,
                filename="paste.txt",
                mime="text/plain",
                source="paste",
                tags=raw_tags,
            )
        )
        return RedirectResponse(url=f"/library/{doc_id}/stream", status_code=303)

    doc_id = library_pipeline.process_doc_creation(
        state.library,
        state.extractor_registry,
        title=raw_title,
        original_bytes=body,
        filename="paste.txt",
        mime="text/plain",
        source="paste",
        tags=raw_tags,
    )
    return RedirectResponse(url=f"/library#{doc_id}", status_code=303)


@app.get("/library/{doc_id}/stream")
async def library_doc_stream(
    doc_id: str, _: str = Depends(require_library_access)
) -> Response:
    """SSE endpoint streaming pipeline status messages."""
    from sse_starlette.sse import EventSourceResponse

    async def event_gen():
        async for msg in state.status_bus.subscribe(doc_id):
            yield {"event": "message", "data": msg}

    return EventSourceResponse(event_gen())


@app.get("/library/{doc_id}", response_class=HTMLResponse)
async def library_doc_detail(
    doc_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    meta = state.library.get(doc_id)
    if meta is None:
        raise HTTPException(404, f"doc {doc_id} not found")
    if not _is_async_request(request):
        return RedirectResponse(url=f"/library#{doc_id}", status_code=303)
    extracted = state.library.get_extracted(doc_id) or ""
    verification = state.library.get_verification(doc_id) or {}
    return state.templates.TemplateResponse(
        request,
        "fragments/library_detail.html",
        {
            "meta": meta,
            "extracted_preview": extracted[:5000],
            "extracted_truncated": len(extracted) > 5000,
            "verification": verification,
        },
    )


@app.post("/library/{doc_id}/tags")
async def library_doc_tags(
    doc_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
    state.library.update_meta(doc_id, tags=tags)
    return RedirectResponse(url=f"/library#{doc_id}", status_code=303)


@app.post("/library/{doc_id}/supersede")
async def library_doc_supersede(
    doc_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(400, "no file in request")
    raw_title = (form.get("title") or "").strip() or upload.filename or "Updated"
    body = await upload.read()
    if len(body) > _LIBRARY_MAX_UPLOAD_BYTES:
        raise HTTPException(413, "upload too large")
    if not body:
        raise HTTPException(400, "empty upload")

    new_id = library_pipeline.process_doc_creation(
        state.library,
        state.extractor_registry,
        title=raw_title,
        original_bytes=body,
        filename=upload.filename,
        mime=upload.content_type or "application/octet-stream",
        source="upload",
        supersedes=doc_id,
    )
    new_meta = state.library.get(new_id)
    if new_meta is None:
        raise HTTPException(500, "supersede: failed to read back new meta")
    state.library.supersede(doc_id, new_meta)
    return RedirectResponse(url=f"/library#{new_id}", status_code=303)


@app.post("/library/{doc_id}/delete")
async def library_doc_soft_delete(
    doc_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.library.soft_delete(doc_id)
    except KeyError:
        raise HTTPException(404, f"doc {doc_id} not found")
    return Response(status_code=204)


@app.post("/library/{doc_id}/restore")
async def library_doc_restore(
    doc_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.library.restore(doc_id)
    except KeyError:
        raise HTTPException(404, f"doc {doc_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url=f"/library#{doc_id}", status_code=303)


@app.post("/library/{doc_id}/purge")
async def library_doc_hard_delete(
    doc_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.library.hard_delete(doc_id)
    except KeyError:
        raise HTTPException(404, f"doc {doc_id} not found")
    return Response(status_code=204)


@app.post("/library/{doc_id}/retry")
async def library_doc_retry(
    doc_id: str, _: str = Depends(require_library_access)
) -> Response:
    """Re-run extraction against the original bytes (e.g. after extractor upgrade)."""
    meta = state.library.get(doc_id)
    if meta is None:
        raise HTTPException(404, f"doc {doc_id} not found")
    original_path = state.library.get_original_path(doc_id)
    if original_path is None:
        raise HTTPException(500, "original file missing")
    body = original_path.read_bytes()

    new_id = library_pipeline.process_doc_creation(
        state.library,
        state.extractor_registry,
        title=meta.title,
        original_bytes=body,
        filename=original_path.name,
        mime=meta.mime,
        source=meta.source,
        tags=meta.tags,
        supersedes=doc_id,
    )
    new_meta = state.library.get(new_id)
    if new_meta is not None:
        state.library.supersede(doc_id, new_meta)
    return RedirectResponse(url=f"/library#{new_id}", status_code=303)


@app.post("/library/{doc_id}/date")
async def library_doc_set_date(
    doc_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    value = (form.get("date") or "").strip()
    if value == "" or value == "unknown":
        state.library.update_meta(doc_id, original_date=None, original_date_source="unknown")
    elif value == "use_upload_date":
        meta = state.library.get(doc_id)
        if meta is None:
            raise HTTPException(404, f"doc {doc_id} not found")
        state.library.update_meta(
            doc_id,
            original_date=meta.created_at.date(),
            original_date_source="user_supplied",
        )
    else:
        try:
            d = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"date must be ISO YYYY-MM-DD, got {value!r}")
        state.library.update_meta(doc_id, original_date=d, original_date_source="user_supplied")
    return RedirectResponse(url=f"/library#{doc_id}", status_code=303)


# ---------------------------------------------------------------------------
# /people/* — sibling people store (commit F of Step 3)
# ---------------------------------------------------------------------------


_VALID_CATEGORIES: tuple[str, ...] = (
    "co-parent",
    "family",
    "partner",
    "friend",
    "professional",
    "child",
    "colleague",
    "other",
)


def _split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _split_lines(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").splitlines() if x.strip()]


@app.get("/people", response_class=HTMLResponse)
async def people_index(
    request: Request, _: str = Depends(require_library_access)
) -> HTMLResponse:
    active = state.people.list_active()
    archived = state.people.list_archived()
    return state.templates.TemplateResponse(
        request,
        "people.html",
        {
            "active": active,
            "archived": archived,
            "categories": _VALID_CATEGORIES,
        },
    )


@app.post("/people/new")
async def people_new(
    request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    category = (form.get("category") or "other").strip()
    if category not in _VALID_CATEGORIES:
        raise HTTPException(400, f"invalid category {category!r}")
    person_id = state.people.add(
        name=name,
        category=category,  # type: ignore[arg-type]
        relationship=(form.get("relationship") or "").strip(),
        summary=(form.get("summary") or "").strip(),
        important_context=_split_lines(form.get("important_context")),
        tags=_split_csv(form.get("tags")),
        aliases=_split_csv(form.get("aliases")),
        notes=(form.get("notes") or "").strip(),
    )
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.get("/people/{person_id}", response_class=HTMLResponse)
async def people_detail(
    person_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    meta = state.people.get(person_id)
    if meta is None:
        raise HTTPException(404, f"person {person_id} not found")
    # Direct browser nav (no HX-Request header) — bounce to the row anchor on
    # the index. The fragment template renders unstyled outside its <details>
    # wrapper.
    if not _is_async_request(request):
        return RedirectResponse(url=f"/people#{person_id}", status_code=303)
    notes = state.people.get_notes(person_id) or ""
    return state.templates.TemplateResponse(
        request,
        "fragments/person_detail.html",
        {"meta": meta, "notes": notes, "categories": _VALID_CATEGORIES},
    )


@app.post("/people/{person_id}")
async def people_update(
    person_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    fields: dict[str, Any] = {}
    if "name" in form:
        name = (form.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        fields["name"] = name
    if "aliases" in form:
        fields["aliases"] = _split_csv(form.get("aliases"))
    if "category" in form:
        category = (form.get("category") or "").strip()
        if category not in _VALID_CATEGORIES:
            raise HTTPException(400, f"invalid category {category!r}")
        fields["category"] = category
    if "relationship" in form:
        fields["relationship"] = (form.get("relationship") or "").strip()
    if "summary" in form:
        fields["summary"] = (form.get("summary") or "").strip()
    if "important_context" in form:
        fields["important_context"] = _split_lines(form.get("important_context"))
    if "tags" in form:
        fields["tags"] = _split_csv(form.get("tags"))

    try:
        state.people.update(person_id, **fields)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.post("/people/{person_id}/notes")
async def people_replace_notes(
    person_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    content = form.get("notes", "")
    try:
        state.people.replace_notes(person_id, content)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.post("/people/{person_id}/link")
async def people_link_doc(
    person_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    doc_id = (form.get("doc_id") or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id required")
    if state.library.get(doc_id) is None:
        raise HTTPException(404, f"doc {doc_id} not found")
    try:
        state.people.link_doc(person_id, doc_id)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.post("/people/{person_id}/unlink")
async def people_unlink_doc(
    person_id: str, request: Request, _: str = Depends(require_library_access)
) -> Response:
    form = await request.form()
    doc_id = (form.get("doc_id") or "").strip()
    try:
        state.people.unlink_doc(person_id, doc_id)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.post("/people/{person_id}/archive")
async def people_archive(
    person_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.people.archive(person_id)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return Response(status_code=204)


@app.post("/people/{person_id}/restore")
async def people_restore(
    person_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.people.restore(person_id)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url=f"/people#{person_id}", status_code=303)


@app.post("/people/{person_id}/delete")
async def people_delete(
    person_id: str, _: str = Depends(require_library_access)
) -> Response:
    try:
        state.people.delete(person_id)
    except KeyError:
        raise HTTPException(404, f"person {person_id} not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Re-export Usage for tests/legacy callers
# ---------------------------------------------------------------------------

__all__ = ["app", "state", "AppState", "Usage"]
