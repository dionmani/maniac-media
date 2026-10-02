"""Notification dispatcher: subscribes to domain events and fans out to configured channels."""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from backend.core.events import Event, EventType, bus
from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import NotificationMessage
from backend.plugins import registry
from backend.plugins.base import NotificationPlugin, PluginKind

log = logging.getLogger("maniac.notifications")

# Notification events offered to users and how domain events map onto them
NOTIFY_EVENTS = {
    "import_complete": "Import complete",
    "download_completed": "Download completed",
    "download_failed": "Download failed / import failed",
    "grabbed": "Release grabbed",
    "health_issue": "Health issues",
}
_EVENT_MAP = {
    EventType.IMPORT_COMPLETED: "import_complete",
    EventType.DOWNLOAD_COMPLETED: "download_completed",
    EventType.DOWNLOAD_FAILED: "download_failed",
    EventType.IMPORT_FAILED: "download_failed",
    EventType.RELEASE_GRABBED: "grabbed",
    EventType.HEALTH_ISSUE: "health_issue",
}


def plugin_for(row: models.NotificationChannel) -> NotificationPlugin:
    cls = registry.get(PluginKind.NOTIFICATION, row.implementation)
    secrets = json.loads(row.secret_settings) if row.secret_settings else {}
    return cls({**(row.settings or {}), **secrets})  # type: ignore[return-value]


def split_settings(implementation: str, values: dict) -> tuple[dict, dict]:
    """Separate secret fields (stored encrypted) from plain ones."""
    cls = registry.get(PluginKind.NOTIFICATION, implementation)
    secret_names = {f.name for f in cls.fields if f.secret}
    plain = {k: v for k, v in values.items() if k not in secret_names}
    secret = {k: v for k, v in values.items() if k in secret_names and v not in (None, "")}
    return plain, secret


def _message(event: Event) -> NotificationMessage | None:
    p = event.payload
    kind = _EVENT_MAP.get(event.type)
    if kind == "import_complete":
        items = p.get("items") or []
        return NotificationMessage(kind, "Imported", f"{p.get('title')}", {"Items": ", ".join(items[:5])}, severity="success")
    if kind == "download_completed":
        return NotificationMessage(kind, "Download completed", p.get("release") or p.get("title", ""), severity="info")
    if kind == "download_failed":
        details = {"Reason": str(p.get("reason", ""))[:500]}
        if p.get("fix_available"):
            details["Fix available"] = "Yes – see Activity → Failures"
        return NotificationMessage(kind, "Download failed" if event.type == EventType.DOWNLOAD_FAILED else "Import failed",
                                   p.get("title") or f"Queue item {p.get('queue_id')}", details, severity="error")
    if kind == "grabbed":
        return NotificationMessage(kind, "Release grabbed", p.get("title", ""), {"Quality": p.get("quality", "")}, severity="info")
    if kind == "health_issue":
        return NotificationMessage(kind, "Health issue", p.get("message", ""), {"Source": p.get("source", "")}, severity="warning")
    return None


async def _on_event(event: Event) -> None:
    kind = _EVENT_MAP.get(event.type)
    if not kind:
        return
    message = _message(event)
    if message is None:
        return

    def channels():
        with session_scope() as db:
            rows = db.scalars(select(models.NotificationChannel).where(models.NotificationChannel.enabled.is_(True)))
            return [(r.name, plugin_for(r)) for r in rows if kind in (r.events or [])]

    for name, plugin in await asyncio.to_thread(channels):
        try:
            await plugin.send(message)
        except Exception as exc:
            log.warning("Notification via %s failed: %s", name, exc)


def install() -> None:
    for event_type in _EVENT_MAP:
        bus.subscribe(event_type, _on_event)
