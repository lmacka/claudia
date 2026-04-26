"""Context loader tests — focus on same-day raw-session stitching (P1-1).

Fixes the 'slept inside again?' confusion: when Liam starts a new session
hours after an earlier same-day one whose audit hasn't completed, the
companion should still see the earlier turns in its system prompt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.context import ContextLoader


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    (tmp_path / "context").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "session-logs").mkdir()
    (tmp_path / "context" / "05_current_state.md").write_text(
        "# current_state\n", encoding="utf-8"
    )
    (tmp_path / "context" / "commitments.md").write_text("", encoding="utf-8")
    return tmp_path


def _write_session_jsonl(
    data_root: Path, session_id: str, created_ago: timedelta, messages: list[tuple[str, str]]
) -> None:
    """Write a fake session JSONL with header + messages."""
    created = (datetime.now(timezone.utc) - created_ago).isoformat()
    path = data_root / "sessions" / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "header",
                    "session_id": session_id,
                    "created_at": created,
                    "mode": "check-in",
                    "model": "claude-sonnet-4-6",
                    "prompt_sha": "deadbeef",
                }
            )
            + "\n"
        )
        for role, content in messages:
            fh.write(
                json.dumps({"type": "message", "role": role, "content": content})
                + "\n"
            )


def test_same_day_transcripts_included_when_recent(
    data_root: Path, tmp_path: Path
) -> None:
    # Create a prompts dir so companion.md read doesn't break the loader.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "companion.md").write_text("# companion stub\n", encoding="utf-8")

    _write_session_jsonl(
        data_root,
        "2026-04-22T06-00-00Z_check-in_aaaa1111",
        created_ago=timedelta(hours=2),
        messages=[
            ("user", "Slept inside last night for the first time in 3 nights."),
            ("assistant", "That's the reintegration marker."),
        ],
    )

    loader = ContextLoader(data_root, prompts)
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

    _write_session_jsonl(
        data_root,
        "2026-04-20T00-00-00Z_check-in_bbbb2222",
        created_ago=timedelta(days=2),
        messages=[
            ("user", "Old content that should not be stitched."),
            ("assistant", "Old assistant reply."),
        ],
    )

    loader = ContextLoader(data_root, prompts)
    blocks = loader.assemble()

    assert "Old content that should not be stitched" not in blocks.block3
    # The 'Earlier today' section header should not appear if nothing qualifies.
    assert "Earlier today" not in blocks.block3


def test_synthetic_opener_is_skipped(data_root: Path, tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "companion.md").write_text("# companion stub\n", encoding="utf-8")

    # Synthetic opener message has is_synthetic_opener in meta and empty visible
    # content — manually simulate what the session starter writes.
    created = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path = data_root / "sessions" / "2026-04-22T07-00-00Z_check-in_cccc3333.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "header",
                    "session_id": "2026-04-22T07-00-00Z_check-in_cccc3333",
                    "created_at": created,
                    "mode": "check-in",
                    "model": "claude-sonnet-4-6",
                    "prompt_sha": "deadbeef",
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": "",
                    "meta": {"is_synthetic_opener": True},
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Real opener content.",
                }
            )
            + "\n"
        )

    loader = ContextLoader(data_root, prompts)
    blocks = loader.assemble()
    assert "Real opener content" in blocks.block3
    # Synthetic opener must not appear as a phantom user turn.
    assert "user: Begin the session" not in blocks.block3
