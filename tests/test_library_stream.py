"""Tests for app/library_stream.py + the SSE route + async dispatch path."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.library_stream import StatusBus

# ---------------------------------------------------------------------------
# StatusBus mechanics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_bus_replays_ring_buffer():
    bus = StatusBus(ring_size=10)
    bus.attach_loop(asyncio.get_event_loop())
    # Producer side fires before any subscriber.
    bus.emit("docA", "first")
    bus.emit("docA", "second")
    bus.emit("docA", "third", terminal=True)

    msgs: list[str] = []
    async for msg in bus.subscribe("docA"):
        msgs.append(msg)
    assert msgs == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_status_bus_live_subscriber_receives_events():
    bus = StatusBus()
    bus.attach_loop(asyncio.get_event_loop())

    msgs: list[str] = []

    async def reader():
        async for msg in bus.subscribe("docA"):
            msgs.append(msg)

    task = asyncio.create_task(reader())
    # Yield to let the subscriber register.
    await asyncio.sleep(0.01)

    bus.emit("docA", "live one")
    bus.emit("docA", "live two")
    bus.emit("docA", "done", terminal=True)

    await asyncio.wait_for(task, timeout=2.0)
    assert msgs == ["live one", "live two", "done"]


@pytest.mark.asyncio
async def test_status_bus_emit_from_thread():
    """Emit safely from a worker thread (the executor case)."""
    bus = StatusBus()
    bus.attach_loop(asyncio.get_running_loop())

    msgs: list[str] = []

    async def reader():
        async for msg in bus.subscribe("docA"):
            msgs.append(msg)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)

    def worker():
        bus.emit("docA", "thread one")
        bus.emit("docA", "thread two")
        bus.emit("docA", "thread done", terminal=True)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    await asyncio.wait_for(task, timeout=2.0)
    assert msgs == ["thread one", "thread two", "thread done"]


@pytest.mark.asyncio
async def test_status_bus_is_terminal():
    bus = StatusBus()
    bus.attach_loop(asyncio.get_event_loop())
    assert not bus.is_terminal("docA")
    bus.emit("docA", "x", terminal=True)
    assert bus.is_terminal("docA")


@pytest.mark.asyncio
async def test_status_bus_two_subscribers_each_get_replay():
    bus = StatusBus()
    bus.attach_loop(asyncio.get_event_loop())
    bus.emit("docA", "first")
    bus.emit("docA", "second", terminal=True)

    async def collect():
        out: list[str] = []
        async for msg in bus.subscribe("docA"):
            out.append(msg)
        return out

    a, b = await asyncio.gather(collect(), collect())
    assert a == ["first", "second"]
    assert b == ["first", "second"]


@pytest.mark.asyncio
async def test_status_bus_clear_removes_ring():
    bus = StatusBus()
    bus.attach_loop(asyncio.get_event_loop())
    bus.emit("docA", "x", terminal=True)
    bus.clear("docA")
    assert not bus.is_terminal("docA")
    msgs: list[str] = []

    async def reader():
        async for msg in bus.subscribe("docA"):
            msgs.append(msg)
            if len(msgs) >= 1:
                return

    # No emits will come; this would hang. So we don't actually start the
    # reader; just confirm internal state is clear.
    assert bus._ring.get("docA") is None or len(bus._ring["docA"]) == 0


# ---------------------------------------------------------------------------
# Async dispatch path: HX-Request triggers async + redirect to /stream
# ---------------------------------------------------------------------------


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


def test_async_paste_redirects_to_stream(client: TestClient):
    r = client.post(
        "/library/paste",
        data={"title": "Async test", "text": "hello async"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/stream" in r.headers["location"]
    # location format: /library/{doc_id}/stream
    parts = r.headers["location"].rstrip("/").split("/")
    assert parts[-1] == "stream"
    doc_id = parts[-2]
    assert doc_id


def test_async_upload_redirects_to_stream(client: TestClient):
    r = client.post(
        "/library/upload",
        files={"file": ("note.txt", b"hello text upload", "text/plain")},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/stream" in r.headers["location"]


def test_sync_paste_unchanged(client: TestClient):
    """Without HX-Request, the synchronous path must still return /library#id."""
    r = client.post(
        "/library/paste",
        data={"text": "synchronous"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/library#" in r.headers["location"]
    assert "/stream" not in r.headers["location"]


def test_stream_endpoint_renders_terminal_eventually(client: TestClient):
    """Smoke: schedule an async paste, hit the stream route, confirm it
    yields events and terminates."""
    r = client.post(
        "/library/paste",
        data={"title": "Stream smoke", "text": "stream test body"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    doc_id = r.headers["location"].rstrip("/").split("/")[-2]

    # Give the background task a moment to start emitting.
    time.sleep(0.5)

    # Connect to the SSE stream. EventSourceResponse uses content-type text/event-stream.
    with client.stream("GET", f"/library/{doc_id}/stream") as response:
        assert response.status_code == 200
        # Read a few bytes to confirm we got something.
        chunks: list[bytes] = []
        deadline = time.time() + 5.0
        for chunk in response.iter_bytes(chunk_size=128):
            chunks.append(chunk)
            if time.time() > deadline:
                break
            if any(b"Done" in c or b"Failed" in c for c in chunks):
                break

    body = b"".join(chunks).decode("utf-8", errors="replace")
    assert "data:" in body  # SSE format
    assert "Uploaded" in body or "Done" in body or "Failed" in body
