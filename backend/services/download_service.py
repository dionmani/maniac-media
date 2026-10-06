"""Download orchestration: hand releases to clients and keep the queue in sync with them."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from backend.core.events import EventType, bus
from backend.core.observability import ACTIVE_DOWNLOADS
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import QueueRepository
from backend.db.session import session_scope
from backend.domain.parser import parse_episode
from backend.domain.quality import parse_quality
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins import registry
from backend.plugins.base import DownloadClientPlugin, PluginKind
from backend.services.audit import SYSTEM, Actor, audit, history

log = logging.getLogger("maniac.downloads")


class DownloadError(RuntimeError):
    pass


def client_settings(row: models.DownloadClient) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "host": row.host,
        "port": row.port,
        "use_ssl": row.use_ssl,
        "url_base": row.url_base,
        "username": row.username,
        "password": row.password,
        "api_key": row.api_key,
        "category": row.category or "maniac-media",
        "remote_path": row.remote_path,
        "local_path": row.local_path,
        "download_folder": get_settings().download_folder,
    }


def build(row: models.DownloadClient) -> DownloadClientPlugin:
    cls = registry.get(PluginKind.DOWNLOAD_CLIENT, row.client_type)
    return cls(client_settings(row))  # type: ignore[return-value]


@dataclass
class ClientRef:
    id: int
    name: str
    category: str
    plugin: DownloadClientPlugin
    remove_completed: bool


def _clients(protocol: str | None = None) -> list[ClientRef]:
    refs = []
    with session_scope() as db:
        rows = db.scalars(select(models.DownloadClient).where(models.DownloadClient.enabled.is_(True)).order_by(models.DownloadClient.priority))
        for row in rows:
            try:
                plugin = build(row)
            except KeyError as exc:
                log.error("%s", exc)
                continue
            if protocol and plugin.protocol != protocol:
                continue
            refs.append(ClientRef(row.id, row.name, row.category or "maniac-media", plugin, bool(row.remove_completed)))
    return refs


def _set_client_health(client_id: int, ok: bool, message: str | None) -> None:
    with session_scope() as db:
        row = db.get(models.DownloadClient, client_id)
        if row:
            row.health_status = "ok" if ok else "error"
            row.health_message = message
            row.last_health_check = utcnow()


async def grab(
    release: ReleaseInfo, *, media_type: str, media_id: int, season: int | None = None, episodes: list[int] | None = None,
    actor: Actor = SYSTEM, retry_count: int = 0, release_gate: str | None = None,
) -> int:
    """``release_gate`` is the reference date that cleared the release (domain/availability.py), kept in history."""
    clients = await asyncio.to_thread(_clients, release.protocol)
    if not clients:
        raise DownloadError(f"No enabled download client handles {release.protocol} releases")
    last_error = None
    for client in clients:
        try:
            client_id = await client.plugin.add(release, client.category)
            await asyncio.to_thread(_set_client_health, client.id, True, "Accepted release")
            break
        except Exception as exc:
            last_error = exc
            log.warning("Client %s refused release: %s", client.name, exc)
            await asyncio.to_thread(_set_client_health, client.id, False, str(exc))
    else:
        raise DownloadError(f"All download clients failed: {last_error}")

    quality = parse_quality(release.title)
    if media_type == "tv" and not episodes:
        parsed = parse_episode(release.title)
        if parsed:
            season = season if season is not None else parsed.season
            episodes = parsed.episodes

    def persist() -> int:
        with session_scope() as db:
            title = _media_title(db, media_type, media_id)
            item = models.DownloadQueueItem(
                title=title,
                media_type=media_type,
                media_id=media_id,
                season_num=season,
                ep_num=episodes[0] if episodes and len(episodes) == 1 else None,
                episode_numbers=episodes or None,
                indexer_name=release.indexer,
                indexer_id=release.indexer_id,
                release_title=release.title,
                quality=quality.quality.name,
                size_bytes=release.size_bytes,
                progress=0.0,
                status="queued",
                download_client_id=client.id,
                client_item_id=client_id,
                download_url=release.download_url,
                guid=release.guid,
                protocol=release.protocol,
                retry_count=retry_count,
            )
            db.add(item)
            db.flush()
            if media_type == "tv":
                stmt = select(models.Episode).where(models.Episode.series_id == media_id)
                if season is not None:
                    stmt = stmt.where(models.Episode.season_number == season)
                if episodes:
                    stmt = stmt.where(models.Episode.episode_number.in_(episodes))
                for ep in db.scalars(stmt):
                    if not ep.has_file:
                        ep.status = "downloading"
            history(db, media_type, media_id, release.title, "grabbed", quality=quality.quality.name,
                    details=f"Sent to {client.name} from {release.indexer} ({release.protocol})", download_id=item.id,
                    data={"indexer": release.indexer, "size": release.size_bytes, "client": client.name, "release_gate": release_gate})
            audit(db, "media", "release.grab", actor=actor, entity_type=media_type, entity_id=media_id,
                  details={"release": release.title, "indexer": release.indexer, "client": client.name, "release_gate": release_gate})
            return item.id

    queue_id = await asyncio.to_thread(persist)
    from backend.workers.queue import job_queue

    await asyncio.to_thread(job_queue.enqueue, "download_monitor", trigger="event", dedupe_key="follow-up", delay_seconds=3)
    bus.publish(EventType.RELEASE_GRABBED, queue_id=queue_id, title=release.title, media_type=media_type, media_id=media_id, quality=quality.quality.name)
    bus.publish(EventType.QUEUE_CHANGED)
    return queue_id


def _media_title(db, media_type: str, media_id: int) -> str:
    obj = db.get(models.Movie if media_type == "movie" else models.Series, media_id)
    if obj is None:
        return "Unknown"
    return f"{obj.title} ({obj.year})" if media_type == "movie" else obj.title


# --------------------------------------------------------------------------- monitoring
async def sync_clients() -> dict:
    """Poll every client, reconcile the queue, and react to state transitions."""
    clients = await asyncio.to_thread(_clients)
    stats = {"clients": len(clients), "updated": 0, "completed": 0, "failed": 0}
    completed_ids: list[int] = []
    for client in clients:
        try:
            items = {i.id.lower(): i for i in await client.plugin.get_items(client.category)}
            await asyncio.to_thread(_set_client_health, client.id, True, f"{len(items)} item(s)")
        except Exception as exc:
            log.warning("Download client %s unreachable: %s", client.name, exc)
            await asyncio.to_thread(_set_client_health, client.id, False, str(exc))
            bus.publish(EventType.HEALTH_ISSUE, source=f"download_client:{client.id}", message=f"{client.name}: {exc}")
            continue
        # a listing can be filtered or truncated; ask for anything we track but didn't see before writing it off
        confirmed = True
        unseen = await asyncio.to_thread(_tracked_ids_missing, client.id, items)
        if unseen:
            try:
                for item in await client.plugin.find_items(unseen):
                    items.setdefault(item.id.lower(), item)
            except Exception as exc:
                log.warning("Could not look up %d item(s) on %s: %s", len(unseen), client.name, exc)
                confirmed = False
        result = await asyncio.to_thread(_reconcile, client, items, confirmed)
        for key in ("updated", "completed", "failed"):
            stats[key] += len(result[key])
        stats["failed"] += len(result["removed"])
        completed_ids.extend(result["completed"])
        for queue_id in result["failed"]:
            await handle_failed(queue_id, reason="Download client reported failure")
        for queue_id in result["removed"]:
            # nothing says the release is bad (duplicate handling, a user, another app or a script removed
            # it), so don't blocklist it or hammer the indexers with a replacement search
            await handle_failed(queue_id, reason="Removed from the download client", release_at_fault=False)

    with session_scope() as db:
        active = db.scalar(
            select(func.count(models.DownloadQueueItem.id)).where(models.DownloadQueueItem.status.in_(("queued", "downloading")))
        ) or 0
    ACTIVE_DOWNLOADS.set(active)
    stats["active"] = active

    if stats["updated"]:
        bus.publish(EventType.QUEUE_CHANGED)
    if completed_ids and get_settings().auto_import:
        from backend.workers.queue import job_queue

        for qid in completed_ids:
            await asyncio.to_thread(job_queue.enqueue, "import_download", {"queue_id": qid}, trigger="event", dedupe_key=str(qid))
    return stats


TRACKED_STATUSES = ("queued", "downloading", "paused", "warning")
ADD_GRACE_SECONDS = 300  # clients take a moment to register magnets / fetch NZBs
MISSING_GRACE_SECONDS = 600  # an item must stay unseen this long before it's considered removed

# queue id -> when the item was first found missing from its client. In-memory on purpose: a restart
# only restarts the grace period, which errs on the side of not writing off live downloads.
_missing_since: dict[int, datetime] = {}


def _tracked_query(client_id: int):
    return select(models.DownloadQueueItem).where(
        models.DownloadQueueItem.download_client_id == client_id,
        models.DownloadQueueItem.status.in_(TRACKED_STATUSES),
    )


def _tracked_ids_missing(client_id: int, items: dict[str, ClientItem]) -> list[str]:
    with session_scope() as db:
        return [q.client_item_id for q in db.scalars(_tracked_query(client_id))
                if q.client_item_id and q.client_item_id.lower() not in items]


def _reconcile(client: ClientRef, items: dict[str, ClientItem], confirmed: bool = True) -> dict[str, list[int]]:
    """Apply client state to the queue. ``confirmed`` is False when unseen items couldn't be double-checked."""
    changed: dict[str, list[int]] = {"updated": [], "completed": [], "failed": [], "removed": []}
    now = utcnow()
    with session_scope() as db:
        for q in db.scalars(_tracked_query(client.id)).all():
            item = items.get((q.client_item_id or "").lower())
            if item is None:
                if not confirmed:
                    continue
                first_missing = _missing_since.setdefault(q.id, now)
                age = (now - (q.added_at or now)).total_seconds()
                if age > ADD_GRACE_SECONDS and (now - first_missing).total_seconds() > MISSING_GRACE_SECONDS:
                    _missing_since.pop(q.id, None)
                    q.status = "failed"
                    q.error_message = "Removed from the download client"
                    changed["removed"].append(q.id)
                continue
            _missing_since.pop(q.id, None)
            before = (q.status, round(q.progress or 0, 1))
            q.progress = item.progress
            q.download_speed_bps = item.speed_bps
            q.eta_seconds = item.eta_seconds
            q.client_position = item.position
            q.download_path = item.output_path or q.download_path
            q.error_message = item.message
            if item.status == "completed":
                q.status = "completed"
                q.progress = 100.0
                q.completed_at = utcnow()
                changed["completed"].append(q.id)
                history(db, q.media_type, q.media_id, q.release_title, "downloaded", quality=q.quality, download_id=q.id,
                        details=f"Download finished in {client.name}")
                bus.publish(EventType.DOWNLOAD_COMPLETED, queue_id=q.id, title=q.title, release=q.release_title, media_type=q.media_type)
            elif item.status == "failed":
                q.status = "failed"
                changed["failed"].append(q.id)
            else:
                q.status = item.status
            if before != (q.status, round(q.progress or 0, 1)):
                changed["updated"].append(q.id)
    return changed


