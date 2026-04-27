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
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
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
from app import config as config_module
from app import google_auth, library_pipeline, safety
from app import summariser as summariser_mod
from app import tool_loop as tool_loop_mod
from app.claude import SONNET, ClaudeClient, Usage
from app.context import TZ as BRISBANE_TZ
from app.context import ContextLoader
from app.extractors import build_registry, make_vision_callables
from app.google_auth import GoogleAuthConfig
from app.library import Library
from app.library_stream import StatusBus
from app.people import People
from app.storage import (
    InMemorySessionStore,
    Message,
    NFSSessionStore,
    SessionHeader,
    SessionStore,
    new_session_id,
)
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
# App state
# ---------------------------------------------------------------------------


class AppState:
    cfg: config_module.Config
    store: SessionStore
    loader: ContextLoader
    claude: ClaudeClient | None
    templates: Jinja2Templates
    app_root: Path
    rate_limiter: auth_mod.IPRateLimiter
    kid_sessions: dict[str, str]  # cookie token -> kid_session_id (in-memory)
    library: Library
    extractor_registry: Any
    status_bus: StatusBus
    people: People


state = AppState()


def _google_cfg(cfg: config_module.Config) -> GoogleAuthConfig:
    return GoogleAuthConfig(
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        redirect_uri=cfg.google_redirect_uri,
        token_path=cfg.data_root / ".credentials" / "google_oauth_token.json",
    )


