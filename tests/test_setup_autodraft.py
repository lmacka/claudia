"""Tests for app/setup_autodraft.py — wizard auto-fill from library docs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from app.claude import Reply, Usage
from app.library import Library, LibraryDocMeta
from app.setup_autodraft import _build_doc_blob, _parse_sections, auto_draft_profile


def _seed_doc(library: Library, *, title: str, text: str) -> str:
    meta = LibraryDocMeta(
        id=library.mint_doc_id(title),
        title=title,
        kind="text",
        source="paste",
        created_at=datetime.now(UTC),
        size_bytes=len(text.encode("utf-8")),
        mime="text/plain",
        extractor="text_verbatim",
        extracted_chars=len(text),
    )
    return library.create_doc(meta, text.encode("utf-8"), "txt", text, {"status": "ok"})


def test_parse_sections_handles_clean_output() -> None:
    text = (
        "WHO: They are a software engineer with ADHD.\n\n"
        "STRESSORS: Co-parenting friction and shed renovation backlog.\n\n"
        "NEVER: Don't moralise.\n\n"
        "FOR: Help thinking through difficult conversations."
    )
    out = _parse_sections(text)
    assert out["section_who"].startswith("They are a software engineer")
    assert out["section_stressors"].startswith("Co-parenting friction")
    assert out["section_never"] == "Don't moralise."
    assert out["section_for"].startswith("Help thinking")


def test_parse_sections_tolerates_preamble_inline_text() -> None:
    """Model occasionally adds a leading sentence; parser should still pull labels."""
    text = (
        "Sure, here's a draft:\n\n"
        "WHO: They are X.\n\n"
        "STRESSORS: They face Y.\n\n"
        "NEVER: Don't Z.\n\n"
        "FOR: Help with W."
    )
    out = _parse_sections(text)
    assert out["section_who"] == "They are X."
    assert out["section_for"] == "Help with W."


def test_parse_sections_returns_empty_for_garbage() -> None:
    assert _parse_sections("just some prose with no labels at all") == {}


def test_build_doc_blob_caps_per_doc(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    long_text = "x" * 20000
    _seed_doc(library, title="Big doc", text=long_text)
    blob = _build_doc_blob(library, max_chars_per_doc=8000)
    # Truncation marker present
    assert "document truncated for prompt" in blob
    # Less than the full input
    assert len(blob) < 20000


def test_build_doc_blob_caps_doc_count(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    for i in range(15):
        _seed_doc(library, title=f"Doc {i}", text=f"contents of doc {i}")
    blob = _build_doc_blob(library, max_docs=5)
    # Only 5 of the 15 docs should be in the blob.
    assert blob.count("contents of doc") == 5


def test_build_doc_blob_skips_empty_extracts(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    # Empty extract should be skipped — it can't help the LLM.
    meta = LibraryDocMeta(
        id=library.mint_doc_id("Empty"),
        title="Empty",
        kind="image",
        source="upload",
        created_at=datetime.now(UTC),
        size_bytes=10,
        mime="image/png",
        extractor="image_vision_ocr",
        extracted_chars=0,
    )
    library.create_doc(meta, b"x" * 10, "png", "", {"status": "ok"})
    _seed_doc(library, title="Has text", text="real content here")
    blob = _build_doc_blob(library)
    assert "real content here" in blob
    assert "Empty" not in blob


def test_auto_draft_profile_returns_empty_for_no_docs(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    claude = MagicMock()
    out = auto_draft_profile(claude, "claude-sonnet-4-6", library)
    assert out == {}
    claude.single_turn.assert_not_called()


def test_auto_draft_profile_calls_claude_and_parses(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    _seed_doc(library, title="Diagnostic", text="Patient: Liam. Diagnosis: ADHD-Combined.")

    claude = MagicMock()
    claude.single_turn.return_value = Reply(
        text=(
            "WHO: Software engineer with ADHD-Combined.\n\n"
            "STRESSORS: Recent diagnostic process.\n\n"
            "NEVER: Don't moralise.\n\n"
            "FOR: Externalise thinking."
        ),
        usage=Usage(input_tokens=500, output_tokens=120, cache_read_tokens=0, cache_write_tokens=0),
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
    )
    out = auto_draft_profile(claude, "claude-sonnet-4-6", library)
    assert out["section_who"].startswith("Software engineer")
    assert out["section_stressors"] == "Recent diagnostic process."
    assert out["section_never"] == "Don't moralise."
    assert out["section_for"] == "Externalise thinking."
    claude.single_turn.assert_called_once()
    call_kwargs = claude.single_turn.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"
    # The doc title appears in the prompt
    assert "Diagnostic" in call_kwargs["user_content"][0]["text"]
