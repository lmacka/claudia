"""Route-level tests for the simplified app."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    ctx = tmp_path / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "05_current_state.md").write_text("# current_state stub\n", encoding="utf-8")
    # Mark setup complete so / doesn't redirect to the wizard.
    (tmp_path / ".setup_complete").write_text("test fixture\n", encoding="utf-8")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def _create_session(client: TestClient) -> str:
    """GET /session/new creates and 303-redirects to /session/<id>."""
    r = client.get("/session/new", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith("/session/")
    return loc.split("/", 2)[2]


def _has_event(client: TestClient, session_id: str, event_type: str) -> bool:
    import app.main as main_module

    return main_module.state.store.has_event(session_id, event_type)


def _wait_for_event(client: TestClient, session_id: str, event: str, timeout: float = 2.0) -> None:
    """Pump the loop with cheap requests until `event` appears (or timeout).

    Background tasks now run via asyncio.create_task instead of FastAPI
    BackgroundTasks (qa-protocol bug 3 fix), so TestClient does not implicitly
    wait for them. Each client request enters the portal and yields to the loop
    long enough for the audit to make progress.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _has_event(client, session_id, event):
            return
        client.get("/healthz")
        _time.sleep(0.02)
    raise AssertionError(f"event {event!r} never appeared within {timeout}s")


# ---------------------------------------------------------------------------
# Session create / view
# ---------------------------------------------------------------------------


def test_home_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "claudia" in r.text
    assert "New session" in r.text


def test_session_new_creates_and_redirects(client: TestClient) -> None:
    sid = _create_session(client)
    assert sid.endswith("session_" + sid.split("session_", 1)[1])  # mode tag is "session"


def test_session_new_blocks_concurrent(client: TestClient) -> None:
    sid = _create_session(client)
    r = client.get("/session/new", follow_redirects=False)
    # Blocked — redirected home rather than creating a second session.
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # Original session still exists
    assert client.get(f"/session/{sid}").status_code == 200


def test_session_view_renders_chat(client: TestClient) -> None:
    sid = _create_session(client)
    r = client.get(f"/session/{sid}")
    assert r.status_code == 200
    assert 'id="msg-form"' in r.text
    assert "End" in r.text


# ---------------------------------------------------------------------------
# End + mood
# ---------------------------------------------------------------------------


def test_end_button_redirects_to_chat_view(client: TestClient) -> None:
    sid = _create_session(client)
    r = client.post(f"/session/{sid}/end", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/session/{sid}"


def test_end_schedules_audit_in_background(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/session/{sid}/end", follow_redirects=False)
    _wait_for_event(client, sid, "audit_applied")
    assert _has_event(client, sid, "audit_scheduled")
    assert _has_event(client, sid, "audit_applied")


def test_chat_view_shows_mood_panel_when_ended(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/session/{sid}/end", follow_redirects=False)
    r = client.get(f"/session/{sid}")
    assert r.status_code == 200
    body = r.text
    assert "mood-end-panel" in body
    assert f"/session/{sid}/mood" in body
    assert 'id="content"' not in body


def test_mood_endpoint_records_and_is_idempotent(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/session/{sid}/end", follow_redirects=False)

    r = client.post(f"/session/{sid}/mood", data={"regulation_score": "7"})
    assert r.status_code == 200, r.text
    assert "7/10" in r.text

    # Mood now lives in the SQLite mood_log table (T-NEW-I phase 2).
    import app.main as main_module
    from app.db_audit import mood_by_session

    moods = mood_by_session(main_module.state.cfg.data_root)
    assert moods.get(sid) == 7

    # Idempotency: a second POST is a no-op (the route checks has_event(mood_recorded)).
    r2 = client.post(f"/session/{sid}/mood", data={"regulation_score": "3"})
    assert r2.status_code == 200
    moods2 = mood_by_session(main_module.state.cfg.data_root)
    assert moods2.get(sid) == 7  # still 7, second POST ignored


def test_mood_rejects_invalid_score(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/session/{sid}/end", follow_redirects=False)
    assert client.post(f"/session/{sid}/mood", data={"regulation_score": "11"}).status_code == 400
    assert client.post(f"/session/{sid}/mood", data={"regulation_score": "foo"}).status_code == 400


# ---------------------------------------------------------------------------
# Auditor side-effects
# ---------------------------------------------------------------------------


def test_session_log_written_after_end(client: TestClient) -> None:
    sid = _create_session(client)
    client.post(f"/session/{sid}/end", follow_redirects=False)
    _wait_for_event(client, sid, "audit_applied")
    import app.main as main_module

    logs_dir = main_module.state.cfg.data_root / "session-logs"
    assert logs_dir.exists()
    logs = list(logs_dir.glob("*.md"))
    assert len(logs) == 1


# ---------------------------------------------------------------------------
# Messages poll (lazy opener)
# ---------------------------------------------------------------------------


def test_messages_poll_returns_spinner_when_no_messages(client: TestClient) -> None:
    sid = _create_session(client)
    r = client.get(f"/session/{sid}/messages-poll")
    assert r.status_code == 200
    body = r.text
    assert "writing opening message" in body
    assert 'hx-trigger="load delay:2s, every 2s"' in body


def test_messages_poll_stops_polling_once_message_lands(client: TestClient) -> None:
    sid = _create_session(client)
    import app.main as main_module
    from app.storage import Message

    main_module.state.store.append_message(
        sid, Message(role="assistant", content="Hello there.")
    )
    r = client.get(f"/session/{sid}/messages-poll")
    assert r.status_code == 200
    body = r.text
    assert "Hello there." in body
    assert "writing opening message" not in body
    assert "hx-trigger" not in body


# ---------------------------------------------------------------------------
# Removed routes return 404
# ---------------------------------------------------------------------------


def test_removed_summary_route_404(client: TestClient) -> None:
    sid = _create_session(client)
    r = client.get(f"/session/{sid}/summary")
    assert r.status_code == 404


def test_removed_session_logs_route_404(client: TestClient) -> None:
    assert client.get("/session-logs").status_code == 404
