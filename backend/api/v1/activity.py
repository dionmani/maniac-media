"""Search, grabs, queue, history, blocklist and wanted lists."""
from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, page_response
from backend.core.security import Permission
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, HistoryRepository, MovieRepository, QueueRepository, Repository
from backend.db.session import session_scope
from backend.domain.diagnosis import CHANGES_SETTINGS_FIX_KINDS, FixKind, ImportFailureReason
from backend.services import download_service, import_diagnosis, metadata_service, search_service
from backend.workers.queue import job_queue

router = APIRouter()
_OPTIONAL_IMPORT_BODY = Body(None)


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
async def retry_import(queue_id: int, user: Requires(Permission.IMPORT), body: schemas.ImportRequest | None = _OPTIONAL_IMPORT_BODY):
    # async because apply_fix is; the DB work runs in a thread so it never blocks the event loop
    def check() -> None:
        with session_scope() as db:
            q = db.get(models.DownloadQueueItem, queue_id)
            if q is None:
                raise HTTPException(404, "Queue item not found")
            if q.status not in ("completed", "import_failed"):
                raise HTTPException(409, f"Item is {q.status}; only completed downloads can be imported")

    await asyncio.to_thread(check)

    if body and body.fix:
        try:
            kind = FixKind(body.fix.kind)
        except ValueError:
            raise HTTPException(400, f"Unknown fix kind: {body.fix.kind}")
        if kind in CHANGES_SETTINGS_FIX_KINDS and Permission.SETTINGS_WRITE not in user.permissions:
            raise HTTPException(403, f"Missing permission: {Permission.SETTINGS_WRITE.value}")
        try:
            result = await import_diagnosis.apply_fix(queue_id, kind, body.fix.params, user.actor, bulk=False)
        except import_diagnosis.FixError as exc:
            raise HTTPException(400, str(exc))
        return result

    force = bool(body and body.strategy == "force")

    def requeue() -> int | None:
        with session_scope() as db:
            q = db.get(models.DownloadQueueItem, queue_id)
            if q is not None:
                q.status = "completed"
        return job_queue.enqueue("import_download", {"queue_id": queue_id, "force": force}, dedupe_key=str(queue_id))

    return {"job_id": await asyncio.to_thread(requeue)}


@router.post("/queue/{queue_id}/diagnose", tags=["queue"])
def diagnose_import(queue_id: int, db: DB, _: Requires(Permission.QUEUE_READ)):
    q = db.get(models.DownloadQueueItem, queue_id)
    if q is None:
        raise HTTPException(404, "Queue item not found")
    if not q.failure_reason:
        raise HTTPException(409, "This item has no recorded import failure to diagnose")
    analysis = import_diagnosis.diagnose(db, q, ImportFailureReason(q.failure_reason), (q.failure_analysis or {}).get("evidence"), q.error_message or "")
    q.failure_reason, q.failure_signature, q.failure_analysis = analysis.reason.value, analysis.signature, analysis.to_dict()
    db.commit()
    return analysis.to_dict()


@router.post("/queue/fixes", tags=["queue"])
async def bulk_fix(req: schemas.QueueFixRequest, user: Requires(Permission.IMPORT)):
    try:
        kind = FixKind(req.kind)
    except ValueError:
        raise HTTPException(400, f"Unknown fix kind: {req.kind}")
    if kind in CHANGES_SETTINGS_FIX_KINDS and Permission.SETTINGS_WRITE not in user.permissions:
        raise HTTPException(403, f"Missing permission: {Permission.SETTINGS_WRITE.value}")
    def find_sample() -> int | None:
        with session_scope() as db:
            return db.scalar(select(models.DownloadQueueItem.id).where(
                models.DownloadQueueItem.status == "import_failed", models.DownloadQueueItem.failure_signature == req.signature))

    sample = await asyncio.to_thread(find_sample)
    if sample is None:
        raise HTTPException(404, "No failed items match this signature")
    try:
        return await import_diagnosis.apply_fix(sample, kind, req.params, user.actor, bulk=True)
    except import_diagnosis.FixError as exc:
        raise HTTPException(400, str(exc))


@router.get("/activity/failures/summary", tags=["queue"])
def failures_summary(db: DB, _: Requires(Permission.QUEUE_READ), days: int = Query(7, ge=1, le=90)):
    return import_diagnosis.summary(db, days)


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
        catalog_days = get_settings().release_gate_catalog_days
        page = repo.paginate(repo.wanted_missing(date.today(), catalog_days), page=paging.page, page_size=paging.page_size,
                             sort=paging.sort or "title", order=paging.order)
        return page_response(page, [schemas.MovieSummary.model_validate(m, context={"catalog_days": catalog_days}) for m in page.items])
    repo = EpisodeRepository(db)
    page = repo.paginate(repo.wanted_missing(utcnow()), page=paging.page, page_size=paging.page_size,
                         sort=paging.sort or "air_date", order=paging.order if paging.sort else "desc")
    titles = dict(db.execute(select(models.Series.id, models.Series.title).where(models.Series.id.in_({e.series_id for e in page.items}))).all())
    return page_response(page, [schemas.EpisodeOut.model_validate(e).model_dump() | {"series_title": titles.get(e.series_id)} for e in page.items])


