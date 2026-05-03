"""
Unified library for ingested documents (PDFs, DOCXs, pastes, screenshots,
chat exports). T-NEW-I phase 3: metadata moves to the SQLite library_docs
table; raw original bytes + extracted text + verification still live on the
filesystem under /data/library/<doc_id>/ as blobs.

Per docs/library-people-plan.md.

The public API of this class is preserved verbatim — call sites (the
/library route handlers in app/main.py, the read_document/list_documents/
search_documents tools, library_pipeline) don't change.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator

from app.db import connect, migrate, transaction

log = structlog.get_logger()


DocKind = Literal["pdf", "docx", "doc", "text", "image", "chat_export"]
DocSource = Literal["upload", "paste", "gmail_attachment"]
DocStatus = Literal["active", "superseded", "deleted"]
VerificationStatus = Literal["ok", "warn", "fail"]
DateSource = Literal[
    "pdf_metadata",
    "pdf_text_pattern",
    "docx_core_props",
    "exif",
    "chat_first_message",
    "user_supplied",
    "unknown",
]


class LibraryDocMeta(BaseModel):
    """Schema for one document's metadata. Stored as a row in library_docs
    (with the full payload JSON-serialised in meta_json for forward-compat
    when fields are added)."""

    id: str
    title: str
    kind: DocKind
    source: DocSource
    created_at: datetime  # when uploaded into library
    original_date: _dt.date | None = None
    original_date_source: DateSource | None = None
    date_range_end: _dt.date | None = None  # chat exports only
    size_bytes: int
    mime: str
    page_count: int | None = None
    extractor: str
    extracted_chars: int
    tags: list[str] = Field(default_factory=list)
    status: DocStatus = "active"
    supersedes: str | None = None
    superseded_by: str | None = None
    verification: VerificationStatus = "ok"
    summary: str | None = None
    linked_people: list[str] = Field(default_factory=list)
    # Chat-export only: populated by ChatExportExtractor.
    participants: list[str] | None = None
    message_count: int | None = None

    @field_validator("id")
    @classmethod
    def _id_safe(cls, v: str) -> str:
        if "/" in v or ".." in v or "\\" in v or v.startswith(".") or not v:
            raise ValueError(f"unsafe doc id: {v!r}")
        return v

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title cannot be empty")
        return v.strip()


def _slugify(text: str, max_len: int = 60) -> str:
    """Lowercase + hyphenated. ASCII-only. Truncated. Empty input → 'untitled'."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def _utc_stamp() -> str:
    """ISO 8601 UTC, colon-free for filesystem safety: 2026-04-25T12-30-45Z."""
    now = datetime.now(UTC).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


