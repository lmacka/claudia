"""
People store: structured records of the people in the user's life that the
companion + auditor reference. T-NEW-I phase 3: metadata moves to the SQLite
people table; notes.md still lives on the filesystem under
/data/people/<person_id>/.

Per docs/library-people-plan.md.

The public API is preserved verbatim — the /people route handlers, the
list_people / lookup_person / search_people tools, and the auditor's
people_updates application all continue to work without changes.
"""

from __future__ import annotations

import contextlib
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


PersonCategory = Literal[
    "co-parent",
    "family",
    "partner",
    "friend",
    "professional",
    "child",
    "colleague",
    "other",
]
PersonStatus = Literal["active", "archived"]


class PersonMeta(BaseModel):
    """Schema for one person. Stored as a row in the people table; the full
    payload is also serialised to meta_json for forward-compat."""

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    category: PersonCategory = "other"
    relationship: str = ""
    summary: str = ""
    important_context: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    linked_documents: list[str] = Field(default_factory=list)
    first_seen: datetime | None = None
    last_mentioned: datetime | None = None
    status: PersonStatus = "active"
    created_at: datetime
    updated_at: datetime

    @field_validator("id")
    @classmethod
    def _id_safe(cls, v: str) -> str:
        if "/" in v or ".." in v or "\\" in v or v.startswith(".") or not v:
            raise ValueError(f"unsafe person id: {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be empty")
        return v.strip()


