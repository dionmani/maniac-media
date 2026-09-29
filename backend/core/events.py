"""In-process domain event bus.

Services publish facts ("download.completed", "import.completed", ...) without knowing who
listens. Subscribers – the WebSocket hub, the notification dispatcher, metrics – react
asynchronously. ``publish`` is safe to call from worker threads: events are marshalled onto
the application's event loop.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger("maniac.events")


class EventType:
    MEDIA_ADDED = "media.added"
    MEDIA_UPDATED = "media.updated"
    MEDIA_DELETED = "media.deleted"
    RELEASE_GRABBED = "release.grabbed"
    DOWNLOAD_PROGRESS = "download.progress"
    DOWNLOAD_COMPLETED = "download.completed"
    DOWNLOAD_FAILED = "download.failed"
    IMPORT_COMPLETED = "import.completed"
    IMPORT_FAILED = "import.failed"
    RENAME_COMPLETED = "rename.completed"
    SCAN_PROGRESS = "scan.progress"
    SCAN_COMPLETED = "scan.completed"
    JOB_UPDATED = "job.updated"
    HEALTH_ISSUE = "health.issue"
    HEALTH_RESTORED = "health.restored"
    QUEUE_CHANGED = "queue.changed"
    DATABASE_MIGRATION = "database.migration"


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "payload": self.payload, "timestamp": self.timestamp}


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Subscribe to one event type, or ``"*"`` for everything."""
        self._handlers[event_type].append(handler)

    def publish(self, event_type: str, **payload: Any) -> None:
        event = Event(event_type, payload)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._dispatch(event))
        else:
            asyncio.run_coroutine_threadsafe(self._dispatch(event), loop)

    async def _dispatch(self, event: Event) -> None:
        for handler in [*self._handlers.get(event.type, []), *self._handlers.get("*", [])]:
            try:
                await handler(event)
            except Exception:
                log.exception("Event handler failed for %s", event.type)


bus = EventBus()
