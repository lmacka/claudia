"""
Document tools: read_document, list_documents, search_documents.

Also: INDEX.md auto-generation on upload and on app startup.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

import structlog

from app.tools.registry import ToolError, ToolSpec

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Safety — make sure tool callers can't escape the data root
# ---------------------------------------------------------------------------


def _safe_resolve(data_root: Path, rel_path: str) -> Path:
    rel = rel_path.lstrip("/").lstrip("\\")
    # Strip a leading "data/" if the model includes it.
    if rel.startswith("data/"):
        rel = rel[len("data/") :]
    candidate = (data_root / rel).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError as e:
        raise ToolError(f"path {rel_path!r} is outside /data") from e
    return candidate


# ---------------------------------------------------------------------------
# PDF handling
# ---------------------------------------------------------------------------


def _pdf_to_text(path: Path, max_pages: int = 20) -> tuple[str, int, int]:
    """Returns (text, total_pages, pages_extracted)."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        raise ToolError("pypdf not available in container") from None
    reader = PdfReader(str(path))
    total = len(reader.pages)
    if total == 0:
        return ("(empty PDF)", 0, 0)
    to_read = min(total, max_pages)
    parts: list[str] = []
    for i in range(to_read):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001
            parts.append(f"[page {i + 1}: extraction failed]")
    return ("\n\n".join(parts).strip(), total, to_read)


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------


def _read_document_handler(data_root: Path) -> callable:
    def _h(args: dict):
        rel = args.get("path")
        if not rel or not isinstance(rel, str):
            raise ToolError("path is required (string)")
        p = _safe_resolve(data_root, rel)
        if not p.exists():
            raise ToolError(f"file not found: {rel}")
        if p.is_dir():
            raise ToolError(f"path is a directory: {rel}")

        suffix = p.suffix.lower()

        if suffix == ".pdf":
            text, total, got = _pdf_to_text(p)
            header = f"[PDF {rel} — {total} pages, extracted {got}]"
            if total > got:
                header += f"\n\n⚠ Only first {got} pages shown. Ask for specific page range if needed."
            return header + "\n\n" + text

        if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            # Return as vision block
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }[suffix]
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            return [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                },
                {"type": "text", "text": f"[image loaded from {rel}]"},
            ]

        # Text-ish files — read as UTF-8, best effort
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="utf-8", errors="replace")
        # Cap at 200KB to avoid blowing the context
        max_bytes = 200 * 1024
        if len(text) > max_bytes:
            text = text[:max_bytes] + f"\n\n[TRUNCATED at {max_bytes} bytes]"
        return f"[{rel}]\n\n{text}"

    return _h


READ_DOCUMENT_SPEC = lambda data_root: ToolSpec(
    name="read_document",
    description=(
        "Read a file from Liam's data directory. Use for reading context files, "
        "uploads, or source-material PDFs and images. Paths are relative to /data "
        "(e.g. 'context/01_background.md' or 'uploads/pdfs/foo.pdf'). For PDFs, "
        "returns extracted text of the first 20 pages with a truncation note if "
        "longer. For images, returns the image for vision analysis. Do not use for "
        "files outside /data."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path under /data, e.g. 'uploads/pdfs/foo.pdf'",
            }
        },
        "required": ["path"],
    },
    handler=_read_document_handler(data_root),
)


# ---------------------------------------------------------------------------
# list_documents — reads INDEX.md (or generates if missing)
# ---------------------------------------------------------------------------


def _list_documents_handler(data_root: Path) -> callable:
    def _h(args: dict):
        index_path = data_root / "context" / "INDEX.md"
        if not index_path.exists():
            rebuild_index(data_root)
        if not index_path.exists():
            return "INDEX.md is empty — no source-material or uploads yet."
        return index_path.read_text(encoding="utf-8")

    return _h


