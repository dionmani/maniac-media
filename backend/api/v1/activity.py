"""Search, grabs, queue, history, blocklist and wanted lists."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, page_response
from backend.core.security import Permission
from backend.db import models
from backend.db.repositories import EpisodeRepository, HistoryRepository, MovieRepository, QueueRepository, Repository
from backend.services import download_service, metadata_service, search_service
from backend.workers.queue import job_queue

router = APIRouter()


# ------------------------------------------------------------------ metadata lookup
@router.get("/lookup", tags=["search"])
async def lookup(_: Requires(Permission.MEDIA_READ), term: str = Query(..., min_length=1, max_length=200),
                 media_type: str = Query("movie", pattern="^(movie|tv)$"), year: int | None = None):
    results = await metadata_service.search(term, media_type, year)
    return [r.to_dict() | {"episodes": [], "episode_count": len(r.episodes)} for r in results]


# ------------------------------------------------------------------ release search
@router.get("/releases", tags=["search"])
async def interactive_search(_: Requires(Permission.SEARCH), media_type: str = Query(..., pattern="^(movie|tv)$"), media_id: int = Query(...),
                             season: int | None = None, episode: int | None = None):
    try:
        return await search_service.interactive(media_type, media_id, season, episode)
    except search_service.SearchError as exc:
        raise HTTPException(404, str(exc))


@router.post("/releases/grab", tags=["search"])
async def grab(req: schemas.GrabRequest, user: Requires(Permission.SEARCH)):
    release = search_service.release_from_cache(req.token)
    if release is None:
        raise HTTPException(410, "Search result expired – please search again")
    try:
        queue_id = await download_service.grab(release, media_type=req.media_type, media_id=req.media_id,
                                               season=req.season, episodes=req.episodes or None, actor=user.actor)
    except download_service.DownloadError as exc:
        raise HTTPException(400, str(exc))
    return {"queue_id": queue_id, "message": f"Sent '{release.title}' to the download client"}


@router.post("/search/automatic", tags=["search"])
def automatic_search(req: schemas.AutoSearchRequest, user: Requires(Permission.SEARCH)):
    job = job_queue.enqueue("automatic_search", {**req.model_dump(), "user_id": user.id, "username": user.username})
    return {"job_id": job, "message": "Automatic search queued"}


@router.post("/search/missing", tags=["search"])
def search_missing(user: Requires(Permission.SEARCH), limit: int = Query(50, ge=1, le=1000)):
    return {"job_id": job_queue.enqueue("missing_search", {"limit": limit}, dedupe_key="manual")}


# ------------------------------------------------------------------ queue
@router.get("/queue", tags=["queue"])
def get_queue(db: DB, _: Requires(Permission.QUEUE_READ), include_finished: bool = False):
    return [schemas.QueueItemOut.model_validate(q) for q in QueueRepository(db).for_display(include_finished)]


@router.delete("/queue/{queue_id}", response_model=schemas.Message, tags=["queue"])
async def remove_queue_item(queue_id: int, user: Requires(Permission.QUEUE_WRITE), remove_from_client: bool = True,
                            blocklist: bool = False, delete_data: bool = False):
    try:
        await download_service.remove(queue_id, remove_from_client=remove_from_client, blocklist=blocklist, delete_data=delete_data, actor=user.actor)
    except download_service.DownloadError as exc:
        raise HTTPException(404, str(exc))
    return schemas.Message(message="Removed from queue")


@router.post("/queue/{queue_id}/retry", response_model=schemas.Message, tags=["queue"])
async def retry_queue_item(queue_id: int, user: Requires(Permission.QUEUE_WRITE)):
    try:
        await download_service.retry(queue_id, user.actor)
    except download_service.DownloadError as exc:
        raise HTTPException(400, str(exc))
    return schemas.Message(message="Download re-sent to client")


@router.post("/queue/{queue_id}/import", tags=["queue"])
def retry_import(queue_id: int, db: DB, user: Requires(Permission.IMPORT)):
    q = db.get(models.DownloadQueueItem, queue_id)
    if q is None:
        raise HTTPException(404, "Queue item not found")
    if q.status not in ("completed", "import_failed"):
        raise HTTPException(409, f"Item is {q.status}; only completed downloads can be imported")
    q.status = "completed"
    db.commit()
    return {"job_id": job_queue.enqueue("import_download", {"queue_id": queue_id}, dedupe_key=str(queue_id))}


@router.post("/queue/refresh", tags=["queue"])
def refresh_queue(_: Requires(Permission.QUEUE_READ)):
    return {"job_id": job_queue.enqueue("download_monitor", dedupe_key="manual")}


# ------------------------------------------------------------------ history & blocklist
@router.get("/history", tags=["history"])
def history(db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging, event_type: str | None = None,
            media_type: str | None = None, media_id: int | None = None):
    stmt = select(models.History)
    if event_type:
        stmt = stmt.where(models.History.event_type.in_(event_type.split(",")))
    if media_type:
        stmt = stmt.where(models.History.media_type == media_type)
    if media_id:
        stmt = stmt.where(models.History.media_id == media_id)
    page = HistoryRepository(db).paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort or "timestamp",
                                          order=paging.order if paging.sort else "desc")
    return page_response(page, [schemas.HistoryOut.model_validate(h) for h in page.items])


class _BlocklistRepo(Repository[models.Blocklist]):
    model = models.Blocklist
    default_sort = "created"
    sortable = {"created": models.Blocklist.created_at, "id": models.Blocklist.id}


@router.get("/blocklist", tags=["history"])
def blocklist(db: DB, _: Requires(Permission.QUEUE_READ), paging: Paging):
    page = _BlocklistRepo(db).paginate(select(models.Blocklist), page=paging.page, page_size=paging.page_size, sort="created", order="desc")
    return page_response(page, [schemas.BlocklistOut.model_validate(b) for b in page.items])


@router.delete("/blocklist/{item_id}", response_model=schemas.Message, tags=["history"])
def unblock(item_id: int, db: DB, _: Requires(Permission.QUEUE_WRITE)):
    row = db.get(models.Blocklist, item_id)
    if row is None:
        raise HTTPException(404, "Not found")
    db.delete(row)
    db.commit()
    return schemas.Message(message="Removed from blocklist")


# ------------------------------------------------------------------ wanted
@router.get("/wanted/missing", tags=["wanted"])
def wanted_missing(db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging, media_type: str = Query("movie", pattern="^(movie|tv)$")):
    if media_type == "movie":
        repo = MovieRepository(db)
        page = repo.paginate(repo.wanted_missing(), page=paging.page, page_size=paging.page_size, sort=paging.sort or "title", order=paging.order)
        return page_response(page, [schemas.MovieSummary.model_validate(m) for m in page.items])
    repo = EpisodeRepository(db)
    page = repo.paginate(repo.wanted_missing(date.today().isoformat()), page=paging.page, page_size=paging.page_size,
                         sort=paging.sort or "air_date", order=paging.order if paging.sort else "desc")
    titles = dict(db.execute(select(models.Series.id, models.Series.title).where(models.Series.id.in_({e.series_id for e in page.items}))).all())
    return page_response(page, [schemas.EpisodeOut.model_validate(e).model_dump() | {"series_title": titles.get(e.series_id)} for e in page.items])


@router.get("/wanted/cutoff", tags=["wanted"])
def wanted_cutoff(db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging):
    stmt = select(models.LibraryIssue).where(models.LibraryIssue.issue_type == "upgrade_available", models.LibraryIssue.resolved.is_(False))

    class _R(Repository[models.LibraryIssue]):
        model = models.LibraryIssue
        default_sort = "id"
        sortable = {"id": models.LibraryIssue.id}

    page = _R(db).paginate(stmt, page=paging.page, page_size=paging.page_size)
    return page_response(page, [{"id": i.id, "media_type": i.media_type, "media_id": i.media_id, "path": i.path, "details": i.details} for i in page.items])