async def handle_failed(queue_id: int, reason: str, *, release_at_fault: bool = True) -> None:
    """Record a failed download and drop it from the queue (History keeps the record); blocklist the release
    and search again only if the release itself is to blame."""
    settings = get_settings()

    def mark() -> tuple[str, int, int | None, list[int] | None, int] | None:
        with session_scope() as db:
            q = db.get(models.DownloadQueueItem, queue_id)
            if q is None:
                return None
            error = q.error_message or reason
            if release_at_fault:
                db.add(models.Blocklist(media_type=q.media_type, media_id=q.media_id, release_title=q.release_title, indexer=q.indexer_name, reason=error))
            history(db, q.media_type, q.media_id, q.release_title, "download_failed", quality=q.quality, download_id=q.id, details=error)
            if q.media_type == "tv":
                stmt = select(models.Episode).where(models.Episode.series_id == q.media_id, models.Episode.status == "downloading")
                if q.season_num is not None:
                    stmt = stmt.where(models.Episode.season_number == q.season_num)
                for ep in db.scalars(stmt):
                    ep.status = "missing"
            info = q.media_type, q.media_id, q.season_num, q.episode_numbers, q.retry_count or 0
            db.delete(q)
            return info

    info = await asyncio.to_thread(mark)
    if info is None:
        return
    media_type, media_id, season, episodes, retries = info
    bus.publish(EventType.DOWNLOAD_FAILED, queue_id=queue_id, reason=reason, media_type=media_type, media_id=media_id)
    bus.publish(EventType.QUEUE_CHANGED)
    if release_at_fault and settings.auto_retry_failed_downloads and retries < settings.max_download_retries:
        from backend.workers.queue import job_queue

        payload = {
            "media_type": media_type, "media_id": media_id, "season": season, "episodes": episodes or [],
            "retry_of": queue_id, "retry_count": retries + 1,
        }
        dedupe_key = f"retry:{media_type}:{media_id}:{season}:{','.join(str(e) for e in (episodes or []))}"
        await asyncio.to_thread(job_queue.enqueue, "automatic_search", payload, trigger="event", dedupe_key=dedupe_key)


