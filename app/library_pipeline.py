"""
Orchestrator that runs the extractor pipeline against a fresh upload/paste
and creates a library document.

This is the synchronous version (commit D). Commit E adds an async wrapper
that streams status messages via SSE.
"""

from __future__ import annotations

import mimetypes
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.extractors import Emit, ExtractorRegistry, _noop_emit
from app.library import DocSource, Library, LibraryDocMeta

log = structlog.get_logger()


# A few extractor-kind strings need title hints derived from extra_meta.
def _refine_title(
    base_title: str,
    extractor_kind: str,
    extra_meta: dict,
) -> str:
    if extractor_kind == "chat_export":
        participants = extra_meta.get("participants") or []
        if participants:
            people = ", ".join(participants[:3])
            extra = f" +{len(participants) - 3}" if len(participants) > 3 else ""
            first = extra_meta.get("first_message_date")
            last = extra_meta.get("last_message_date")
            if first and last:
                return f"Chat — {people}{extra} — {first} → {last}"[:140]
            return f"Chat — {people}{extra}"[:140]
    return base_title


def _ext_for(filename: str | None, mime: str) -> str:
    if filename:
        suf = Path(filename).suffix.lstrip(".").lower()
        if suf:
            return suf
    guessed = mimetypes.guess_extension(mime or "") or ""
    return guessed.lstrip(".").lower() or "bin"


def process_doc_creation(
    library: Library,
    registry: ExtractorRegistry,
    *,
    title: str,
    original_bytes: bytes,
    filename: str | None,
    mime: str,
    source: DocSource,
    supersedes: str | None = None,
    tags: list[str] | None = None,
    emit: Emit = _noop_emit,
) -> str:
    """
    End-to-end: pick extractor → extract → verify → detect_date → create_doc.

    Returns the new doc_id. Raises if the registry has no handler or the
    extractor fails outright. Verification warnings are recorded in the
    doc's meta.json (status: 'warn') but do not raise.
    """
    if not original_bytes:
        raise ValueError("original_bytes is empty")
    ext = _ext_for(filename, mime)

    emit(f"Uploaded — {len(original_bytes)} bytes, {mime or 'unknown mime'}")

    # The extractors take a Path. Stage to a tempfile so they can use
    # path-based libraries (pypdf / python-docx / Pillow).
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=f".{ext}", prefix="claudia-lib-"
    ) as tmp:
        tmp.write(original_bytes)
        tmp_path = Path(tmp.name)
    try:
        emit("Detecting type…")
        extractor = registry.pick(tmp_path, mime)
        if extractor is None:
            raise ValueError(f"no extractor matches mime={mime!r} ext={ext!r}")

        extract = extractor.extract(tmp_path, emit)
        emit("Detecting date…")
        date_detection = extractor.detect_date(tmp_path)
        emit("Building index…")
        verify = extractor.verify(tmp_path, extract, emit)
        emit(f"Sanity check: {verify.status}")

        refined_title = _refine_title(title, extractor.kind, extract.extra_meta)

        meta = LibraryDocMeta(
            id=library.mint_doc_id(refined_title),
            title=refined_title,
            kind=extractor.kind,  # kind matches DocKind literal by construction
            source=source,
            created_at=datetime.now(UTC),
            original_date=date_detection.date,
            original_date_source=(
                date_detection.source
                if date_detection.source != "unknown" or date_detection.date is not None
                else "unknown"
            ),
            date_range_end=_parse_iso_date(extract.extra_meta.get("last_message_date")),
            size_bytes=len(original_bytes),
            mime=mime or "application/octet-stream",
            page_count=extract.page_count,
            extractor=extract.extractor,
            extracted_chars=len(extract.extracted_md),
            tags=tags or [],
            verification=verify.status,
            supersedes=supersedes,
            participants=extract.extra_meta.get("participants"),
            message_count=extract.extra_meta.get("message_count"),
        )
        verification_payload = {
            "status": verify.status,
            "checks": verify.checks,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        doc_id = library.create_doc(
            meta, original_bytes, ext, extract.extracted_md, verification_payload
        )
        emit(f"Done — {meta.title}")
        return doc_id
    finally:
        tmp_path.unlink(missing_ok=True)


def _parse_iso_date(value):
    """Helper for chat-export date_range_end. Accepts None / '' / 'YYYY-MM-DD'."""
    if not value:
        return None
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
