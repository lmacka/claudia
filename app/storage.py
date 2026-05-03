"""
Session storage protocol + types + the in-memory implementation.

The production / dev backing store is `SqliteSessionStore` in
`app/storage_sqlite.py` (T-NEW-I, v0.7.0). This module hosts:

- The `SessionStore` Protocol every implementation conforms to
- The `SessionHeader` / `Message` / `SessionMeta` dataclasses
- The `new_session_id` factory
- `InMemorySessionStore`, used by the parametrized storage tests as a
  fast in-process implementation. No production code instantiates it.

The legacy `NFSSessionStore` (JSONL on /data) was removed in v0.8.0
phase A — see /home/liamm/.claude/plans/ok-but-first-inspect-crystalline-seal.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    def has_event(self, session_id: str, event_type: str) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory implementation (tests only; production uses SqliteSessionStore)
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

    def has_event(self, session_id: str, event_type: str) -> bool:
        return any(kind == event_type for kind, _payload in self._events.get(session_id, []))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def new_session_id(mode: str) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    short = uuid.uuid4().hex[:8]
    return f"{now}_{mode}_{short}"
