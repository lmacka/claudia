"""
Unified library for ingested documents (PDFs, DOCXs, pastes, screenshots,
chat exports). One folder per document under /data/library/, with a sidecar
meta.json, the verbatim original, the pre-extracted markdown, and a
verification.json. A manifest.json at the root is the machine-readable index.

Per docs/library-people-plan.md.

Library is the data layer only. Extraction (turning original bytes into
extracted markdown) lives in app/extractors.py. The /library route handlers
in app/main.py orchestrate the two.

All file IO is synchronous (mirrors app/storage.py). Mutations take an
fcntl exclusive lock on /data/library/.locks/<resource>.lock to serialise
concurrent uploads. Manifest writes are atomic via temp + rename.
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
    """The meta.json schema. One per document."""

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
    """
    Owns every read/write under /data/library/.

    Methods are synchronous. Mutations serialise on per-resource fcntl locks.
    Manifest writes are atomic (temp + rename).

    Layout:
      <root>/
        manifest.json
        .locks/
          manifest.lock
          <doc_id>.lock
        <doc_id>/
          meta.json
          original.<ext>
          extracted.md
          verification.json
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.locks_dir = root / ".locks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

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

    # --- path helpers -------------------------------------------------------

    def _doc_dir(self, doc_id: str) -> Path:
        # Defence-in-depth: validate the id even though pydantic does.
        if "/" in doc_id or ".." in doc_id or doc_id.startswith("."):
            raise ValueError(f"unsafe doc id: {doc_id!r}")
        candidate = (self.root / doc_id).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as e:
            raise ValueError(f"doc id escapes library root: {doc_id!r}") from e
        return candidate

    def _meta_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "meta.json"

    def _original_path(self, doc_id: str, ext: str) -> Path:
        ext = ext.lstrip(".")
        return self._doc_dir(doc_id) / f"original.{ext}"

    def _extracted_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "extracted.md"

    def _verification_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "verification.json"

    def _manifest_path(self) -> Path:
        return self.root / "manifest.json"

    # --- id minting ---------------------------------------------------------

    def mint_doc_id(self, title: str) -> str:
        """
        Returns a unique, sortable, filesystem-safe doc id derived from the
        title and current UTC timestamp. Collisions get a numeric suffix.
        """
        base = f"{_utc_stamp()}_{_slugify(title)}"
        with self._lock("manifest"):
            candidate = base
            n = 2
            while (self.root / candidate).exists():
                candidate = f"{base}-{n}"
                n += 1
            return candidate

    # --- atomic write helper ------------------------------------------------

    def _atomic_write(self, path: Path, content: bytes | str) -> None:
        """Write to a sibling .tmp then rename. Caller holds the lock."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if isinstance(content, str):
            tmp.write_text(content, encoding="utf-8")
        else:
            tmp.write_bytes(content)
        os.replace(tmp, path)

    def _write_meta(self, doc_id: str, meta: LibraryDocMeta) -> None:
        payload = meta.model_dump(mode="json")
        self._atomic_write(
            self._meta_path(doc_id),
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        )

    def _read_meta(self, doc_id: str) -> LibraryDocMeta | None:
        path = self._meta_path(doc_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LibraryDocMeta.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.error("library.meta_read_failed", doc_id=doc_id, error=str(e))
            return None

    # --- public CRUD --------------------------------------------------------

    def create_doc(
        self,
        meta: LibraryDocMeta,
        original_bytes: bytes,
        original_ext: str,
        extracted_md: str,
        verification: dict[str, Any],
    ) -> str:
        """
        Atomically create a new document folder and update the manifest.

        Caller has already minted `meta.id` via `mint_doc_id` (or constructs
        one consistently). All four sidecar files are written before manifest
        regen so partial states don't appear in the manifest.
        """
        if meta.size_bytes != len(original_bytes):
            raise ValueError(
                f"meta.size_bytes ({meta.size_bytes}) != len(original_bytes) ({len(original_bytes)})"
            )
        if meta.extracted_chars != len(extracted_md):
            raise ValueError(
                f"meta.extracted_chars ({meta.extracted_chars}) != len(extracted_md) ({len(extracted_md)})"
            )

        with self._lock(f"doc-{meta.id}"):
            doc_dir = self._doc_dir(meta.id)
            if doc_dir.exists():
                raise FileExistsError(f"doc {meta.id} already exists")
            doc_dir.mkdir(parents=True, exist_ok=False)
            self._atomic_write(self._original_path(meta.id, original_ext), original_bytes)
            self._atomic_write(self._extracted_path(meta.id), extracted_md)
            self._atomic_write(
                self._verification_path(meta.id),
                json.dumps(verification, indent=2, sort_keys=True),
            )
            self._write_meta(meta.id, meta)

        self.rebuild_manifest()
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
        path = self._verification_path(doc_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
        results: list[LibraryDocMeta] = []
        if not self.root.exists():
            return results
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            meta = self._read_meta(entry.name)
            if meta is not None:
                results.append(meta)
        results.sort(key=lambda m: m.created_at, reverse=True)
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
        self.rebuild_manifest()
        return patched

    def supersede(self, old_id: str, new_meta: LibraryDocMeta) -> None:
        """
        Mark old as superseded, new as active. The new doc folder must
        already exist (caller created it via create_doc with a `supersedes`
        pointer set on its meta).
        """
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
        self.rebuild_manifest()
        log.info("library.doc_superseded", old=old_id, new=new_meta.id)

    def soft_delete(self, doc_id: str) -> None:
        with self._lock(f"doc-{doc_id}"):
            meta = self._read_meta(doc_id)
            if meta is None:
                raise KeyError(f"doc {doc_id} not found")
            patched = meta.model_copy(update={"status": "deleted"})
            self._write_meta(doc_id, patched)
        self.rebuild_manifest()
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
        self.rebuild_manifest()
        log.info("library.doc_restored", doc_id=doc_id)

    def hard_delete(self, doc_id: str) -> None:
        with self._lock(f"doc-{doc_id}"):
            doc_dir = self._doc_dir(doc_id)
            if not doc_dir.exists():
                raise KeyError(f"doc {doc_id} not found")
            shutil.rmtree(doc_dir)
        self.rebuild_manifest()
        log.info("library.doc_hard_deleted", doc_id=doc_id)

    # --- manifest -----------------------------------------------------------

    def rebuild_manifest(self) -> None:
        """Regenerate /data/library/manifest.json from on-disk meta.json files."""
        with self._lock("manifest"):
            entries = [m.model_dump(mode="json") for m in self.list_all()]
            payload = {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "count": len(entries),
                "entries": entries,
            }
            self._atomic_write(
                self._manifest_path(),
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False),
            )

    # --- INDEX.md rendering -------------------------------------------------

    def render_index_md(self) -> str:
        """
        Markdown rendering of the active manifest, intended to feed the
        companion's system-prompt block 2 (replacing the legacy INDEX.md
        that scanned uploads/).
        """
        active = [m for m in self.list_all() if m.status == "active"]
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
