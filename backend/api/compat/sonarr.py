"""Sonarr v3 API facade (reported as Sonarr 4), so Seerr can request TV shows.

Added in Seerr as a Sonarr server with URL base ``/api/compat/sonarr``; auth and conventions match
the Radarr facade (see ``radarr.py``). Seerr's contract (``server/api/servarr/sonarr.ts``):

* Shows are identified by **TVDB id**. A library series may lack one (added via TVmaze/TMDB), so a
  ``tvdb:`` lookup that misses falls back to matching the provider record by TVmaze/IMDb/TMDB id
  and backfills ``tvdb_id``; otherwise Seerr would add the show a second time.
* A new show's season list comes from the lookup result: Seerr marks requested seasons monitored
  and posts the list back, so lookups must carry ``seasons``. Without metadata there is no season
  list, and the lookup returns nothing rather than letting Seerr add a show with no seasons.
* Availability comes from per-season ``statistics`` (episodeFileCount vs totalEpisodeCount).
* Requesting more seasons of a known show is a ``PUT /series`` with flipped season flags, followed
  by ``MissingEpisodeSearch``.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session, selectinload

from backend.api.compat.common import (
    CommandIn,
    Lenient,
    add_servarr_routes,
    client_names,
    command_response,
    genres,
    images,
    is_known_root,
    iso,
    queue_page,
    queue_record,
    source_name,
    validation_error,
)
from backend.api.deps import DB, Requires, allow_api_key_query, guard_maintenance, require
from backend.core.events import EventType, bus
from backend.core.security import Permission
from backend.core.settings import get_settings
from backend.db import models
from backend.db.repositories import EpisodeRepository, QueueRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata
from backend.services import media_service, metadata_service, quality_service, tag_service
from backend.workers.queue import job_queue

URL_BASE = "/api/compat/sonarr"

router = APIRouter(prefix=f"{URL_BASE}/api/v3", include_in_schema=False, dependencies=[Depends(guard_maintenance)])


def _root_folders() -> list[str]:
    return [get_settings().media_root_tv]


add_servarr_routes(router, url_base=URL_BASE, version="4.0.0.0", root_folders=_root_folders)

_SERIES_TYPES = {"standard", "daily", "anime"}
_NO_STATS = {"total_episode_count": 0, "episode_file_count": 0, "episode_count": 0, "size_on_disk": 0, "previous_airing": None}


class SeasonIn(Lenient):
    season_number: int = Field(alias="seasonNumber")
    monitored: bool = False


class AddOptions(Lenient):
    search_for_missing_episodes: bool = Field(False, alias="searchForMissingEpisodes")


class SeriesIn(Lenient):
    id: int | None = None
    title: str = ""
    year: int | None = None
    tvdb_id: int = Field(0, alias="tvdbId")
    quality_profile_id: int | None = Field(None, alias="qualityProfileId")
    profile_id: int | None = Field(None, alias="profileId")
    root_folder_path: str | None = Field(None, alias="rootFolderPath")
    monitored: bool = True
    monitor_new_items: str | None = Field(None, alias="monitorNewItems")
    series_type: str | None = Field(None, alias="seriesType")
    seasons: list[SeasonIn] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)
    add_options: AddOptions | None = Field(None, alias="addOptions")


class EpisodeMonitorIn(Lenient):
    episode_ids: list[int] = Field(default_factory=list, alias="episodeIds")
    monitored: bool = True


# --------------------------------------------------------------------------- mapping
def _airing(day: str | None) -> str | None:
    return f"{day}T00:00:00Z" if day else None


def _statistics(st: dict[str, Any]) -> dict[str, Any]:
    count = st["episode_count"]
    return {
        "previousAiring": _airing(st["previous_airing"]),
        "episodeFileCount": st["episode_file_count"],
        "episodeCount": count,
        "totalEpisodeCount": st["total_episode_count"],
        "sizeOnDisk": st["size_on_disk"],
        "percentOfEpisodes": round(st["episode_file_count"] / count * 100, 1) if count else 0.0,
    }


def series_resource(series: models.Series, stats: dict[int, dict[str, Any]]) -> dict[str, Any]:
    season_rows = {s.season_number: s for s in series.seasons}
    seasons = []
    for number in sorted(set(season_rows) | set(stats)):
        row = season_rows.get(number)
        seasons.append({"seasonNumber": number, "monitored": bool(row and row.monitored), "statistics": _statistics(stats.get(number, _NO_STATS))})
    totals = {k: sum(stats.get(n, _NO_STATS)[k] for n in stats) for k in ("total_episode_count", "episode_file_count", "episode_count", "size_on_disk")}
    totals["previous_airing"] = max((st["previous_airing"] for st in stats.values() if st["previous_airing"]), default=None)
    series_stats = _statistics(totals)
    series_stats["seasonCount"] = len([n for n in seasons if n["seasonNumber"] > 0])
    series_stats["releaseGroups"] = []
    status = (series.status or "continuing").lower()
    return {
        "id": series.id,
        "title": series.title,
        "sortTitle": (series.sort_title or series.title).lower(),
        "status": status,
        "ended": status == "ended",
        "overview": series.overview or "",
        "network": series.network or "",
        "images": images(series.poster_path, series.backdrop_path),
        "seasons": seasons,
        "year": series.year,
        "path": series.path or "",
        "rootFolderPath": series.root_folder or "",
        "qualityProfileId": series.quality_profile_id or 1,
        "profileId": series.quality_profile_id or 1,
        "languageProfileId": 1,
        "seasonFolder": True,
        "monitored": series.monitored,
        "monitorNewItems": "all" if series.monitor_new_seasons else "none",
        "useSceneNumbering": False,
        "runtime": series.runtime_minutes or 0,
        "tvdbId": series.tvdb_id or 0,
        "tvRageId": 0,
        "tvMazeId": series.tvmaze_id or 0,
        "tmdbId": series.tmdb_id or 0,
        "imdbId": series.imdb_id or "",
        "firstAired": _airing(series.first_aired),
        "seriesType": series.series_type or "standard",
        "cleanTitle": "".join(ch for ch in series.title.lower() if ch.isalnum()),
        # Seerr links to {externalUrl}/series/{titleSlug}, which is MM's own detail route
        "titleSlug": str(series.id),
        "certification": series.certification or "",
        "genres": genres(series.genres),
        "tags": series.tags or [],
        "added": iso(series.added_date),
        "ratings": {"votes": series.votes or 0, "value": series.rating or 0},
        "statistics": series_stats,
    }


def lookup_resource(meta: MediaMetadata, tvdb_id: int | None = None) -> dict[str, Any]:
    """A not-in-library lookup result (no ``id``). ``seasons`` is what Seerr builds its add request from."""
    numbers = sorted({e.season_number for e in meta.episodes}) or list(range(1, (meta.season_count or 1) + 1))
    return {
        "title": meta.title,
        "sortTitle": meta.title.lower(),
        "status": (meta.status or "continuing").lower(),
        "overview": meta.overview or "",
        "network": meta.studio or "",
        "images": images(meta.poster_url, meta.backdrop_url),
        "remotePoster": meta.poster_url or "",
        "seasons": [{"seasonNumber": n, "monitored": False} for n in numbers],
        "year": meta.year or 0,
        "monitored": False,
        "runtime": meta.runtime_minutes or 0,
        "tvdbId": meta.tvdb_id or tvdb_id or 0,
        "tvMazeId": meta.tvmaze_id or 0,
        "tmdbId": meta.tmdb_id or 0,
        "imdbId": meta.imdb_id or "",
        "firstAired": _airing(meta.release_date),
        "seriesType": "standard",
        "titleSlug": str(meta.tvdb_id or tvdb_id or ""),
        "certification": meta.certification or "",
        "genres": list(meta.genres),
        "tags": [],
        "ratings": {"votes": meta.votes or 0, "value": meta.rating or 0},
    }


def episode_resource(ep: models.Episode) -> dict[str, Any]:
    return {
        "id": ep.id,
        "seriesId": ep.series_id,
        "episodeFileId": 0,
        "seasonNumber": ep.season_number,
        "episodeNumber": ep.episode_number,
        "title": ep.title,
        "airDate": ep.air_date,
        "airDateUtc": f"{ep.air_date_utc:%Y-%m-%dT%H:%M:%S}Z" if ep.air_date_utc else _airing(ep.air_date),
        "overview": ep.overview or "",
        "hasFile": ep.has_file,
        "monitored": ep.monitored,
        "absoluteEpisodeNumber": ep.absolute_number,
        "unverifiedSceneNumbering": False,
    }


def _resource(db: Session, series: models.Series) -> dict[str, Any]:
    return series_resource(series, SeriesRepository(db).season_stats([series.id]).get(series.id, {}))


def _series_or_404(db: Session, series_id: int) -> models.Series:
    series = db.get(models.Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


def _match_library(db: Session, meta: MediaMetadata, tvdb_id: int) -> models.Series | None:
    """Find a library series the provider record belongs to by its other ids, and give it the TVDB id
    Seerr knows it by when it has none yet."""
    repo = SeriesRepository(db)
    series = ((repo.by_tvmaze(meta.tvmaze_id) if meta.tvmaze_id else None)
              or (repo.by_imdb(meta.imdb_id) if meta.imdb_id else None)
              or (repo.by_tmdb(meta.tmdb_id) if meta.tmdb_id else None))
    if series is not None and series.tvdb_id is None and repo.by_tvdb(tvdb_id) is None:
        series.tvdb_id = tvdb_id
        db.commit()
    return series


def _apply_seasons(db: Session, series: models.Series, seasons: list[SeasonIn], actor) -> list[int]:
    """Flip only the seasons whose flag changed: set_season_monitored rewrites every episode in it.
    Returns the season numbers switched on, i.e. what this request asked for."""
    current = {s.season_number: s.monitored for s in series.seasons}
    switched_on = []
    for season in seasons:
        if season.season_number in current and current[season.season_number] != season.monitored:
            media_service.set_season_monitored(db, series.id, season.season_number, season.monitored, actor)
            if season.monitored:
                switched_on.append(season.season_number)
    return sorted(switched_on)


# --------------------------------------------------------------------------- series
@router.get("/languageprofile")
@router.get("/languageProfile")
@allow_api_key_query
def language_profiles(_: Requires(Permission.MEDIA_READ)):
    # Only Sonarr v3 has these; we report v4 so Seerr shouldn't ask, but answer if it does.
    return [{"id": 1, "name": "Default"}]


@router.get("/series")
@allow_api_key_query
def list_series(db: DB, _: Requires(Permission.MEDIA_READ), tvdbId: int | None = None):  # Sonarr's param name
    repo = SeriesRepository(db)
    if tvdbId is not None:
        series = repo.by_tvdb(tvdbId)
        return [_resource(db, series)] if series else []
    # Seerr matches everything by TVDB id; a series without one can't be matched, only error in its sync
    stmt = repo.query().where(models.Series.tvdb_id.is_not(None)).options(selectinload(models.Series.seasons)).order_by(models.Series.id)
    all_series = db.scalars(stmt).all()
    stats = repo.season_stats([s.id for s in all_series])
    return [series_resource(s, stats.get(s.id, {})) for s in all_series]


@router.get("/series/lookup")
@allow_api_key_query
async def lookup_series(term: str, _: Requires(Permission.MEDIA_READ)):
    term = term.strip()
    kind, _sep, value = term.partition(":")
    if kind.lower() == "tvdb" and value.strip().isdigit():
        tvdb_id = int(value)

        def in_library() -> dict | None:
            with session_scope() as db:
                series = SeriesRepository(db).by_tvdb(tvdb_id)
                return _resource(db, series) if series else None

        if found := await asyncio.to_thread(in_library):
            return [found]
        meta = await metadata_service.fetch_series(tvdb_id=tvdb_id)
        if meta is None:
            return []

        def matched() -> dict | None:
            with session_scope() as db:
                series = _match_library(db, meta, tvdb_id)
                return _resource(db, series) if series else None

        if found := await asyncio.to_thread(matched):
            return [found]
        return [lookup_resource(meta, tvdb_id)]

    results = await metadata_service.search(term, "tv")

    def merge() -> list[dict]:
        with session_scope() as db:
            repo = SeriesRepository(db)
            out = []
            for meta in results:
                series = (repo.by_tvdb(meta.tvdb_id) if meta.tvdb_id else None) or (repo.by_tvmaze(meta.tvmaze_id) if meta.tvmaze_id else None)
                out.append(_resource(db, series) if series else lookup_resource(meta))
            return out

    return await asyncio.to_thread(merge)


@router.get("/series/{series_id}")
@allow_api_key_query
def get_series(series_id: int, db: DB, _: Requires(Permission.MEDIA_READ)):
    return _resource(db, _series_or_404(db, series_id))


@router.post("/series", status_code=201)
@allow_api_key_query
async def add_series(body: SeriesIn, user: Requires(Permission.MEDIA_WRITE)):
    if body.tvdb_id <= 0:
        return validation_error("TvdbId", "'Tvdb Id' must be greater than '0'.")
    profile_id = body.quality_profile_id or body.profile_id
    root = body.root_folder_path or _root_folders()[0]
    if not is_known_root(root, _root_folders()):
        return validation_error("RootFolderPath", f"Root folder '{root}' is not configured in Maniac Media")

    def precheck() -> JSONResponse | None:
        nonlocal profile_id
        with session_scope() as db:
            profile_id = profile_id or quality_service.default_profile_id(db, "tv")
            if db.get(models.QualityProfile, profile_id) is None:
                return validation_error("QualityProfileId", f"Quality profile {profile_id} does not exist")
            if SeriesRepository(db).by_tvdb(body.tvdb_id):
                return validation_error("TvdbId", "This series has already been added")
            return None

    if (error := await asyncio.to_thread(precheck)) is not None:
        return error

    meta = await metadata_service.fetch_series(tvdb_id=body.tvdb_id)
    if meta is None:
        last = max((s.season_number for s in body.seasons), default=1)
        meta = MediaMetadata(media_type="tv", provider="manual", title=body.title or f"TVDB {body.tvdb_id}", year=body.year,
                             tvdb_id=body.tvdb_id, season_count=max(last, 1))

    def persist() -> dict:
        with session_scope() as db:
            try:
                # "none" then switch on exactly the seasons Seerr asked for (it sends the rest as unmonitored)
                series = media_service.add_series(db, meta, quality_profile_id=profile_id, root_folder=root,
                                                  monitored=body.monitored, monitor="none", actor=user.actor)
            except media_service.ConflictError:
                return {}
            if series.tvdb_id is None and SeriesRepository(db).by_tvdb(body.tvdb_id) is None:
                series.tvdb_id = body.tvdb_id
            changes: dict[str, Any] = {"monitor_new_seasons": body.monitor_new_items != "none"}
            if body.series_type in _SERIES_TYPES:
                changes["series_type"] = body.series_type
            media_service.update_series(db, series, changes, user.actor)
            db.flush()
            db.refresh(series)
            requested = _apply_seasons(db, series, body.seasons, user.actor)
            tag_service.set_tags(db, series, body.tags, actor=user.actor, source=source_name(db, user), seasons=requested)
            db.flush()
            return _resource(db, series)

    out = await asyncio.to_thread(persist)
    if not out:
        return validation_error("TvdbId", "This series has already been added")
    bus.publish(EventType.MEDIA_ADDED, media_type="tv", media_id=out["id"], title=out["title"])
    if (body.add_options and body.add_options.search_for_missing_episodes) or get_settings().search_on_add:
        await asyncio.to_thread(_search_series, out["id"], None, user.id, user.username)
    return out


def _update(series_id: int, body: SeriesIn, db: Session, user) -> dict:
    series = _series_or_404(db, series_id)
    changes: dict[str, Any] = {"monitored": body.monitored}
    profile_id = body.quality_profile_id or body.profile_id
    if profile_id and profile_id != series.quality_profile_id:
        if db.get(models.QualityProfile, profile_id) is None:
            raise HTTPException(400, f"Quality profile {profile_id} does not exist")
        changes["quality_profile_id"] = profile_id
    if body.series_type in _SERIES_TYPES:
        changes["series_type"] = body.series_type
    if body.monitor_new_items in ("all", "none"):
        changes["monitor_new_seasons"] = body.monitor_new_items == "all"
    # rootFolderPath/path are ignored: moving an existing show's files is not a request-manager job.
    media_service.update_series(db, series, changes, user.actor)
    requested = _apply_seasons(db, series, body.seasons, user.actor)
    tag_service.set_tags(db, series, body.tags, actor=user.actor, source=source_name(db, user), seasons=requested)
    db.commit()
    db.refresh(series)
    bus.publish(EventType.MEDIA_UPDATED, media_type="tv", media_id=series.id)
    return _resource(db, series)


@router.put("/series", status_code=202)
@allow_api_key_query
def update_series(body: SeriesIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    if not body.id:
        raise HTTPException(400, "Series id is required")
    return _update(body.id, body, db, user)


@router.put("/series/{series_id}", status_code=202)
@allow_api_key_query
def update_series_by_id(series_id: int, body: SeriesIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    return _update(series_id, body, db, user)


@router.delete("/series/{series_id}")
@allow_api_key_query
def delete_series(series_id: int, db: DB, user: Requires(Permission.MEDIA_DELETE), deleteFiles: bool = False):
    series = _series_or_404(db, series_id)
    # same opt-in as movies: Seerr always sends deleteFiles=true, and files only ever go to the recycle bin
    delete_files = deleteFiles and get_settings().seerr_allow_file_deletion
    media_service.delete_series(db, series, delete_files=delete_files, actor=user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_DELETED, media_type="tv", media_id=series_id)
    return {}


# --------------------------------------------------------------------------- episodes
@router.get("/episode")
@allow_api_key_query
def list_episodes(db: DB, _: Requires(Permission.MEDIA_READ), seriesId: int | None = None):
    if seriesId is None:
        raise HTTPException(400, "seriesId is required")
    return [episode_resource(ep) for ep in EpisodeRepository(db).for_series(seriesId)]


@router.put("/episode/monitor", status_code=202)
@allow_api_key_query
def monitor_episodes(body: EpisodeMonitorIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    if body.episode_ids:
        media_service.set_episodes_monitored(db, body.episode_ids, body.monitored, user.actor)
        db.commit()
    return [episode_resource(ep) for ep in (db.get(models.Episode, i) for i in body.episode_ids) if ep is not None]


# --------------------------------------------------------------------------- commands & queue
def _enqueue(series_id: int, season: int, episodes: list[int] | None, user_id: int, username: str) -> int | None:
    payload = {"media_type": "tv", "media_id": series_id, "season": season, "episodes": episodes or [], "user_id": user_id, "username": username}
    key = f"compat:tv:{series_id}:{season}:{','.join(map(str, episodes or []))}"
    return job_queue.enqueue("automatic_search", payload, dedupe_key=key)


def _search_series(series_id: int, season: int | None, user_id: int, username: str) -> list[int]:
    """A season search per season with monitored, aired, missing episodes (a pack first, then single
    episodes, see search_service.automatic). ``season`` limits it to one."""
    with session_scope() as db:
        seasons = EpisodeRepository(db).seasons_with_missing(series_id)
    if season is not None:
        seasons = [season]
    return [job for n in seasons if (job := _enqueue(series_id, n, None, user_id, username))]


@router.post("/command", status_code=201)
@allow_api_key_query
def run_command(body: CommandIn, request: Request, db: DB, user: Requires(Permission.MEDIA_READ)):
    name = body.name.lower()
    if name in ("missingepisodesearch", "seriessearch", "seasonsearch", "episodesearch"):
        require(Permission.SEARCH)(user, request)
        job_ids: list[int] = []
        if name == "episodesearch":
            grouped: dict[tuple[int, int], list[int]] = defaultdict(list)
            for ep in (db.get(models.Episode, i) for i in body.episode_ids):
                if ep is not None:
                    grouped[(ep.series_id, ep.season_number)].append(ep.episode_number)
            job_ids = [job for (sid, season), eps in grouped.items() if (job := _enqueue(sid, season, sorted(eps), user.id, user.username))]
        elif body.series_id and db.get(models.Series, body.series_id) is not None:
            season = body.season_number if name == "seasonsearch" else None
            job_ids = _search_series(body.series_id, season, user.id, user.username)
        return command_response(body, "queued", job_ids)
    # RefreshMonitoredDownloads etc.: acknowledged, MM's download monitor polls on its own schedule
    return command_response(body, "completed", [])


@router.get("/queue")
@allow_api_key_query
def queue(db: DB, _: Requires(Permission.QUEUE_READ), page: int = 1, pageSize: int = 1000):
    items = [q for q in QueueRepository(db).for_display() if q.media_type == "tv"]
    names = client_names(db)
    episodes: dict[tuple[int, int, int], models.Episode] = {}
    episode_repo = EpisodeRepository(db)
    for series_id in {q.media_id for q in items}:
        episodes.update({(ep.series_id, ep.season_number, ep.episode_number): ep for ep in episode_repo.for_series(series_id)})
    records = []
    for q in items:
        base = {**queue_record(q, names), "seriesId": q.media_id, "seasonNumber": q.season_num}
        numbers = q.episode_numbers or ([q.ep_num] if q.ep_num is not None else [])
        matched = [ep for n in numbers if (ep := episodes.get((q.media_id, q.season_num or 0, n))) is not None]
        if not matched:  # season pack whose episodes aren't known yet: one record for the season
            records.append(base)
        # Sonarr lists a download once per episode it covers; Seerr shows progress per episode
        for ep in matched:
            records.append({**base, "episodeId": ep.id, "episode": episode_resource(ep)})
    return queue_page(records, page, pageSize)