def _name_slug(name: str) -> str:
    """Lowercase + hyphenated, ASCII-only. 'Rhiannon O''Hara' → 'rhiannon-ohara'."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unnamed"


def _levenshtein(a: str, b: str) -> int:
    """Small edit-distance for near-name match in auditor proposals."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class People:
    """SQLite-backed people store (T-NEW-I phase 3).

    Metadata in the people table; notes.md still on disk under <root>/<id>/
    (notes can grow large and are append-friendly without DB churn). Manifest
    rebuild is a no-op — the table IS the manifest.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.locks_dir = root / ".locks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self._data_root = root.parent
        migrate(self._data_root)

    # --- locks --------------------------------------------------------------

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

    # --- paths (notes blob only — meta lives in DB) -------------------------

    def _person_dir(self, person_id: str) -> Path:
        if "/" in person_id or ".." in person_id or person_id.startswith("."):
            raise ValueError(f"unsafe person id: {person_id!r}")
        candidate = (self.root / person_id).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as e:
            raise ValueError(f"person id escapes people root: {person_id!r}") from e
        return candidate

    def _notes_path(self, person_id: str) -> Path:
        return self._person_dir(person_id) / "notes.md"

    # --- atomic write -------------------------------------------------------

    def _atomic_write(self, path: Path, content: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if isinstance(content, str):
            tmp.write_text(content, encoding="utf-8")
        else:
            tmp.write_bytes(content)
        os.replace(tmp, path)

    # --- DB helpers ---------------------------------------------------------

    def _row_to_meta(self, row) -> PersonMeta | None:
        try:
            data = json.loads(row["meta_json"])
            return PersonMeta.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("people.meta_parse_failed", person_id=row["id"], error=str(e))
            return None

    def _read_meta(self, person_id: str) -> PersonMeta | None:
        with connect(self._data_root) as db:
            row = db.execute(
                "SELECT * FROM people WHERE id = ?", (person_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_meta(row)

    def _write_meta(self, person_id: str, meta: PersonMeta) -> None:
        payload = meta.model_dump(mode="json")
        meta_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with connect(self._data_root) as db, transaction(db):
            db.execute(
                """INSERT INTO people (
                    id, name, category, status, last_mentioned, meta_json
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    category=excluded.category,
                    status=excluded.status,
                    last_mentioned=excluded.last_mentioned,
                    meta_json=excluded.meta_json""",
                (
                    meta.id,
                    meta.name,
                    meta.category,
                    meta.status,
                    meta.last_mentioned.isoformat() if meta.last_mentioned else None,
                    meta_json,
                ),
            )

    # --- id minting ---------------------------------------------------------

    def mint_id(self, name: str) -> str:
        """Slug + collision-handled. Unique across active+archived."""
        base = _name_slug(name)
        candidate = base
        n = 2
        with self._lock("manifest"), connect(self._data_root) as db:
            while True:
                exists_in_db = db.execute(
                    "SELECT 1 FROM people WHERE id = ?", (candidate,)
                ).fetchone()
                exists_on_disk = (self.root / candidate).exists()
                if not exists_in_db and not exists_on_disk:
                    return candidate
                candidate = f"{base}-{n}"
                n += 1

    # --- public CRUD --------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        category: PersonCategory = "other",
        relationship: str = "",
        summary: str = "",
        important_context: list[str] | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        notes: str = "",
    ) -> str:
        person_id = self.mint_id(name)
        now = datetime.now(UTC)
        meta = PersonMeta(
            id=person_id,
            name=name,
            aliases=aliases or [],
            category=category,
            relationship=relationship,
            summary=summary,
            important_context=important_context or [],
            tags=tags or [],
            first_seen=now,
            created_at=now,
            updated_at=now,
        )
        with self._lock(f"person-{person_id}"):
            # Notes file lives on disk; meta in DB.
            self._person_dir(person_id).mkdir(parents=True, exist_ok=True)
            self._atomic_write(self._notes_path(person_id), notes)
            self._write_meta(person_id, meta)
        log.info("people.person_added", person_id=person_id, name=name)
        return person_id

    def get(self, person_id: str) -> PersonMeta | None:
        return self._read_meta(person_id)

    def get_notes(self, person_id: str) -> str | None:
        path = self._notes_path(person_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_all(self) -> list[PersonMeta]:
        with connect(self._data_root) as db:
            rows = db.execute(
                "SELECT * FROM people ORDER BY LOWER(name) ASC"
            ).fetchall()
        results: list[PersonMeta] = []
        for row in rows:
            meta = self._row_to_meta(row)
            if meta is not None:
                results.append(meta)
        return results

    def list_active(self) -> list[PersonMeta]:
        return [p for p in self.list_all() if p.status == "active"]

    def list_archived(self) -> list[PersonMeta]:
        return [p for p in self.list_all() if p.status == "archived"]

    def update(self, person_id: str, **fields: Any) -> PersonMeta:
        with self._lock(f"person-{person_id}"):
            current = self._read_meta(person_id)
            if current is None:
                raise KeyError(f"person {person_id} not found")
            patched = current.model_copy(update={**fields, "updated_at": datetime.now(UTC)})
            self._write_meta(person_id, patched)
        return patched

    def replace_notes(self, person_id: str, content: str) -> None:
        with self._lock(f"person-{person_id}"):
            if self._read_meta(person_id) is None:
                raise KeyError(f"person {person_id} not found")
            self._person_dir(person_id).mkdir(parents=True, exist_ok=True)
            self._atomic_write(self._notes_path(person_id), content)
            self.update_silent(person_id, updated_at=datetime.now(UTC))
        log.info("people.notes_replaced", person_id=person_id, chars=len(content))

    def append_note(self, person_id: str, addition: str) -> None:
        """Auditor-friendly: append a paragraph to notes.md."""
        with self._lock(f"person-{person_id}"):
            if self._read_meta(person_id) is None:
                raise KeyError(f"person {person_id} not found")
            self._person_dir(person_id).mkdir(parents=True, exist_ok=True)
            existing = self.get_notes(person_id) or ""
            stamp = datetime.now(UTC).strftime("%Y-%m-%d")
            block = (
                ("\n\n" if existing and not existing.endswith("\n\n") else "")
                + f"_(auditor, {stamp}):_ {addition.strip()}\n"
            )
            self._atomic_write(self._notes_path(person_id), existing + block)
            self.update_silent(person_id, updated_at=datetime.now(UTC))

    def update_silent(self, person_id: str, **fields: Any) -> None:
        """Like update() but skips logging. Used internally to bump
        updated_at without a separate log entry."""
        current = self._read_meta(person_id)
        if current is None:
            raise KeyError(f"person {person_id} not found")
        patched = current.model_copy(update=fields)
        self._write_meta(person_id, patched)

    def link_doc(self, person_id: str, doc_id: str) -> None:
        with self._lock(f"person-{person_id}"):
            current = self._read_meta(person_id)
            if current is None:
                raise KeyError(f"person {person_id} not found")
            if doc_id in current.linked_documents:
                return
            patched = current.model_copy(
                update={
                    "linked_documents": current.linked_documents + [doc_id],
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_meta(person_id, patched)

    def unlink_doc(self, person_id: str, doc_id: str) -> None:
        with self._lock(f"person-{person_id}"):
            current = self._read_meta(person_id)
            if current is None:
                raise KeyError(f"person {person_id} not found")
            if doc_id not in current.linked_documents:
                return
            new_links = [d for d in current.linked_documents if d != doc_id]
            patched = current.model_copy(
                update={"linked_documents": new_links, "updated_at": datetime.now(UTC)}
            )
            self._write_meta(person_id, patched)

    def touch(self, person_id: str) -> None:
        """Bump last_mentioned. Used by lookup_person tool."""
        with self._lock(f"person-{person_id}"):
            current = self._read_meta(person_id)
            if current is None:
                raise KeyError(f"person {person_id} not found")
            now = datetime.now(UTC)
            patched = current.model_copy(update={"last_mentioned": now, "updated_at": now})
            self._write_meta(person_id, patched)

    def archive(self, person_id: str) -> None:
        self.update(person_id, status="archived")

    def restore(self, person_id: str) -> None:
        meta = self.get(person_id)
        if meta is None:
            raise KeyError(f"person {person_id} not found")
        if meta.status != "archived":
            raise ValueError(f"person {person_id} is not archived")
        self.update(person_id, status="active")

    def delete(self, person_id: str) -> None:
        """Hard delete — no soft tier per spec (people records are small,
        archive is enough soft-delete)."""
        with self._lock(f"person-{person_id}"):
            with connect(self._data_root) as db, transaction(db):
                cur = db.execute("DELETE FROM people WHERE id = ?", (person_id,))
                if cur.rowcount == 0:
                    raise KeyError(f"person {person_id} not found")
            person_dir = self._person_dir(person_id)
            if person_dir.exists():
                shutil.rmtree(person_dir)
        log.info("people.person_deleted", person_id=person_id)

    # --- search -------------------------------------------------------------

    def find_near_match(self, name: str, max_distance: int = 2) -> PersonMeta | None:
        """Find an existing person whose name or alias is within Levenshtein
        distance of `name`. Used by auditor proposals to merge alias on
        near-match instead of creating duplicates.

        v0.8.1: scale the effective max distance with name length so short
        names need exact matches. With max_distance=2 unscaled, "Rhi" and
        "Bri" matched (distance 2 = subst R→B + h→r), merging two distinct
        people. The new heuristic:
            len <= 3:  effective max = 0 (exact match only)
            len == 4:  effective max = 1 (single typo)
            len >= 5:  effective max = min(2, max_distance)
        Names this short are usually nicknames where one off-character
        means a different person, not a typo.
        """
        target = name.strip().lower()
        if not target:
            return None
        best: tuple[int, PersonMeta] | None = None
        for meta in self.list_all():
            candidates = [meta.name.lower()] + [a.lower() for a in meta.aliases]
            for cand in candidates:
                # Effective threshold: tighter for short names.
                shorter = min(len(target), len(cand))
                if shorter <= 3:
                    effective = 0
                elif shorter == 4:
                    effective = 1
                else:
                    effective = min(2, max_distance)
                d = _levenshtein(target, cand)
                if d <= effective and (best is None or d < best[0]):
                    best = (d, meta)
        return best[1] if best else None

    def search(self, query: str) -> list[PersonMeta]:
        """Substring match across name, aliases, tags, summary, notes."""
        q = query.strip().lower()
        if not q:
            return []
        out: list[PersonMeta] = []
        for meta in self.list_active():
            haystack = " ".join(
                [meta.name, " ".join(meta.aliases), " ".join(meta.tags), meta.summary]
            ).lower()
            notes = (self.get_notes(meta.id) or "").lower()
            if q in haystack or q in notes:
                out.append(meta)
        return out

    # --- people.md rendering ------------------------------------------------

    def render_people_md(self) -> str:
        """Markdown roster of every active person, intended to feed the
        companion's system-prompt block 2 alongside INDEX.md."""
        active = self.list_active()
        if not active:
            return "# people.md\n\n(no people known yet)"
        lines = ["# people.md", ""]
        for meta in active:
            aliases = (
                f" (also: {', '.join(meta.aliases)})" if meta.aliases else ""
            )
            summary_chunk = f" — {meta.summary}" if meta.summary else ""
            lines.append(
                f"- **{meta.name}**{aliases} — {meta.category}{summary_chunk}"
            )
        lines.append("")
        return "\n".join(lines)
