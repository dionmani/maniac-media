"""Radarr v3 API facade, so request managers (Seerr/Overseerr/Jellyseerr) can drive the movie library.

Seerr is added as a Radarr server with URL base ``/api/compat/radarr``; it then calls
``{base}/api/v3/...`` with ``?apikey=`` (it never sends a header), so every route opts in to
query-param keys and the key itself must allow them. Only the subset Seerr uses is implemented
(see its ``server/api/servarr/{base,radarr}.ts``): status, profiles, root folders, tags, movie
list/get/lookup/add/update/delete, commands and the queue. Responses carry the fields Seerr reads
plus the common Radarr ones.
"""
from __future__ import annotations

import asyncio
import os
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
    validation_error,
)
from backend.api.deps import DB, Requires, allow_api_key_query, guard_maintenance, require
from backend.core.events import EventType, bus
from backend.core.security import Permission
from backend.core.settings import get_settings
from backend.db import models
from backend.db.repositories import MovieRepository, QueueRepository
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata
from backend.services import media_service, metadata_service
from backend.workers.queue import job_queue

URL_BASE = "/api/compat/radarr"

# Kept out of /api/openapi.json: that spec documents Maniac Media's own versioned API only.
router = APIRouter(prefix=f"{URL_BASE}/api/v3", include_in_schema=False, dependencies=[Depends(guard_maintenance)])


def _root_folders() -> list[str]:
    return [get_settings().media_root_movies]


add_servarr_routes(router, url_base=URL_BASE, version="5.0.0.0", root_folders=_root_folders)

_STATUS = {"announced": "announced", "in_cinemas": "inCinemas", "released": "released"}
_AVAILABILITY = {"announced": "announced", "in_cinemas": "inCinemas", "released": "released", "predb": "released"}


class AddOptions(Lenient):
    search_for_movie: bool = Field(False, alias="searchForMovie")


class MovieIn(Lenient):
    id: int | None = None
    title: str = ""
    year: int | None = None
    tmdb_id: int = Field(0, alias="tmdbId")
    imdb_id: str | None = Field(None, alias="imdbId")
    quality_profile_id: int | None = Field(None, alias="qualityProfileId")
    profile_id: int | None = Field(None, alias="profileId")
    root_folder_path: str | None = Field(None, alias="rootFolderPath")
    monitored: bool = True
    tags: list[int] = Field(default_factory=list)
    add_options: AddOptions | None = Field(None, alias="addOptions")


# --------------------------------------------------------------------------- mapping
def _movie_file(movie: models.Movie) -> dict[str, Any] | None:
    primary = next((f for f in movie.files if f.role == "main"), None)
    if primary is None:
        return None
    return {
        "id": primary.id,
        "movieId": movie.id,
        "relativePath": primary.relative_path or os.path.basename(primary.path),
        "path": primary.path,
        "size": primary.size_bytes or 0,
        "dateAdded": iso(primary.added_at),
        "sceneName": primary.scene_name,
        "releaseGroup": primary.release_group,
        "edition": primary.edition or "",
        "quality": {"quality": {"name": primary.quality or movie.quality or "Unknown"}},
        "mediaInfo": {
            "audioChannels": primary.audio_channels or 0,
            "audioCodec": primary.audio_codec,
            "audioLanguages": "/".join(primary.audio_languages or []),
            "videoBitDepth": primary.video_bit_depth or 0,
            "videoCodec": primary.video_codec,
            "videoDynamicRange": "HDR" if primary.hdr_format else "",
            "videoDynamicRangeType": primary.hdr_format or "",
            "resolution": f"{primary.video_width}x{primary.video_height}" if primary.video_width else None,
            "subtitles": "/".join(primary.subtitle_languages or []),
        },
        "qualityCutoffNotMet": False,
    }


def movie_resource(movie: models.Movie) -> dict[str, Any]:
    status = movie.status or "released"
    resource: dict[str, Any] = {
        "id": movie.id,
        "title": movie.title,
        "originalTitle": movie.original_title or movie.title,
        "sortTitle": (movie.sort_title or movie.title).lower(),
        "year": movie.year,
        "overview": movie.overview or "",
        "tmdbId": movie.tmdb_id or 0,
        "imdbId": movie.imdb_id or "",
        # Seerr links to {externalUrl}/movie/{titleSlug}; the SPA redirects /movie/:id to the detail page
        "titleSlug": str(movie.id),
        "status": _STATUS.get(status, "released"),
        "isAvailable": status == "released" or movie.has_file,
        "monitored": movie.monitored,
        "hasFile": movie.has_file,
        "qualityProfileId": movie.quality_profile_id or 1,
        "profileId": movie.quality_profile_id or 1,
        "minimumAvailability": _AVAILABILITY.get(movie.minimum_availability or "released", "released"),
        "path": movie.path or "",
        "folderName": movie.path or "",
        "rootFolderPath": movie.root_folder or "",
        "sizeOnDisk": movie.file_size_bytes or 0,
        "runtime": movie.runtime_minutes or 0,
        "genres": genres(movie.genres),
        "studio": movie.studio or "",
        "certification": movie.certification or "",
        "added": iso(movie.added_date),
        "images": images(movie.poster_path, movie.backdrop_path),
        "tags": [],
    }
    if movie.has_file and (file := _movie_file(movie)) is not None:
        resource["movieFile"] = file
    return resource


