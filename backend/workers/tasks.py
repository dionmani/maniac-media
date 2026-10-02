"""Job handlers – thin adapters from the job queue to application services."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.session import db_manager, session_scope
from backend.services import (
    download_service,
    fileops,
    health_service,
    import_service,
    indexer_service,
    media_file_service,
    metadata_service,
    renamer_service,
    scanner_service,
    search_service,
)
from backend.services.audit import Actor
from backend.workers.queue import JobContext, job_queue


@job_queue.register("rss_sync", "RSS Sync", priority=3)
async def rss_sync(ctx: JobContext):
    return await search_service.rss_sync()


@job_queue.register("download_monitor", "Download Monitoring", priority=1, max_attempts=1)
async def download_monitor(ctx: JobContext):
    stats = await download_service.sync_clients()
    if stats.get("active"):
        # adaptive polling: follow active downloads closely, idle back to the scheduled interval
        await asyncio.to_thread(job_queue.enqueue, "download_monitor", trigger="event", dedupe_key="follow-up",
                                delay_seconds=get_settings().download_poll_seconds)
    return stats


@job_queue.register("download_folder_scan", "Download Folder Scan", priority=4, max_attempts=1)
async def download_folder_scan(ctx: JobContext):
    return await asyncio.to_thread(import_service.scan_download_folder)


@job_queue.register("import_download", "Import Download", priority=2, exclusive=False)
async def import_download(ctx: JobContext):
    queue_id = int(ctx.payload["queue_id"])
    force = bool(ctx.payload.get("force", False))
    result = await asyncio.to_thread(import_service.import_queue_item, queue_id, force)
    if result.imported:
        await download_service.remove_completed_from_client(queue_id)
    return {"imported": result.imported, "skipped": result.skipped, "failed": result.failed}


@job_queue.register("metadata_refresh", "Metadata Refresh", priority=7)
async def metadata_refresh(ctx: JobContext):
    stats = await metadata_service.refresh_library(
        db_manager.session, media_type=ctx.payload.get("media_type", "all"), limit=ctx.payload.get("limit"),
        force_ids=ctx.payload.get("ids"), progress=ctx.progress,
    )
    if stats.get("more_pending"):
        # work through a large backlog (e.g. right after upgrading from v1) in polite batches
        await asyncio.to_thread(job_queue.enqueue, "metadata_refresh", {"media_type": ctx.payload.get("media_type", "all")},
                                trigger="event", dedupe_key="backlog", delay_seconds=30)
    return stats


@job_queue.register("library_scan", "Library Scan", priority=6)
async def library_scan(ctx: JobContext):
    result = await asyncio.to_thread(
        scanner_service.scan, ctx.payload.get("scope", "all"), ctx.payload.get("incremental", True), ctx.progress,
        ctx.payload.get("media_id"),
    )
    job_queue.enqueue("metadata_refresh", {"media_type": "all"}, trigger="event", dedupe_key="post-scan")
    return result


@job_queue.register("automatic_search", "Automatic Search", priority=4, exclusive=False, max_attempts=1)
async def automatic_search(ctx: JobContext):
    p = ctx.payload
    return await search_service.automatic(p["media_type"], int(p["media_id"]), p.get("season"), p.get("episodes") or None,
                                          actor=Actor(p.get("user_id"), p.get("username")), retry_count=int(p.get("retry_count", 0)))


@job_queue.register("missing_search", "Wanted: Missing Search", priority=8)
async def missing_search(ctx: JobContext):
    return await search_service.search_missing(limit=int(ctx.payload.get("limit", 50)), progress=ctx.progress)


@job_queue.register("rename", "Bulk Rename", priority=5)
async def rename(ctx: JobContext):
    p = ctx.payload
    return await asyncio.to_thread(
        renamer_service.execute, p["items"], dry_run=False, include_folders=p.get("include_folders", False),
        actor=Actor(p.get("user_id"), p.get("username")),
    )


@job_queue.register("health_check", "Health Check", priority=5, max_attempts=1)
async def health_check(ctx: JobContext):
    indexers = await indexer_service.health_check_all()
    checks = await asyncio.to_thread(health_service.run_checks)
    return {"indexers": indexers, "problems": [c.message for c in checks if c.status != "ok"]}


@job_queue.register("cleanup", "Housekeeping", priority=9)
async def cleanup(ctx: JobContext):
    def run():
        settings = get_settings()
        now = utcnow()
        with session_scope() as db:
            jobs = db.execute(delete(models.Job).where(models.Job.status.in_(("completed", "failed", "cancelled")),
                                                        models.Job.created_at < now - timedelta(days=settings.job_retention_days))).rowcount
            # routine download polls (thousands a day while anything downloads) carry nothing worth a week's keep
            jobs += db.execute(delete(models.Job).where(models.Job.name == "download_monitor", models.Job.status == "completed",
                                                         models.Job.created_at < now - timedelta(days=1))).rowcount
            history = db.execute(delete(models.History).where(models.History.timestamp < now - timedelta(days=settings.history_retention_days))).rowcount
            audit_rows = db.execute(delete(models.AuditLog).where(models.AuditLog.timestamp < now - timedelta(days=settings.audit_retention_days))).rowcount
            queue = db.execute(delete(models.DownloadQueueItem).where(models.DownloadQueueItem.status == "imported",
                                                                       models.DownloadQueueItem.completed_at < now - timedelta(days=2))).rowcount
            # failed downloads are dropped from the queue once recorded in History; these are leftovers
            queue += db.execute(delete(models.DownloadQueueItem).where(models.DownloadQueueItem.status == "failed",
                                                                        models.DownloadQueueItem.added_at < now - timedelta(hours=1))).rowcount
            # resolved issues are noise after 30 days; dismissed ones carry the user's intent and stay
            # unless the title they point at is gone
            issues = db.execute(delete(models.LibraryIssue).where(models.LibraryIssue.status == "resolved",
                                                                   models.LibraryIssue.resolved_at < now - timedelta(days=30))).rowcount
            for media_type, model in (("movie", models.Movie), ("tv", models.Series)):
                issues += db.execute(delete(models.LibraryIssue).where(
                    models.LibraryIssue.status == "dismissed", models.LibraryIssue.media_type == media_type,
                    models.LibraryIssue.media_id.not_in(select(model.id)))).rowcount
            consistency = media_file_service.check_consistency(db)
        recycled = fileops.cleanup_recycle_bin(Path(settings.recycle_bin), settings.recycle_bin_days)
        return {"jobs": jobs, "history": history, "audit": audit_rows, "finished_queue": queue, "issues": issues, "recycle_folders": recycled, "consistency": consistency}

    return await asyncio.to_thread(run)


@job_queue.register("inspect_file", "Media Inspection (File)", priority=6, exclusive=False, max_attempts=2)
async def inspect_file(ctx: JobContext):
    from backend.services.inspection_service import inspect_file as _inspect_file
    media_file_id = int(ctx.payload["media_file_id"])
    force = bool(ctx.payload.get("force", False))
    outcome = await _inspect_file(media_file_id, force=force)
    return {"status": outcome.status, "error": outcome.error}


@job_queue.register("media_inspection", "Media Inspection (Backlog)", priority=7)
async def media_inspection(ctx: JobContext):
    from backend.services.inspection_service import inspect_backlog
    stats = await inspect_backlog(limit=200, progress=ctx.progress)
    if stats.get("status") == "more_pending":
        await asyncio.to_thread(
            job_queue.enqueue, "media_inspection", {}, trigger="event", dedupe_key="backlog", delay_seconds=10
        )
    return stats


# name -> (default interval minutes, enabled by default)
SCHEDULED_DEFAULTS: dict[str, tuple[int, bool]] = {
    "download_monitor": (1, True),
    "rss_sync": (15, True),
    "download_folder_scan": (5, True),
    "health_check": (15, True),
    "metadata_refresh": (12 * 60, True),
    "library_scan": (6 * 60, True),
    "media_inspection": (6 * 60, True),
    "missing_search": (24 * 60, False),
    "cleanup": (24 * 60, True),
}


def ensure_scheduled_tasks() -> None:
    settings = get_settings()
    with session_scope() as db:
        existing = {t.name for t in db.scalars(select(models.ScheduledTask))}
        for name, (interval, enabled) in SCHEDULED_DEFAULTS.items():
            if name not in existing:
                if name == "rss_sync":
                    interval = settings.rss_sync_interval_minutes
                db.add(models.ScheduledTask(name=name, interval_minutes=interval, enabled=enabled))
