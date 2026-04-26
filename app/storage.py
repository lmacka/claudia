"""
Session storage for robo-therapist.

Design:
- One JSONL file per session under /data/sessions/.
- First line = session header (mode, model, prompt_sha, created_at).
- Subsequent lines = message or event records (append-only).
- fcntl exclusive locks on /data/.locks/<resource>.lock for every mutation.

Two implementations:
- NFSSessionStore: prod, reads/writes /data.
- InMemorySessionStore: tests + --local mode.

The two implement the same SessionStore Protocol.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class SessionHeader:
    session_id: str
    created_at: str  # ISO 8601
    mode: str  # "check-in" | "vent"
    model: str  # "claude-sonnet-4-6" | "claude-opus-4-7"
    prompt_sha: str  # SHA-256 of companion prompt (+ auditor when it exists)
    title: str | None = None
    status: str = "active"  # active | ended | aborted
    ended_at: str | None = None
    token_total: int = 0
    cost_total_usd: float = 0.0


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system_event"
    content: str
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMeta:
    """Summary row for session list rendering."""

    session_id: str
    created_at: str
    mode: str
    model: str
    status: str
    title: str | None
    last_activity: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class SessionStore(Protocol):
    def create_session(self, header: SessionHeader) -> None: ...
    def append_message(self, session_id: str, message: Message) -> None: ...
    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None: ...
    def load_header(self, session_id: str) -> SessionHeader: ...
    def update_header(self, session_id: str, **changes: Any) -> None: ...
    def load_messages(self, session_id: str) -> list[Message]: ...
    def list_sessions(self) -> list[SessionMeta]: ...
    def active_session(self) -> SessionMeta | None: ...


# ---------------------------------------------------------------------------
# NFS implementation
# ---------------------------------------------------------------------------


class NFSSessionStore:
    """
    Append-only JSONL per session on /data.

    Record types on disk:
        {"type": "header", ...}
        {"type": "message", ...}
        {"type": "event", ...}

    Header is written once (first line); updates are append-only "header_update"
    records. On read, the final header is reconstructed by folding header_updates
    over the initial header.
    """

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.sessions_dir = data_root / "sessions"
        self.locks_dir = data_root / ".locks"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    # --- lock helper --------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self, resource: str) -> Iterator[None]:
        """Exclusive fcntl lock on /data/.locks/<resource>.lock."""
        lock_path = self.locks_dir / f"{resource}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # --- path helpers -------------------------------------------------------

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def _append(self, session_id: str, record: dict[str, Any]) -> None:
        path = self._session_path(session_id)
        with self._lock(f"session-{session_id}"), path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # --- protocol methods --------------------------------------------------

    def create_session(self, header: SessionHeader) -> None:
        path = self._session_path(header.session_id)
        if path.exists():
            raise FileExistsError(f"session {header.session_id} already exists")
        self._append(
            header.session_id,
            {"type": "header", **header.__dict__},
        )
        log.info("session.created", session_id=header.session_id, mode=header.mode)

    def append_message(self, session_id: str, message: Message) -> None:
        self._append(
            session_id,
            {"type": "message", **message.__dict__},
        )

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        self._append(
            session_id,
            {
                "type": "event",
                "event_type": event_type,
                "ts": datetime.now(UTC).isoformat(),
                "payload": payload,
            },
        )

    def load_header(self, session_id: str) -> SessionHeader:
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(session_id)
        header_dict: dict[str, Any] = {}
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type")
                if rtype == "header":
                    header_dict = {k: v for k, v in record.items() if k != "type"}
                elif rtype == "header_update":
                    header_dict.update(record.get("changes", {}))
        if not header_dict:
            raise ValueError(f"session {session_id} has no header")
        return SessionHeader(**header_dict)

    def update_header(self, session_id: str, **changes: Any) -> None:
        self._append(
            session_id,
            {
                "type": "header_update",
                "ts": datetime.now(UTC).isoformat(),
                "changes": changes,
            },
        )

    def load_messages(self, session_id: str) -> list[Message]:
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(session_id)
        out: list[Message] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "message":
                    out.append(
                        Message(
                            role=record["role"],
                            content=record["content"],
                            ts=record.get(
                                "ts", datetime.now(UTC).isoformat()
                            ),
                            meta=record.get("meta", {}),
                        )
                    )
        return out

    def list_sessions(self) -> list[SessionMeta]:
        out: list[SessionMeta] = []
        for path in sorted(self.sessions_dir.glob("*.jsonl"), reverse=True):
            session_id = path.stem
            try:
                header = self.load_header(session_id)
            except (FileNotFoundError, ValueError):
                continue
            try:
                mtime = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC
                ).isoformat()
            except OSError:
                mtime = header.created_at
            out.append(
                SessionMeta(
                    session_id=session_id,
                    created_at=header.created_at,
                    mode=header.mode,
                    model=header.model,
                    status=header.status,
                    title=header.title,
                    last_activity=mtime,
                )
            )
        return out

    def active_session(self) -> SessionMeta | None:
        for meta in self.list_sessions():
            if meta.status == "active":
                return meta
        return None


# ---------------------------------------------------------------------------
# In-memory implementation (tests + --local)
# ---------------------------------------------------------------------------


class InMemorySessionStore:
    def __init__(self) -> None:
        self._headers: dict[str, SessionHeader] = {}
        self._messages: dict[str, list[Message]] = {}
        self._events: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def create_session(self, header: SessionHeader) -> None:
        if header.session_id in self._headers:
            raise FileExistsError(header.session_id)
        self._headers[header.session_id] = header
        self._messages[header.session_id] = []
        self._events[header.session_id] = []

    def append_message(self, session_id: str, message: Message) -> None:
        self._messages.setdefault(session_id, []).append(message)

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        self._events.setdefault(session_id, []).append((event_type, payload))

    def load_header(self, session_id: str) -> SessionHeader:
        if session_id not in self._headers:
            raise FileNotFoundError(session_id)
        return self._headers[session_id]

    def update_header(self, session_id: str, **changes: Any) -> None:
        header = self.load_header(session_id)
        for k, v in changes.items():
            if hasattr(header, k):
                setattr(header, k, v)

    def load_messages(self, session_id: str) -> list[Message]:
        if session_id not in self._messages:
            raise FileNotFoundError(session_id)
        return list(self._messages[session_id])

    def list_sessions(self) -> list[SessionMeta]:
        return [
            SessionMeta(
                session_id=h.session_id,
                created_at=h.created_at,
                mode=h.mode,
                model=h.model,
                status=h.status,
                title=h.title,
                last_activity=h.ended_at or h.created_at,
            )
            for h in sorted(
                self._headers.values(), key=lambda x: x.created_at, reverse=True
            )
        ]

    def active_session(self) -> SessionMeta | None:
        for meta in self.list_sessions():
            if meta.status == "active":
                return meta
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def new_session_id(mode: str) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    short = uuid.uuid4().hex[:8]
    return f"{now}_{mode}_{short}"
