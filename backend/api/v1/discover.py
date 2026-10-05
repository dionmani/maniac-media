"""Discover: new and upcoming movies & shows, plus the "not interested" list."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from backend.api import schemas
from backend.api.deps import DB, Requires
from backend.core.security import Permission
from backend.db.repositories import DiscoverRepository
from backend.domain import discover as rules
from backend.services import discover_service

router = APIRouter(tags=["discover"])

MediaType = Literal["movie", "tv"]


# exclusions first: "/discover/exclusions" must not be read as a media type
@router.get("/discover/exclusions", response_model=list[schemas.DiscoverExclusionOut])
def list_exclusions(db: DB, _: Requires(Permission.MEDIA_READ), media_type: MediaType | None = None):
    return DiscoverRepository(db).exclusions(media_type)


@router.post("/discover/exclusions", response_model=schemas.DiscoverExclusionOut, status_code=201)
def add_exclusion(req: schemas.DiscoverExclusionCreate, db: DB, _: Requires(Permission.MEDIA_WRITE)):
    row = DiscoverRepository(db).add_exclusion(req.media_type, req.tmdb_id, req.title, req.year)
    db.commit()
    return row


@router.delete("/discover/exclusions/{media_type}/{tmdb_id}", response_model=schemas.Message)
def remove_exclusion(media_type: MediaType, tmdb_id: int, db: DB, _: Requires(Permission.MEDIA_WRITE)):
    if not DiscoverRepository(db).remove_exclusion(media_type, tmdb_id):
        raise HTTPException(404, "Title is not hidden")
    db.commit()
    return schemas.Message(message="Title will show in Discover again")


@router.get("/discover/{media_type}")
async def overview(media_type: MediaType, _: Requires(Permission.MEDIA_READ), limit: int = Query(20, ge=1, le=100)):
    return await discover_service.overview(media_type, limit)


@router.get("/discover/{media_type}/{section}")
async def section(media_type: MediaType, section: str, _: Requires(Permission.MEDIA_READ)):
    if not rules.is_section(media_type, section):
        raise HTTPException(404, "Unknown Discover section")
    return await discover_service.section(media_type, section)


@router.post("/discover/{media_type}/refresh", response_model=schemas.Message)
def refresh(media_type: MediaType, _: Requires(Permission.MEDIA_WRITE)):
    discover_service.invalidate(media_type)
    return schemas.Message(message="Discover will reload on next view")
