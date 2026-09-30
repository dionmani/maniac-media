"""Library operations: scanning, issues, manual import, renaming."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, page_response
from backend.core.security import Permission
from backend.db import models
from backend.db.repositories import Repository
from backend.domain.issues import REGISTRY
from backend.services import import_service, issue_service, renamer_service, scanner_service
from backend.workers.queue import job_queue

router = APIRouter(tags=["library"])


@router.post("/library/scan")
def scan(req: schemas.ScanRequest, user: Requires(Permission.LIBRARY_SCAN)):
    if not req.incremental:
        scanner_service.reset_scan_state("movie" if req.scope == "movies" else "tv" if req.scope == "tv" else None)
    job = job_queue.enqueue("library_scan", req.model_dump(), dedupe_key=f"scan:{req.scope}")
    return {"job_id": job, "message": f"{'Incremental' if req.incremental else 'Full'} {req.scope} scan queued"}


class _IssueRepo(Repository[models.LibraryIssue]):
    model = models.LibraryIssue
    default_sort = "detected"
    sortable = {
        "detected": models.LibraryIssue.first_detected_at, "id": models.LibraryIssue.id, "type": models.LibraryIssue.issue_type,
        "last_seen": models.LibraryIssue.last_seen_at, "severity": models.LibraryIssue.severity,
    }


@router.get("/library/issues")
def issues(db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging, issue_type: str | None = None, media_type: str | None = None,
           media_id: int | None = None, severity: Literal["info", "warning", "error"] | None = None,
           status: Literal["open", "resolved", "dismissed", "all"] = "open"):
    stmt = select(models.LibraryIssue)
    if status != "all":
        stmt = stmt.where(models.LibraryIssue.status == status)
    if issue_type:
        stmt = stmt.where(models.LibraryIssue.issue_type == issue_type)
    if media_type:
        stmt = stmt.where(models.LibraryIssue.media_type == media_type)
    if media_id is not None:
        stmt = stmt.where(models.LibraryIssue.media_id == media_id)
    if severity:
        stmt = stmt.where(models.LibraryIssue.severity == severity)
    page = _IssueRepo(db).paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort, order=paging.order)
    return page_response(page, [issue_service.serialize(i) for i in page.items])


@router.get("/library/issues/summary")
def issues_summary(db: DB, _: Requires(Permission.MEDIA_READ)):
    return issue_service.summary(db)


@router.get("/library/issue-types")
def issue_types(_: Requires(Permission.MEDIA_READ)):
    return [spec.to_dict() for spec in REGISTRY.values()]


def _apply(db, user, issue: models.LibraryIssue, action: str, params: dict) -> str:
    if action == "delete_file" and Permission.MEDIA_DELETE not in user.permissions:
        raise HTTPException(403, f"Missing permission: {Permission.MEDIA_DELETE.value}")
    try:
        return issue_service.resolve(db, issue, action, params, user.actor)
    except issue_service.IssueActionError as exc:
        raise HTTPException(422, str(exc))


@router.post("/library/issues/bulk")
def bulk_issues(req: schemas.IssueBulkRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    rows = {i.id: i for i in db.scalars(select(models.LibraryIssue).where(models.LibraryIssue.id.in_(req.ids)))}
    done, failed = 0, []
    for issue_id in dict.fromkeys(req.ids):
        issue = rows.get(issue_id)
        if issue is None:
            failed.append({"id": issue_id, "error": "Issue not found"})
            continue
        try:
            with db.begin_nested():
                _apply(db, user, issue, req.action, req.params)
            done += 1
        except HTTPException as exc:
            failed.append({"id": issue_id, "error": exc.detail})
    if not done and failed and all(f["error"] != "Issue not found" for f in failed):
        raise HTTPException(422, failed[0]["error"])  # the action itself is invalid for these issues
    db.commit()
    issue_service.publish_changed(db)
    return {"processed": done, "failed": failed}


@router.post("/library/issues/{issue_id}/resolve", response_model=schemas.Message)
def resolve_issue(issue_id: int, db: DB, user: Requires(Permission.MEDIA_WRITE), req: schemas.IssueActionRequest | None = None):
    issue = db.get(models.LibraryIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    req = req or schemas.IssueActionRequest()
    message = _apply(db, user, issue, req.action, req.params)
    db.commit()
    issue_service.publish_changed(db)
    return schemas.Message(message=message)


@router.post("/library/manual-import/scan")
async def manual_import_scan(req: schemas.ManualImportScanRequest, _: Requires(Permission.IMPORT)):
    try:
        return await asyncio.to_thread(import_service.manual_scan, req.folder)
    except import_service.ImportError_ as exc:
        raise HTTPException(404, str(exc))


@router.post("/library/manual-import")
async def manual_import(req: schemas.ManualImportRequest, user: Requires(Permission.IMPORT)):
    result = await asyncio.to_thread(import_service.manual_import, [i.model_dump() for i in req.items], req.mode, user.actor)
    return {"imported": result.imported, "failed": result.failed, "failed_paths": result.failed_paths}


@router.post("/library/manual-import/match")
async def manual_import_match(req: schemas.ManualImportItem, _: Requires(Permission.IMPORT)):
    try:
        return await asyncio.to_thread(import_service.annotate_match, req.path, req.media_type, req.media_id, req.season, req.episodes)
    except import_service.ImportError_ as exc:
        raise HTTPException(404, str(exc))


@router.post("/library/manual-import/delete")
async def manual_import_delete(req: schemas.ManualImportDeleteRequest, user: Requires(Permission.IMPORT)):
    result = await asyncio.to_thread(import_service.delete_candidates, req.paths, user.actor)
    return {"deleted": result.deleted, "failed": result.failed}


@router.get("/library/download-folder")
async def download_folder_report(_: Requires(Permission.QUEUE_READ)):
    """Current state of the watch folder: completed, partial, corrupt and untracked downloads."""
    return await asyncio.to_thread(import_service.scan_download_folder)


# ------------------------------------------------------------------ renamer
@router.get("/rename/preview")
async def rename_preview(_: Requires(Permission.MEDIA_READ), media_type: str = Query("all", pattern="^(movie|tv|all)$"),
                         include_folders: bool = False, media_id: int | None = None):
    return await asyncio.to_thread(renamer_service.preview, media_type, include_folders=include_folders, media_id=media_id)


@router.post("/rename")
async def rename(req: schemas.RenameRequest, user: Requires(Permission.RENAME)):
    items = [i.model_dump() for i in req.items]
    if req.dry_run:
        return await asyncio.to_thread(renamer_service.execute, items, dry_run=True, include_folders=req.include_folders, actor=user.actor)
    if len(items) > 50:  # big batches run in the background with progress events
        job = job_queue.enqueue("rename", {"items": items, "include_folders": req.include_folders, "user_id": user.id, "username": user.username})
        return {"job_id": job, "queued": len(items)}
    return await asyncio.to_thread(renamer_service.execute, items, dry_run=False, include_folders=req.include_folders, actor=user.actor)
