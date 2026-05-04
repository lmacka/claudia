"""Context loader tests — focus on same-day raw-session stitching (P1-1).

Fixes the 'slept inside again?' confusion: when Liam starts a new session
hours after an earlier same-day one whose audit hasn't completed, the
companion should still see the earlier turns in its system prompt.

Sessions are seeded into the SqliteSessionStore (the same store production
uses) and the loader is wired with a same_day_transcripts_provider that
queries it — same shape as the production wiring in main.py lifespan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.context import ContextLoader
from app.storage import Message, SessionHeader
from app.storage_sqlite import SqliteSessionStore


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "05_current_state.md").write_text(
        "# current_state\n", encoding="utf-8"
    )
    return tmp_path


def _seed_session(
    store: SqliteSessionStore,
    session_id: str,
    created_ago: timedelta,
    messages: list[tuple[str, str, dict]],
) -> None:
    """Seed a session into the SQLite store. messages is list of (role, content, meta)."""
    created = (datetime.now(UTC) - created_ago).isoformat()
    store.create_session(
        SessionHeader(
            session_id=session_id,
            created_at=created,
            mode="check-in",
            model="claude-sonnet-4-6",
            prompt_sha="deadbeef",
        )
    )
    for role, content, meta in messages:
        store.append_message(session_id, Message(role=role, content=content, meta=meta))


def _provider_for(store: SqliteSessionStore):
    """Build a same_day_transcripts_provider that reads from the given store.
    Mirrors the production helper in main.py:_recent_same_day_transcripts_from_store."""

    def _provider(window_hours: int = 8, max_chars_each: int = 2500, max_sessions: int = 6) -> str:
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        parts: list[str] = []
        for meta in store.list_sessions()[:max_sessions]:
            try:
                created = datetime.fromisoformat(meta.created_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if created < cutoff:
                continue
            messages = store.load_messages(meta.session_id)
            lines: list[str] = []
            for m in messages:
                if m.role not in ("user", "assistant"):
                    continue
                content = (m.content or "").strip()
                if not content:
                    continue
                if (m.meta or {}).get("is_synthetic_opener"):
                    continue
                lines.append(f"{m.role}: {content}")
            if not lines:
                continue
            tail = "\n".join(lines)
            if len(tail) > max_chars_each:
                tail = "…\n" + tail[-max_chars_each:]
            parts.append(f"### {meta.session_id}\n{tail}")
        return "\n\n".join(parts)

    return _provider


def test_same_day_transcripts_included_when_recent(
    data_root: Path, tmp_path: Path
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "companion.md").write_text("# companion stub\n", encoding="utf-8")

    store = SqliteSessionStore(data_root)
    _seed_session(
        store,
        "2026-04-22T06-00-00Z_check-in_aaaa1111",
        created_ago=timedelta(hours=2),
        messages=[
            ("user", "Slept inside last night for the first time in 3 nights.", {}),
            ("assistant", "That's the reintegration marker.", {}),
        ],
    )

    loader = ContextLoader(
        data_root, prompts, same_day_transcripts_provider=_provider_for(store)
    )
    blocks = loader.assemble()

    assert "Earlier today" in blocks.block3
    assert "Slept inside last night" in blocks.block3
    assert "reintegration marker" in blocks.block3


def test_same_day_transcripts_excluded_when_outside_window(
    data_root: Path, tmp_path: Path
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "companion.md").write_text("# companion stub\n", encoding="utf-8")

    store = SqliteSessionStore(data_root)
    _seed_session(
        store,
        "2026-04-20T00-00-00Z_check-in_bbbb2222",
        created_ago=timedelta(days=2),
        messages=[
            ("user", "Old content that should not be stitched.", {}),
            ("assistant", "Old assistant reply.", {}),
        ],
    )

    loader = ContextLoader(
        data_root, prompts, same_day_transcripts_provider=_provider_for(store)
    )
    blocks = loader.assemble()

    assert "Old content that should not be stitched" not in blocks.block3
    assert "Earlier today" not in blocks.block3


def test_synthetic_opener_is_skipped(data_root: Path, tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "companion.md").write_text("# companion stub\n", encoding="utf-8")

    store = SqliteSessionStore(data_root)
    _seed_session(
        store,
        "2026-04-22T07-00-00Z_check-in_cccc3333",
        created_ago=timedelta(hours=1),
        messages=[
            ("user", "", {"is_synthetic_opener": True}),
            ("assistant", "Real opener content.", {}),
        ],
    )

    loader = ContextLoader(
        data_root, prompts, same_day_transcripts_provider=_provider_for(store)
    )
    blocks = loader.assemble()
    assert "Real opener content" in blocks.block3
    assert "user: Begin the session" not in blocks.block3