def _reconcile_cutoff_issues(db: DB) -> None:
    from backend.domain.issues import IssueType
    from backend.domain.quality import parse_quality
    from backend.services.issue_service import IssueRecorder
    from backend.services.quality_service import get_matcher, spec_for, to_spec
    from backend.services.search_service import compute_file_format_score

    profiles = {p.id: to_spec(p) for p in db.scalars(select(models.QualityProfile))}
    matcher = get_matcher(db)
    movie_rec = IssueRecorder(db, "movie")
    for m in db.scalars(select(models.Movie).where(models.Movie.has_file.is_(True))):
        movie_rec.touch(m.id, IssueType.UPGRADE_AVAILABLE)
        if not m.monitored or m.quality_profile_id is None:
            continue
        profile = profiles.get(m.quality_profile_id) or spec_for(db, m.quality_profile_id)
        if not profile or not profile.upgrade_allowed:
            continue
        q_below = not profile.cutoff_met(parse_quality(m.quality or m.file_path or "").quality)
        if q_below:
            movie_rec.report(
                IssueType.UPGRADE_AVAILABLE, "movie", m.id, m.file_path or "",
                f"{m.quality or 'Unknown'} is below the profile cutoff {profile.cutoff}",
                data={"current": m.quality, "cutoff": profile.cutoff},
            )
        elif profile.format_scores:
            current_fs, _ = compute_file_format_score(db, "movie", m.id, m.file_path, profile, matcher)
            if current_fs is not None and current_fs < profile.upgrade_until_format_score:
                movie_rec.report(
                    IssueType.UPGRADE_AVAILABLE, "movie", m.id, m.file_path or "",
                    f"Format score {current_fs} is below the profile cutoff {profile.upgrade_until_format_score}",
                    data={
                        "current": m.quality,
                        "cutoff": profile.cutoff,
                        "current_format_score": current_fs,
                        "upgrade_until_format_score": profile.upgrade_until_format_score,
                    },
                )
    movie_rec.finish()

    tv_rec = IssueRecorder(db, "tv")
    series_map = {s.id: (s.monitored, s.quality_profile_id) for s in db.scalars(select(models.Series))}
    for ep in db.scalars(select(models.Episode).where(models.Episode.has_file.is_(True))):
        tv_rec.touch(ep.series_id, IssueType.UPGRADE_AVAILABLE)
        monitored, profile_id = series_map.get(ep.series_id, (False, 1))
        if not (monitored and ep.monitored):
            continue
        profile = profiles.get(profile_id) or spec_for(db, profile_id)
        if not profile or not profile.upgrade_allowed or not ep.quality or ep.quality == "Detected (File)":
            continue
        code = f"S{ep.season_number:02d}E{ep.episode_number:02d}"
        q_below = not profile.cutoff_met(parse_quality(ep.quality).quality)
        if q_below:
            tv_rec.report(
                IssueType.UPGRADE_AVAILABLE, "tv", ep.series_id, ep.file_path or "",
                f"{code}: {ep.quality} is below the profile cutoff {profile.cutoff}",
                data={"current": ep.quality, "cutoff": profile.cutoff},
                episode_id=ep.id, discriminator=code,
            )
        elif profile.format_scores:
            current_fs, _ = compute_file_format_score(
                db, "tv", ep.series_id, ep.file_path, profile, matcher, episode_id=ep.id
            )
            if current_fs is not None and current_fs < profile.upgrade_until_format_score:
                tv_rec.report(
                    IssueType.UPGRADE_AVAILABLE, "tv", ep.series_id, ep.file_path or "",
                    f"{code}: format score {current_fs} is below the profile cutoff {profile.upgrade_until_format_score}",
                    data={
                        "current": ep.quality,
                        "cutoff": profile.cutoff,
                        "current_format_score": current_fs,
                        "upgrade_until_format_score": profile.upgrade_until_format_score,
                    },
                    episode_id=ep.id, discriminator=code,
                )
    tv_rec.finish()
    db.commit()


@router.get("/wanted/cutoff", tags=["wanted"])
def wanted_cutoff(db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging):
    _reconcile_cutoff_issues(db)
    stmt = select(models.LibraryIssue).where(models.LibraryIssue.issue_type == "upgrade_available", models.LibraryIssue.status == "open")

    class _R(Repository[models.LibraryIssue]):
        model = models.LibraryIssue
        default_sort = "id"
        sortable = {"id": models.LibraryIssue.id}

    page = _R(db).paginate(stmt, page=paging.page, page_size=paging.page_size)
    return page_response(page, [{"id": i.id, "media_type": i.media_type, "media_id": i.media_id, "path": i.path, "details": i.details} for i in page.items])
