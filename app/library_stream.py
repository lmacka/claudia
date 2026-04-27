"""
StatusBus: per-doc fan-out for upload/extraction status messages.

The library pipeline pushes single-line status updates (e.g. "Extracting
page 12/50…") via emit(). SSE subscribers stream the events. A bounded
ring buffer per doc enables late joiners to replay the recent history
on reconnect within ~60s of the upload starting.

Thread-safety: emit() is callable from worker threads (the pipeline
runs in an executor). Internally it uses a threading.Lock for shared
state and asyncio.AbstractEventLoop.call_soon_threadsafe to dispatch
into per-subscriber asyncio queues on the main loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

_END = object()  # sentinel marking terminal end-of-stream


@dataclass
class _Subscriber:
    queue: asyncio.Queue


class StatusBus:
    def __init__(self, *, ring_size: int = 20) -> None:
        self._ring: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=ring_size))
        self._subscribers: dict[str, list[_Subscriber]] = defaultdict(list)
        self._terminal: set[str] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the running asyncio event loop. Called once at app startup."""
        self._loop = loop

    # --- producer ------------------------------------------------------------

    def emit(self, doc_id: str, msg: str, *, terminal: bool = False) -> None:
        """
        Thread-safe. Append `msg` to ring buffer + dispatch to subscribers.
        If terminal=True, mark the stream complete so future subscribers
        replay the buffer and exit cleanly.
        """
        with self._lock:
            self._ring[doc_id].append(msg)
            if terminal:
                self._terminal.add(doc_id)
            subs = list(self._subscribers.get(doc_id, []))
        if self._loop is None:
            return  # no loop attached (test-only or startup race)
        for sub in subs:
            self._loop.call_soon_threadsafe(_safe_put, sub.queue, msg)
        if terminal:
            for sub in subs:
                self._loop.call_soon_threadsafe(_safe_put, sub.queue, _END)

    def is_terminal(self, doc_id: str) -> bool:
        with self._lock:
            return doc_id in self._terminal

    # --- consumer ------------------------------------------------------------

    async def subscribe(self, doc_id: str) -> AsyncIterator[str]:
        """
        Async generator yielding str messages until the producer marks the
        stream terminal. Replays the ring buffer first.
        """
        sub = _Subscriber(queue=asyncio.Queue())
        with self._lock:
            replay = list(self._ring.get(doc_id, []))
            terminal = doc_id in self._terminal
            self._subscribers[doc_id].append(sub)

        try:
            for msg in replay:
                yield msg
            if terminal:
                return
            while True:
                item = await sub.queue.get()
                if item is _END:
                    return
                yield item
        finally:
            with self._lock:
                if sub in self._subscribers.get(doc_id, []):
                    self._subscribers[doc_id].remove(sub)

    # --- housekeeping --------------------------------------------------------

    def clear(self, doc_id: str) -> None:
        """Drop ring + terminal flag for a doc. Called on hard delete."""
        with self._lock:
            self._ring.pop(doc_id, None)
            self._terminal.discard(doc_id)
            # Existing subscribers will hang until they hit the END sentinel
            # or the connection drops; clear() doesn't proactively close them.


def _safe_put(q: asyncio.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        # Bounded queue overrun — drop oldest event for this subscriber.
        try:
            q.get_nowait()
            q.put_nowait(item)
        except asyncio.QueueEmpty:
            pass