def lookup_resource(meta: MediaMetadata) -> dict[str, Any]:
    """A not-in-library lookup result: Radarr omits ``id``, and Seerr keys off that."""
    return {
        "title": meta.title,
        "originalTitle": meta.original_title or meta.title,
        "year": meta.year or 0,
        "overview": meta.overview or "",
        "tmdbId": meta.tmdb_id or 0,
        "imdbId": meta.imdb_id or "",
        "titleSlug": str(meta.tmdb_id or ""),
        "monitored": False,
        "hasFile": False,
        "isAvailable": False,
        "runtime": meta.runtime_minutes or 0,
        "genres": list(meta.genres),
        "images": images(meta.poster_url, meta.backdrop_url),
        "tags": [],
    }


def _movie_or_404(db: Session, movie_id: int) -> models.Movie:
    movie = db.get(models.Movie, movie_id)
    if movie is None:
        raise HTTPException(404, "Movie not found")
    return movie


# --------------------------------------------------------------------------- movies
@router.get("/movie")
@allow_api_key_query
def list_movies(db: DB, _: Requires(Permission.MEDIA_READ), tmdbId: int | None = None):  # Radarr's param name
    repo = MovieRepository(db)
    if tmdbId is not None:
        movie = repo.by_tmdb(tmdbId)
        return [movie_resource(movie)] if movie else []
    movies = db.scalars(repo.query().options(selectinload(models.Movie.files)).order_by(models.Movie.id)).all()
    return [movie_resource(m) for m in movies]


@router.get("/movie/lookup")
@allow_api_key_query
async def lookup_movie(term: str, _: Requires(Permission.MEDIA_READ)):
    term = term.strip()
    kind, _sep, value = term.partition(":")
    kind = kind.lower()

    def in_library(tmdb_id: int | None = None, imdb_id: str | None = None) -> dict | None:
        with session_scope() as db:
            repo = MovieRepository(db)
            movie = (repo.by_tmdb(tmdb_id) if tmdb_id else None) or (repo.by_imdb(imdb_id) if imdb_id else None)
            return movie_resource(movie) if movie else None

    if kind == "tmdb" and value.strip().isdigit():
        tmdb_id = int(value)
        if found := await asyncio.to_thread(in_library, tmdb_id):
            return [found]
        meta = await metadata_service.fetch_movie(tmdb_id=tmdb_id)
        # No metadata (provider down / no TMDB key) must not block a request: Seerr re-sends the
        # title and year on add, which is enough for a manual entry the background refresh fills in.
        return [lookup_resource(meta) if meta else lookup_resource(MediaMetadata(media_type="movie", provider="manual", title="", tmdb_id=tmdb_id))]
    if kind == "imdb" and value.strip():
        imdb_id = value.strip()
        if found := await asyncio.to_thread(in_library, None, imdb_id):
            return [found]
        meta = await metadata_service.fetch_movie(imdb_id=imdb_id)
        return [lookup_resource(meta)] if meta else []

    results = await metadata_service.search(term, "movie")

    def merge() -> list[dict]:
        with session_scope() as db:
            repo = MovieRepository(db)
            out = []
            for meta in results:
                movie = repo.by_tmdb(meta.tmdb_id) if meta.tmdb_id else None
                out.append(movie_resource(movie) if movie else lookup_resource(meta))
            return out

    return await asyncio.to_thread(merge)


@router.get("/movie/{movie_id}")
@allow_api_key_query
def get_movie(movie_id: int, db: DB, _: Requires(Permission.MEDIA_READ)):
    return movie_resource(_movie_or_404(db, movie_id))


