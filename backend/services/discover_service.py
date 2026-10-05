"""Discover: new and upcoming movies and shows the user may not have heard of.

The first metadata provider that supports discover (TMDB) supplies each section; what belongs in a
section is defined in domain/discover.py. Section lists are cached in memory for a few hours (the
``discover_refresh`` job keeps them warm), while the parts that change with the library - "already
added" and "not interested" - are applied on every request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from backend.core.settings import AppSettings, get_settings
from backend.db.repositories import DiscoverRepository, MovieRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain import discover as rules
from backend.domain.types import DiscoverItem
from backend.plugins.base import MetadataProvider
from backend.services import metadata_service

log = logging.getLogger("maniac.discover")

CACHE_TTL_SECONDS = 6 * 3600
MEDIA_TYPES = ("movie", "tv")


@dataclass
class _Entry:
    fetched_at: float
    items: list[DiscoverItem]
    first_seen: dict[int, datetime]


# (provider, media_type, section, region, languages) -> entry. Concurrent cold requests may both
# fetch; that only costs a few duplicate provider calls.
_cache: dict[tuple, _Entry] = {}


def provider_for(media_type: str) -> MetadataProvider | None:
    return next((p for p in metadata_service.providers(media_type) if p.supports_discover), None)


def invalidate(media_type: str | None = None) -> None:
    for key in [k for k in _cache if media_type is None or k[1] == media_type]:
        _cache.pop(key, None)


async def overview(media_type: str, limit: int | None = None) -> dict:
    """Every section for ``media_type``; ``limit`` caps the items per section (rows on the page)."""
    return await _sections(media_type, list(rules.SECTIONS[media_type]), limit)


async def section(media_type: str, key: str) -> dict:
    """One section in full ("See all"); same shape as :func:`overview`."""
    return await _sections(media_type, [key], None)


async def refresh_all() -> dict:
    """Re-fetch every section (the scheduled job), so page loads are served from a warm cache."""
    stats: dict[str, int | str] = {}
    settings = get_settings()
    for media_type in MEDIA_TYPES:
        provider = provider_for(media_type)
        if provider is None:
            continue
        invalidate(media_type)
        for key in rules.SECTIONS[media_type]:
            try:
                stats[f"{media_type}.{key}"] = len((await _entry(provider, media_type, key, settings)).items)
            except Exception as exc:
                stats[f"{media_type}.{key}"] = f"failed: {_message(exc)}"
                log.warning("Discover refresh of %s/%s failed: %s", media_type, key, exc)
    return stats or {"status": "skipped", "reason": "no metadata provider supports discover (add a TMDB API key)"}


async def _sections(media_type: str, keys: list[str], limit: int | None) -> dict:
    settings = get_settings()
    provider = provider_for(media_type)
    result: dict = {"media_type": media_type, "configured": provider is not None, "region": settings.discover_region,
                    "provider": provider.name if provider else None, "sections": []}
    if provider is None:
        return result
    fetched = await asyncio.gather(*(_entry(provider, media_type, k, settings) for k in keys), return_exceptions=True)
    entries = {k: e for k, e in zip(keys, fetched, strict=True) if isinstance(e, _Entry)}
    excluded, library = await asyncio.to_thread(_library_state, media_type, [i for e in entries.values() for i in e.items])
    for key, entry in zip(keys, fetched, strict=True):
        out: dict = {"key": key, "label": rules.SECTIONS[media_type][key]}
        if isinstance(entry, _Entry):
            items = [_item_out(i, entry.first_seen, library) for i in entry.items if i.meta.tmdb_id not in excluded]
            out |= {"items": items[:limit] if limit else items, "total": len(items)}
        else:
            log.warning("Discover %s/%s failed: %s", media_type, key, entry)
            out |= {"items": [], "total": 0, "error": _message(entry)}
        result["sections"].append(out)
    return result


async def _entry(provider: MetadataProvider, media_type: str, key: str, settings: AppSettings) -> _Entry:
    cache_key = (provider.key, media_type, key, settings.discover_region, tuple(settings.discover_languages))
    cached = _cache.get(cache_key)
    if cached is not None and time.monotonic() - cached.fetched_at < CACHE_TTL_SECONDS:
        return cached
    items = await provider.discover(media_type, key, region=settings.discover_region, languages=list(settings.discover_languages),
                                    today=date.today())
    items = [i for i in items if i.meta.tmdb_id]  # everything (badges, exclusions) is keyed by TMDB id
    first_seen = await asyncio.to_thread(_record_seen, media_type, key, [i.meta.tmdb_id for i in items])
    entry = _Entry(time.monotonic(), items, first_seen)
    _cache[cache_key] = entry
    return entry


def _record_seen(media_type: str, key: str, tmdb_ids: list[int]) -> dict[int, datetime]:
    with session_scope() as db:
        return DiscoverRepository(db).record_seen(media_type, key, tmdb_ids)


def _library_state(media_type: str, items: list[DiscoverItem]) -> tuple[set[int], dict[int, int]]:
    """(excluded TMDB ids, {tmdb_id: library id}) for the titles about to be shown."""
    with session_scope() as db:
        excluded = DiscoverRepository(db).excluded_ids(media_type)
        tmdb_ids = list({i.meta.tmdb_id for i in items if i.meta.tmdb_id})
        library: dict[int, int] = {}
        if media_type == "movie":
            by_tmdb, by_imdb = MovieRepository(db).ids_by_external(tmdb_ids, list({i.meta.imdb_id for i in items if i.meta.imdb_id}))
            for i in items:
                found = by_tmdb.get(i.meta.tmdb_id) or (by_imdb.get(i.meta.imdb_id) if i.meta.imdb_id else None)  # type: ignore[arg-type]
                if found:
                    library[i.meta.tmdb_id] = found  # type: ignore[index]
        else:
            by_tmdb, by_tvdb = SeriesRepository(db).ids_by_external(tmdb_ids, list({i.meta.tvdb_id for i in items if i.meta.tvdb_id}))
            for i in items:
                found = by_tmdb.get(i.meta.tmdb_id) or (by_tvdb.get(i.meta.tvdb_id) if i.meta.tvdb_id else None)  # type: ignore[arg-type]
                if found:
                    library[i.meta.tmdb_id] = found  # type: ignore[index]
        return excluded, library


def _item_out(item: DiscoverItem, first_seen: dict[int, datetime], library: dict[int, int]) -> dict:
    library_id = library.get(item.meta.tmdb_id)  # type: ignore[arg-type]
    seen = first_seen.get(item.meta.tmdb_id)  # type: ignore[arg-type]
    return item.to_dict() | {
        "in_library": library_id is not None,
        "library_id": library_id,
        "first_seen_at": seen.replace(tzinfo=timezone.utc).isoformat() if seen else None,
    }


def _message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__
