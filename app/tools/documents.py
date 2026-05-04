"""
Document tools: read_document, list_documents, search_documents.

Rewritten in commit H against the unified Library (app/library.py). The
legacy uploads/ filesystem path is supported for backwards compatibility
during the migration window — read_document accepts either a doc_id
('2026-04-25T12-30-45Z_dc-diagnostic') or a path ('uploads/pdfs/foo.pdf').

INDEX.md is no longer generated to disk. Block 2 renders it dynamically
from the Library manifest at request time (see app/context.py).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import structlog

from app.library import Library
from app.tools.registry import ToolError, ToolSpec

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Safety — make sure tool callers can't escape the data root
# ---------------------------------------------------------------------------


_LEGACY_PREFIXES = ("uploads/", "context/", "archives/", "session-exports/")


def _safe_resolve(data_root: Path, rel_path: str) -> Path:
    rel = rel_path.lstrip("/").lstrip("\\")
    if rel.startswith("data/"):
        rel = rel[len("data/") :]
    candidate = (data_root / rel).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError as e:
        raise ToolError(f"path {rel_path!r} is outside /data") from e
    return candidate


def _looks_like_legacy_path(s: str) -> bool:
    s = s.lstrip("/").lstrip("\\")
    if s.startswith("data/"):
        s = s[len("data/") :]
    return any(s.startswith(prefix) for prefix in _LEGACY_PREFIXES)


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------


_READ_CAP_BYTES = 1024 * 1024  # 1MB per spec; up from the legacy 200KB.

_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _parse_pages_range(value: str | None, total: int | None) -> tuple[int, int] | None:
    """Parse 'N-M' inclusive (1-indexed) into (start, end) or return None."""
    if not value:
        return None
    parts = value.replace(" ", "").split("-")
    try:
        if len(parts) == 1:
            start = end = int(parts[0])
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except ValueError:
        raise ToolError(f"pages must be N or N-M, got {value!r}")
    if start < 1 or end < start:
        raise ToolError(f"pages range invalid: {value!r}")
    if total is not None and start > total:
        raise ToolError(f"pages start {start} > total {total}")
    return start, end


def _slice_by_pages(extracted: str, pages: tuple[int, int]) -> str:
    """Slice 'extracted.md' (which has '## Page N' markers from the PdfExtractor)
    to the requested page range. Falls back to whole content if no markers."""
    import re as _re

    if "## Page " not in extracted:
        return extracted
    # Split keeping the headers attached to their page text.
    parts = _re.split(r"(?m)^## Page (\d+)\n", extracted)
    # parts: [prefix, "1", "<text>", "2", "<text>", ...]
    out: list[str] = []
    for i in range(1, len(parts), 2):
        try:
            page_n = int(parts[i])
        except ValueError:
            continue
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if pages[0] <= page_n <= pages[1]:
            out.append(f"## Page {page_n}\n{body}")
    return "\n".join(out) if out else extracted


def _format_doc_for_model(library: Library, doc_id: str, pages: str | None) -> object:
    """Render a library doc for read_document."""
    meta = library.get(doc_id)
    if meta is None:
        raise ToolError(f"doc {doc_id!r} not found")

    # Image kind: return vision block of the original.<ext>.
    if meta.kind == "image":
        original = library.get_original_path(doc_id)
        if original is None:
            raise ToolError(f"doc {doc_id!r} has no original file")
        suffix = original.suffix.lower()
        mime = _IMAGE_MIME_BY_SUFFIX.get(suffix, "image/png")
        b64 = base64.b64encode(original.read_bytes()).decode("ascii")
        date_line = (
            f"Original date: {meta.original_date.isoformat()} (from {meta.original_date_source})"
            if meta.original_date
            else f"Original date: unknown — uploaded {meta.created_at.date().isoformat()}"
        )
        return [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": f"[{meta.title}] {date_line}"},
        ]

    extracted = library.get_extracted(doc_id) or ""
    page_range = _parse_pages_range(pages, meta.page_count)
    if page_range is not None:
        extracted = _slice_by_pages(extracted, page_range)

    # 1MB cap with truncation note.
    if len(extracted) > _READ_CAP_BYTES:
        extracted = extracted[:_READ_CAP_BYTES] + (
            f"\n\n[TRUNCATED at {_READ_CAP_BYTES} bytes — use pages: 'N-M' to fetch a slice]"
        )

    date_line = (
        f"> Original date: {meta.original_date.isoformat()} (from {meta.original_date_source})"
        if meta.original_date
        else f"> Original date: unknown — uploaded {meta.created_at.date().isoformat()}"
    )
    header = (
        f"# {meta.title}\n"
        f"_{meta.kind} · `{doc_id}`_\n"
        f"{date_line}\n"
    )
    if page_range is not None:
        header += f"_pages: {page_range[0]}-{page_range[1]}_\n"
    return f"{header}\n{extracted}"


def _read_document_handler(library: Library, data_root: Path) -> Callable[[dict], object]:
    def _h(args: dict) -> object:
        ident = args.get("path") or args.get("id_or_path") or args.get("id")
        if not ident or not isinstance(ident, str):
            raise ToolError("path / id_or_path is required (string)")
        ident = ident.strip()
        pages = args.get("pages")
        if pages is not None and not isinstance(pages, str):
            raise ToolError("pages must be a string (e.g. '5-12')")

        # Library doc_id first.
        if not _looks_like_legacy_path(ident) and library.get(ident) is not None:
            return _format_doc_for_model(library, ident, pages)

        # Legacy filesystem path.
        p = _safe_resolve(data_root, ident)
        if not p.exists():
            raise ToolError(f"not found as doc_id or path: {ident}")
        if p.is_dir():
            raise ToolError(f"path is a directory: {ident}")

        suffix = p.suffix.lower()
        if suffix in _IMAGE_MIME_BY_SUFFIX:
            mime = _IMAGE_MIME_BY_SUFFIX[suffix]
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            return [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": f"[image loaded from {ident}]"},
            ]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"read failed: {e}") from e
        if len(text) > _READ_CAP_BYTES:
            text = text[:_READ_CAP_BYTES] + f"\n\n[TRUNCATED at {_READ_CAP_BYTES} bytes]"
        return f"[{ident}]\n\n{text}"

    return _h


READ_DOCUMENT_SPEC = lambda library, data_root: ToolSpec(  # noqa: E731
    name="read_document",
    description=(
        "Read a document. Accepts EITHER a library doc_id (e.g. "
        "'2026-04-25T12-30-45Z_dc-diagnostic') OR a legacy filesystem path "
        "(e.g. 'context/01_background.md'). For library docs, returns the "
        "pre-extracted markdown with an `Original date:` header line; "
        "use `pages: 'N-M'` to slice big PDFs. For images, returns the "
        "image as a vision block. 1MB read cap with truncation note."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Library doc_id OR legacy path under /data",
            },
            "pages": {
                "type": "string",
                "description": "Optional page slice for paginated docs, e.g. '5-12' or '1' (1-indexed, inclusive).",
            },
        },
        "required": ["path"],
    },
    handler=_read_document_handler(library, data_root),
)


# ---------------------------------------------------------------------------
# list_documents — returns library.render_index_md
# ---------------------------------------------------------------------------


def _list_documents_handler(library: Library) -> Callable[[dict], str]:
    def _h(_args: dict) -> str:
        return library.render_index_md()

    return _h


LIST_DOCUMENTS_SPEC = lambda library: ToolSpec(  # noqa: E731
    name="list_documents",
    description=(
        "List all active library documents as markdown. Each entry shows "
        "title, original date (when known), kind, and tags. Use this when "
        "you need to know what documents exist; the same list is in block 2 "
        "of your system prompt at session start."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_list_documents_handler(library),
)


# ---------------------------------------------------------------------------
# search_documents — grep library/*/extracted.md
# ---------------------------------------------------------------------------


def _search_documents_handler(library: Library) -> Callable[[dict], str]:
    def _h(args: dict) -> str:
        q = args.get("query")
        if not q or not isinstance(q, str):
            raise ToolError("query is required (string)")
        limit = int(args.get("limit") or 20)
        q_lower = q.lower()
        hits: list[str] = []
        for meta in library.list_active():
            extracted = library.get_extracted(meta.id)
            if extracted is None:
                continue
            haystack = extracted.lower()
            idx = haystack.find(q_lower)
            if idx < 0:
                # Title match still counts.
                if q_lower in meta.title.lower():
                    hits.append(f"`{meta.id}` — {meta.title} (title match)")
                    if len(hits) >= limit:
                        break
                continue
            start = max(0, idx - 80)
            end = min(len(extracted), idx + len(q) + 80)
            snippet = extracted[start:end].replace("\n", " ").strip()
            hits.append(f"`{meta.id}` — {meta.title}\n  … {snippet} …")
            if len(hits) >= limit:
                break
        if not hits:
            return f"No matches for {q!r} in the library."
        return "\n\n".join(hits[:limit])

    return _h


SEARCH_DOCUMENTS_SPEC = lambda library: ToolSpec(  # noqa: E731
    name="search_documents",
    description=(
        "Substring search across the extracted text of every active library "
        "document. Returns matching doc_ids with snippets. Use to find "
        "specific names, dates, or phrases when the index alone isn't enough."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive substring."},
            "limit": {"type": "integer", "description": "Max results (default 20)."},
        },
        "required": ["query"],
    },
    handler=_search_documents_handler(library),
)


