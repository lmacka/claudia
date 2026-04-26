"""
People store: structured records of the people in the user's life that the
companion + auditor reference.

Mirrors app/library.py: one folder per entity, manifest, atomic writes,
fcntl locks. Auditor proposes adds/updates after each session; user
manages via /people.

Per docs/library-people-plan.md.
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
    """meta.json schema for one person."""

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
    """Owns every read/write under /data/people/."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.locks_dir = root / ".locks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

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

    # --- paths --------------------------------------------------------------

    def _person_dir(self, person_id: str) -> Path:
        if "/" in person_id or ".." in person_id or person_id.startswith("."):
            raise ValueError(f"unsafe person id: {person_id!r}")
        candidate = (self.root / person_id).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as e:
            raise ValueError(f"person id escapes people root: {person_id!r}") from e
        return candidate

    def _meta_path(self, person_id: str) -> Path:
        return self._person_dir(person_id) / "meta.json"

    def _notes_path(self, person_id: str) -> Path:
        return self._person_dir(person_id) / "notes.md"

    def _manifest_path(self) -> Path:
        return self.root / "manifest.json"

    # --- atomic write -------------------------------------------------------

    def _atomic_write(self, path: Path, content: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if isinstance(content, str):
            tmp.write_text(content, encoding="utf-8")
        else:
            tmp.write_bytes(content)
        os.replace(tmp, path)

    def _write_meta(self, person_id: str, meta: PersonMeta) -> None:
        payload = meta.model_dump(mode="json")
        self._atomic_write(
            self._meta_path(person_id),
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        )

    def _read_meta(self, person_id: str) -> PersonMeta | None:
        path = self._meta_path(person_id)
        if not path.exists():
            return None
        try:
            return PersonMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.error("people.meta_read_failed", person_id=person_id, error=str(e))
            return None

    # --- id minting ---------------------------------------------------------

    def mint_id(self, name: str) -> str:
        """Slug + collision-handled. Unique across active+archived."""
        base = _name_slug(name)
        with self._lock("manifest"):
            candidate = base
            n = 2
            while (self.root / candidate).exists():
                candidate = f"{base}-{n}"
                n += 1
            return candidate

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
            self._person_dir(person_id).mkdir(parents=True, exist_ok=False)
            self._write_meta(person_id, meta)
            self._atomic_write(self._notes_path(person_id), notes)
        self.rebuild_manifest()
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
        results: list[PersonMeta] = []
        if not self.root.exists():
            return results
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            meta = self._read_meta(entry.name)
            if meta is not None:
                results.append(meta)
        # Sort by name (case-insensitive) for predictable UI ordering.
        results.sort(key=lambda m: m.name.lower())
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
        self.rebuild_manifest()
        return patched

    def replace_notes(self, person_id: str, content: str) -> None:
        with self._lock(f"person-{person_id}"):
            if not self._person_dir(person_id).exists():
                raise KeyError(f"person {person_id} not found")
            self._atomic_write(self._notes_path(person_id), content)
            self.update_silent(person_id, updated_at=datetime.now(UTC))
        log.info("people.notes_replaced", person_id=person_id, chars=len(content))

    def append_note(self, person_id: str, addition: str) -> None:
        """Auditor-friendly: append a paragraph to notes.md."""
        with self._lock(f"person-{person_id}"):
            if not self._person_dir(person_id).exists():
                raise KeyError(f"person {person_id} not found")
            existing = self.get_notes(person_id) or ""
            stamp = datetime.now(UTC).strftime("%Y-%m-%d")
            block = (
                ("\n\n" if existing and not existing.endswith("\n\n") else "")
                + f"_(auditor, {stamp}):_ {addition.strip()}\n"
            )
            self._atomic_write(self._notes_path(person_id), existing + block)
            self.update_silent(person_id, updated_at=datetime.now(UTC))

    def update_silent(self, person_id: str, **fields: Any) -> None:
        """Like update() but skips manifest rebuild (caller handles batching)."""
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
        self.rebuild_manifest()

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
        self.rebuild_manifest()

    def touch(self, person_id: str) -> None:
        """Bump last_mentioned. Used by lookup_person tool."""
        with self._lock(f"person-{person_id}"):
            current = self._read_meta(person_id)
            if current is None:
                raise KeyError(f"person {person_id} not found")
            now = datetime.now(UTC)
            patched = current.model_copy(update={"last_mentioned": now, "updated_at": now})
            self._write_meta(person_id, patched)
        # No manifest rebuild — last_mentioned is bumped frequently and the
        # manifest is consumed by the UI / system prompt at request time.

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
            person_dir = self._person_dir(person_id)
            if not person_dir.exists():
                raise KeyError(f"person {person_id} not found")
            shutil.rmtree(person_dir)
        self.rebuild_manifest()
        log.info("people.person_deleted", person_id=person_id)

    # --- search -------------------------------------------------------------

    def find_near_match(self, name: str, max_distance: int = 2) -> PersonMeta | None:
        """Find an existing person whose name or alias is within Levenshtein
        distance of `name`. Used by auditor proposals to merge alias on
        near-match instead of creating duplicates."""
        target = name.strip().lower()
        best: tuple[int, PersonMeta] | None = None
        for meta in self.list_all():
            candidates = [meta.name.lower()] + [a.lower() for a in meta.aliases]
            for cand in candidates:
                d = _levenshtein(target, cand)
                if d <= max_distance and (best is None or d < best[0]):
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

    # --- manifest -----------------------------------------------------------

    def rebuild_manifest(self) -> None:
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

    # --- people.md rendering ------------------------------------------------

    def render_people_md(self) -> str:
        """
        Markdown roster of every active person, intended to feed the
        companion's system-prompt block 2 alongside INDEX.md.
        """
        active = [p for p in self.list_active()]
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
