"""Metadata façade: provider selection, fallbacks, and applying metadata to library entities.

Business code asks for "a movie matching X" – it never knows which provider answered.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.secrets import metadata_key_name, secret_store
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.domain.availability import wants_air_times
from backend.domain.parser import normalize_title
from backend.domain.types import MediaMetadata
from backend.plugins import registry
from backend.plugins.base import MetadataProvider, PluginKind

log = logging.getLogger("maniac.metadata")

# v1 and the scanner used stock placeholder art; treat it as "no artwork".
PLACEHOLDER_HOSTS = ("images.unsplash.com",)


def _is_local(path: str | None) -> bool:
    """Artwork the user keeps next to their media (folder.jpg, fanart.jpg) always wins."""
    import os

    return bool(path) and not path.startswith(("http://", "https://")) and os.path.isfile(path)


def _is_v1_placeholder(text: str | None) -> bool:
    return bool(text) and text.startswith(("Theatrical film '", "Television series '", "Discovered from filesystem", "Cast: ", "Cinema release: ", "Series: ", "Starring: "))


def _is_placeholder(url: str | None) -> bool:
    return not url or any(h in url for h in PLACEHOLDER_HOSTS)


def providers(media_type: str) -> list[MetadataProvider]:
    settings = get_settings()
    result = []
    for key, cls in registry.available(PluginKind.METADATA).items():
        conf = settings.metadata_providers.get(key)
        if conf is not None and not conf.enabled:
            continue
        opts = {"language": settings.metadata_language}
        if cls.requires_api_key or any(f.secret for f in cls.fields):
            try:
                opts["api_key"] = secret_store.get(metadata_key_name(key))
                if key == "tvdb":
                    opts["pin"] = secret_store.get(f"metadata.{key}.pin")
            except Exception:
                opts["api_key"] = None
        plugin = cls(opts)  # type: ignore[call-arg]
        if not plugin.is_configured():  # type: ignore[attr-defined]
            continue
        if media_type == "movie" and not plugin.supports_movies:  # type: ignore[attr-defined]
            continue
        if media_type == "tv" and not plugin.supports_series:  # type: ignore[attr-defined]
            continue
        result.append((conf.priority if conf else 100, plugin))
    return [p for _, p in sorted(result, key=lambda x: x[0])]  # type: ignore[misc]


async def search(query: str, media_type: str, year: int | None = None) -> list[MediaMetadata]:
    errors = []
    for provider in providers(media_type):
        try:
            results = await (provider.search_movies(query, year) if media_type == "movie" else provider.search_series(query))
            if results:
                return results[:25]
        except Exception as exc:
            errors.append(f"{provider.name}: {exc}")
            log.warning("Metadata search via %s failed: %s", provider.name, exc)
    if errors:
        log.info("All metadata providers failed or returned nothing: %s", errors)
    return []


async def fetch_movie(*, tmdb_id: int | None = None, imdb_id: str | None = None, title: str | None = None, year: int | None = None) -> MediaMetadata | None:
    """Best full record, merging a richer provider over a keyless one."""
    best: MediaMetadata | None = None
    for provider in providers("movie"):
        try:
            meta = await provider.get_movie(tmdb_id=tmdb_id, imdb_id=imdb_id)
            if meta is None and title and not (tmdb_id or imdb_id):
                found = await provider.search_movies(title, year)
                found = [f for f in found if not year or not f.year or abs(f.year - year) <= 1]
                if found and normalize_title(found[0].title) == normalize_title(title):
                    meta = await provider.get_movie(tmdb_id=found[0].tmdb_id, imdb_id=found[0].imdb_id) or found[0]
            if meta:
                best = _merge(best, meta)
                imdb_id = imdb_id or meta.imdb_id
                tmdb_id = tmdb_id or meta.tmdb_id
                if best.overview and best.cast and best.genres:
                    break
        except Exception as exc:
            log.warning("Movie lookup via %s failed: %s", provider.name, exc)
    return best


async def fetch_series(
    *, tvdb_id: int | None = None, tmdb_id: int | None = None, tvmaze_id: int | None = None, imdb_id: str | None = None,
    title: str | None = None, year: int | None = None,
) -> MediaMetadata | None:
    best: MediaMetadata | None = None
    answers: list[MediaMetadata] = []
    for provider in providers("tv"):
        try:
            meta = await provider.get_series(tvdb_id=tvdb_id, tmdb_id=tmdb_id, tvmaze_id=tvmaze_id, imdb_id=imdb_id)
            if meta is None and title and not any((tvdb_id, tmdb_id, tvmaze_id, imdb_id)):
                found = [f for f in await provider.search_series(title) if normalize_title(f.title) == normalize_title(title)]
                if year:
                    found.sort(key=lambda f: abs((f.year or year) - year))
                if found:
                    f = found[0]
                    meta = await provider.get_series(tvdb_id=f.tvdb_id, tmdb_id=f.tmdb_id, tvmaze_id=f.tvmaze_id, imdb_id=f.imdb_id) or f
            if meta:
                answers.append(meta)
                best = _merge(best, meta)
                tvdb_id, tvmaze_id, imdb_id = tvdb_id or meta.tvdb_id, tvmaze_id or meta.tvmaze_id, imdb_id or meta.imdb_id
                tmdb_id = tmdb_id or meta.tmdb_id
                if best.episodes and best.overview:
                    break
        except Exception as exc:
            log.warning("Series lookup via %s failed: %s", provider.name, exc)
    if best is not None:
        await _fill_air_times(best, answers)
    return best


async def _fill_air_times(best: MediaMetadata, answers: list[MediaMetadata]) -> None:
    """Borrow exact air times from a provider that has them when the episode list came from one that only has dates.

    The release gate holds a date-only episode until that date is over everywhere; a real air time lets it go
    as soon as the episode has aired. Only shows with recent or upcoming episodes are worth the extra lookup.
    """
    if any(e.air_date_utc for e in best.episodes) or not wants_air_times((e.air_date for e in best.episodes), date.today()):
        return
    source = next((a for a in answers if a is not best and any(e.air_date_utc for e in a.episodes)), None)
    if source is None:
        for provider in providers("tv"):
            if not provider.provides_air_times or provider.key == best.provider:  # type: ignore[attr-defined]
                continue
            try:
                source = await provider.get_series(tvdb_id=best.tvdb_id, tmdb_id=best.tmdb_id, tvmaze_id=best.tvmaze_id, imdb_id=best.imdb_id)
            except Exception as exc:
                log.info("Air times via %s unavailable: %s", provider.name, exc)
            if source is not None:
                break
    if source is None:
        return
    # providers number some shows differently, so a time is only taken when the date agrees too
    times = {(e.season_number, e.episode_number, e.air_date): e.air_date_utc for e in source.episodes if e.air_date_utc}
    for ep in best.episodes:
        ep.air_date_utc = times.get((ep.season_number, ep.episode_number, ep.air_date))
    _merge(best, source)  # fills ids the first provider lacked (e.g. tvmaze_id), so the next refresh looks it up directly


def series_lookup(series: models.Series) -> dict:
    """The ids a refresh looks a library series up by (a TVDB id only counts once a provider confirmed it)."""
    return {"tvmaze_id": series.tvmaze_id, "imdb_id": series.imdb_id, "tvdb_id": series.tvdb_id if series.metadata_source else None}


async def fetch_series_for(ids: dict, title: str, year: int | None) -> MediaMetadata | None:
    meta = await fetch_series(**ids) if any(ids.values()) else None
    return meta or await fetch_series(title=title, year=year)


def _merge(base: MediaMetadata | None, extra: MediaMetadata) -> MediaMetadata:
    if base is None:
        return extra
    for field in base.__dataclass_fields__:
        current = getattr(base, field)
        if current in (None, "", []) and getattr(extra, field) not in (None, "", []):
            setattr(base, field, getattr(extra, field))
    return base


def _sort_title(title: str) -> str:
    lowered = title.lower()
    for article in ("the ", "a ", "an "):
        if lowered.startswith(article):
            return title[len(article):] + ", " + title[: len(article) - 1]
    return title


def _cast(meta: MediaMetadata) -> list[dict]:
    return [{"name": c.name, "character": c.character, "image": c.image} for c in meta.cast]


def apply_movie(db: Session, movie: models.Movie, meta: MediaMetadata, *, overwrite_title: bool = False) -> None:
    if overwrite_title and meta.title:
        movie.title = meta.title
    movie.year = meta.year or movie.year
    movie.sort_title = _sort_title(movie.title)
    movie.original_title = meta.original_title or movie.original_title
    movie.overview = meta.overview or (None if _is_v1_placeholder(movie.overview) else movie.overview)
    movie.tagline = meta.tagline or movie.tagline
    if not _is_local(movie.poster_path) and (meta.poster_url or _is_placeholder(movie.poster_path)):
        movie.poster_path = meta.poster_url or movie.poster_path
    if not _is_local(movie.backdrop_path) and (meta.backdrop_url or _is_placeholder(movie.backdrop_path)):
        movie.backdrop_path = meta.backdrop_url or meta.poster_url or movie.backdrop_path
    if meta.genres:
        movie.genres = ", ".join(meta.genres)
    # v1 stamped 120 min on every scanned movie; don't keep that fiction once we know the real source
    movie.runtime_minutes = meta.runtime_minutes or (None if movie.metadata_source is None and movie.runtime_minutes == 120 else movie.runtime_minutes)
    movie.rating = meta.rating if meta.rating is not None else movie.rating
    movie.votes = meta.votes if meta.votes is not None else movie.votes
    movie.certification = meta.certification or movie.certification
    movie.release_date = meta.release_date or movie.release_date
    movie.digital_release_date = meta.digital_release_date or movie.digital_release_date
    movie.physical_release_date = meta.physical_release_date or movie.physical_release_date
    movie.studio = meta.studio or (None if movie.studio in ("Vault", "Theatrical Release", "Disk Library") else movie.studio)
    if movie.genres in ("Cinema", "Feature Film") and not meta.genres:
        movie.genres = None
    if meta.cast:
        movie.cast_members = _cast(meta)
    if meta.crew:
        movie.crew = meta.crew
    if meta.status:
        movie.status = meta.status
    if meta.production_status:
        movie.production_status = meta.production_status
    if meta.imdb_id:
        movie.imdb_id = meta.imdb_id
    if meta.tmdb_id and movie.tmdb_id != meta.tmdb_id:
        clash = db.scalar(select(models.Movie.id).where(models.Movie.tmdb_id == meta.tmdb_id, models.Movie.id != movie.id))
        if clash is None:
            movie.tmdb_id = meta.tmdb_id
    movie.metadata_source = meta.provider
    movie.last_metadata_refresh = utcnow()


def apply_series(db: Session, series: models.Series, meta: MediaMetadata, *, create_episodes: bool = True) -> dict[str, int]:
    series.year = meta.year or series.year
    series.sort_title = _sort_title(series.title)
    series.original_title = meta.original_title or series.original_title
    series.overview = meta.overview or (None if _is_v1_placeholder(series.overview) else series.overview)
    if not _is_local(series.poster_path) and (meta.poster_url or _is_placeholder(series.poster_path)):
        series.poster_path = meta.poster_url or series.poster_path
    if not _is_local(series.backdrop_path) and (meta.backdrop_url or _is_placeholder(series.backdrop_path)):
        series.backdrop_path = meta.backdrop_url or meta.poster_url or series.backdrop_path
    if meta.genres:
        series.genres = ", ".join(meta.genres)
    series.network = meta.studio or (None if series.network in ("Vault Series", "Disk Library") else series.network)
    series.status = meta.status or series.status
    series.runtime_minutes = meta.runtime_minutes or series.runtime_minutes
    series.rating = meta.rating if meta.rating is not None else series.rating
    series.votes = meta.votes if meta.votes is not None else series.votes
    series.certification = meta.certification or series.certification
    series.first_aired = meta.release_date or series.first_aired
    if meta.cast:
        series.cast_members = _cast(meta)
    series.imdb_id = meta.imdb_id or series.imdb_id
    series.tvmaze_id = meta.tvmaze_id or series.tvmaze_id
    series.tmdb_id = meta.tmdb_id or series.tmdb_id
    if meta.tvdb_id and series.tvdb_id != meta.tvdb_id:
        clash = db.scalar(select(models.Series.id).where(models.Series.tvdb_id == meta.tvdb_id, models.Series.id != series.id))
        if clash is None:
            series.tvdb_id = meta.tvdb_id
    series.metadata_source = meta.provider
    series.last_metadata_refresh = utcnow()
    return merge_episodes(db, series, meta) if create_episodes else {"added": 0, "updated": 0}


def merge_episodes(db: Session, series: models.Series, meta: MediaMetadata) -> dict[str, int]:
    """Upsert seasons/episodes by (season, episode) number – never deletes file-backed rows."""
    if not meta.episodes:
        return {"added": 0, "updated": 0}
    db.flush()
    seasons = {s.season_number: s for s in db.scalars(select(models.Season).where(models.Season.series_id == series.id))}
    existing = {
        (e.season_number, e.episode_number): e
        for e in db.scalars(select(models.Episode).where(models.Episode.series_id == series.id))
    }
    added = updated = 0
    monitor_new = series.monitor_new_seasons is not False
    for ep in meta.episodes:
        season = seasons.get(ep.season_number)
        if season is None:
            season = models.Season(series_id=series.id, season_number=ep.season_number, monitored=monitor_new and ep.season_number > 0)
            db.add(season)
            db.flush()
            seasons[ep.season_number] = season
        row = existing.get((ep.season_number, ep.episode_number))
        if row is None:
            row = models.Episode(
                series_id=series.id,
                season_id=season.id,
                season_number=ep.season_number,
                episode_number=ep.episode_number,
                title=ep.title,
                monitored=season.monitored,
                has_file=False,
                status="missing",
            )
            db.add(row)
            existing[(ep.season_number, ep.episode_number)] = row
            added += 1
        else:
            updated += 1
        row.title = ep.title or row.title
        row.overview = ep.overview or row.overview
        if ep.air_date_utc:
            row.air_date_utc = ep.air_date_utc
        elif ep.air_date and ep.air_date != row.air_date:
            row.air_date_utc = None  # rescheduled, and nobody knows the new time yet
        row.air_date = ep.air_date or row.air_date
        row.runtime_minutes = ep.runtime_minutes or row.runtime_minutes
        row.tvmaze_id = ep.tvmaze_id or row.tvmaze_id
        row.tvdb_id = ep.tvdb_id or row.tvdb_id
        row.absolute_number = ep.absolute_number or row.absolute_number
    series.total_seasons = max([n for n in seasons if n > 0] or [series.total_seasons or 1])
    return {"added": added, "updated": updated}


def stale_rows(db: Session, series: models.Series, meta: MediaMetadata) -> tuple[list[models.Episode], list[models.Season], int]:
    """Rows `merge_episodes` left behind that `meta` no longer describes (e.g. from a provider that numbered seasons differently).

    Returns (episodes to drop, seasons left empty by that, episodes kept only because they have a file / are downloading).
    With no episode list from the provider nothing is stale – an empty answer must never read as "delete everything".
    """
    if not meta.episodes:
        return [], [], 0
    known = {(e.season_number, e.episode_number) for e in meta.episodes}
    episodes: list[models.Episode] = []
    kept = 0
    remaining: dict[int, int] = {}
    for ep in db.scalars(select(models.Episode).where(models.Episode.series_id == series.id)):
        if (ep.season_number, ep.episode_number) in known:
            remaining[ep.season_id] = remaining.get(ep.season_id, 0) + 1
        elif ep.has_file or ep.status == "downloading":
            remaining[ep.season_id] = remaining.get(ep.season_id, 0) + 1
            kept += 1
        else:
            episodes.append(ep)
    meta_seasons = {e.season_number for e in meta.episodes}
    seasons = [
        s for s in db.scalars(select(models.Season).where(models.Season.series_id == series.id))
        if not remaining.get(s.id) and s.season_number not in meta_seasons
    ]
    return episodes, seasons, kept


# --------------------------------------------------------------------------- refresh job
THIN_PROVIDERS = {"imdb"}  # can match a title, but returns no overview/dates/genres


def _richer_provider(media_type: str) -> bool:
    return any(p.key not in THIN_PROVIDERS for p in providers(media_type))  # type: ignore[attr-defined]


async def refresh_library(session_factory, *, media_type: str = "all", limit: int | None = None, force_ids: list[int] | None = None, progress=None) -> dict:
    """Refresh stale metadata in batches; unmatched items are matched by title/year."""
    settings = get_settings()
    batch = limit or settings.metadata_refresh_batch_size
    stale_before = utcnow() - timedelta(days=7)
    thin_before = utcnow() - timedelta(days=1)
    stats = {"movies": 0, "series": 0, "failed": 0}

    def pick(model, kind: str):
        with session_factory() as db:
            stmt = select(model.id)
            if force_ids:
                stmt = stmt.where(model.id.in_(force_ids))
            else:
                due = or_(model.last_metadata_refresh.is_(None), model.last_metadata_refresh < stale_before)
                if _richer_provider(kind):
                    # matched, but only thinly (IMDb suggest has no plot or dates): worth a daily retry once a
                    # provider that can fill it in is configured, instead of waiting out the 7-day cycle
                    thin = and_(model.metadata_source.is_not(None), or_(model.overview.is_(None), model.overview == ""),
                                model.last_metadata_refresh < thin_before)
                    due = or_(due, thin)
                stmt = stmt.where(due).order_by(model.last_metadata_refresh.is_not(None), model.last_metadata_refresh).limit(batch)
            return list(db.scalars(stmt))

    movie_ids = pick(models.Movie, "movie") if media_type in ("all", "movie") else []
    series_ids = pick(models.Series, "tv") if media_type in ("all", "tv") else []
    total = len(movie_ids) + len(series_ids)
    done = 0
    stats["more_pending"] = not force_ids and (len(movie_ids) >= batch or len(series_ids) >= batch)

    for movie_id in movie_ids:
        done += 1
        try:
            with session_factory() as db:
                movie = db.get(models.Movie, movie_id)
                if movie is None:
                    continue
                ids = {"tmdb_id": movie.tmdb_id if movie.metadata_source == "tmdb" else None, "imdb_id": movie.imdb_id}
                title, year = movie.title, movie.year
            meta = await fetch_movie(**ids) if any(ids.values()) else None
            if meta is None:
                meta = await fetch_movie(title=title, year=year)
            with session_factory() as db:
                movie = db.get(models.Movie, movie_id)
                if movie is None:
                    continue
                if meta:
                    apply_movie(db, movie, meta)
                    stats["movies"] += 1
                else:
                    movie.last_metadata_refresh = utcnow()
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    stats["failed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log.warning("Metadata refresh failed for movie %s: %s", movie_id, exc)
        if progress:
            progress(done / max(total, 1) * 100, f"Refreshing metadata {done}/{total}")
        await asyncio.sleep(0.25)  # be a polite API citizen

    for series_id in series_ids:
        done += 1
        try:
            with session_factory() as db:
                s = db.get(models.Series, series_id)
                if s is None:
                    continue
                ids, title, year = series_lookup(s), s.title, s.year
            meta = await fetch_series_for(ids, title, year)
            with session_factory() as db:
                s = db.get(models.Series, series_id)
                if s is None:
                    continue
                if meta:
                    apply_series(db, s, meta)
                    stats["series"] += 1
                else:
                    s.last_metadata_refresh = utcnow()
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    stats["failed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log.warning("Metadata refresh failed for series %s: %s", series_id, exc)
        if progress:
            progress(done / max(total, 1) * 100, f"Refreshing metadata {done}/{total}")
        await asyncio.sleep(0.25)
    return stats
