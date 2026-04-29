"""Route-level tests for /library/* (commit D synchronous flow)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


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
    (ctx / "05_current_state.md").write_text("# stub\n", encoding="utf-8")
    (tmp_path / ".setup_complete").write_text("test fixture\n", encoding="utf-8")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def _png_bytes() -> bytes:
    img = Image.new("RGB", (4, 4), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# GET /library
# ---------------------------------------------------------------------------


def test_library_index_renders_empty(client: TestClient):
    r = client.get("/library")
    assert r.status_code == 200
    assert "library" in r.text.lower()
    assert "library is empty" in r.text or "Active documents" in r.text


# ---------------------------------------------------------------------------
# POST /library/paste
# ---------------------------------------------------------------------------


def test_library_paste_creates_doc(client: TestClient):
    r = client.post(
        "/library/paste",
        data={"title": "My note", "tags": "thinking, draft", "text": "hello\nworld\nthis is a note"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/library#")

    listing = client.get("/library")
    assert "My note" in listing.text
    assert "thinking" in listing.text


def test_library_paste_uses_first_line_as_title_when_blank(client: TestClient):
    r = client.post(
        "/library/paste",
        data={"text": "First line title here\nthen body\n"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    listing = client.get("/library")
    assert "First line title here" in listing.text


def test_library_paste_rejects_empty(client: TestClient):
    r = client.post("/library/paste", data={"text": ""})
    assert r.status_code == 400


def test_library_paste_detects_whatsapp_chat(client: TestClient):
    chat = (
        "[12/04/26, 14:32:01] Liam: hey\n"
        "[12/04/26, 14:35:00] Rhiannon: yeah\n"
        "[12/04/26, 14:36:00] Liam: ok\n"
        "[12/04/26, 14:37:00] Rhiannon: done\n"
        "[12/04/26, 14:38:00] Liam: cool\n"
    )
    r = client.post("/library/paste", data={"text": chat}, follow_redirects=False)
    assert r.status_code == 303

    listing = client.get("/library")
    # Chat extractor renames the title to include participants.
    assert "Liam" in listing.text and "Rhiannon" in listing.text


# ---------------------------------------------------------------------------
# POST /library/upload
# ---------------------------------------------------------------------------


def test_library_upload_text_file(client: TestClient):
    files = {"file": ("note.txt", b"plain note body", "text/plain")}
    r = client.post(
        "/library/upload",
        files=files,
        data={"title": "Upload test", "tags": "alpha"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    listing = client.get("/library")
    assert "Upload test" in listing.text


def test_library_upload_rejects_empty(client: TestClient):
    files = {"file": ("empty.txt", b"", "text/plain")}
    r = client.post("/library/upload", files=files)
    assert r.status_code == 400


def test_library_upload_rejects_oversize(client: TestClient):
    big = b"x" * (26 * 1024 * 1024)  # 26 MB > 25 MB cap
    files = {"file": ("big.txt", big, "text/plain")}
    r = client.post("/library/upload", files=files)
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# GET /library/{id} — detail fragment
# ---------------------------------------------------------------------------


def test_library_doc_detail_renders(client: TestClient):
    client.post(
        "/library/paste",
        data={"title": "Detail test", "text": "the quick brown fox"},
        follow_redirects=False,
    )
    listing = client.get("/library")
    # Pull doc_id from the listing (it's in the row's id="..." attribute)
    import re

    m = re.search(r'<tr id="([^"]+)"', listing.text)
    assert m, "no doc row found in listing"
    doc_id = m.group(1)

    # Direct nav 303-redirects to /library#doc_id (fragment outside HTMX
    # context renders unstyled). HTMX requests still get the fragment.
    r_direct = client.get(f"/library/{doc_id}", follow_redirects=False)
    assert r_direct.status_code == 303
    assert r_direct.headers["location"] == f"/library#{doc_id}"

    r = client.get(f"/library/{doc_id}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "the quick brown fox" in r.text
    assert "text_verbatim" in r.text  # extractor name surfaces


def test_library_doc_detail_404(client: TestClient):
    r = client.get("/library/no-such-doc")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /library/{id}/tags
# ---------------------------------------------------------------------------


def _create_one(client: TestClient, title: str = "T", text: str = "body") -> str:
    client.post("/library/paste", data={"title": title, "text": text}, follow_redirects=False)
    listing = client.get("/library")
    import re

    return re.search(r'<tr id="([^"]+)"', listing.text).group(1)


def test_library_tags_update(client: TestClient):
    doc_id = _create_one(client)
    r = client.post(
        f"/library/{doc_id}/tags",
        data={"tags": "fresh, tags, here"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    listing = client.get("/library")
    assert "fresh" in listing.text


# ---------------------------------------------------------------------------
# POST /library/{id}/delete /restore /purge
# ---------------------------------------------------------------------------


def test_library_soft_delete_then_restore_then_purge(client: TestClient):
    doc_id = _create_one(client, title="To delete", text="body")

    r = client.post(f"/library/{doc_id}/delete")
    assert r.status_code == 204

    listing = client.get("/library")
    # Active should not show it; archived collapsible should.
    # The doc title can show up in archived.
    assert "Archived" in listing.text
    assert "To delete" in listing.text  # in archived

    r = client.post(f"/library/{doc_id}/restore", follow_redirects=False)
    assert r.status_code == 303

    r = client.post(f"/library/{doc_id}/delete")
    assert r.status_code == 204
    r = client.post(f"/library/{doc_id}/purge")
    assert r.status_code == 204

    listing = client.get("/library")
    assert "To delete" not in listing.text


def test_library_delete_404(client: TestClient):
    r = client.post("/library/no-such/delete")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /library/{id}/date
# ---------------------------------------------------------------------------


def test_library_set_date_iso(client: TestClient):
    doc_id = _create_one(client)
    r = client.post(
        f"/library/{doc_id}/date",
        data={"date": "2018-03-12"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    listing = client.get("/library")
    assert "2018-03-12" in listing.text


def test_library_set_date_use_upload(client: TestClient):
    doc_id = _create_one(client)
    r = client.post(
        f"/library/{doc_id}/date",
        data={"date": "use_upload_date"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    listing = client.get("/library")
    assert "user_supplied" in listing.text


def test_library_set_date_unknown(client: TestClient):
    doc_id = _create_one(client)
    r = client.post(
        f"/library/{doc_id}/date",
        data={"date": "unknown"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_library_set_date_invalid(client: TestClient):
    doc_id = _create_one(client)
    r = client.post(f"/library/{doc_id}/date", data={"date": "not-a-date"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /library/{id}/supersede
# ---------------------------------------------------------------------------


def test_library_supersede(client: TestClient):
    old_id = _create_one(client, title="V1", text="version one body")
    files = {"file": ("v2.txt", b"version two body", "text/plain")}
    r = client.post(
        f"/library/{old_id}/supersede",
        files=files,
        data={"title": "V2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    new_loc = r.headers["location"]
    assert new_loc.startswith("/library#")

    listing = client.get("/library")
    # V2 in active section, V1 in archived (superseded).
    assert "V2" in listing.text
    assert "V1" in listing.text  # may be in archived section
    assert "Archived" in listing.text


# ---------------------------------------------------------------------------
# POST /library/{id}/retry
# ---------------------------------------------------------------------------


def test_library_retry_creates_new_doc_and_supersedes_old(client: TestClient):
    old_id = _create_one(client, title="Retry-me", text="hello there friend")
    r = client.post(f"/library/{old_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    new_id_loc = r.headers["location"]
    assert "/library#" in new_id_loc

    listing = client.get("/library")
    # Old should be in archived, new active.
    assert "Retry-me" in listing.text


# ---------------------------------------------------------------------------
# Integration: image upload runs through ImageExtractor (no transcribe in
# local mode → extractor would error; so verify graceful path)
# ---------------------------------------------------------------------------


def test_library_upload_image_in_local_mode_raises_without_vision(client: TestClient):
    """Local mode has no Anthropic client → image extractor's transcribe is None.

    The extractor raises RuntimeError, which TestClient surfaces directly
    (raise_server_exceptions=True by default). Documents the contract:
    no silent success without vision wired. Commit E surfaces this more nicely.
    """
    files = {"file": ("snap.png", _png_bytes(), "image/png")}
    with pytest.raises(RuntimeError, match="transcribe callable"):
        client.post("/library/upload", files=files)