LIST_DOCUMENTS_SPEC = lambda data_root: ToolSpec(
    name="list_documents",
    description=(
        "List all indexed documents in context/source-material and uploads. "
        "Returns INDEX.md, which is the auto-generated catalog of available files "
        "with one-line descriptions. Use this before read_document when you don't "
        "know what's available."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=_list_documents_handler(data_root),
)


# ---------------------------------------------------------------------------
# search_documents — simple grep
# ---------------------------------------------------------------------------


def _search_documents_handler(data_root: Path) -> callable:
    def _h(args: dict):
        q = args.get("query")
        if not q or not isinstance(q, str):
            raise ToolError("query is required (string)")
        limit = int(args.get("limit") or 20)
        q_lower = q.lower()
        roots = [
            data_root / "context",
            data_root / "uploads",
            data_root / "archives",
        ]
        hits: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    if q_lower in p.name.lower():
                        hits.append(f"{p.relative_to(data_root)} — (binary, filename match)")
                    if len(hits) >= limit:
                        break
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if q_lower not in text.lower():
                    continue
                # Grab a context snippet
                idx = text.lower().find(q_lower)
                start = max(0, idx - 80)
                end = min(len(text), idx + len(q) + 80)
                snippet = text[start:end].replace("\n", " ").strip()
                hits.append(f"{p.relative_to(data_root)}:\n  … {snippet} …")
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        if not hits:
            return f"No matches for {q!r} in /data"
        return "\n\n".join(hits[:limit])

    return _h


SEARCH_DOCUMENTS_SPEC = lambda data_root: ToolSpec(
    name="search_documents",
    description=(
        "Grep-style text search across context/, uploads/, and archives/. "
        "Returns matching files with short snippets. Use for finding things the "
        "index doesn't describe (e.g. search for a specific name or phrase)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive substring to search for"},
            "limit": {"type": "integer", "description": "Max results (default 20)"},
        },
        "required": ["query"],
    },
    handler=_search_documents_handler(data_root),
)


# ---------------------------------------------------------------------------
# INDEX.md generator
# ---------------------------------------------------------------------------


@dataclass
class IndexEntry:
    path: str
    size: int
    description: str


def _one_line_for(path: Path) -> str:
    """Generate a one-line description for a file based on its name/content."""
    suffix = path.suffix.lower()
    name = path.stem.replace("_", " ").replace("-", " ")
    if suffix == ".md":
        # Pull first H1 or first non-empty line
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("# "):
                    return line[2:].strip()
                return line[:120]
        except OSError:
            pass
    if suffix == ".pdf":
        return f"{name} (PDF)"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return f"{name} (image)"
    if suffix == ".txt":
        try:
            first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            return first[:120]
        except (OSError, IndexError):
            pass
    return name


def rebuild_index(data_root: Path) -> Path:
    """Scan context/source-material + uploads/ and write context/INDEX.md."""
    ctx_dir = data_root / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    index_path = ctx_dir / "INDEX.md"

    sections: list[tuple[str, list[IndexEntry]]] = []

    for label, root in [
        ("source-material", ctx_dir / "source-material"),
        ("uploads", data_root / "uploads"),
    ]:
        entries: list[IndexEntry] = []
        if root.exists():
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                if p.name.startswith("."):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                rel = str(p.relative_to(data_root))
                entries.append(
                    IndexEntry(
                        path=rel, size=size, description=_one_line_for(p)
                    )
                )
        sections.append((label, entries))

    lines = ["# INDEX.md", "", "_auto-generated. Lists files available via `read_document`._", ""]
    for label, entries in sections:
        lines.append(f"## {label}")
        lines.append("")
        if not entries:
            lines.append("_(none)_")
            lines.append("")
            continue
        for e in entries:
            lines.append(f"- `{e.path}` ({_human_size(e.size)}) — {e.description}")
        lines.append("")

    content = "\n".join(lines)
    # Atomic write
    tmp = index_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(index_path)
    return index_path


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"
