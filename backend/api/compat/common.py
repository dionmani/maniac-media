"""Pieces shared by the Radarr and Sonarr facades: wire-format helpers and the "servarr base" routes
(system status, quality profiles, root folders, tags) that both apps expose identically."""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import __version__
from backend.api.deps import DB, CurrentUser, Requires, allow_api_key_query
from backend.core.security import Permission
from backend.db import models
from backend.services import tag_service

# MM queue status -> (servarr status, trackedDownloadStatus, trackedDownloadState)
QUEUE_STATE = {
    "queued": ("queued", "ok", "downloading"),
    "downloading": ("downloading", "ok", "downloading"),
    "paused": ("paused", "ok", "downloading"),
    "completed": ("completed", "ok", "importPending"),
    "importing": ("completed", "ok", "importing"),
    "warning": ("warning", "warning", "downloading"),
    "import_failed": ("completed", "warning", "importBlocked"),
}


class Lenient(BaseModel):
    """Request bodies: Seerr echoes whole resources back, so unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TagIn(Lenient):
    label: str = Field(min_length=1, max_length=256)


class CommandIn(Lenient):
    name: str
    movie_ids: list[int] = Field(default_factory=list, alias="movieIds")
    series_id: int | None = Field(None, alias="seriesId")
    season_number: int | None = Field(None, alias="seasonNumber")
    episode_ids: list[int] = Field(default_factory=list, alias="episodeIds")


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def timeleft(seconds: int | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}.{clock}" if days else clock


def images(poster: str | None, fanart: str | None) -> list[dict[str, str]]:
    out = []
    for kind, url in (("poster", poster), ("fanart", fanart)):
        if url and url.startswith(("http://", "https://")):
            out.append({"coverType": kind, "remoteUrl": url, "url": url})
    return out


def genres(value: str | None) -> list[str]:
    return [g.strip() for g in (value or "").split(",") if g.strip()]


def validation_error(prop: str, message: str) -> JSONResponse:
    """Servarr's 400 shape: a list of failed property validations."""
    return JSONResponse([{"propertyName": prop, "errorMessage": message, "severity": "error"}], status_code=400)


def norm_path(path: str | None) -> str:
    return os.path.normcase(os.path.normpath(path.strip())) if path and path.strip() else ""


def is_known_root(path: str, roots: list[str]) -> bool:
    return norm_path(path) in {norm_path(r) for r in roots}


def source_name(db: Session, user: CurrentUser) -> str:
    """How history credits the caller: the API key's name (e.g. "Seerr")."""
    key = db.get(models.ApiKey, user.api_key_id) if user.api_key_id else None
    return key.name if key is not None else "request manager"


def tag_resource(tag: models.Tag) -> dict[str, Any]:
    return {"id": tag.id, "label": tag.label}


def client_names(db: Session) -> dict[int, str]:
    return {c.id: c.name for c in db.scalars(select(models.DownloadClient)).all()}


def queue_record(item: models.DownloadQueueItem, names: dict[int, str]) -> dict[str, Any]:
    """The fields common to Radarr and Sonarr queue records; callers add movieId/seriesId etc."""
    status, tracked_status, tracked_state = QUEUE_STATE.get(item.status, ("queued", "ok", "downloading"))
    size = item.size_bytes or 0
    eta = iso(datetime.now(UTC) + timedelta(seconds=item.eta_seconds)) if item.eta_seconds is not None and item.eta_seconds >= 0 else None
    return {
        "id": item.id,
        "title": item.release_title or item.title,
        "size": size,
        "sizeleft": max(0, int(size * (1 - min(item.progress or 0.0, 100.0) / 100))),
        "timeleft": timeleft(item.eta_seconds),
        "estimatedCompletionTime": eta,
        "added": iso(item.added_at),
        "status": status,
        "trackedDownloadStatus": tracked_status,
        "trackedDownloadState": tracked_state,
        "statusMessages": [{"title": item.release_title, "messages": [item.error_message]}] if item.error_message else [],
        "errorMessage": item.error_message or "",
        "downloadId": item.client_item_id or "",
        "protocol": item.protocol or "unknown",
        "downloadClient": names.get(item.download_client_id or 0, ""),
        "indexer": item.indexer_name or "",
        "quality": {"quality": {"name": item.quality or "Unknown"}},
    }


