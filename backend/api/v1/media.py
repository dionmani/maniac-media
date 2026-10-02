"""Movies & series endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, page_response
from backend.core.events import EventType, bus
from backend.core.security import Permission
from backend.core.settings import get_settings
from backend.db import models
from backend.db.repositories import MediaFileRepository, MovieRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain.parser import parse_external_id
from backend.domain.types import MediaMetadata
from backend.services import media_service, metadata_service, renamer_service
from backend.workers.queue import job_queue

router = APIRouter()


# ================================================================== movies
@router.get("/movies", tags=["movies"])
def list_movies(
    db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging,
    search: str | None = Query(None, max_length=200), status: str | None = None, monitored: bool | None = None,
    has_file: bool | None = None, genre: str | None = None, year_from: int | None = None, year_to: int | None = None,
    quality_profile_id: int | None = None,
):
    repo = MovieRepository(db)
    stmt = repo.query(search=search, status=status, monitored=monitored, has_file=has_file, genre=genre,
                      year_from=year_from, year_to=year_to, quality_profile_id=quality_profile_id)
    page = repo.paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort, order=paging.order)
    return page_response(page, [schemas.MovieSummary.model_validate(m) for m in page.items])


@router.get("/movies/lookup", tags=["movies"])
async def lookup_movies(_: Requires(Permission.MEDIA_READ), query: str, year: int | None = None):
    return await metadata_service.search(query, "movie", year)


@router.get("/movies/{movie_id}", response_model=schemas.MovieOut, tags=["movies"])
def get_movie(movie_id: int, db: DB, _: Requires(Permission.MEDIA_READ)):
    movie = db.get(models.Movie, movie_id)
    if movie is None:
        raise HTTPException(404, "Movie not found")
    return movie


@router.post("/movies", response_model=schemas.MovieOut, status_code=201, tags=["movies"])
async def add_movie(req: schemas.AddMediaRequest, user: Requires(Permission.MEDIA_WRITE)):
    meta = await metadata_service.fetch_movie(tmdb_id=req.tmdb_id, imdb_id=req.imdb_id) if (req.tmdb_id or req.imdb_id) else None
    if meta is None:
        meta = MediaMetadata(media_type="movie", provider="manual", title=req.title, year=req.year, tmdb_id=req.tmdb_id, imdb_id=req.imdb_id)

    def persist():
        with session_scope() as db:
            try:
                movie = media_service.add_movie(db, meta, quality_profile_id=req.quality_profile_id, root_folder=req.root_folder,
                                                monitored=req.monitored, actor=user.actor)
            except media_service.ConflictError as exc:
                raise HTTPException(409, str(exc))
            db.flush()
            return schemas.MovieOut.model_validate(movie)

    out = await asyncio.to_thread(persist)
    bus.publish(EventType.MEDIA_ADDED, media_type="movie", media_id=out.id, title=out.title)
    if req.search_on_add or get_settings().search_on_add:
        await asyncio.to_thread(job_queue.enqueue, "automatic_search", {"media_type": "movie", "media_id": out.id, "user_id": user.id, "username": user.username})
    return out


@router.patch("/movies/{movie_id}", response_model=schemas.MovieOut, tags=["movies"])
def update_movie(movie_id: int, req: schemas.MovieUpdate, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    movie = db.get(models.Movie, movie_id)
    if movie is None:
        raise HTTPException(404, "Movie not found")
    media_service.update_movie(db, movie, req.model_dump(exclude_unset=True), user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_UPDATED, media_type="movie", media_id=movie.id)
    return movie


@router.delete("/movies/{movie_id}", response_model=schemas.Message, tags=["movies"])
def delete_movie(movie_id: int, db: DB, user: Requires(Permission.MEDIA_DELETE), delete_files: bool = False):
    movie = db.get(models.Movie, movie_id)
    if movie is None:
        raise HTTPException(404, "Movie not found")
    title = movie.title
    media_service.delete_movie(db, movie, delete_files=delete_files, actor=user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_DELETED, media_type="movie", media_id=movie_id)
    return schemas.Message(message=f"'{title}' removed" + (" (file moved to recycle bin)" if delete_files else ""))


@router.post("/movies/{movie_id}/refresh", tags=["movies"])
def refresh_movie(movie_id: int, user: Requires(Permission.MEDIA_WRITE)):
    job = job_queue.enqueue("metadata_refresh", {"media_type": "movie", "ids": [movie_id]}, dedupe_key=f"movie:{movie_id}")
    return {"job_id": job}


@router.post("/movies/bulk", tags=["movies"])
def bulk_movies(req: schemas.BulkEditRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    """Bulk edit: {"ids": [...], "monitored": bool?, "quality_profile_id": int?}."""
    changes = _bulk_changes(req, db, ("monitored", "quality_profile_id"))
    count = 0
    for movie in db.query(models.Movie).filter(models.Movie.id.in_(req.ids)):
        media_service.update_movie(db, movie, changes, user.actor)
        count += 1
    db.commit()
    return {"updated": count}


def _bulk_changes(req: schemas.BulkEditRequest, db, fields: tuple[str, ...]) -> dict:
    changes = {k: getattr(req, k) for k in fields if getattr(req, k) is not None}
    if not changes:
        raise HTTPException(422, "Provide at least one change")
    if "quality_profile_id" in changes and db.get(models.QualityProfile, changes["quality_profile_id"]) is None:
        raise HTTPException(422, "Unknown quality profile")
    return changes


# ================================================================== series
@router.get("/series", tags=["series"])
def list_series(
    db: DB, _: Requires(Permission.MEDIA_READ), paging: Paging,
    search: str | None = Query(None, max_length=200), status: str | None = None, monitored: bool | None = None, network: str | None = None,
):
    repo = SeriesRepository(db)
    page = repo.paginate(repo.query(search=search, status=status, monitored=monitored, network=network),
                         page=paging.page, page_size=paging.page_size, sort=paging.sort, order=paging.order)
    stats = repo.episode_stats([s.id for s in page.items])
    items = []
    for s in page.items:
        out = schemas.SeriesSummary.model_validate(s)
        for k, v in stats.get(s.id, {}).items():
            setattr(out, k, v)
        items.append(out)
    return page_response(page, items)


@router.get("/series/lookup", tags=["series"])
async def lookup_series(_: Requires(Permission.MEDIA_READ), query: str, year: int | None = None):
    return await metadata_service.search(query, "tv", year)


@router.get("/series/{series_id}", response_model=schemas.SeriesOut, tags=["series"])
def get_series(series_id: int, db: DB, _: Requires(Permission.MEDIA_READ)):
    repo = SeriesRepository(db)
    series = repo.with_episodes(series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    out = schemas.SeriesOut.model_validate(series)
    for k, v in repo.episode_stats([series.id]).get(series.id, {}).items():
        setattr(out, k, v)
    return out


@router.post("/series", response_model=schemas.SeriesOut, status_code=201, tags=["series"])
async def add_series(req: schemas.AddMediaRequest, user: Requires(Permission.MEDIA_WRITE)):
    meta = await metadata_service.fetch_series(tvdb_id=req.tvdb_id, tmdb_id=req.tmdb_id, tvmaze_id=req.tvmaze_id, imdb_id=req.imdb_id)
    if meta is None:
        meta = await metadata_service.fetch_series(title=req.title, year=req.year)
    if meta is None:
        meta = MediaMetadata(media_type="tv", provider="manual", title=req.title, year=req.year, season_count=1)

    def persist():
        with session_scope() as db:
            try:
                series = media_service.add_series(db, meta, quality_profile_id=req.quality_profile_id, root_folder=req.root_folder,
                                                  monitored=req.monitored, monitor=req.monitor, actor=user.actor)
            except media_service.ConflictError as exc:
                raise HTTPException(409, str(exc))
            db.flush()
            return series.id

    series_id = await asyncio.to_thread(persist)
    bus.publish(EventType.MEDIA_ADDED, media_type="tv", media_id=series_id, title=meta.title)
    if req.search_on_add or get_settings().search_on_add:
        await asyncio.to_thread(job_queue.enqueue, "missing_search", {"limit": 50})

    def load():
        with session_scope() as db:
            return get_series(series_id, db, None)

    return await asyncio.to_thread(load)


@router.post("/series/bulk", tags=["series"])
def bulk_series(req: schemas.BulkEditRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    """Bulk edit: {"ids": [...], "monitored"?, "quality_profile_id"?, "monitor_new_seasons"?}."""
    changes = _bulk_changes(req, db, ("monitored", "quality_profile_id", "monitor_new_seasons"))
    count = 0
    for series in db.query(models.Series).filter(models.Series.id.in_(req.ids)):
        media_service.update_series(db, series, changes, user.actor)
        count += 1
    db.commit()
    return {"updated": count}


@router.patch("/series/{series_id}", response_model=schemas.SeriesSummary, tags=["series"])
def update_series(series_id: int, req: schemas.SeriesUpdate, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    media_service.update_series(db, series, req.model_dump(exclude_unset=True), user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series.id)
    return series


@router.put("/series/{series_id}/seasons/{season_number}/monitor", response_model=schemas.Message, tags=["series"])
def monitor_season(series_id: int, season_number: int, req: schemas.MonitorRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    try:
        media_service.set_season_monitored(db, series_id, season_number, req.monitored, user.actor)
    except media_service.MediaError as exc:
        raise HTTPException(404, str(exc))
    db.commit()
    bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series_id)
    return schemas.Message(message=f"Season {season_number} {'monitored' if req.monitored else 'unmonitored'}")


@router.put("/episodes/monitor", tags=["series"])
def monitor_episodes(req: schemas.EpisodeMonitorRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    count = media_service.set_episodes_monitored(db, req.episode_ids, req.monitored, user.actor)
    db.commit()
    return {"updated": count}


@router.put("/series/{series_id}/monitoring", response_model=schemas.Message, tags=["series"])
def apply_preset(series_id: int, preset: str, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    if preset not in ("all", "future", "missing", "none", "latest", "first"):
        raise HTTPException(422, "Unknown preset")
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    media_service.apply_monitoring_preset(db, series, preset)
    db.commit()
    return schemas.Message(message=f"Applied '{preset}' monitoring")


@router.post("/series/{series_id}/seasons", response_model=schemas.Message, status_code=201, tags=["series"])
def add_season(series_id: int, req: schemas.AddSeasonRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    try:
        media_service.add_season(db, series, req.season_number, req.episode_count, user.actor)
    except media_service.MediaError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return schemas.Message(message=f"Season {req.season_number} added")


@router.post("/series/{series_id}/episodes", response_model=schemas.EpisodeOut, status_code=201, tags=["series"])
def add_episode(series_id: int, req: schemas.AddEpisodeRequest, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    try:
        ep = media_service.add_episode(db, series, req.season_number, req.episode_number, req.title, req.air_date, user.actor)
    except media_service.MediaError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return ep


@router.delete("/series/{series_id}/episodes/{episode_id}", response_model=schemas.DeleteEpisodesResult, tags=["series"])
def delete_episode(series_id: int, episode_id: int, db: DB, user: Requires(Permission.MEDIA_DELETE),
                   delete_files: bool = False, dry_run: bool = True):
    """Remove one episode (and optionally move its file to the recycle bin). Defaults to a preview."""
    series = db.get(models.Series, series_id)
    ep = db.get(models.Episode, episode_id)
    if series is None or ep is None or ep.series_id != series_id:
        raise HTTPException(404, "Episode not found")
    result = media_service.remove_episodes(db, series, [ep], delete_files=delete_files, apply=not dry_run, actor=user.actor)
    db.commit()
    if result["applied"]:
        bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series_id)
    return schemas.DeleteEpisodesResult(**result)


@router.delete("/series/{series_id}/seasons/{season_number}", response_model=schemas.DeleteEpisodesResult, tags=["series"])
def delete_season(series_id: int, season_number: int, db: DB, user: Requires(Permission.MEDIA_DELETE),
                  delete_files: bool = False, dry_run: bool = True):
    """Remove a whole season (and optionally move its files to the recycle bin). Defaults to a preview."""
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    eps = list(db.scalars(select(models.Episode).where(models.Episode.series_id == series_id, models.Episode.season_number == season_number)))
    season = db.scalar(select(models.Season).where(models.Season.series_id == series_id, models.Season.season_number == season_number))
    if season is None and not eps:
        raise HTTPException(404, "Season not found")
    result = media_service.remove_episodes(db, series, eps, delete_files=delete_files, apply=not dry_run, actor=user.actor, drop_season=season_number)
    db.commit()
    if result["applied"]:
        bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series_id)
    return schemas.DeleteEpisodesResult(**result)


@router.delete("/series/{series_id}", response_model=schemas.Message, tags=["series"])
def delete_series(series_id: int, db: DB, user: Requires(Permission.MEDIA_DELETE), delete_files: bool = False):
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    title = series.title
    media_service.delete_series(db, series, delete_files=delete_files, actor=user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_DELETED, media_type="tv", media_id=series_id)
    return schemas.Message(message=f"'{title}' removed")


@router.post("/series/{series_id}/prune-stale", response_model=schemas.PruneStaleResult, tags=["series"])
async def prune_stale(series_id: int, user: Requires(Permission.MEDIA_WRITE), dry_run: bool = True):
    """Remove file-less seasons/episodes that current metadata no longer lists. Defaults to a preview."""
    def lookup():
        with session_scope() as db:
            s = db.get(models.Series, series_id)
            if s is None:
                raise HTTPException(404, "Series not found")
            return metadata_service.series_lookup(s), s.title, s.year

    ids, title, year = await asyncio.to_thread(lookup)
    meta = await metadata_service.fetch_series_for(ids, title, year)
    if meta is None or not meta.episodes:
        raise HTTPException(502, "No metadata provider returned an episode list for this series; nothing can be compared")

    def persist():
        with session_scope() as db:
            s = db.get(models.Series, series_id)
            if s is None:
                raise HTTPException(404, "Series not found")
            return media_service.prune_stale_seasons(db, s, meta, apply=not dry_run, actor=user.actor)

    result = await asyncio.to_thread(persist)
    applied = not dry_run and result["episodes"] + len(result["seasons"]) > 0
    if applied:
        bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series_id)
    return schemas.PruneStaleResult(**result, applied=applied)


@router.post("/series/{series_id}/refresh", tags=["series"])
def refresh_series(series_id: int, user: Requires(Permission.MEDIA_WRITE)):
    job = job_queue.enqueue("metadata_refresh", {"media_type": "tv", "ids": [series_id]}, dedupe_key=f"series:{series_id}")
    return {"job_id": job}


# ================================================================== link ("fix match")
@router.post("/movies/{movie_id}/link", response_model=schemas.LinkResult, tags=["movies"])
async def link_movie(movie_id: int, req: schemas.LinkRequest, user: Requires(Permission.MEDIA_WRITE)):
    """Point a movie at the title an IMDb/TMDB id identifies. ``dry_run`` (default) only previews the match."""
    return await _link("movie", movie_id, req, user)


@router.post("/series/{series_id}/link", response_model=schemas.LinkResult, tags=["series"])
async def link_series(series_id: int, req: schemas.LinkRequest, user: Requires(Permission.MEDIA_WRITE)):
    """Point a series at the show a TVDB/IMDb/TMDB id identifies. ``dry_run`` (default) only previews the match."""
    return await _link("tv", series_id, req, user)


async def _link(media_type: str, media_id: int, req: schemas.LinkRequest, user) -> schemas.LinkResult:
    movie = media_type == "movie"
    model, label = (models.Movie, "Movie") if movie else (models.Series, "Series")
    ids = parse_external_id(req.external_id, media_type)
    if movie and "tvdb_id" in ids:
        raise HTTPException(422, "TVDB ids are for TV shows – link a movie with its IMDb (tt…) or TMDB id")
    if not ids:
        raise HTTPException(422, "Enter an IMDb id (e.g. tt0163651), a " + ("TMDB" if movie else "TVDB") + " id, or a link to the title's page")

    def exists():
        with session_scope() as db:
            if db.get(model, media_id) is None:
                raise HTTPException(404, f"{label} not found")

    await asyncio.to_thread(exists)
    meta = await (metadata_service.fetch_movie(**ids) if movie else metadata_service.fetch_series(**ids))
    if meta is None:
        raise HTTPException(404, f"No metadata provider recognised '{req.external_id.strip()}' as a {'movie' if movie else 'TV show'}")
    for key, value in ids.items():  # keep the id the user gave even if the provider didn't echo it back
        if not getattr(meta, key):
            setattr(meta, key, value)
    match = schemas.LinkMatch(
        provider=meta.provider, title=meta.title, year=meta.year, overview=meta.overview, poster_url=meta.poster_url,
        imdb_id=meta.imdb_id, tmdb_id=meta.tmdb_id, tvdb_id=meta.tvdb_id, episode_count=len(meta.episodes),
    )

    def persist() -> schemas.LinkResult:
        with session_scope() as db:
            item = db.get(model, media_id)
            if item is None:
                raise HTTPException(404, f"{label} not found")
            clash = media_service.link_clash(db, item, meta)
            existing = schemas.LinkExisting(id=clash.id, title=clash.title, year=clash.year) if clash else None
            if clash and movie and (req.dry_run or req.merge):
                if req.dry_run:  # preview what "merge into the existing movie" would do
                    plan = schemas.MergePlan(**media_service.merge_candidates(db, item, clash))
                    return schemas.LinkResult(match=match, existing=existing, merge=plan)
                try:
                    media_service.merge_movie(db, item, clash, req.keep_path, user.actor)
                except media_service.MediaError as exc:
                    raise HTTPException(422, str(exc))
                return schemas.LinkResult(match=match, existing=existing, merged_into=clash.id, applied=True)
            if req.dry_run or clash:
                if clash and not req.dry_run:
                    raise HTTPException(409, f"'{clash.title} ({clash.year})' is already in the library")
                return schemas.LinkResult(match=match, existing=existing)
            (media_service.link_movie if movie else media_service.link_series)(db, item, meta, user.actor)
            return schemas.LinkResult(match=match, applied=True)

    result = await asyncio.to_thread(persist)
    if not result.applied:
        return result
    if result.merged_into is not None:
        bus.publish(EventType.MEDIA_DELETED, media_type=media_type, media_id=media_id)
    target_id = result.merged_into or media_id
    bus.publish(EventType.MEDIA_UPDATED, media_type=media_type, media_id=target_id)
    if req.rename:
        try:
            organized = await asyncio.to_thread(renamer_service.organize, media_type, target_id, user.actor)
            result.renamed, result.rename_errors = organized["renamed"], organized["errors"]
        except Exception as exc:  # the link itself already succeeded – report, don't fail it
            result.rename_errors = [str(exc)]
        if result.renamed:
            bus.publish(EventType.MEDIA_UPDATED, media_type=media_type, media_id=target_id)
    return result


# ================================================================== artwork
ART_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


@router.get("/artwork/{media_type}/{media_id}/{kind}", tags=["media"], include_in_schema=False)
def artwork(media_type: str, media_id: int, kind: str, db: DB):
    """Serve local artwork found next to media files.

    Deliberately unauthenticated (``<img>`` tags cannot send bearer tokens), which is safe because the
    file is resolved from the library record by id – callers can never choose an arbitrary path – and
    only image files are ever returned.
    """
    from fastapi.responses import FileResponse
    from pathlib import Path

    model = {"movie": models.Movie, "tv": models.Series}.get(media_type)
    if model is None or kind not in ("poster", "backdrop"):
        raise HTTPException(404, "Not found")
    item = db.get(model, media_id)
    path = getattr(item, f"{kind}_path", None) if item else None
    if not path or path.startswith(("http://", "https://")):
        raise HTTPException(404, "Not found")
    file = Path(path)
    if file.suffix.lower() not in ART_TYPES or not file.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(file, media_type=ART_TYPES[file.suffix.lower()], headers={"Cache-Control": "private, max-age=86400"})


# ================================================================== calendar
@router.get("/calendar", tags=["series"])
def calendar(db: DB, _: Requires(Permission.MEDIA_READ), start: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
             end: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    rows = (
        db.query(models.Episode, models.Series.title, models.Series.poster_path)
        .join(models.Series, models.Series.id == models.Episode.series_id)
        .filter(models.Episode.air_date >= start, models.Episode.air_date <= end)
        .order_by(models.Episode.air_date)
        .limit(1000)
        .all()
    )
    movies = (
        db.query(models.Movie)
        .filter(((models.Movie.release_date >= start) & (models.Movie.release_date <= end))
                | ((models.Movie.digital_release_date >= start) & (models.Movie.digital_release_date <= end)))
        .limit(500)
        .all()
    )
    return {
        "episodes": [{**schemas.EpisodeOut.model_validate(e).model_dump(), "series_title": t, "poster": schemas.public_art("tv", e.series_id, p)}
                     for e, t, p in rows],
        "movies": [schemas.MovieSummary.model_validate(m).model_dump() | {"release_date": m.release_date, "digital_release_date": m.digital_release_date} for m in movies],
    }


# ================================================================== media files
@router.get("/media-files", tags=["media-files"])
def list_media_files(
    db: DB,
    _: Requires(Permission.MEDIA_READ),
    paging: Paging,
    movie_id: int | None = None,
    series_id: int | None = None,
    episode_id: int | None = None,
):
    repo = MediaFileRepository(db)
    stmt = repo.query(movie_id=movie_id, series_id=series_id, episode_id=episode_id)
    page = repo.paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort, order=paging.order)
    return page_response(page, [schemas.MediaFileOut.model_validate(f) for f in page.items])


@router.get("/media-files/{media_file_id}", response_model=schemas.MediaFileDetailOut, tags=["media-files"])
def get_media_file(media_file_id: int, db: DB, _: Requires(Permission.MEDIA_READ)):
    file = db.get(models.MediaFile, media_file_id)
    if file is None:
        raise HTTPException(404, "Media file not found")
    return file


@router.post("/media-files/{media_file_id}/inspect", tags=["media-files"])
def inspect_media_file(media_file_id: int, user: Requires(Permission.MEDIA_WRITE)):
    job = job_queue.enqueue("inspect_file", {"media_file_id": media_file_id, "force": True}, dedupe_key=f"file:{media_file_id}")
    return {"job_id": job}