def _build_tool_registry(cfg: config_module.Config, session_id: str | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(READ_DOCUMENT_SPEC(state.library, cfg.data_root))
    reg.register(LIST_DOCUMENTS_SPEC(state.library))
    reg.register(SEARCH_DOCUMENTS_SPEC(state.library))
    reg.register(list_people_spec(state.people))
    reg.register(lookup_person_spec(state.people, state.library))
    reg.register(search_people_spec(state.people))

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

    state.store = InMemorySessionStore() if cfg.is_local else NFSSessionStore(cfg.data_root)
    # ContextLoader's people_md_provider is wired below once state.people is
    # constructed; until then it returns "".
    state.loader = ContextLoader(
        cfg.data_root,
        cfg.prompts_dir,
        mode=cfg.mode,
        display_name=cfg.display_name,
        kid_parent_display_name=cfg.kid_parent_display_name,
        people_md_provider=lambda: state.people.render_people_md() if hasattr(state, "people") else "",
    )
    state.claude = None if cfg.is_local else ClaudeClient(api_key=cfg.anthropic_api_key)
    state.rate_limiter = auth_mod.IPRateLimiter()
    state.kid_sessions = {}

    state.library = Library(cfg.data_root / "library")
    if state.claude is not None:
        transcribe, spot_check = make_vision_callables(state.claude, model=SONNET)
    else:
        transcribe, spot_check = None, None
    state.extractor_registry = build_registry(transcribe=transcribe, spot_check=spot_check)
    state.status_bus = StatusBus()
    state.status_bus.attach_loop(asyncio.get_running_loop())
    state.people = People(cfg.data_root / "people")

    # Block 2 renders the library index dynamically — no INDEX.md write
    # at startup needed.

    import time as _time

    state.templates = Jinja2Templates(directory=str(state.app_root / "templates"))
    state.templates.env.globals["asset_version"] = str(int(_time.time()))
    state.templates.env.globals["claudia_mode"] = cfg.mode
    state.templates.env.globals["display_name"] = cfg.display_name
    state.templates.env.globals["parent_display_name"] = cfg.kid_parent_display_name
    state.templates.env.globals["crisis_footer_text"] = safety.CRISIS_FOOTER_TEXT
    state.templates.env.globals["au_hotlines"] = safety.AU_HOTLINES
    state.templates.env.globals["theme"] = "sage"

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

_basic_auth = HTTPBasic(realm="claudia", auto_error=False)
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


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic_auth),
) -> str:
    """
    Auth for the kid-facing surface (/, /session/*, etc.).

    Adult mode: same as before — basic auth.
    Kid mode: cookie-based session. Kid must have logged in via /login.
    """
    cfg = state.cfg
    if cfg.is_local:
        return "liam"

    if cfg.is_kid:
        cookie = request.cookies.get(auth_mod.KID_COOKIE_NAME, "")
        if cookie and cookie in state.kid_sessions:
            return state.kid_sessions[cookie]
        # No valid cookie — redirect to /login. We raise an exception
        # the caller transforms into a redirect.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="kid-not-logged-in",
            headers={"Location": "/login"},
        )

    # Adult mode
    if credentials is None or not _check_basic_credentials(credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="claudia"'},
        )
    return credentials.username


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
async def _kid_login_redirect(request: Request, exc: _HTTPExc) -> Response:
    if (
        exc.status_code == 401
        and exc.detail == "kid-not-logged-in"
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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    cfg = state.cfg
    if not cfg.is_kid:
        # Adult mode: shouldn't be here, just redirect home
        return RedirectResponse(url="/", status_code=303)
    is_first_time = not auth_mod.is_passphrase_set(cfg.data_root)
    return state.templates.TemplateResponse(
        request,
        "kid_login.html",
        {
            "is_first_time": is_first_time,
            "display_name": cfg.display_name,
            "parent_display_name": cfg.kid_parent_display_name,
        },
    )


@app.post("/login")
async def login_submit(request: Request) -> Response:
    cfg = state.cfg
    if not cfg.is_kid:
        return RedirectResponse(url="/", status_code=303)

    ip = _client_ip(request)
    if not state.rate_limiter.check(ip):
        log.warning("kid_auth.rate_limited", ip=ip)
        return Response(
            content="Too many attempts. Try again in a few minutes.",
            status_code=429,
        )

    form = await request.form()
    passphrase = str(form.get("passphrase", ""))
    confirm = str(form.get("confirm", ""))

    is_first_time = not auth_mod.is_passphrase_set(cfg.data_root)

    if is_first_time:
        # Setup flow: passphrase + confirm
        if passphrase != confirm:
            return state.templates.TemplateResponse(
                request,
                "kid_login.html",
                {
                    "is_first_time": True,
                    "display_name": cfg.display_name,
                    "parent_display_name": cfg.kid_parent_display_name,
                    "error": "The two passwords didn't match.",
                },
                status_code=400,
            )
        try:
            auth_mod.set_passphrase(cfg.data_root, passphrase)
        except ValueError as e:
            return state.templates.TemplateResponse(
                request,
                "kid_login.html",
                {
                    "is_first_time": True,
                    "display_name": cfg.display_name,
                    "parent_display_name": cfg.kid_parent_display_name,
                    "error": str(e),
                },
                status_code=400,
            )
        # First passphrase set. v1 dev mode: no encryption, no break-glass
        # envelope. Continue to login below to set the cookie.
        log.info("kid_auth.passphrase_set")

    # Login flow: verify
    if not auth_mod.verify_passphrase(cfg.data_root, passphrase):
        state.rate_limiter.record(ip)
        log.warning("kid_auth.verify_failed", ip=ip)
        return state.templates.TemplateResponse(
            request,
            "kid_login.html",
            {
                "is_first_time": False,
                "display_name": cfg.display_name,
                "parent_display_name": cfg.kid_parent_display_name,
                "error": "That password didn't match. Try again.",
            },
            status_code=401,
        )

    state.rate_limiter.reset(ip)
    token = auth_mod.new_kid_session_token()
    state.kid_sessions[token] = cfg.display_name or "kid"
    log.info("kid_auth.login_success", display_name=cfg.display_name)

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=auth_mod.KID_COOKIE_NAME,
        value=token,
        max_age=auth_mod.KID_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/logout")
async def logout(request: Request) -> Response:
    cfg = state.cfg
    if cfg.is_kid:
        cookie = request.cookies.get(auth_mod.KID_COOKIE_NAME, "")
        if cookie:
            state.kid_sessions.pop(cookie, None)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth_mod.KID_COOKIE_NAME, path="/")
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
            "parent_display_name": cfg.kid_parent_display_name,
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
            "parent_display_name": cfg.kid_parent_display_name,
            "summary": None,
            "themes": [],
            "flag": "none",
            "people_added": [],
        },
    )


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
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
    p = data_root / "context" / "mood-log.jsonl"
    out: dict[str, int] = {}
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = rec.get("session")
            score = rec.get("regulation_score")
            if isinstance(sid, str) and isinstance(score, int):
                out[sid] = score
    except OSError:
        pass
    return out