def queue_page(records: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    page, page_size = max(1, page), max(1, min(page_size, 1000))
    start = (page - 1) * page_size
    return {
        "page": page,
        "pageSize": page_size,
        "sortKey": "timeleft",
        "sortDirection": "ascending",
        "totalRecords": len(records),
        "records": records[start:start + page_size],
    }


def command_response(body: CommandIn, status: str, job_ids: list[int]) -> dict[str, Any]:
    return {
        "id": job_ids[0] if job_ids else 0,
        "name": body.name,
        "commandName": body.name,
        "body": body.model_dump(by_alias=True, exclude_defaults=True),
        "status": status,
        "queued": iso(datetime.now(UTC)),
        "trigger": "manual",
    }


def add_servarr_routes(router: APIRouter, *, url_base: str, version: str, root_folders: Callable[[], list[str]]) -> None:
    """Register the endpoints every servarr app has. Seerr spells some paths in camelCase
    (``/qualityProfile``); the real apps route case-insensitively, so both spellings are served."""

    @router.get("/system/status")
    @allow_api_key_query
    def system_status(request: Request, _: Requires(Permission.MEDIA_READ)):
        return {
            "appName": "Maniac Media",
            "instanceName": "Maniac Media",
            # Seerr reads the major version (Sonarr <= 3 makes it ask for language profiles)
            "version": version,
            "maniacMediaVersion": __version__,
            "urlBase": request.scope.get("root_path", "") + url_base,
            "isProduction": True,
            "isAdmin": False,
            "authentication": "external",
            "startTime": None,
        }

    @router.get("/qualityprofile")
    @router.get("/qualityProfile")
    @allow_api_key_query
    def quality_profiles(db: DB, _: Requires(Permission.MEDIA_READ)):
        profiles = db.scalars(select(models.QualityProfile).order_by(models.QualityProfile.id)).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "upgradeAllowed": bool(p.upgrade_allowed),
                "cutoff": p.cutoff,
                "items": [{"quality": {"name": q}, "allowed": True} for q in (p.items or [])],
            }
            for p in profiles
        ]

    @router.get("/rootfolder")
    @router.get("/rootFolder")
    @allow_api_key_query
    def list_root_folders(_: Requires(Permission.MEDIA_READ)):
        out = []
        for i, path in enumerate(root_folders(), start=1):
            try:
                usage = shutil.disk_usage(path)
                free, total, accessible = usage.free, usage.total, True
            except OSError:
                free, total, accessible = 0, 0, False
            out.append({"id": i, "path": path, "accessible": accessible, "freeSpace": free, "totalSpace": total, "unmappedFolders": []})
        return out

    # Tags are shared by both facades. Seerr's "tag requests" finds or creates one per requesting
    # user, then sends its id with the add/update; tag_service turns that into request attribution.
    @router.get("/tag")
    @allow_api_key_query
    def list_tags(db: DB, _: Requires(Permission.MEDIA_READ)):
        return [tag_resource(t) for t in tag_service.list_tags(db)]

    @router.post("/tag", status_code=201)
    @allow_api_key_query
    def create_tag(body: TagIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
        try:
            tag = tag_service.create_tag(db, body.label, user.actor)
        except tag_service.TagError as exc:
            return validation_error("Label", str(exc))
        db.commit()
        return tag_resource(tag)

    @router.put("/tag/{tag_id}", status_code=202)
    @allow_api_key_query
    def rename_tag(tag_id: int, body: TagIn, db: DB, user: Requires(Permission.MEDIA_WRITE)):
        try:
            tag = tag_service.rename_tag(db, tag_id, body.label, user.actor)
        except tag_service.TagError as exc:
            return validation_error("Label", str(exc))
        db.commit()
        return tag_resource(tag)
