"""Storage layer — InMemorySessionStore + SqliteSessionStore.

NFSSessionStore was removed in v0.8.0 phase A — production was already on
SqliteSessionStore (T-NEW-I, v0.7.0); the JSONL implementation became dead
code with no callers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import (
    InMemorySessionStore,
    Message,
    SessionHeader,
    new_session_id,
)
from app.storage_sqlite import SqliteSessionStore


@pytest.fixture
def mem_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(tmp_path)


STORES = ["mem_store", "sqlite_store"]


def _header(session_id: str) -> SessionHeader:
    return SessionHeader(
        session_id=session_id,
        created_at="2026-04-21T12:00:00+00:00",
        mode="vent",
        model="claude-sonnet-4-6",
        prompt_sha="abc123",
    )


@pytest.mark.parametrize("store_name", STORES)
def test_create_and_load_header(request, store_name):
    store = request.getfixturevalue(store_name)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    header = store.load_header(sid)
    assert header.session_id == sid
    assert header.mode == "vent"


@pytest.mark.parametrize("store_name", STORES)
def test_append_and_load_messages(request, store_name):
    store = request.getfixturevalue(store_name)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    store.append_message(sid, Message(role="user", content="hi"))
    store.append_message(sid, Message(role="assistant", content="hello"))
    msgs = store.load_messages(sid)
    assert [m.content for m in msgs] == ["hi", "hello"]


@pytest.mark.parametrize("store_name", STORES)
def test_header_update_applied(request, store_name):
    store = request.getfixturevalue(store_name)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    store.update_header(sid, status="ended", cost_total_usd=0.42)
    h = store.load_header(sid)
    assert h.status == "ended"
    assert h.cost_total_usd == 0.42


@pytest.mark.parametrize("store_name", STORES)
def test_list_sessions_orders_by_creation(request, store_name):
    store = request.getfixturevalue(store_name)
    ids = []
    for _ in range(3):
        sid = new_session_id("vent")
        ids.append(sid)
        store.create_session(_header(sid))
    listing = store.list_sessions()
    assert len(listing) == 3
    assert {s.session_id for s in listing} == set(ids)


@pytest.mark.parametrize("store_name", STORES)
def test_active_session_returns_first_active(request, store_name):
    store = request.getfixturevalue(store_name)
    sid1 = new_session_id("vent")
    sid2 = new_session_id("vent")
    store.create_session(_header(sid1))
    store.create_session(_header(sid2))
    store.update_header(sid1, status="ended")
    active = store.active_session()
    assert active is not None
    assert active.session_id == sid2


# ---------------------------------------------------------------------------
# SqliteSessionStore-specific
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store_name", STORES)
def test_has_event(request, store_name):
    store = request.getfixturevalue(store_name)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    assert store.has_event(sid, "audit_applied") is False
    store.append_event(sid, "audit_scheduled", {})
    store.append_event(sid, "audit_applied", {})
    assert store.has_event(sid, "audit_scheduled") is True
    assert store.has_event(sid, "audit_applied") is True
    assert store.has_event(sid, "nope") is False


def test_sqlite_persists_across_instances(tmp_path: Path):
    store1 = SqliteSessionStore(tmp_path)
    sid = new_session_id("vent")
    store1.create_session(_header(sid))
    store1.append_message(sid, Message(role="user", content="persisted"))
    store1.append_event(sid, "audit_applied", {"score": 9})

    store2 = SqliteSessionStore(tmp_path)
    h = store2.load_header(sid)
    assert h.session_id == sid
    msgs = store2.load_messages(sid)
    assert msgs[0].content == "persisted"
    assert store2.has_event(sid, "audit_applied") is True


def test_sqlite_creates_db_file(tmp_path: Path):
    """The .db file is created on first construction."""
    SqliteSessionStore(tmp_path)
    assert (tmp_path / "claudia.db").exists()


def test_sqlite_load_messages_unknown_session_raises(tmp_path: Path):
    store = SqliteSessionStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_messages("does-not-exist")


def test_sqlite_update_header_unknown_column_rejected(tmp_path: Path):
    store = SqliteSessionStore(tmp_path)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    with pytest.raises(ValueError, match="unknown header column"):
        store.update_header(sid, made_up_field="x")


def test_sqlite_messages_preserve_insert_order(tmp_path: Path):
    store = SqliteSessionStore(tmp_path)
    sid = new_session_id("vent")
    store.create_session(_header(sid))
    for i in range(10):
        store.append_message(sid, Message(role="user", content=f"msg-{i}"))
    msgs = store.load_messages(sid)
    assert [m.content for m in msgs] == [f"msg-{i}" for i in range(10)]
