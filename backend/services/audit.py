"""Audit trail & media history.

``audit()`` records *who did what* (security-relevant, append-only). ``history()`` records
what happened to a media item (grabs, imports, renames) and feeds the activity views.
Both are written inside the caller's unit of work so they commit or roll back together
with the change they describe – nothing important happens without leaving a trace.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from backend.db import models

log = logging.getLogger("maniac.audit")


@dataclass
class Actor:
    user_id: int | None = None
    username: str | None = None
    ip: str | None = None

    @classmethod
    def system(cls, name: str = "system") -> "Actor":
        return cls(None, name, None)


SYSTEM = Actor.system()


def audit(
    db: Session,
    category: str,
    action: str,
    *,
    actor: Actor | None = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    details: dict[str, Any] | None = None,
    severity: str = "info",
    success: bool = True,
) -> models.AuditLog:
    actor = actor or SYSTEM
    entry = models.AuditLog(
        user_id=actor.user_id,
        username=actor.username,
        ip_address=actor.ip,
        category=category,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=details,
        severity=severity,
        success=success,
    )
    db.add(entry)
    log.log(
        logging.WARNING if severity in ("warning", "error", "critical") or not success else logging.INFO,
        "audit %s.%s by %s on %s:%s",
        category,
        action,
        actor.username or "system",
        entity_type,
        entity_id,
        extra={"audit": True, "category": category, "action": action},
    )
    return entry


def history(
    db: Session,
    media_type: str,
    media_id: int | None,
    title: str,
    event_type: str,
    *,
    quality: str | None = None,
    details: str | None = None,
    download_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> models.History:
    entry = models.History(
        media_type=media_type,
        media_id=media_id,
        title=title,
        event_type=event_type,
        quality=quality,
        details=details,
        download_id=download_id,
        data=data,
    )
    db.add(entry)
    return entry