async def retry(queue_id: int, actor: Actor) -> None:
    """Re-send the same release to its client (e.g. after fixing a client problem)."""
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            raise DownloadError("Queue item not found")
        if not q.download_url:
            raise DownloadError("Original download link unavailable; run a new search instead")
        release = ReleaseInfo(guid=q.guid or q.release_title, title=q.release_title, download_url=q.download_url,
                              indexer=q.indexer_name or "", indexer_id=q.indexer_id, protocol=q.protocol or "torrent", size_bytes=q.size_bytes or 0)
        client_row = db.get(models.DownloadClient, q.download_client_id) if q.download_client_id else None
        plugin = build(client_row) if client_row else None
        category = client_row.category if client_row else "maniac-media"
    if plugin is None:
        raise DownloadError("Original download client no longer exists")
    new_id = await plugin.add(release, category or "maniac-media")
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        q.client_item_id = new_id
        q.status = "queued"
        q.progress = 0.0
        q.error_message = None
        q.retry_count = (q.retry_count or 0) + 1
        q.added_at = utcnow()
        audit(db, "media", "download.retry", actor=actor, entity_type="queue", entity_id=queue_id, details={"release": q.release_title})
    bus.publish(EventType.QUEUE_CHANGED)


async def remove(queue_id: int, *, remove_from_client: bool, blocklist: bool, delete_data: bool, actor: Actor) -> None:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            raise DownloadError("Queue item not found")
        client_row = db.get(models.DownloadClient, q.download_client_id) if q.download_client_id else None
        plugin = build(client_row) if client_row and remove_from_client else None
        client_item = q.client_item_id
    if plugin and client_item:
        try:
            await plugin.remove(client_item, delete_data)
        except Exception as exc:
            log.warning("Could not remove item from client: %s", exc)
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            return
        if blocklist:
            db.add(models.Blocklist(media_type=q.media_type, media_id=q.media_id, release_title=q.release_title, indexer=q.indexer_name, reason="Removed by user"))
        if q.media_type == "tv":
            for ep in db.scalars(select(models.Episode).where(models.Episode.series_id == q.media_id, models.Episode.status == "downloading")):
                ep.status = "downloaded" if ep.has_file else "missing"
        audit(db, "delete", "queue.remove", actor=actor, entity_type="queue", entity_id=queue_id,
              details={"release": q.release_title, "blocklist": blocklist, "remove_from_client": remove_from_client})
        db.delete(q)
    bus.publish(EventType.QUEUE_CHANGED)


async def remove_completed_from_client(queue_id: int) -> None:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None or not q.download_client_id:
            return
        row = db.get(models.DownloadClient, q.download_client_id)
        if row is None or not row.remove_completed:
            return
        plugin, item_id = build(row), q.client_item_id
    if item_id:
        try:
            await plugin.remove(item_id, delete_data=False)
        except Exception as exc:
            log.info("Could not remove completed item from client: %s", exc)


async def test_client(row_settings: dict, client_type: str) -> tuple[bool, str]:
    cls = registry.get(PluginKind.DOWNLOAD_CLIENT, client_type)
    return await cls(row_settings).test()


def active_queue() -> list[models.DownloadQueueItem]:
    with session_scope() as db:
        return list(QueueRepository(db).active())
