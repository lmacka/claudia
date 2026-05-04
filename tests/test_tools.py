"""Tool registry + document tools (rewritten for commit H — Library-backed)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.library import Library, LibraryDocMeta
from app.tools.documents import (
    LIST_DOCUMENTS_SPEC,
    READ_DOCUMENT_SPEC,
    SEARCH_DOCUMENTS_SPEC,
    _safe_resolve,
)
from app.tools.registry import ToolError, ToolRegistry

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seed_legacy(data_root: Path):
    (data_root / "context").mkdir(parents=True, exist_ok=True)
    (data_root / "uploads" / "pdfs").mkdir(parents=True, exist_ok=True)
    (data_root / "context" / "01_background.md").write_text("# Background\n\nLiam is 41.\n")
    (data_root / "context" / "05_current_state.md").write_text("# State\nday 3\n")
    (data_root / "uploads" / "pdfs" / "report.txt").write_text(
        "report text with important keyword\n"
    )


def _seed_library(library: Library, *, title: str, text: str) -> str:
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


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_safe_resolve_blocks_traversal(tmp_path: Path):
    _seed_legacy(tmp_path)
    with pytest.raises(ToolError):
        _safe_resolve(tmp_path, "../../etc/passwd")


def test_safe_resolve_strips_leading_data_prefix(tmp_path: Path):
    _seed_legacy(tmp_path)
    p = _safe_resolve(tmp_path, "data/context/01_background.md")
    assert p.name == "01_background.md"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_registers_and_dispatches():
    reg = ToolRegistry()
    called = {}

    from app.tools.registry import ToolSpec

    def _h(args):
        called["args"] = args
        return "ok"

    reg.register(
        ToolSpec(
            name="t",
            description="d",
            input_schema={"type": "object"},
            handler=_h,
        )
    )
    out = reg.invoke("t", {"a": 1})
    assert out == "ok"
    assert called["args"] == {"a": 1}
    assert "t" in reg.names()


def test_registry_unknown_tool_errors():
    reg = ToolRegistry()
    with pytest.raises(ToolError):
        reg.invoke("missing", {})


def test_registry_handler_exception_becomes_tool_error():
    reg = ToolRegistry()
    from app.tools.registry import ToolSpec

    def _h(args):
        raise RuntimeError("boom")

    reg.register(ToolSpec("t", "d", {"type": "object"}, _h))
    with pytest.raises(ToolError):
        reg.invoke("t", {})


# ---------------------------------------------------------------------------
# read_document — library doc_id path
# ---------------------------------------------------------------------------


def test_read_document_by_doc_id(tmp_path: Path):
    library = Library(tmp_path / "library")
    doc_id = _seed_library(library, title="DC Diagnostic", text="contents go here")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    out = spec.handler({"path": doc_id})
    assert isinstance(out, str)
    assert "DC Diagnostic" in out
    assert "contents go here" in out
    # Date-source label is part of the header.
    assert "Original date:" in out


def test_read_document_by_doc_id_404(tmp_path: Path):
    library = Library(tmp_path / "library")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": "no-such-doc-id"})


def test_read_document_image_returns_vision_block(tmp_path: Path):
    import io as _io

    from PIL import Image

    library = Library(tmp_path / "library")
    img = Image.new("RGB", (4, 4), (1, 2, 3))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    meta = LibraryDocMeta(
        id=library.mint_doc_id("Snap"),
        title="Snap",
        kind="image",
        source="upload",
        created_at=datetime.now(UTC),
        size_bytes=len(png_bytes),
        mime="image/png",
        extractor="image_vision_ocr",
        extracted_chars=0,
    )
    library.create_doc(meta, png_bytes, "png", "", {"status": "ok"})
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    out = spec.handler({"path": meta.id})
    assert isinstance(out, list)
    assert out[0]["type"] == "image"
    assert out[1]["type"] == "text"


def test_read_document_pages_slice(tmp_path: Path):
    library = Library(tmp_path / "library")
    multi_page = "## Page 1\nfirst\n## Page 2\nsecond\n## Page 3\nthird\n"
    meta = LibraryDocMeta(
        id=library.mint_doc_id("Multi"),
        title="Multi",
        kind="pdf",
        source="upload",
        created_at=datetime.now(UTC),
        size_bytes=10,
        mime="application/pdf",
        page_count=3,
        extractor="pdf_pypdf",
        extracted_chars=len(multi_page),
    )
    library.create_doc(meta, b"x" * 10, "pdf", multi_page, {"status": "ok"})
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    out = spec.handler({"path": meta.id, "pages": "2-3"})
    assert "first" not in out
    assert "second" in out
    assert "third" in out


def test_read_document_pages_invalid(tmp_path: Path):
    library = Library(tmp_path / "library")
    doc_id = _seed_library(library, title="X", text="body")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": doc_id, "pages": "junk"})


# ---------------------------------------------------------------------------
# read_document — legacy filesystem fallback
# ---------------------------------------------------------------------------


def test_read_document_legacy_filesystem(tmp_path: Path):
    _seed_legacy(tmp_path)
    library = Library(tmp_path / "library")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    out = spec.handler({"path": "context/01_background.md"})
    assert "Liam is 41" in out


def test_read_document_legacy_missing_raises(tmp_path: Path):
    _seed_legacy(tmp_path)
    library = Library(tmp_path / "library")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": "context/nope.md"})


def test_read_document_legacy_dir_raises(tmp_path: Path):
    _seed_legacy(tmp_path)
    library = Library(tmp_path / "library")
    spec = READ_DOCUMENT_SPEC(library, tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": "context"})


# ---------------------------------------------------------------------------
# list_documents — library-backed
# ---------------------------------------------------------------------------


def test_list_documents_renders_library(tmp_path: Path):
    library = Library(tmp_path / "library")
    _seed_library(library, title="Doc One", text="alpha")
    _seed_library(library, title="Doc Two", text="beta")
    spec = LIST_DOCUMENTS_SPEC(library)
    out = spec.handler({})
    assert "Doc One" in out
    assert "Doc Two" in out


def test_list_documents_empty(tmp_path: Path):
    library = Library(tmp_path / "library")
    spec = LIST_DOCUMENTS_SPEC(library)
    out = spec.handler({})
    assert "library is empty" in out


# ---------------------------------------------------------------------------
# search_documents — library-backed grep
# ---------------------------------------------------------------------------


def test_search_documents_finds_match(tmp_path: Path):
    library = Library(tmp_path / "library")
    doc_id = _seed_library(library, title="Report", text="report text with important keyword")
    spec = SEARCH_DOCUMENTS_SPEC(library)
    out = spec.handler({"query": "important"})
    assert doc_id in out
    assert "important" in out.lower()


def test_search_documents_title_match_only(tmp_path: Path):
    library = Library(tmp_path / "library")
    doc_id = _seed_library(library, title="Diagnostic Report", text="completely unrelated body")
    spec = SEARCH_DOCUMENTS_SPEC(library)
    out = spec.handler({"query": "diagnostic"})
    assert doc_id in out
    assert "title match" in out


def test_search_documents_no_match(tmp_path: Path):
    library = Library(tmp_path / "library")
    _seed_library(library, title="X", text="body")
    spec = SEARCH_DOCUMENTS_SPEC(library)
    out = spec.handler({"query": "flibbertigibbet"})
    assert "No matches" in out


def test_search_documents_excludes_archived(tmp_path: Path):
    library = Library(tmp_path / "library")
    keep = _seed_library(library, title="Active", text="active body")
    drop = _seed_library(library, title="Deleted", text="active body")
    library.soft_delete(drop)
    spec = SEARCH_DOCUMENTS_SPEC(library)
    out = spec.handler({"query": "active"})
    assert keep in out
    assert drop not in out


