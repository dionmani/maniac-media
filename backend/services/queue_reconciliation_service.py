"""Keep the Activity queue in step with what actually reached the library.

An import commits its file work (placement, library record, history) before the queue row is
updated, and other importers (manual import, the watch folder) used to move a tracked download
without touching its queue row at all. Either way the row kept saying "import failed" / "path not
found" for a download that was imported. This module settles such rows, but only on evidence: a
committed ``imported``/``upgraded`` history event for the same media that belongs to this queue
row. A missing path, a matching name or ``has_file`` is never enough on its own.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db import models
from backend.db.session import session_scope
from backend.domain.parser import parse_episode

log = logging.getLogger("maniac.reconcile")

STALE_STATUSES = ("completed", "import_failed")
_IMPORT_EVENTS = ("imported", "upgraded")
_BATCH = 200


# --------------------------------------------------------------------------- ownership
def _parts(path: str | Path) -> tuple[str, ...]:
    """Path components, normalised for the host filesystem (case-folded only where the OS is)."""
    return Path(os.path.normcase(os.path.abspath(str(path)))).parts


def _is_container_root(path: str) -> bool:
    """A download/category root owns nothing in particular – never evidence of ownership."""
    from backend.services.import_service import _CLIENT_DIRS

    p = Path(path)
    if p.name.lower() in _CLIENT_DIRS:
        return True
    try:
        return _parts(p) == _parts(get_settings().download_folder)
    except Exception:
        return False


def path_owns(queue_path: str | None, src: str | Path) -> bool:
    """Whether ``src`` is the queue entry's file or lies inside its release folder (component-wise,
    so ``Release`` does not own ``Release.Other``)."""
    if not queue_path or _is_container_root(queue_path):
        return False
    owner, target = _parts(queue_path), _parts(src)
    return len(target) >= len(owner) and target[:len(owner)] == owner


def _episodes_of(q: models.DownloadQueueItem) -> list[int]:
    return list(q.episode_numbers or ([q.ep_num] if q.ep_num else []))


def _compatible(q: models.DownloadQueueItem, media_type: str, media_id: int, season: int | None, episodes: list[int]) -> bool:
    if q.media_type != media_type or q.media_id != media_id:
        return False
    if media_type != "tv":
        return True
    if q.season_num is not None and season is not None and q.season_num != season:
        return False
    wanted = _episodes_of(q)
    return not wanted or not episodes or set(episodes) <= set(wanted)


def find_owner(db: Session, src: str | Path, media_type: str, media_id: int, season: int | None = None,
               episodes: list[int] | None = None) -> models.DownloadQueueItem | None:
    """The single queue entry that owns ``src`` for this media target, else None (ambiguous = None)."""
    rows = db.scalars(select(models.DownloadQueueItem).where(
        models.DownloadQueueItem.download_path.is_not(None), models.DownloadQueueItem.media_type == media_type,
        models.DownloadQueueItem.media_id == media_id, models.DownloadQueueItem.status != "imported"))
    owners = [q for q in rows if path_owns(q.download_path, src) and _compatible(q, media_type, media_id, season, episodes or [])]
    return owners[0] if len(owners) == 1 else None


def is_owned_by_queue(db: Session, src: str | Path) -> bool:
    """Whether any not-yet-imported queue entry owns this file (regardless of media target)."""
    rows = db.scalars(select(models.DownloadQueueItem.download_path).where(
        models.DownloadQueueItem.download_path.is_not(None), models.DownloadQueueItem.status != "imported"))
    return any(path_owns(p, src) for p in rows)


# --------------------------------------------------------------------------- evidence
def has_import_evidence(db: Session, q: models.DownloadQueueItem) -> bool:
    """Whether committed history shows this queue entry's download reached the library (completely)."""
    stmt = select(models.History).where(
        models.History.event_type.in_(_IMPORT_EVENTS), models.History.media_type == q.media_type,
        models.History.media_id == q.media_id)
    if q.added_at is not None:  # SQLite can reuse a deleted row's id; ignore an earlier owner's history
        stmt = stmt.where(models.History.timestamp >= q.added_at)
    rows = list(db.scalars(stmt.order_by(models.History.id.desc()).limit(_BATCH)))
    explicit = [h for h in rows if h.download_id == q.id]
    others = [h for h in rows if h.download_id is None and (h.data or {}).get("source")
              and path_owns(q.download_path, h.data["source"])]
    if not explicit and others and _unique_owner(db, q):
        explicit = others  # legacy history without a link: source ownership is unique, so it's this entry's
    if not explicit:
        return False
    if q.media_type != "tv":
        return True
    wanted = _episodes_of(q)
    if not wanted:
        return False  # a pack's completeness can't be judged from one event; leave it for review
    covered: set[int] = set()
    for h in explicit:
        data = h.data or {}
        season, eps = data.get("season"), data.get("episodes")
        if eps is None:  # legacy event: read it off the imported file's name
            parsed = parse_episode(Path(data.get("source") or "").name)
            season, eps = (parsed.season, parsed.episodes) if parsed else (None, [])
        if q.season_num is not None and season is not None and season != q.season_num:
            continue
        covered.update(eps or [])
    return set(wanted) <= covered


def _unique_owner(db: Session, q: models.DownloadQueueItem) -> bool:
    rows = db.scalars(select(models.DownloadQueueItem).where(
        models.DownloadQueueItem.media_type == q.media_type, models.DownloadQueueItem.media_id == q.media_id,
        models.DownloadQueueItem.download_path.is_not(None), models.DownloadQueueItem.id != q.id))
    return not any(path_owns(o.download_path, q.download_path or "") or path_owns(q.download_path, o.download_path or "")
                   for o in rows)


# --------------------------------------------------------------------------- settling
def mark_imported(db: Session, queue_id: int, from_statuses: tuple[str, ...]) -> bool:
    """Conditionally move a row to ``imported`` (clearing failure state). Never recreates a removed row."""
    res = db.execute(update(models.DownloadQueueItem).where(
        models.DownloadQueueItem.id == queue_id, models.DownloadQueueItem.status.in_(from_statuses)
    ).values(status="imported", progress=100.0, download_speed_bps=0, eta_seconds=None, error_message=None,
             failure_reason=None, failure_signature=None, failure_analysis=None))
    return res.rowcount > 0


def settle(queue_id: int, from_statuses: tuple[str, ...] = STALE_STATUSES) -> bool:
    """Settle one row if the evidence supports it (own transaction); publishes the change."""
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None or q.status not in from_statuses or not has_import_evidence(db, q):
            return False
        ok = mark_imported(db, queue_id, from_statuses)
    if ok:
        bus.publish(EventType.QUEUE_CHANGED)
    return ok


def reconcile_stale(limit: int = _BATCH) -> int:
    """Settle stale ``completed``/``import_failed`` rows whose import is already in history.

    Never touches ``importing`` rows (possibly in flight). No file work, history or notifications.
    """
    fixed = 0
    with session_scope() as db:
        rows = list(db.scalars(select(models.DownloadQueueItem).where(
            models.DownloadQueueItem.status.in_(STALE_STATUSES)).order_by(models.DownloadQueueItem.id).limit(limit)))
        for q in rows:
            if has_import_evidence(db, q) and mark_imported(db, q.id, STALE_STATUSES):
                fixed += 1
                log.info("Queue item %s settled as imported from history evidence", q.id)
    if fixed:
        bus.publish(EventType.QUEUE_CHANGED)
    return fixed
