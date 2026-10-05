"""In-process pub/sub so WebSocket clients receive events as they happen."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from computeruse.telemetry.events import Event


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event | None]]] = defaultdict(set)
        self._global: set[asyncio.Queue[Event | None]] = set()

    def subscribe(self, session_id: str | None = None, maxsize: int = 1000) -> asyncio.Queue[Event | None]:
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        (self._subs[session_id] if session_id else self._global).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue, session_id: str | None = None) -> None:
        (self._subs.get(session_id, set()) if session_id else self._global).discard(q)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.session_id, ())) + list(self._global):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest item rather than stall the agent.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    def close_session(self, session_id: str) -> None:
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
