"""Tests for app/tools/people.py + app/summariser.apply_people_updates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.library import Library, LibraryDocMeta
from app.people import People
from app.summariser import PeopleUpdate, apply_people_updates
from app.tools.people import list_people_spec, lookup_person_spec, search_people_spec
from app.tools.registry import ToolError


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def people(tmp_path: Path) -> People:
    return People(tmp_path / "people")


@pytest.fixture
def library(tmp_path: Path) -> Library:
    return Library(tmp_path / "library")


@pytest.fixture
def seeded(people: People, library: Library) -> dict:
    rh = people.add(
        name="Rhiannon O'Hara",
        aliases=["Rhi"],
        category="co-parent",
        relationship="Ex-partner, mother of Jasper",
        summary="Co-parents Jasper.",
        important_context=["School flashpoint"],
    )
    sof = people.add(name="Sofia", category="friend", summary="School friend")

    meta = LibraryDocMeta(
        id=library.mint_doc_id("DC Diagnostic"),
        title="DC Diagnostic",
        kind="pdf",
        source="upload",
        created_at=datetime.now(UTC),
        size_bytes=1,
        mime="application/pdf",
        extractor="pdf_pypdf",
        extracted_chars=1,
    )
    library.create_doc(meta, b"x", "pdf", "x", {"status": "ok"})
    people.link_doc(rh, meta.id)
    return {"rh_id": rh, "sofia_id": sof, "doc_id": meta.id}


# ---------------------------------------------------------------------------
# list_people_spec
# ---------------------------------------------------------------------------


def test_list_people_returns_roster(people: People, seeded: dict):
    spec = list_people_spec(people)
    out = spec.handler({})
    assert "Rhiannon O'Hara" in out
    assert "Sofia" in out
    assert "co-parent" in out


def test_list_people_handles_empty(people: People):
    spec = list_people_spec(people)
    out = spec.handler({})
    assert "no people known yet" in out


# ---------------------------------------------------------------------------
# lookup_person_spec
# ---------------------------------------------------------------------------


def test_lookup_by_id(people: People, library: Library, seeded: dict):
    spec = lookup_person_spec(people, library)
    out = spec.handler({"id_or_name": seeded["rh_id"]})
    assert "Rhiannon O'Hara" in out
    assert "School flashpoint" in out
    assert "DC Diagnostic" in out  # linked doc title surfaces


def test_lookup_by_name(people: People, library: Library, seeded: dict):
    spec = lookup_person_spec(people, library)
    out = spec.handler({"id_or_name": "Rhiannon O'Hara"})
    assert "Rhiannon" in out


def test_lookup_near_match(people: People, library: Library, seeded: dict):
    spec = lookup_person_spec(people, library)
    out = spec.handler({"id_or_name": "Rhianon O'Hara"})  # typo
    assert "Rhiannon" in out


def test_lookup_alias(people: People, library: Library, seeded: dict):
    spec = lookup_person_spec(people, library)
    out = spec.handler({"id_or_name": "Rhi"})
    assert "Rhiannon" in out


def test_lookup_bumps_last_mentioned(people: People, library: Library, seeded: dict):
    spec = lookup_person_spec(people, library)
    before = people.get(seeded["rh_id"])
    assert before.last_mentioned is None
    spec.handler({"id_or_name": seeded["rh_id"]})
    after = people.get(seeded["rh_id"])
    assert after.last_mentioned is not None


def test_lookup_404_raises(people: People, library: Library):
    spec = lookup_person_spec(people, library)
    with pytest.raises(ToolError):
        spec.handler({"id_or_name": "nobody-by-that-name-or-id"})


def test_lookup_empty_raises(people: People, library: Library):
    spec = lookup_person_spec(people, library)
    with pytest.raises(ToolError):
        spec.handler({"id_or_name": ""})


# ---------------------------------------------------------------------------
# search_people_spec
# ---------------------------------------------------------------------------


def test_search_finds_by_summary(people: People, seeded: dict):
    spec = search_people_spec(people)
    out = spec.handler({"query": "school"})
    # "School friend" summary on Sofia
    assert "Sofia" in out


def test_search_no_match(people: People, seeded: dict):
    spec = search_people_spec(people)
    out = spec.handler({"query": "zebra-xyz"})
    assert "no people match" in out


def test_search_empty_query(people: People):
    spec = search_people_spec(people)
    out = spec.handler({"query": ""})
    assert "empty query" in out or "no results" in out


# ---------------------------------------------------------------------------
# apply_people_updates
# ---------------------------------------------------------------------------


def test_apply_add_creates_new(people: People):
    applied = apply_people_updates(
        people,
        [PeopleUpdate(action="add", name="Marcus Chen", category="friend", summary="from work")],
    )
    assert len(applied) == 1
    assert applied[0]["action"] == "added"
    new_id = applied[0]["id"]
    meta = people.get(new_id)
    assert meta is not None
    assert meta.name == "Marcus Chen"
    assert meta.summary == "from work"


def test_apply_add_with_near_match_merges(people: People):
    pid = people.add(name="Rhiannon O'Hara", category="other")
    applied = apply_people_updates(
        people,
        [
            PeopleUpdate(
                action="add",
                name="Rhianon O'Hara",  # typo
                category="co-parent",
                summary="Co-parents Jasper.",
            )
        ],
    )
    assert len(applied) == 1
    assert applied[0]["action"] == "merged_into_existing"
    assert applied[0]["id"] == pid

    # Existing record should now have aliased + summary + category
    meta = people.get(pid)
    assert "Rhianon O'Hara" in meta.aliases
    assert meta.summary == "Co-parents Jasper."
    assert meta.category == "co-parent"  # promoted from "other"


def test_apply_update_partial(people: People):
    pid = people.add(name="Sofia", summary="initial")
    applied = apply_people_updates(
        people,
        [PeopleUpdate(action="update", id=pid, summary="english class")],
    )
    assert len(applied) == 1
    assert applied[0]["action"] == "updated"
    assert people.get(pid).summary == "english class"


def test_apply_update_missing_id_skipped(people: People):
    applied = apply_people_updates(
        people, [PeopleUpdate(action="update", summary="orphan")]
    )
    assert applied == []


def test_apply_update_unknown_id_skipped(people: People):
    applied = apply_people_updates(
        people, [PeopleUpdate(action="update", id="ghost", summary="x")]
    )
    assert applied == []


def test_apply_touch(people: People):
    pid = people.add(name="X")
    assert people.get(pid).last_mentioned is None
    applied = apply_people_updates(people, [PeopleUpdate(action="touch", id=pid)])
    assert len(applied) == 1
    assert applied[0]["action"] == "touched"
    assert people.get(pid).last_mentioned is not None


def test_apply_append_note_via_update(people: People):
    pid = people.add(name="Sofia")
    applied = apply_people_updates(
        people,
        [
            PeopleUpdate(
                action="update",
                id=pid,
                append_note="auditor noticed Sofia mentioned twice this session.",
            )
        ],
    )
    assert len(applied) == 1
    notes = people.get_notes(pid)
    assert "auditor noticed" in notes


def test_apply_skips_invalid_action(people: People):
    applied = apply_people_updates(
        people, [PeopleUpdate(action="garble", name="X")]
    )
    assert applied == []


def test_apply_add_missing_name_skipped(people: People):
    applied = apply_people_updates(people, [PeopleUpdate(action="add", name="")])
    assert applied == []
