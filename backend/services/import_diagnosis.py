"""Import failure diagnosis: a cheap probe pipeline that turns an :class:`ImportError_` into an
:class:`ImportFailureAnalysis` (reason, evidence, suggested fixes), plus the services that apply
a fix and aggregate failures for the Activity "Failures" view.

Probes only stat/list the filesystem (no network) and are bounded, so they're safe to run
synchronously right after a failure, in the same worker thread that ran the import.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.session import session_scope
from backend.domain.diagnosis import (
    FixKind,
    FixSuggestion,
    ImportFailureAnalysis,
    ImportFailureReason,
    infer_path_mapping,
)
from backend.domain.parser import is_sample, is_video, normalise_path_key
from backend.plugins.download_clients._common import map_remote_path
from backend.services.audit import Actor, audit
from backend.workers.queue import job_queue

log = logging.getLogger("maniac.import_diagnosis")

_MAX_SEARCH_ENTRIES = 5000
_MAX_SEARCH_DEPTH = 3
ARCHIVE_PART_RX = re.compile(r"\.(rar|r\d{2,3}|zip|7z)$", re.IGNORECASE)
DISC_MARKERS = {"video_ts", "bdmv", "certificate"}


class FixError(RuntimeError):
    """A fix could not be applied (bad params, missing path, unknown client…)."""


def _now() -> str:
    return utcnow().isoformat()


# --------------------------------------------------------------------------- probes
def _search_basename(root: Path, basename: str) -> list[Path]:
    """Bounded search for a file/folder with the same name under ``root`` (depth <= 3, <= 5000 entries)."""
    found: list[Path] = []
    scanned = 0
    stem = Path(basename).stem.lower()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and scanned < _MAX_SEARCH_ENTRIES:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > _MAX_SEARCH_ENTRIES:
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name == basename:
                    found.append(Path(entry.path))
                if depth < _MAX_SEARCH_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
            elif entry.name == basename or Path(entry.name).stem.lower() == stem:
                found.append(Path(entry.path))
    return found


def _probe_path_not_found(db: Session, q: models.DownloadQueueItem, evidence: dict) -> ImportFailureAnalysis:
    download_path = evidence.get("download_path") or q.download_path
    client = db.get(models.DownloadClient, q.download_client_id) if q.download_client_id else None
    basename = Path(download_path).name if download_path else None
    candidates: list[Path] = []
    root = Path(get_settings().download_folder)
    if basename and root.exists():
        candidates = _search_basename(root, basename)
    mapping = infer_path_mapping(download_path, [str(c) for c in candidates]) if download_path and candidates else None
    if mapping and client is not None:
        remote, local = mapping
        return ImportFailureAnalysis(
            reason=ImportFailureReason.REMOTE_PATH_MAPPING,
            summary=(f"'{client.name}' reports download paths as {remote}, but Maniac Media sees them at {local}. "
                     "Fixing the path mapping will unblock every failure from this client."),
            evidence={"download_path": download_path, "found_at": str(candidates[0]), "suggested_remote_path": remote,
                     "suggested_local_path": local, "client_id": client.id, "client_name": client.name},
            fixes=(FixSuggestion(
                kind=FixKind.APPLY_PATH_MAPPING,
                label=f"Map {remote} → {local} on client '{client.name}'",
                params={"client_id": client.id, "remote_path": remote, "local_path": local},
                safe_auto=False, changes_settings=True, bulk_applicable=True,
            ),),
            signature=f"remote_path_mapping:client={client.id}:{remote}",
            confidence=0.9, analysed_at=_now(),
        )
    return ImportFailureAnalysis(
        reason=ImportFailureReason.PATH_NOT_FOUND,
        summary=f"The download path {download_path} does not exist and no matching file was found under the download folder.",
        evidence={"download_path": download_path},
        fixes=(
            FixSuggestion(FixKind.MANUAL_IMPORT, "Look for it in manual import", {"folder": str(root)}),
            FixSuggestion(FixKind.REMOVE, "Remove from queue"),
        ),
        signature=f"path_not_found:{normalise_path_key(download_path or '')}",
        confidence=0.5, analysed_at=_now(),
    )


def _probe_no_video_files(q: models.DownloadQueueItem) -> ImportFailureAnalysis:
    path = Path(q.download_path) if q.download_path else None
    names: list[str] = []
    if path and path.exists():
        try:
            names = [p.name for p in path.iterdir()] if path.is_dir() else [path.name]
        except OSError:
            names = []
    lower = [n.lower() for n in names]
    videos = [n for n in names if is_video(n)]

    if any(n.endswith(".iso") for n in lower) or any(n in DISC_MARKERS for n in lower):
        reason, summary = ImportFailureReason.UNSUPPORTED_FORMAT, "This release is a disc image (ISO/BDMV), which Maniac Media does not import directly."
        fixes = (FixSuggestion(FixKind.BLOCKLIST_AND_SEARCH, "Blocklist and search for a different release"),)
    elif any(ARCHIVE_PART_RX.search(n) for n in lower):
        reason = ImportFailureReason.ARCHIVE_NOT_EXTRACTED
        summary = ("The download contains an unextracted archive. SABnzbd/NZBGet can extract it natively; qBittorrent "
                  "needs a post-processing script.")
        fixes = (FixSuggestion(FixKind.OPEN_SETTINGS, "Open download client settings", {"page": "download-clients"}),
                FixSuggestion(FixKind.RETRY, "Retry import"))
    elif videos and all(is_sample(n) for n in videos):
        reason, summary = ImportFailureReason.SAMPLE_ONLY, "Only a sample video was found in this download."
        fixes = (FixSuggestion(FixKind.BLOCKLIST_AND_SEARCH, "Blocklist and search for a different release",
                               safe_auto=get_settings().auto_retry_failed_downloads),)
    else:
        reason, summary = ImportFailureReason.NO_VIDEO_FILES, "No video files were found in this download."
        fixes = (FixSuggestion(FixKind.BLOCKLIST_AND_SEARCH, "Blocklist and search for a different release"),
                FixSuggestion(FixKind.REMOVE, "Remove from queue"))
    return ImportFailureAnalysis(
        reason=reason, summary=summary, evidence={"download_path": str(path) if path else None, "entries": names[:50]},
        fixes=fixes, signature=f"{reason.value}:{q.media_type}:{q.media_id}", confidence=0.7, analysed_at=_now(),
    )


_FS_REASONS = (ImportFailureReason.PERMISSION_DENIED, ImportFailureReason.DISK_FULL, ImportFailureReason.CROSS_DEVICE_FAILED,
              ImportFailureReason.PATH_TOO_LONG, ImportFailureReason.DESTINATION_EXISTS)


def _probe_filesystem(reason: ImportFailureReason, evidence: dict) -> ImportFailureAnalysis:
    path = evidence.get("path")
    extra: dict = {}
    fixes: tuple[FixSuggestion, ...] = (FixSuggestion(FixKind.RETRY, "Retry import"),)
    if reason == ImportFailureReason.PERMISSION_DENIED:
        p = Path(path) if path else None
        if p is not None:
            extra["source_readable"] = os.access(p, os.R_OK) if p.exists() else None
            extra["destination_writable"] = os.access(p.parent, os.W_OK) if p.parent.exists() else None
        extra["docker"] = Path("/.dockerenv").exists()
        summary = f"Permission denied accessing {path}. Check filesystem permissions (and PUID/PGID if running in Docker)."
    elif reason == ImportFailureReason.DISK_FULL:
        try:
            target = Path(path).parent if path and Path(path).parent.exists() else Path(path)
            extra["free_bytes"] = shutil.disk_usage(target).free
        except (OSError, TypeError):
            pass
        summary = f"Not enough free disk space to place the file at {path}."
    elif reason == ImportFailureReason.CROSS_DEVICE_FAILED:
        summary = "Source and destination are on different volumes and the cross-device fallback failed."
    elif reason == ImportFailureReason.PATH_TOO_LONG:
        summary = f"The destination path is too long: {path}"
        fixes = (FixSuggestion(FixKind.OPEN_SETTINGS, "Review naming templates", {"page": "naming"}),)
    else:  # DESTINATION_EXISTS
        summary = f"A file already exists at the destination: {path}"
        fixes = (FixSuggestion(FixKind.MANUAL_IMPORT, "Review in manual import", {"folder": str(Path(path).parent) if path else ""}),
                FixSuggestion(FixKind.RETRY, "Retry import"))
    return ImportFailureAnalysis(
        reason=reason, summary=summary, evidence={**evidence, **extra}, fixes=fixes,
        signature=f"{reason.value}:{normalise_path_key(path or '')}", confidence=0.8, analysed_at=_now(),
    )


def _probe_bad_file(reason: ImportFailureReason, evidence: dict) -> ImportFailureAnalysis:
    file = evidence.get("file")
    detail = evidence.get("detail")
    summary = f"{file}: {detail or reason.value.replace('_', ' ')}" if file else (detail or reason.value.replace("_", " "))
    fixes = (FixSuggestion(FixKind.BLOCKLIST_AND_SEARCH, "Blocklist and search for a different release",
                          safe_auto=get_settings().auto_retry_failed_downloads),)
    return ImportFailureAnalysis(reason=reason, summary=summary, evidence=evidence, fixes=fixes,
                                 signature=f"{reason.value}:{file or ''}", confidence=0.85, analysed_at=_now())


def _probe_unparseable(reason: ImportFailureReason, evidence: dict, q: models.DownloadQueueItem) -> ImportFailureAnalysis:
    folder = str(Path(q.download_path).parent) if q.download_path else ""
    summary = f"Could not determine the episode for {evidence.get('file', q.release_title)}."
    fixes = (FixSuggestion(FixKind.MANUAL_IMPORT, "Open in manual import", {"folder": folder}),)
    return ImportFailureAnalysis(reason=reason, summary=summary, evidence=evidence, fixes=fixes,
                                 signature=f"{reason.value}:{q.media_type}:{q.media_id}", confidence=0.6, analysed_at=_now())


_QUALITY_LABELS = {
    ImportFailureReason.NOT_AN_UPGRADE: "isn't an upgrade over the existing file",
    ImportFailureReason.DUPLICATE: "is a duplicate of the existing file",
    ImportFailureReason.QUALITY_NOT_ALLOWED: "isn't allowed by the quality profile",
}


def _probe_quality(reason: ImportFailureReason, evidence: dict, q: models.DownloadQueueItem) -> ImportFailureAnalysis:
    summary = f"{q.release_title} {_QUALITY_LABELS[reason]}."
    fixes = (FixSuggestion(FixKind.FORCE_IMPORT, "Force import anyway", {"queue_id": q.id}),
            FixSuggestion(FixKind.REMOVE, "Remove from queue"))
    return ImportFailureAnalysis(reason=reason, summary=summary, evidence=evidence, fixes=fixes,
                                 signature=f"{reason.value}:{q.media_type}:{q.media_id}", confidence=0.9, analysed_at=_now())


def _probe_naming(evidence: dict) -> ImportFailureAnalysis:
    fixes = (FixSuggestion(FixKind.OPEN_SETTINGS, "Open naming settings", {"page": "naming"}),)
    return ImportFailureAnalysis(reason=ImportFailureReason.NAMING_TEMPLATE_ERROR, summary=f"Naming template error: {evidence.get('error')}",
                                 evidence=evidence, fixes=fixes, signature="naming_template_error", confidence=0.95, analysed_at=_now())


def _probe_media_deleted(evidence: dict, q: models.DownloadQueueItem) -> ImportFailureAnalysis:
    fixes = (FixSuggestion(FixKind.REMOVE, "Remove from queue"),)
    return ImportFailureAnalysis(reason=ImportFailureReason.MEDIA_DELETED, summary="The library item this download was for no longer exists.",
                                 evidence=evidence, fixes=fixes, signature=f"media_deleted:{q.media_type}:{q.media_id}",
                                 confidence=1.0, analysed_at=_now())


def _probe_unknown(evidence: dict, message: str) -> ImportFailureAnalysis:
    fixes = (FixSuggestion(FixKind.RETRY, "Retry import"),)
    return ImportFailureAnalysis(reason=ImportFailureReason.UNKNOWN, summary=(message or "Unknown import failure")[:300],
                                 evidence=evidence, fixes=fixes, signature="unknown", confidence=0.3, analysed_at=_now())


def diagnose(db: Session, q: models.DownloadQueueItem, reason: ImportFailureReason, evidence: dict | None, message: str) -> ImportFailureAnalysis:
    """Probe the system for the root cause of ``reason`` and return a full analysis.

    Never raises: a probe that fails to produce a confident answer falls back to the generic
    "unknown" analysis rather than breaking the import failure path.
    """
    evidence = dict(evidence or {})
    try:
        if reason == ImportFailureReason.PATH_NOT_FOUND:
            return _probe_path_not_found(db, q, evidence)
        if reason == ImportFailureReason.NO_VIDEO_FILES:
            return _probe_no_video_files(q)
        if reason in _FS_REASONS:
            return _probe_filesystem(reason, evidence)
        if reason in (ImportFailureReason.CORRUPT_FILE, ImportFailureReason.TOO_SMALL, ImportFailureReason.SAMPLE_ONLY,
                     ImportFailureReason.UNSUPPORTED_FORMAT, ImportFailureReason.ARCHIVE_NOT_EXTRACTED):
            return _probe_bad_file(reason, evidence)
        if reason in (ImportFailureReason.UNPARSEABLE_EPISODE, ImportFailureReason.NOT_MATCHED):
            return _probe_unparseable(reason, evidence, q)
        if reason in _QUALITY_LABELS:
            return _probe_quality(reason, evidence, q)
        if reason == ImportFailureReason.NAMING_TEMPLATE_ERROR:
            return _probe_naming(evidence)
        if reason == ImportFailureReason.MEDIA_DELETED:
            return _probe_media_deleted(evidence, q)
    except Exception:
        log.exception("Diagnosis probe failed for queue item %s", q.id)
    return _probe_unknown(evidence, message)


# --------------------------------------------------------------------------- fixes
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _bulk_targets(queue_id: int, bulk: bool) -> list[int]:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            raise FixError("Queue item not found")
        signature = q.failure_signature
        if not bulk or not signature:
            return [queue_id]
        return [row for row in db.scalars(select(models.DownloadQueueItem.id).where(
            models.DownloadQueueItem.status == "import_failed", models.DownloadQueueItem.failure_signature == signature
        ))]


def _requeue(targets: list[int]) -> None:
    for qid in targets:
        job_queue.enqueue("import_download", {"queue_id": qid}, dedupe_key=str(qid))


def _apply_path_mapping(client_id: int, remote_path: str | None, local_path: str, targets: list[int], actor: Actor) -> None:
    if not local_path or not Path(local_path).is_dir():
        raise FixError(f"Local path does not exist or is not a directory: {local_path}")
    with session_scope() as db:
        client = db.get(models.DownloadClient, client_id)
        if client is None:
            raise FixError("Download client not found")
        client.remote_path, client.local_path = remote_path, local_path
        audit(db, "settings", "download_client.path_mapping", actor=actor, entity_type="download_client", entity_id=client_id,
             details={"remote_path": remote_path, "local_path": local_path})
        # Re-apply the (now correct) mapping to paths already recorded on the queue – otherwise
        # a retry would read the same unmapped path and fail the exact same way.
        for qid in targets:
            q = db.get(models.DownloadQueueItem, qid)
            if q is not None and q.download_path:
                q.download_path = map_remote_path(q.download_path, {"remote_path": remote_path, "local_path": local_path})
    _requeue(targets)


def _create_folder(path: str, targets: list[int]) -> None:
    folder = Path(path)
    settings = get_settings()
    roots = [Path(settings.media_root_movies), Path(settings.media_root_tv)]
    if not folder.parts or not any(_is_within(folder, root) for root in roots):
        raise FixError("Folder is outside the configured media roots")
    folder.mkdir(parents=True, exist_ok=True)
    _requeue(targets)


def _error_message(queue_id: int) -> str:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        return (q.error_message if q else None) or "Import failed"


async def apply_fix(queue_id: int, kind: FixKind, params: dict, actor: Actor, *, bulk: bool = False) -> dict:
    """Apply one catalogued fix, optionally to every queue item sharing the same failure signature.

    Async (some fixes call async download-service code), so every DB / filesystem step is pushed
    to a worker thread rather than run on the event loop.
    """
    params = params or {}
    targets = await asyncio.to_thread(_bulk_targets, queue_id, bulk)

    if kind == FixKind.APPLY_PATH_MAPPING:
        await asyncio.to_thread(_apply_path_mapping, int(params["client_id"]), params.get("remote_path"),
                                str(params.get("local_path") or ""), targets, actor)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.CREATE_FOLDER:
        await asyncio.to_thread(_create_folder, str(params.get("path") or ""), targets)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.FORCE_IMPORT:
        from backend.services import import_service

        outcomes = {}
        for qid in targets:
            try:
                outcomes[qid] = await asyncio.to_thread(import_service.import_queue_item, qid, True)
            except import_service.ImportError_ as exc:
                outcomes[qid] = str(exc)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.RETRY_WHEN_STABLE:
        def stable() -> None:
            for qid in targets:
                job_queue.enqueue("import_download", {"queue_id": qid}, delay_seconds=300, dedupe_key=f"stable:{qid}")

        await asyncio.to_thread(stable)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.RETRY:
        await asyncio.to_thread(_requeue, targets)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.BLOCKLIST_AND_SEARCH:
        from backend.services import download_service

        for qid in targets:
            error = await asyncio.to_thread(_error_message, qid)
            await download_service.handle_failed(qid, error, release_at_fault=True)
        return {"applied": kind.value, "retried": len(targets)}

    if kind == FixKind.REMOVE:
        from backend.services import download_service

        for qid in targets:
            await download_service.remove(qid, remove_from_client=bool(params.get("remove_from_client", False)),
                                          blocklist=bool(params.get("blocklist", False)), delete_data=bool(params.get("delete_data", False)),
                                          actor=actor)
        return {"applied": kind.value, "retried": len(targets)}

    if kind in (FixKind.MANUAL_IMPORT, FixKind.OPEN_SETTINGS):
        return {"applied": kind.value, "deep_link": params}

    raise FixError(f"Unsupported fix: {kind}")


# --------------------------------------------------------------------------- aggregate
def summary(db: Session, days: int = 7) -> dict:
    """Group current import failures by root cause so a single fix can clear many rows at once."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(days=max(1, days))
    rows = list(db.scalars(select(models.DownloadQueueItem).where(
        models.DownloadQueueItem.status == "import_failed", models.DownloadQueueItem.updated_at >= cutoff,
    )))
    by_reason: dict[str, int] = {}
    by_signature: dict[str, list[models.DownloadQueueItem]] = {}
    for r in rows:
        reason = r.failure_reason or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_signature.setdefault(r.failure_signature or reason, []).append(r)

    top = []
    for sig, items in sorted(by_signature.items(), key=lambda kv: -len(kv[1]))[:20]:
        first = items[0]
        fix = None
        fixes = (first.failure_analysis or {}).get("fixes") or []
        if fixes:
            bulkable = [f for f in fixes if f.get("bulk_applicable")]
            fix = (bulkable or fixes)[0]
        top.append({
            "signature": sig, "reason": first.failure_reason or "unknown", "count": len(items),
            "examples": [i.title for i in items[:5]], "fix": fix,
        })
    return {
        "total": len(rows),
        "by_reason": [{"reason": k, "count": v} for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])],
        "top_signatures": top,
    }