class Library:
    """SQLite-backed library (T-NEW-I phase 3).

    Metadata lives in the library_docs table; raw bytes / extracted text /
    verification still live on the filesystem under <root>/<doc_id>/ as blobs.
    The public API matches the previous file-based implementation so call
    sites don't have to change.

    Layout on disk (blobs only):
      <root>/
        <doc_id>/
          original.<ext>    # raw bytes
          extracted.md      # extracted text
        .locks/             # for blob writes only

    The DB lives at <data_root>/claudia.db (parent of <root>). __init__
    derives data_root from `root.parent` to call migrate().
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.locks_dir = root / ".locks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        # The DB lives in the data root (parent of the library folder).
        self._data_root = root.parent
        migrate(self._data_root)

    # --- lock helper --------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self, resource: str) -> Iterator[None]:
        lock_path = self.locks_dir / f"{resource}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # --- path helpers (for blobs only — meta lives in DB) -------------------

    def _doc_dir(self, doc_id: str) -> Path:
        if "/" in doc_id or ".." in doc_id or doc_id.startswith("."):
            raise ValueError(f"unsafe doc id: {doc_id!r}")
        candidate = (self.root / doc_id).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as e:
            raise ValueError(f"doc id escapes library root: {doc_id!r}") from e
        return candidate

    def _original_path(self, doc_id: str, ext: str) -> Path:
        ext = ext.lstrip(".")
        return self._doc_dir(doc_id) / f"original.{ext}"

    def _extracted_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "extracted.md"

    # --- DB helpers ---------------------------------------------------------

    def _row_to_meta(self, row) -> LibraryDocMeta | None:
        try:
            data = json.loads(row["meta_json"])
            return LibraryDocMeta.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("library.meta_parse_failed", doc_id=row["id"], error=str(e))
            return None

    def _read_meta(self, doc_id: str) -> LibraryDocMeta | None:
        with connect(self._data_root) as db:
            row = db.execute(
                "SELECT * FROM library_docs WHERE id = ?", (doc_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_meta(row)

    def _write_meta(self, doc_id: str, meta: LibraryDocMeta) -> None:
        payload = meta.model_dump(mode="json")
        meta_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with connect(self._data_root) as db, transaction(db):
            db.execute(
                """INSERT INTO library_docs (
                    id, title, kind, source, created_at, original_date,
                    original_date_source, date_range_end, size_bytes, mime,
                    page_count, extractor, extracted_chars, status, supersedes,
                    superseded_by, verification, meta_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    source=excluded.source,
                    created_at=excluded.created_at,
                    original_date=excluded.original_date,
                    original_date_source=excluded.original_date_source,
                    date_range_end=excluded.date_range_end,
                    size_bytes=excluded.size_bytes,
                    mime=excluded.mime,
                    page_count=excluded.page_count,
                    extractor=excluded.extractor,
                    extracted_chars=excluded.extracted_chars,
                    status=excluded.status,
                    supersedes=excluded.supersedes,
                    superseded_by=excluded.superseded_by,
                    verification=excluded.verification,
                    meta_json=excluded.meta_json""",
                (
                    meta.id,
                    meta.title,
                    meta.kind,
                    meta.source,
                    meta.created_at.isoformat(),
                    meta.original_date.isoformat() if meta.original_date else None,
                    meta.original_date_source,
                    meta.date_range_end.isoformat() if meta.date_range_end else None,
                    meta.size_bytes,
                    meta.mime,
                    meta.page_count,
                    meta.extractor,
                    meta.extracted_chars,
                    meta.status,
                    meta.supersedes,
                    meta.superseded_by,
                    meta.verification,
                    meta_json,
                ),
            )

    # --- id minting ---------------------------------------------------------

    def mint_doc_id(self, title: str) -> str:
        """Returns a unique, sortable, filesystem-safe doc id derived from
        the title and current UTC timestamp. Collisions are resolved by
        checking both the DB and the on-disk doc directory."""
        base = f"{_utc_stamp()}_{_slugify(title)}"
        candidate = base
        n = 2
        with self._lock("manifest"), connect(self._data_root) as db:
            while True:
                exists_in_db = db.execute(
                    "SELECT 1 FROM library_docs WHERE id = ?", (candidate,)
                ).fetchone()
                exists_on_disk = (self.root / candidate).exists()
                if not exists_in_db and not exists_on_disk:
                    return candidate
                candidate = f"{base}-{n}"
                n += 1

    # --- atomic write helper for blobs --------------------------------------

    def _atomic_write(self, path: Path, content: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if isinstance(content, str):
            tmp.write_text(content, encoding="utf-8")
        else:
            tmp.write_bytes(content)
        os.replace(tmp, path)

    # --- public CRUD --------------------------------------------------------

    def create_doc(
        self,
        meta: LibraryDocMeta,
        original_bytes: bytes,
        original_ext: str,
        extracted_md: str,
        verification: dict[str, Any],
    ) -> str:
        if meta.size_bytes != len(original_bytes):
            raise ValueError(
                f"meta.size_bytes ({meta.size_bytes}) != len(original_bytes) ({len(original_bytes)})"
            )
        if meta.extracted_chars != len(extracted_md):
            raise ValueError(
                f"meta.extracted_chars ({meta.extracted_chars}) != len(extracted_md) ({len(extracted_md)})"
            )

        with self._lock(f"doc-{meta.id}"):
            # Row uniqueness check + blob folder creation
            with connect(self._data_root) as db:
                existing = db.execute(
                    "SELECT 1 FROM library_docs WHERE id = ?", (meta.id,)
                ).fetchone()
            if existing is not None:
                raise FileExistsError(f"doc {meta.id} already exists")
            doc_dir = self._doc_dir(meta.id)
            if doc_dir.exists():
                # Stale blob folder from a prior failed write — clean it up
                # so create_doc is idempotent against partial state.
                shutil.rmtree(doc_dir)
            doc_dir.mkdir(parents=True, exist_ok=False)
            self._atomic_write(self._original_path(meta.id, original_ext), original_bytes)
            self._atomic_write(self._extracted_path(meta.id), extracted_md)
            # Verification stored in the DB column (small JSON).
            verification_json = json.dumps(verification, sort_keys=True)
            payload = meta.model_dump(mode="json")
            meta_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with connect(self._data_root) as db, transaction(db):
                db.execute(
                    """INSERT INTO library_docs (
                        id, title, kind, source, created_at, original_date,
                        original_date_source, date_range_end, size_bytes, mime,
                        page_count, extractor, extracted_chars, status, supersedes,
                        superseded_by, verification, verification_json, meta_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        meta.id,
                        meta.title,
                        meta.kind,
                        meta.source,
                        meta.created_at.isoformat(),
                        meta.original_date.isoformat() if meta.original_date else None,
                        meta.original_date_source,
                        meta.date_range_end.isoformat() if meta.date_range_end else None,
                        meta.size_bytes,
                        meta.mime,
                        meta.page_count,
                        meta.extractor,
                        meta.extracted_chars,
                        meta.status,
                        meta.supersedes,
                        meta.superseded_by,
                        meta.verification,
                        verification_json,
                        meta_json,
                    ),
                )

        log.info(
            "library.doc_created",
            doc_id=meta.id,
            kind=meta.kind,
            source=meta.source,
            extracted_chars=meta.extracted_chars,
            verification=meta.verification,
        )
        return meta.id

    def get(self, doc_id: str) -> LibraryDocMeta | None:
        return self._read_meta(doc_id)

    def get_extracted(self, doc_id: str) -> str | None:
        path = self._extracted_path(doc_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def get_verification(self, doc_id: str) -> dict[str, Any] | None:
        with connect(self._data_root) as db:
            row = db.execute(
                "SELECT verification_json FROM library_docs WHERE id = ?", (doc_id,)
            ).fetchone()
        if row is None or row["verification_json"] is None:
            return None
        try:
            return json.loads(row["verification_json"])
        except json.JSONDecodeError:
            return None

    def get_original_path(self, doc_id: str) -> Path | None:
        """Returns the path to the original.* file, whichever extension."""
        doc_dir = self._doc_dir(doc_id)
        if not doc_dir.exists():
            return None
        for child in doc_dir.iterdir():
            if child.is_file() and child.name.startswith("original."):
                return child
        return None

    def list_all(self) -> list[LibraryDocMeta]:
        """Every document, regardless of status. Sorted by created_at desc."""
        with connect(self._data_root) as db:
            rows = db.execute(
                "SELECT * FROM library_docs ORDER BY created_at DESC"
            ).fetchall()
        results: list[LibraryDocMeta] = []
        for row in rows:
            meta = self._row_to_meta(row)
            if meta is not None:
                results.append(meta)
        return results

    def list_active(self) -> list[LibraryDocMeta]:
        return [m for m in self.list_all() if m.status == "active"]

    def list_archived(self) -> list[LibraryDocMeta]:
        return [m for m in self.list_all() if m.status in ("superseded", "deleted")]

    def update_meta(self, doc_id: str, **fields: Any) -> LibraryDocMeta:
        """Partial update — the caller passes only the fields to change."""
        with self._lock(f"doc-{doc_id}"):
            current = self._read_meta(doc_id)
            if current is None:
                raise KeyError(f"doc {doc_id} not found")
            patched = current.model_copy(update=fields)
            self._write_meta(doc_id, patched)
        return patched

    def supersede(self, old_id: str, new_meta: LibraryDocMeta) -> None:
        if new_meta.supersedes != old_id:
            raise ValueError(
                f"new doc.supersedes ({new_meta.supersedes!r}) does not match old_id ({old_id!r})"
            )
        with self._lock(f"doc-{old_id}"):
            old = self._read_meta(old_id)
            if old is None:
                raise KeyError(f"doc {old_id} not found")
            patched = old.model_copy(update={"status": "superseded", "superseded_by": new_meta.id})
            self._write_meta(old_id, patched)
        log.info("library.doc_superseded", old=old_id, new=new_meta.id)

    def soft_delete(self, doc_id: str) -> None:
        with self._lock(f"doc-{doc_id}"):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise KeyError(f"doc {doc_id} not found")
            patched = meta.model_copy(update={"status": "deleted"})
            self._write_meta(doc_id, patched)
        log.info("library.doc_soft_deleted", doc_id=doc_id)

    def restore(self, doc_id: str) -> None:
        with self._lock(f"doc-{doc_id}"):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise KeyError(f"doc {doc_id} not found")
            if meta.status != "deleted":
                raise ValueError(f"doc {doc_id} is not in deleted state")
            patched = meta.model_copy(update={"status": "active"})
            self._write_meta(doc_id, patched)
        log.info("library.doc_restored", doc_id=doc_id)

    def hard_delete(self, doc_id: str) -> None:
        with self._lock(f"doc-{doc_id}"):
            with connect(self._data_root) as db, transaction(db):
                cur = db.execute("DELETE FROM library_docs WHERE id = ?", (doc_id,))
                if cur.rowcount == 0:
                    raise KeyError(f"doc {doc_id} not found")
            doc_dir = self._doc_dir(doc_id)
            if doc_dir.exists():
                shutil.rmtree(doc_dir)
        log.info("library.doc_hard_deleted", doc_id=doc_id)

    # --- manifest (no-op back-compat) --------------------------------------

    def rebuild_manifest(self) -> None:
        """No-op since v0.7.0 — the DB is the manifest. Kept as a stub so
        legacy callers (and tests) don't break. A concurrent reader would
        previously have read the manifest.json snapshot; SQLite reads see the
        latest state directly."""
        return

    # --- INDEX.md rendering -------------------------------------------------

    def render_index_md(self) -> str:
        """Markdown rendering of the active manifest, intended to feed the
        companion's system-prompt block 2."""
        active = self.list_active()
        if not active:
            return "# INDEX.md\n\n(library is empty — no documents yet)"
        lines = ["# INDEX.md", ""]
        for meta in active:
            date_str = (
                meta.original_date.isoformat()
                if meta.original_date
                else "date unknown"
            )
            tags_str = f" — {', '.join(meta.tags)}" if meta.tags else ""
            verif = ""
            if meta.verification == "warn":
                verif = " (extraction warning)"
            elif meta.verification == "fail":
                verif = " (extraction failed)"
            lines.append(
                f"- **{meta.title}** ({date_str}, {meta.kind}){tags_str}{verif}\n"
                f"  - id: `{meta.id}`"
            )
        lines.append("")
        return "\n".join(lines)