@router.post("/movie", status_code=201)
@allow_api_key_query
async def add_movie(body: MovieIn, user: Requires(Permission.MEDIA_WRITE)):
    if body.tmdb_id <= 0:
        return validation_error("TmdbId", "'Tmdb Id' must be greater than '0'.")
    profile_id = body.quality_profile_id or body.profile_id or 1
    root = body.root_folder_path or _root_folders()[0]
    if not is_known_root(root, _root_folders()):
        return validation_error("RootFolderPath", f"Root folder '{root}' is not configured in Maniac Media")

    def precheck() -> JSONResponse | None:
        with session_scope() as db:
            if db.get(models.QualityProfile, profile_id) is None:
                return validation_error("QualityProfileId", f"Quality profile {profile_id} does not exist")
            if MovieRepository(db).by_tmdb(body.tmdb_id):
                return validation_error("TmdbId", "This movie has already been added")
            return None

    if (error := await asyncio.to_thread(precheck)) is not None:
        return error

    meta = await metadata_service.fetch_movie(tmdb_id=body.tmdb_id)
    if meta is None:
        meta = MediaMetadata(media_type="movie", provider="manual", title=body.title or f"TMDB {body.tmdb_id}",
                             year=body.year, tmdb_id=body.tmdb_id, imdb_id=body.imdb_id or None)

    def persist() -> dict:
        with session_scope() as db:
            try:
                movie = media_service.add_movie(db, meta, quality_profile_id=profile_id, root_folder=root,
                                                monitored=body.monitored, actor=user.actor)
            except media_service.ConflictError:
                return {}
            db.flush()
            return movie_resource(movie)

    out = await asyncio.to_thread(persist)
    if not out:
        return validation_error("TmdbId", "This movie has already been added")
    bus.publish(EventType.MEDIA_ADDED, media_type="movie", media_id=out["id"], title=out["title"])
    if (body.add_options and body.add_options.search_for_movie) or get_settings().search_on_add:
        await asyncio.to_thread(_enqueue_search, out["id"], user.id, user.username)
    return out


def _update(movie_id: int, body: MovieIn, db: Session, user) -> dict:
    movie = _movie_or_404(db, movie_id)
    changes: dict[str, Any] = {"monitored": body.monitored}
    profile_id = body.quality_profile_id or body.profile_id
    if profile_id and profile_id != movie.quality_profile_id:
        if db.get(models.QualityProfile, profile_id) is None:
            raise HTTPException(400, f"Quality profile {profile_id} does not exist")
        changes["quality_profile_id"] = profile_id
    # rootFolderPath/path changes are ignored: moving an existing movie's files is not a request-manager job.
    media_service.update_movie(db, movie, changes, user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_UPDATED, media_type="movie", media_id=movie.id)
    return movie_resource(movie)


@router.put("/movie", status_code=202)
@allow_api_key_query
def update_movie(body: MovieIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    if not body.id:
        raise HTTPException(400, "Movie id is required")
    return _update(body.id, body, db, user)


@router.put("/movie/{movie_id}", status_code=202)
@allow_api_key_query
def update_movie_by_id(movie_id: int, body: MovieIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
    return _update(movie_id, body, db, user)


@router.delete("/movie/{movie_id}")
@allow_api_key_query
def delete_movie(movie_id: int, db: DB, user: Requires(Permission.MEDIA_DELETE), deleteFiles: bool = False):
    movie = _movie_or_404(db, movie_id)
    # Seerr always asks for deleteFiles=true; honour it only when the admin opted in, and even then
    # files go to the recycle bin (media_service.delete_movie), never straight to deletion.
    delete_files = deleteFiles and get_settings().seerr_allow_file_deletion
    media_service.delete_movie(db, movie, delete_files=delete_files, actor=user.actor)
    db.commit()
    bus.publish(EventType.MEDIA_DELETED, media_type="movie", media_id=movie_id)
    return {}


# --------------------------------------------------------------------------- commands & queue
def _enqueue_search(movie_id: int, user_id: int, username: str) -> int | None:
    return job_queue.enqueue("automatic_search", {"media_type": "movie", "media_id": movie_id, "user_id": user_id, "username": username},
                             dedupe_key=f"compat:movie:{movie_id}")


@router.post("/command", status_code=201)
@allow_api_key_query
def run_command(body: CommandIn, request: Request, db: DB, user: Requires(Permission.MEDIA_READ)):
    job_ids: list[int] = []
    if body.name.lower() == "moviessearch":
        require(Permission.SEARCH)(user, request)
        for movie_id in body.movie_ids:
            if db.get(models.Movie, movie_id) is not None and (job := _enqueue_search(movie_id, user.id, user.username)):
                job_ids.append(job)
        return command_response(body, "queued", job_ids)
    # RefreshMonitoredDownloads (sent every minute by Seerr) and friends: MM's download monitor
    # already polls on its own schedule, so these are acknowledged without doing anything.
    return command_response(body, "completed", job_ids)


@router.get("/queue")
@allow_api_key_query
def queue(db: DB, _: Requires(Permission.QUEUE_READ), page: int = 1, pageSize: int = 1000):
    names = client_names(db)
    records = [{**queue_record(q, names), "movieId": q.media_id} for q in QueueRepository(db).for_display() if q.media_type == "movie"]
    return queue_page(records, page, pageSize)
