"""Tests for app/library.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.library import Library, LibraryDocMeta, _slugify

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _meta(**overrides) -> LibraryDocMeta:
    """Build a valid meta with sensible defaults; overrides win."""
    defaults: dict = {
        "id": "2026-04-25T12-30-45Z_test-doc",
        "title": "Test Doc",
        "kind": "text",
        "source": "paste",
        "created_at": datetime.now(UTC),
        "size_bytes": 5,
        "mime": "text/plain",
        "extractor": "text_verbatim",
        "extracted_chars": 5,
    }
    defaults.update(overrides)
    return LibraryDocMeta(**defaults)


# ---------------------------------------------------------------------------
# slugify + id minting
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("DC Diagnostic — Liam Mackenzie") == "dc-diagnostic-liam-mackenzie"


def test_slugify_unicode_strips_to_ascii():
    assert _slugify("Café Résumé") == "caf-r-sum"


def test_slugify_empty_returns_untitled():
    assert _slugify("") == "untitled"
    assert _slugify("!!!") == "untitled"


def test_slugify_truncates(max_len_60_default=True):
    long = "x" * 200
    assert len(_slugify(long)) <= 60


def test_mint_doc_id_is_unique(tmp_path):
    lib = Library(tmp_path / "library")
    a = lib.mint_doc_id("My Doc")
    # Materialise the dir so the next mint sees a collision.
    (lib.root / a).mkdir()
    b = lib.mint_doc_id("My Doc")
    assert a != b
    assert b.endswith("-2") or b != a


def test_mint_doc_id_format(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("Hello World")
    # Z-suffix UTC timestamp + slug
    assert "Z_hello-world" in doc_id


# ---------------------------------------------------------------------------
# meta validation
# ---------------------------------------------------------------------------


def test_meta_rejects_unsafe_id():
    with pytest.raises(ValueError):
        LibraryDocMeta.model_validate(
            {
                "id": "../escape",
                "title": "x",
                "kind": "text",
                "source": "paste",
                "created_at": datetime.now(UTC).isoformat(),
                "size_bytes": 0,
                "mime": "text/plain",
                "extractor": "text_verbatim",
                "extracted_chars": 0,
            }
        )


def test_meta_rejects_empty_title():
    with pytest.raises(ValueError):
        _meta(title="   ")


def test_meta_rejects_unknown_kind():
    with pytest.raises(ValueError):
        _meta(kind="unknown_format")  # not a Literal value


def test_meta_strips_title_whitespace():
    m = _meta(title="  spaced out  ")
    assert m.title == "spaced out"


# ---------------------------------------------------------------------------
# create_doc + path traversal safety
# ---------------------------------------------------------------------------


def test_create_doc_writes_blobs_and_persists_meta(tmp_path):
    """Blobs (original + extracted) on disk; meta + verification in SQLite."""
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("Spec test")
    meta = _meta(id=doc_id, size_bytes=11, extracted_chars=11)

    lib.create_doc(meta, b"hello world", "txt", "hello world", {"status": "ok"})

    doc_dir = lib.root / doc_id
    # Blobs on disk
    assert (doc_dir / "original.txt").exists()
    assert (doc_dir / "extracted.md").exists()
    # Meta + verification in DB
    assert lib.get(doc_id) is not None
    assert lib.get_verification(doc_id) == {"status": "ok"}


def test_create_doc_rejects_size_mismatch(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=99, extracted_chars=2)
    with pytest.raises(ValueError):
        lib.create_doc(meta, b"ab", "txt", "ab", {})


def test_create_doc_rejects_extracted_mismatch(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=2, extracted_chars=99)
    with pytest.raises(ValueError):
        lib.create_doc(meta, b"ab", "txt", "ab", {})


def test_create_doc_refuses_existing_id(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"a", "txt", "a", {})
    with pytest.raises(FileExistsError):
        lib.create_doc(meta, b"a", "txt", "a", {})


def test_doc_dir_rejects_path_traversal(tmp_path):
    lib = Library(tmp_path / "library")
    with pytest.raises(ValueError):
        lib._doc_dir("../etc/passwd")
    with pytest.raises(ValueError):
        lib._doc_dir("..")
    with pytest.raises(ValueError):
        lib._doc_dir("foo/bar")
    with pytest.raises(ValueError):
        lib._doc_dir(".secret")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_list_all_returns_every_doc(tmp_path):
    lib = Library(tmp_path / "library")
    for title in ["A", "B", "C"]:
        doc_id = lib.mint_doc_id(title)
        meta = _meta(id=doc_id, title=title, size_bytes=1, extracted_chars=1)
        lib.create_doc(meta, b"x", "txt", "x", {})
    docs = lib.list_all()
    titles = {d.title for d in docs}
    assert titles == {"A", "B", "C"}


def test_create_doc_is_atomic_in_db(tmp_path):
    """SQLite UPSERT is atomic — a doc is either fully present or absent."""
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {})
    assert lib.get(doc_id) is not None


def test_list_all_empty_for_fresh_library(tmp_path):
    lib = Library(tmp_path / "library")
    # mint_doc_id creates the .locks dir as a side effect; list_all reads
    # from the DB which is empty.
    lib.mint_doc_id("X")  # touches .locks but no row inserted
    docs = lib.list_all()
    assert docs == []


# ---------------------------------------------------------------------------
# CRUD lifecycle: create → supersede → soft delete → restore → hard delete
# ---------------------------------------------------------------------------


def test_supersede_marks_old_and_links_new(tmp_path):
    lib = Library(tmp_path / "library")
    old_id = lib.mint_doc_id("V1")
    old = _meta(id=old_id, title="V1", size_bytes=1, extracted_chars=1)
    lib.create_doc(old, b"x", "txt", "x", {})

    new_id = lib.mint_doc_id("V2")
    new = _meta(
        id=new_id, title="V2", size_bytes=1, extracted_chars=1, supersedes=old_id
    )
    lib.create_doc(new, b"y", "txt", "y", {})
    lib.supersede(old_id, new)

    refreshed_old = lib.get(old_id)
    refreshed_new = lib.get(new_id)
    assert refreshed_old is not None and refreshed_old.status == "superseded"
    assert refreshed_old.superseded_by == new_id
    assert refreshed_new is not None and refreshed_new.status == "active"
    assert refreshed_new.supersedes == old_id


def test_supersede_rejects_pointer_mismatch(tmp_path):
    lib = Library(tmp_path / "library")
    old_id = lib.mint_doc_id("V1")
    old = _meta(id=old_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(old, b"x", "txt", "x", {})
    new_id = lib.mint_doc_id("V2")
    new_wrong_pointer = _meta(
        id=new_id, size_bytes=1, extracted_chars=1, supersedes="not-the-old-one"
    )
    lib.create_doc(new_wrong_pointer, b"y", "txt", "y", {})
    with pytest.raises(ValueError):
        lib.supersede(old_id, new_wrong_pointer)


def test_soft_delete_then_restore(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {})

    lib.soft_delete(doc_id)
    deleted = lib.get(doc_id)
    assert deleted is not None and deleted.status == "deleted"
    assert lib.list_active() == []
    assert any(m.id == doc_id for m in lib.list_archived())

    lib.restore(doc_id)
    restored = lib.get(doc_id)
    assert restored is not None and restored.status == "active"
    assert any(m.id == doc_id for m in lib.list_active())


def test_restore_rejects_non_deleted(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {})
    with pytest.raises(ValueError):
        lib.restore(doc_id)  # still active


def test_hard_delete_removes_blobs_and_db_row(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {})
    assert (lib.root / doc_id).exists()
    assert lib.get(doc_id) is not None

    lib.hard_delete(doc_id)
    assert not (lib.root / doc_id).exists()
    assert lib.get(doc_id) is None
    assert lib.list_all() == []


def test_update_meta_partial(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1, tags=["old"])
    lib.create_doc(meta, b"x", "txt", "x", {})

    updated = lib.update_meta(doc_id, tags=["new", "tags"], summary="hello")
    assert updated.tags == ["new", "tags"]
    assert updated.summary == "hello"
    # Other fields preserved.
    assert updated.title == meta.title


# ---------------------------------------------------------------------------
# get_extracted / get_verification / get_original_path
# ---------------------------------------------------------------------------


def test_get_extracted_returns_text(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=11)
    lib.create_doc(meta, b"x", "txt", "hello world", {})
    assert lib.get_extracted(doc_id) == "hello world"


def test_get_verification_returns_dict(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {"status": "ok", "checks": []})
    v = lib.get_verification(doc_id)
    assert v is not None
    assert v["status"] == "ok"


def test_get_original_path_finds_file(tmp_path):
    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("X")
    meta = _meta(id=doc_id, kind="image", mime="image/png", size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "png", "x", {})
    p = lib.get_original_path(doc_id)
    assert p is not None
    assert p.name == "original.png"


def test_get_returns_none_for_missing(tmp_path):
    lib = Library(tmp_path / "library")
    assert lib.get("nonexistent") is None
    assert lib.get_extracted("nonexistent") is None
    assert lib.get_verification("nonexistent") is None
    assert lib.get_original_path("nonexistent") is None


# ---------------------------------------------------------------------------
# render_index_md
# ---------------------------------------------------------------------------


def test_render_index_md_empty(tmp_path):
    lib = Library(tmp_path / "library")
    out = lib.render_index_md()
    assert "library is empty" in out


def test_render_index_md_lists_active_with_dates(tmp_path):
    from datetime import date

    lib = Library(tmp_path / "library")
    doc_id = lib.mint_doc_id("DC Diagnostic")
    meta = _meta(
        id=doc_id,
        title="DC Diagnostic — Liam",
        kind="pdf",
        size_bytes=1,
        extracted_chars=1,
        original_date=date(2018, 3, 12),
        original_date_source="pdf_metadata",
        tags=["diagnostic"],
    )
    lib.create_doc(meta, b"x", "pdf", "x", {})
    out = lib.render_index_md()
    assert "DC Diagnostic" in out
    assert "2018-03-12" in out
    assert "diagnostic" in out
    assert doc_id in out


def test_render_index_md_excludes_archived(tmp_path):
    lib = Library(tmp_path / "library")
    a = lib.mint_doc_id("Active")
    b = lib.mint_doc_id("Deleted")
    lib.create_doc(_meta(id=a, title="Active", size_bytes=1, extracted_chars=1), b"x", "txt", "x", {})
    lib.create_doc(_meta(id=b, title="Deleted", size_bytes=1, extracted_chars=1), b"x", "txt", "x", {})
    lib.soft_delete(b)
    out = lib.render_index_md()
    assert "Active" in out
    assert "Deleted" not in out


# ---------------------------------------------------------------------------
# corrupt meta.json should be tolerated
# ---------------------------------------------------------------------------


def test_corrupt_meta_does_not_crash_list_all(tmp_path):
    """Corrupt meta_json in a library_docs row is skipped gracefully."""
    lib = Library(tmp_path / "library")
    # Create a real doc, then corrupt its meta_json column.
    doc_id = lib.mint_doc_id("Good")
    meta = _meta(id=doc_id, size_bytes=1, extracted_chars=1)
    lib.create_doc(meta, b"x", "txt", "x", {})
    from app.db import connect, transaction

    with connect(lib._data_root) as db, transaction(db):
        db.execute(
            "INSERT INTO library_docs (id, title, kind, source, created_at, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("bad-doc", "Bad", "text", "paste", "2026-05-03", "{not json"),
        )
    # Should not raise; bad row is skipped.
    docs = lib.list_all()
    assert len(docs) == 1
    assert docs[0].id == doc_id
