"""Tool registry + document tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.documents import (
    LIST_DOCUMENTS_SPEC,
    READ_DOCUMENT_SPEC,
    SEARCH_DOCUMENTS_SPEC,
    _safe_resolve,
    rebuild_index,
)
from app.tools.registry import ToolError, ToolRegistry


def _seed(data_root: Path):
    (data_root / "context").mkdir(parents=True, exist_ok=True)
    (data_root / "uploads" / "pdfs").mkdir(parents=True, exist_ok=True)
    (data_root / "archives").mkdir(parents=True, exist_ok=True)
    (data_root / "context" / "01_background.md").write_text("# Background\n\nLiam is 41.\n")
    (data_root / "context" / "05_current_state.md").write_text("# State\nday 3\n")
    (data_root / "uploads" / "pdfs" / "report.txt").write_text("report text with important keyword\n")


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_safe_resolve_blocks_traversal(tmp_path: Path):
    _seed(tmp_path)
    with pytest.raises(ToolError):
        _safe_resolve(tmp_path, "../../etc/passwd")


def test_safe_resolve_strips_leading_data_prefix(tmp_path: Path):
    _seed(tmp_path)
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
# Document tools
# ---------------------------------------------------------------------------


def test_read_document_text(tmp_path: Path):
    _seed(tmp_path)
    spec = READ_DOCUMENT_SPEC(tmp_path)
    out = spec.handler({"path": "context/01_background.md"})
    assert "Liam is 41" in out


def test_read_document_missing_raises(tmp_path: Path):
    _seed(tmp_path)
    spec = READ_DOCUMENT_SPEC(tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": "context/nope.md"})


def test_read_document_dir_raises(tmp_path: Path):
    _seed(tmp_path)
    spec = READ_DOCUMENT_SPEC(tmp_path)
    with pytest.raises(ToolError):
        spec.handler({"path": "context"})


def test_list_documents_generates_index(tmp_path: Path):
    _seed(tmp_path)
    # No INDEX.md yet
    spec = LIST_DOCUMENTS_SPEC(tmp_path)
    out = spec.handler({})
    assert "source-material" in out
    assert "uploads" in out
    assert (tmp_path / "context" / "INDEX.md").exists()


def test_search_documents_finds_match(tmp_path: Path):
    _seed(tmp_path)
    spec = SEARCH_DOCUMENTS_SPEC(tmp_path)
    out = spec.handler({"query": "important"})
    assert "report.txt" in out


def test_search_documents_no_match(tmp_path: Path):
    _seed(tmp_path)
    spec = SEARCH_DOCUMENTS_SPEC(tmp_path)
    out = spec.handler({"query": "flibbertigibbet"})
    assert "No matches" in out


# ---------------------------------------------------------------------------
# Index regeneration
# ---------------------------------------------------------------------------


def test_rebuild_index_lists_uploads_and_source_material(tmp_path: Path):
    _seed(tmp_path)
    (tmp_path / "context" / "source-material").mkdir(parents=True, exist_ok=True)
    (tmp_path / "context" / "source-material" / "foo.pdf").write_bytes(b"%PDF-1.0\n")
    rebuild_index(tmp_path)
    content = (tmp_path / "context" / "INDEX.md").read_text()
    assert "foo.pdf" in content
    assert "report.txt" in content