def _mood_sparkline(data_root: Path) -> str | None:
    p = data_root / "context" / "mood-log.jsonl"
    if not p.exists():
        return None
    scores: list[int] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = r.get("regulation_score")
            if isinstance(s, int) and 1 <= s <= 10:
                scores.append(s)
    except OSError:
        return None
    if not scores:
        return None
    scores = scores[-20:]
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[min(7, max(0, (s - 1) * 7 // 9))] for s in scores) + f"  ({scores[-1]}/10)"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.get("/session/new")
async def session_new(
    background_tasks: BackgroundTasks, _: str = Depends(require_auth)
) -> RedirectResponse:
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
        background_tasks.add_task(_seed_opener_safe, session_id, SONNET, blocks)

    return RedirectResponse(f"/session/{session_id}", status_code=303)


def _seed_opener_safe(session_id: str, model: str, blocks) -> None:
    try:
        _seed_opener(session_id, model, blocks)
    except Exception as e:  # noqa: BLE001
        log.warning("session.opener_failed", session_id=session_id, error=str(e))
        try:
            state.store.append_event(session_id, "opener_failed", {"error": str(e)})
        except Exception:  # noqa: BLE001
            pass


def _seed_opener(session_id: str, model: str, blocks) -> None:
    assert state.claude is not None
    synthetic_user = "Begin the session."
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
        claude=state.claude,
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
    background_tasks: BackgroundTasks,
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
        assert state.claude is not None
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
            state.claude,
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


@app.post("/session/{session_id}/end")
async def session_end(
    session_id: str,
    background_tasks: BackgroundTasks,
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
    background_tasks.add_task(_run_audit_and_apply, session_id)
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
    p = data_root / "context" / "mood-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": ts or datetime.now(UTC).isoformat(),
        "session": session_id,
        "regulation_score": regulation_score,
    }
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Auditor (background)
# ---------------------------------------------------------------------------


def _event_present(session_id: str, event_type: str) -> bool:
    if isinstance(state.store, NFSSessionStore):
        path = state.store.sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "event" and rec.get("event_type") == event_type:
                return True
        return False
    if hasattr(state.store, "_events"):
        for kind, _payload in state.store._events.get(session_id, []):  # type: ignore[attr-defined]
            if kind == event_type:
                return True
    return False


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
            assert state.claude is not None
            report = summariser_mod.run_auditor(state.claude, state.cfg.prompts_dir, header.model or SONNET, inp)
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

    state.store.append_event(session_id, "audit_applied", {})


# ---------------------------------------------------------------------------
# Report (therapist handover)
# ---------------------------------------------------------------------------


@app.get("/report", response_class=HTMLResponse)
async def report_form(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
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
        assert state.claude is not None
        markdown = await asyncio.to_thread(
            _run_handover_call,
            state.claude,
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
    claude: ClaudeClient,
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


@app.get("/connect-gmail", response_class=HTMLResponse)
async def connect_gmail(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    gcfg = _google_cfg(state.cfg)
    if not gcfg.is_complete():
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "config_missing"}, status_code=500
        )
    stat = google_auth.status(gcfg)
    if stat["state"] == "connected":
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "connected", "scopes": stat.get("scopes", [])}
        )
    auth_url, _s = google_auth.begin_flow(gcfg)
    return state.templates.TemplateResponse(
        request, "connect_gmail.html", {"variant": "connect", "auth_url": auth_url}
    )


@app.get("/connect-gmail/disconnect")
async def connect_gmail_disconnect(_: str = Depends(require_auth)) -> RedirectResponse:
    google_auth.revoke(_google_cfg(state.cfg))
    return RedirectResponse("/", status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
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
    gcfg = _google_cfg(state.cfg)
    try:
        google_auth.exchange_code(gcfg, code, returned_state)
    except Exception as e:  # noqa: BLE001
        log.exception("oauth.exchange_failed")
        return state.templates.TemplateResponse(
            request, "connect_gmail.html", {"variant": "callback_error", "error": str(e)}, status_code=500
        )
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
    credentials: HTTPBasicCredentials | None = Depends(_basic_auth),
    admin_credentials: HTTPBasicCredentials | None = Depends(_admin_basic_auth),
) -> str:
    """
    Library access shape:
      - local mode: pass.
      - kid mode: parent-admin only (kid doesn't manage library).
      - adult mode: regular adult auth.
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
    if credentials is None or not _check_basic_credentials(credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="claudia"'},
        )
    return credentials.username


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
) -> HTMLResponse:
    meta = state.library.get(doc_id)
    if meta is None:
        raise HTTPException(404, f"doc {doc_id} not found")
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
) -> HTMLResponse:
    meta = state.people.get(person_id)
    if meta is None:
        raise HTTPException(404, f"person {person_id} not found")
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
