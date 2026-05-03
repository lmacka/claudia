"""Tests for app/people.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.people import People, PersonMeta, _levenshtein, _name_slug

# ---------------------------------------------------------------------------
# slug + levenshtein helpers
# ---------------------------------------------------------------------------


def test_name_slug_basic():
    assert _name_slug("Rhiannon O'Hara") == "rhiannon-o-hara"
    assert _name_slug("Dr. Tanya Collins") == "dr-tanya-collins"


def test_name_slug_unicode():
    assert _name_slug("Café Résumé") == "caf-r-sum"


def test_name_slug_empty():
    assert _name_slug("") == "unnamed"
    assert _name_slug("   ") == "unnamed"


def test_levenshtein():
    assert _levenshtein("kitten", "sitting") == 3
    assert _levenshtein("rhiannon", "rhiannon") == 0
    assert _levenshtein("rhiannon", "rhiannan") == 1


# ---------------------------------------------------------------------------
# meta validation
# ---------------------------------------------------------------------------


def test_person_meta_rejects_unsafe_id():
    with pytest.raises(ValueError):
        PersonMeta.model_validate(
            {
                "id": "../escape",
                "name": "x",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )


def test_person_meta_rejects_empty_name():
    with pytest.raises(ValueError):
        PersonMeta.model_validate(
            {
                "id": "ok",
                "name": "  ",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )


def test_person_meta_rejects_unknown_category():
    with pytest.raises(ValueError):
        PersonMeta.model_validate(
            {
                "id": "ok",
                "name": "x",
                "category": "nonsense",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )


# ---------------------------------------------------------------------------
# add + collision
# ---------------------------------------------------------------------------


def test_add_persists_meta_and_notes(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Rhiannon O'Hara", category="co-parent", relationship="ex")

    assert pid == "rhiannon-o-hara"
    # Notes blob on disk
    assert (p.root / pid / "notes.md").exists()
    # Meta in DB
    meta = p.get(pid)
    assert meta is not None
    assert meta.name == "Rhiannon O'Hara"
    assert meta.category == "co-parent"


def test_add_with_collision_appends_suffix(tmp_path: Path):
    p = People(tmp_path / "people")
    a = p.add(name="John Smith")
    b = p.add(name="John Smith")
    assert a == "john-smith"
    assert b == "john-smith-2"
    assert a != b


# ---------------------------------------------------------------------------
# get + list_*
# ---------------------------------------------------------------------------


def test_get_returns_none_for_missing(tmp_path: Path):
    p = People(tmp_path / "people")
    assert p.get("nobody") is None
    assert p.get_notes("nobody") is None


def test_list_active_excludes_archived(tmp_path: Path):
    p = People(tmp_path / "people")
    a = p.add(name="Active Person")
    b = p.add(name="Archived Person")
    p.archive(b)
    active = [m.id for m in p.list_active()]
    archived = [m.id for m in p.list_archived()]
    assert a in active and a not in archived
    assert b in archived and b not in active


def test_list_all_sorted_by_name(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="Charlie")
    p.add(name="alpha")  # lowercase comes first via case-insensitive sort
    p.add(name="Beta")
    names = [m.name for m in p.list_all()]
    assert names == ["alpha", "Beta", "Charlie"]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_partial_preserves_other_fields(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Sofia", relationship="classmate")
    updated = p.update(pid, summary="english class friend")
    assert updated.summary == "english class friend"
    assert updated.relationship == "classmate"  # untouched
    assert updated.updated_at >= updated.created_at


def test_update_404(tmp_path: Path):
    p = People(tmp_path / "people")
    with pytest.raises(KeyError):
        p.update("nobody", summary="x")


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


def test_replace_notes(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Sofia")
    p.replace_notes(pid, "fresh notes\n\nin markdown")
    assert p.get_notes(pid) == "fresh notes\n\nin markdown"


def test_append_note_adds_paragraph_with_stamp(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Sofia")
    p.replace_notes(pid, "initial paragraph.")
    p.append_note(pid, "auditor noticed Sofia got mentioned in two sessions.")
    notes = p.get_notes(pid)
    assert "initial paragraph" in notes
    assert "_(auditor," in notes
    assert "auditor noticed Sofia" in notes


# ---------------------------------------------------------------------------
# link / unlink
# ---------------------------------------------------------------------------


def test_link_doc_then_unlink(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Sofia")
    p.link_doc(pid, "doc-123")
    assert "doc-123" in p.get(pid).linked_documents

    # Idempotent
    p.link_doc(pid, "doc-123")
    assert p.get(pid).linked_documents.count("doc-123") == 1

    p.unlink_doc(pid, "doc-123")
    assert "doc-123" not in p.get(pid).linked_documents


# ---------------------------------------------------------------------------
# touch + archive + restore + delete
# ---------------------------------------------------------------------------


def test_touch_bumps_last_mentioned(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Sofia")
    assert p.get(pid).last_mentioned is None
    p.touch(pid)
    after = p.get(pid)
    assert after.last_mentioned is not None
    assert after.updated_at >= after.created_at


def test_archive_restore(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="X")
    p.archive(pid)
    assert p.get(pid).status == "archived"
    p.restore(pid)
    assert p.get(pid).status == "active"


def test_restore_rejects_non_archived(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="X")
    with pytest.raises(ValueError):
        p.restore(pid)


def test_delete_removes_dir(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="X")
    assert (p.root / pid).exists()
    p.delete(pid)
    assert not (p.root / pid).exists()


# ---------------------------------------------------------------------------
# find_near_match
# ---------------------------------------------------------------------------


def test_find_near_match_by_name(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="Rhiannon O'Hara")
    near = p.find_near_match("Rhianon O'Hara")  # one-letter typo
    assert near is not None
    assert near.name == "Rhiannon O'Hara"


def test_find_near_match_by_alias(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="Rhiannon O'Hara", aliases=["Rhi", "Bri"])
    near = p.find_near_match("Rhi")
    assert near is not None
    assert near.name == "Rhiannon O'Hara"


def test_find_near_match_returns_none_when_far(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="Rhiannon")
    assert p.find_near_match("zebra") is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_matches_name_alias_summary_notes(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(
        name="Tanya Collins",
        aliases=["Dr T"],
        summary="Jasper's diagnostician",
        tags=["professional", "autism"],
    )
    p.replace_notes(pid, "Issued the DC Diagnostic in 2018.")

    assert any(m.id == pid for m in p.search("tanya"))
    assert any(m.id == pid for m in p.search("Dr T"))
    assert any(m.id == pid for m in p.search("Jasper"))
    assert any(m.id == pid for m in p.search("autism"))
    assert any(m.id == pid for m in p.search("diagnostic"))
    assert p.search("nothing-matches") == []


def test_search_excludes_archived(tmp_path: Path):
    p = People(tmp_path / "people")
    pid = p.add(name="Findable")
    p.archive(pid)
    assert p.search("findable") == []


# ---------------------------------------------------------------------------
# manifest + render
# ---------------------------------------------------------------------------


def test_list_all_returns_every_person(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="A")
    p.add(name="B")
    people = p.list_all()
    names = {entry.name for entry in people}
    assert names == {"A", "B"}


def test_render_people_md_empty(tmp_path: Path):
    p = People(tmp_path / "people")
    out = p.render_people_md()
    assert "no people known yet" in out


def test_render_people_md_with_entries(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(
        name="Rhiannon O'Hara",
        aliases=["Rhi"],
        category="co-parent",
        summary="Mother of Jasper",
    )
    p.add(name="Dr Tanya Collins", category="professional", summary="Diagnostician")
    out = p.render_people_md()
    assert "Rhiannon O'Hara" in out
    assert "Rhi" in out
    assert "co-parent" in out
    assert "Mother of Jasper" in out
    assert "Dr Tanya Collins" in out


def test_render_people_md_excludes_archived(tmp_path: Path):
    p = People(tmp_path / "people")
    p.add(name="Active")
    b = p.add(name="Archived")
    p.archive(b)
    out = p.render_people_md()
    assert "Active" in out
    assert "Archived" not in out


# ---------------------------------------------------------------------------
# Path traversal safety
# ---------------------------------------------------------------------------


def test_person_dir_rejects_path_traversal(tmp_path: Path):
    p = People(tmp_path / "people")
    with pytest.raises(ValueError):
        p._person_dir("../escape")
    with pytest.raises(ValueError):
        p._person_dir(".secret")
    with pytest.raises(ValueError):
        p._person_dir("foo/bar")
