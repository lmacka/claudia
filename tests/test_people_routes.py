"""Route-level tests for /people/*."""

from __future__ import annotations

import re
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
    (ctx / "05_current_state.md").write_text("# stub\n", encoding="utf-8")
    from app.db_kv import kv_set as _kv_set
    _kv_set(tmp_path, "setup_completed_at", "test fixture")

    import app.main as main_module

    with TestClient(main_module.app) as c:
        yield c


def _row_id(html: str) -> str:
    m = re.search(r'<tr id="([^"]+)"', html)
    assert m, "no person row found"
    return m.group(1)


# ---------------------------------------------------------------------------
# GET /people
# ---------------------------------------------------------------------------


def test_people_index_renders_empty(client: TestClient):
    r = client.get("/people")
    assert r.status_code == 200
    assert "people" in r.text.lower()
    assert "no people yet" in r.text or "Active people" in r.text


# ---------------------------------------------------------------------------
# POST /people/new
# ---------------------------------------------------------------------------


def test_people_new_minimum_fields(client: TestClient):
    r = client.post(
        "/people/new",
        data={"name": "Sofia", "category": "friend"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/people#" in r.headers["location"]

    listing = client.get("/people")
    assert "Sofia" in listing.text


def test_people_new_full_fields(client: TestClient):
    r = client.post(
        "/people/new",
        data={
            "name": "Rhiannon O'Hara",
            "aliases": "Rhi, Bri",
            "category": "co-parent",
            "relationship": "Ex-partner, mother of Jasper",
            "summary": "Co-parents Jasper. Lives in Dayboro.",
            "important_context": "School flashpoint\nText misfires",
            "tags": "co-parent, high-conflict-history",
            "notes": "Detailed notes here",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    listing = client.get("/people")
    assert "Rhiannon" in listing.text
    assert "Rhi" in listing.text


def test_people_new_rejects_blank_name(client: TestClient):
    r = client.post("/people/new", data={"name": "  "})
    assert r.status_code == 400


def test_people_new_rejects_invalid_category(client: TestClient):
    r = client.post("/people/new", data={"name": "X", "category": "alien"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /people/{id}
# ---------------------------------------------------------------------------


_HX = {"HX-Request": "true"}


def test_people_detail_renders(client: TestClient):
    client.post("/people/new", data={"name": "Sofia"}, follow_redirects=False)
    listing = client.get("/people")
    pid = _row_id(listing.text)

    r = client.get(f"/people/{pid}", headers=_HX)
    assert r.status_code == 200
    assert "Sofia" in r.text
    # Detail fragment has form fields
    assert 'name="aliases"' in r.text


def test_people_detail_redirects_on_direct_nav(client: TestClient):
    client.post("/people/new", data={"name": "Sofia"}, follow_redirects=False)
    listing = client.get("/people")
    pid = _row_id(listing.text)
    # No HX-Request header → bounce to the index anchor.
    r = client.get(f"/people/{pid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/people#{pid}"


def test_people_detail_404(client: TestClient):
    r = client.get("/people/no-such", headers=_HX)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /people/{id} (update meta)
# ---------------------------------------------------------------------------


def _make_one(client: TestClient, name: str = "X") -> str:
    client.post("/people/new", data={"name": name}, follow_redirects=False)
    return _row_id(client.get("/people").text)


def test_people_update_meta(client: TestClient):
    pid = _make_one(client, name="Sofia")
    r = client.post(
        f"/people/{pid}",
        data={"summary": "english class friend", "tags": "friend, school"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    detail = client.get(f"/people/{pid}", headers=_HX)
    assert "english class friend" in detail.text
    assert "friend, school" in detail.text


def test_people_update_404(client: TestClient):
    r = client.post("/people/no-such", data={"summary": "x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /people/{id}/notes
# ---------------------------------------------------------------------------


def test_people_replace_notes(client: TestClient):
    pid = _make_one(client)
    r = client.post(
        f"/people/{pid}/notes",
        data={"notes": "fresh notes content"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    detail = client.get(f"/people/{pid}", headers=_HX)
    assert "fresh notes content" in detail.text


# ---------------------------------------------------------------------------
# /link + /unlink
# ---------------------------------------------------------------------------


def test_people_link_doc_404_when_doc_missing(client: TestClient):
    pid = _make_one(client)
    r = client.post(f"/people/{pid}/link", data={"doc_id": "no-such-doc"})
    assert r.status_code == 404


def test_people_link_doc_then_unlink(client: TestClient):
    pid = _make_one(client)
    # Create a real library doc to link.
    client.post(
        "/library/paste",
        data={"title": "Linkable doc", "text": "body"},
        follow_redirects=False,
    )
    listing_lib = client.get("/library")
    doc_match = re.search(r'<tr id="([^"]+)"', listing_lib.text)
    assert doc_match
    doc_id = doc_match.group(1)

    r = client.post(f"/people/{pid}/link", data={"doc_id": doc_id}, follow_redirects=False)
    assert r.status_code == 303

    detail = client.get(f"/people/{pid}", headers=_HX)
    assert doc_id in detail.text

    r = client.post(f"/people/{pid}/unlink", data={"doc_id": doc_id}, follow_redirects=False)
    assert r.status_code == 303
    detail = client.get(f"/people/{pid}", headers=_HX)
    assert doc_id not in detail.text


# ---------------------------------------------------------------------------
# archive + restore + delete
# ---------------------------------------------------------------------------


def test_people_archive_then_restore_then_delete(client: TestClient):
    pid = _make_one(client, name="To archive")
    r = client.post(f"/people/{pid}/archive")
    assert r.status_code == 204
    listing = client.get("/people")
    # Should be in Archived section now
    assert "Archived" in listing.text
    assert "To archive" in listing.text  # in archived

    r = client.post(f"/people/{pid}/restore", follow_redirects=False)
    assert r.status_code == 303

    r = client.post(f"/people/{pid}/archive")
    assert r.status_code == 204
    r = client.post(f"/people/{pid}/delete")
    assert r.status_code == 204

    listing = client.get("/people")
    assert "To archive" not in listing.text


def test_people_archive_404(client: TestClient):
    r = client.post("/people/no-such/archive")
    assert r.status_code == 404


def test_people_restore_rejects_active(client: TestClient):
    pid = _make_one(client)
    r = client.post(f"/people/{pid}/restore")
    assert r.status_code == 400  # not archived
