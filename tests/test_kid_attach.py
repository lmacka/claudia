"""Kid-mode OCR-discard attachment endpoint."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


def _fresh_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "kid"):
    """Force-reload the FastAPI app so global state picks up env changes."""
    import importlib
    import sys

    monkeypatch.setenv("CLAUDIA_OPS_MODE", "local")
    monkeypatch.setenv("CLAUDIA_MODE", mode)
    monkeypatch.setenv("CLAUDIA_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDIA_DISPLAY_NAME", "Jasper")
    monkeypatch.setenv("CLAUDIA_KID_PARENT_DISPLAY_NAME", "Liam")
    monkeypatch.setenv(
        "CLAUDIA_PROMPTS_DIR",
        str(Path(__file__).resolve().parents[1] / "app" / "prompts"),
    )
    ctx = tmp_path / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "05_current_state.md").write_text("# current_state stub\n", encoding="utf-8")
    from app.db_kv import kv_set as _kv_set
    _kv_set(tmp_path, "setup_completed_at", "test fixture")

    if "app.main" in sys.modules:
        importlib.reload(sys.modules["app.main"])
    import app.main as main_module

    return main_module


@pytest.fixture
def kid_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    main_module = _fresh_app(tmp_path, monkeypatch, mode="kid")
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def adult_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    main_module = _fresh_app(tmp_path, monkeypatch, mode="adult")
    with TestClient(main_module.app) as c:
        yield c


def _png_bytes(text: str = "stub") -> bytes:
    img = Image.new("RGB", (32, 24), color=(60, 80, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _create_session(client: TestClient) -> str:
    r = client.get("/session/new", follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/", 2)[2]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_kid_attach_appends_user_ocr_and_assistant_messages(
    kid_client: TestClient, tmp_path: Path
) -> None:
    sid = _create_session(kid_client)

    files = {"file": ("snap.png", _png_bytes(), "image/png")}
    data = {"content": "what does this mean?"}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.text

    # Fragment should contain the three blocks.
    assert "snap.png" in body
    assert "what I read from the screenshot" in body
    assert "what does this mean?" in body
    # Local mock OCR output.
    assert "[local-mock OCR of snap.png]" in body
    # Assistant reply (mock).
    assert "[local mock reply]" in body

    # Verify staging file is gone.
    staging = tmp_path / "kid-attach-staging"
    if staging.exists():
        assert list(staging.iterdir()) == [], "staging file should have been deleted"


def test_kid_attach_persists_user_and_system_event_messages(
    kid_client: TestClient,
) -> None:
    import app.main as main_module

    sid = _create_session(kid_client)
    files = {"file": ("photo.jpg", _png_bytes(), "image/jpeg")}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": "hi"})
    assert r.status_code == 200

    msgs = main_module.state.store.load_messages(sid)
    roles = [m.role for m in msgs]
    # Expect at minimum: user (attachment), system_event (OCR), assistant (reply).
    assert "user" in roles
    assert "system_event" in roles
    assert "assistant" in roles

    user_msg = next(m for m in msgs if m.role == "user" and "[attached:" in m.content)
    assert "photo.jpg" in user_msg.content
    assert user_msg.meta.get("kid_attach") is True
    assert user_msg.meta.get("filename") == "photo.jpg"

    sys_msg = next(m for m in msgs if m.role == "system_event")
    assert sys_msg.meta.get("kind") == "kid_attach_ocr"
    assert sys_msg.meta.get("filename") == "photo.jpg"
    assert "local-mock OCR" in sys_msg.content


def test_kid_attach_records_event(kid_client: TestClient) -> None:
    import app.main as main_module

    sid = _create_session(kid_client)
    files = {"file": ("ev.png", _png_bytes(), "image/png")}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": ""})
    assert r.status_code == 200

    assert main_module.state.store.has_event(sid, "kid_attach")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_kid_attach_rejects_non_image_extension(kid_client: TestClient) -> None:
    sid = _create_session(kid_client)
    files = {"file": ("doc.pdf", b"%PDF-1.4 stub", "application/pdf")}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": "x"})
    assert r.status_code == 415


def test_kid_attach_rejects_oversize(kid_client: TestClient) -> None:
    sid = _create_session(kid_client)
    big = b"\x00" * (10 * 1024 * 1024 + 1)  # 10 MB + 1
    files = {"file": ("big.png", big, "image/png")}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": ""})
    assert r.status_code == 413


def test_kid_attach_rejects_empty(kid_client: TestClient) -> None:
    sid = _create_session(kid_client)
    files = {"file": ("empty.png", b"", "image/png")}
    r = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": ""})
    assert r.status_code == 400


def test_kid_attach_rejects_missing_file_field(kid_client: TestClient) -> None:
    sid = _create_session(kid_client)
    r = kid_client.post(f"/session/{sid}/kid-attach", data={"content": "hi"})
    assert r.status_code == 400


def test_kid_attach_404_for_unknown_session(kid_client: TestClient) -> None:
    files = {"file": ("snap.png", _png_bytes(), "image/png")}
    r = kid_client.post("/session/does-not-exist/kid-attach", files=files, data={"content": ""})
    assert r.status_code == 404


def test_kid_attach_410_for_ended_session(kid_client: TestClient) -> None:
    sid = _create_session(kid_client)
    r = kid_client.post(f"/session/{sid}/end", follow_redirects=False)
    assert r.status_code in (303, 200)

    files = {"file": ("snap.png", _png_bytes(), "image/png")}
    r2 = kid_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": ""})
    assert r2.status_code == 410


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def test_kid_attach_404_in_adult_mode(adult_client: TestClient) -> None:
    sid = _create_session(adult_client)
    files = {"file": ("snap.png", _png_bytes(), "image/png")}
    r = adult_client.post(f"/session/{sid}/kid-attach", files=files, data={"content": "x"})
    assert r.status_code == 404
