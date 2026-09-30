"""Search engine: interactive search, automatic search, wanted/missing sweeps and RSS matching."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select

from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, QueueRepository
from backend.db.session import session_scope
from backend.domain.parser import normalize_title, parse_episode, parse_movie
from backend.domain.quality import get_quality, parse_quality
from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.services import download_service, indexer_service
from backend.services.audit import SYSTEM, Actor
from backend.services.decision_engine import Decision, Target, evaluate, rank
from backend.services.quality_service import spec_for

log = logging.getLogger("maniac.search")


class SearchError(ValueError):
    pass


# `_already_queued` (a SELECT) and the eventual grab (an INSERT) are separated by a real
# indexer search, so two concurrent calls for the same target can both pass the check before
# either grabs, producing duplicate downloads. Serialize per-target with an in-process lock —
# the worker pool is a single asyncio event loop, so this closes the race for every caller
# (automatic search, RSS sync, missing-search sweep, failed-download retry) without a DB migration.
_target_locks: dict[tuple, asyncio.Lock] = {}
_target_locks_guard = asyncio.Lock()


async def _lock_for(media_type: str, media_id: int, season: int | None, episodes: list[int] | None) -> asyncio.Lock:
    key = (media_type, media_id, season, tuple(sorted(episodes)) if episodes else ())
    async with _target_locks_guard:
        lock = _target_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _target_locks[key] = lock
        return lock


def _min_seeders(db) -> dict[int, int]:
    return {i.id: (i.minimum_seeders or 1) for i in db.scalars(select(models.Indexer))}


def _blocklist(db, media_type: str, media_id: int) -> set[str]:
    rows = db.scalars(select(models.Blocklist.release_title).where(models.Blocklist.media_type == media_type, models.Blocklist.media_id == media_id))
    return {r.lower() for r in rows}


def build_target(media_type: str, media_id: int, season: int | None = None, episodes: list[int] | None = None) -> tuple[Target, SearchCriteria, set[str]]:
    with session_scope() as db:
        if media_type == "movie":
            movie = db.get(models.Movie, media_id)
            if movie is None:
                raise SearchError("Movie not found")
            current = parse_quality(movie.quality or movie.file_path or "") if movie.has_file else None
            if current and current.quality.name == "Unknown" and movie.quality:
                current = parse_quality(get_quality(movie.quality).name)
            target = Target("movie", movie.id, movie.title, movie.year, spec_for(db, movie.quality_profile_id),
                            current_quality=current, has_file=movie.has_file, aliases=[movie.original_title or ""],
                            minimum_seeders=_min_seeders(db))
            criteria = SearchCriteria("movie", movie.title, movie.year, imdb_id=movie.imdb_id, tmdb_id=movie.tmdb_id)
            return target, criteria, _blocklist(db, "movie", movie.id)

        series = db.get(models.Series, media_id)
        if series is None:
            raise SearchError("Series not found")
        stmt = select(models.Episode).where(models.Episode.series_id == series.id)
        if season is not None:
            stmt = stmt.where(models.Episode.season_number == season)
        if episodes:
            stmt = stmt.where(models.Episode.episode_number.in_(episodes))
        eps = list(db.scalars(stmt))
        with_files = [parse_quality(e.quality or e.file_path or "") for e in eps if e.has_file]
        all_have = bool(eps) and all(e.has_file for e in eps)
        worst = min(with_files, key=lambda q: q.quality.weight) if with_files and all_have else None
        target = Target("tv", series.id, series.title, series.year, spec_for(db, series.quality_profile_id),
                        season=season, episodes=list(episodes or []), current_quality=worst, has_file=all_have,
                        aliases=[series.original_title or ""], minimum_seeders=_min_seeders(db))
        criteria = SearchCriteria("tv", series.title, series.year, imdb_id=series.imdb_id, tvdb_id=series.tvdb_id,
                                  tvmaze_id=series.tvmaze_id, season=season, episode=episodes[0] if episodes and len(episodes) == 1 else None)
        return target, criteria, _blocklist(db, "tv", series.id)


async def interactive(media_type: str, media_id: int, season: int | None = None, episode: int | None = None) -> dict:
    target, criteria, blocked = await asyncio.to_thread(build_target, media_type, media_id, season, [episode] if episode else None)
    releases, report = await indexer_service.search(criteria)
    decisions = rank([evaluate(r, target, blocked) for r in releases])
    await asyncio.to_thread(_touch_search, media_type, media_id, season, [episode] if episode else None)
    return {
        "releases": [{**d.to_dict(), "download_url_token": _release_cache_put(d.release)} for d in decisions],
        "indexers": report,
    }


# Releases shown in interactive search are cached server-side so the client never sees or
# replays raw download URLs (which usually embed indexer API keys).
_release_cache: dict[str, tuple[float, ReleaseInfo]] = {}


def _release_cache_put(release: ReleaseInfo) -> str:
    import secrets
    import time

    now = time.time()
    for key in [k for k, (ts, _) in _release_cache.items() if now - ts > 3600]:
        _release_cache.pop(key, None)
    token = secrets.token_urlsafe(16)
    _release_cache[token] = (now, release)
    return token


def release_from_cache(token: str) -> ReleaseInfo | None:
    entry = _release_cache.get(token)
    return entry[1] if entry else None


def _touch_search(media_type: str, media_id: int, season: int | None, episodes: list[int] | None) -> None:
    with session_scope() as db:
        now = utcnow()
        if media_type == "movie":
            m = db.get(models.Movie, media_id)
            if m:
                m.last_search_at = now
        else:
            s = db.get(models.Series, media_id)
            if s:
                s.last_search_at = now
            stmt = select(models.Episode).where(models.Episode.series_id == media_id)
            if season is not None:
                stmt = stmt.where(models.Episode.season_number == season)
            if episodes:
                stmt = stmt.where(models.Episode.episode_number.in_(episodes))
            for e in db.scalars(stmt):
                e.last_search_at = now


async def automatic(media_type: str, media_id: int, season: int | None = None, episodes: list[int] | None = None,
                     actor: Actor = SYSTEM, retry_count: int = 0) -> dict:
    """Search and grab the best approved release for the target."""
    lock = await _lock_for(media_type, media_id, season, episodes)
    async with lock:
        queued = await asyncio.to_thread(_already_queued, media_type, media_id, season, episodes)
        if queued:
            return {"grabbed": None, "reason": "Already in the download queue"}
        target, criteria, blocked = await asyncio.to_thread(build_target, media_type, media_id, season, episodes)
        if media_type == "tv" and season is not None and not episodes:
            # season search: prefer a pack, fall back to individual episodes
            releases, _ = await indexer_service.search(criteria)
            best = _best(releases, target, blocked)
            if best:
                qid = await download_service.grab(best.release, media_type="tv", media_id=media_id, season=season,
                                                    episodes=best.episodes, actor=actor, retry_count=retry_count)
                await asyncio.to_thread(_touch_search, media_type, media_id, season, episodes)
                return {"grabbed": best.release.title, "queue_id": qid}
            missing = await asyncio.to_thread(_missing_episode_numbers, media_id, season)
            grabbed = []
            for number in missing:
                # each episode is a distinct lock key, so recursing here does not self-deadlock
                result = await automatic("tv", media_id, season, [number], actor, retry_count=retry_count)
                if result.get("grabbed"):
                    grabbed.append(result["grabbed"])
            return {"grabbed": grabbed or None, "reason": None if grabbed else "No acceptable releases found"}

        releases, report = await indexer_service.search(criteria)
        best = _best(releases, target, blocked)
        await asyncio.to_thread(_touch_search, media_type, media_id, season, episodes)
        if not best:
            return {"grabbed": None, "reason": "No acceptable releases found", "indexers": report, "candidates": len(releases)}
        qid = await download_service.grab(best.release, media_type=media_type, media_id=media_id, season=season if season is not None else best.season,
                                          episodes=episodes or best.episodes, actor=actor, retry_count=retry_count)
        return {"grabbed": best.release.title, "queue_id": qid, "quality": best.quality.quality.name}


def _best(releases: list[ReleaseInfo], target: Target, blocked: set[str]) -> Decision | None:
    decisions = [d for d in rank([evaluate(r, target, blocked) for r in releases]) if d.approved]
    return decisions[0] if decisions else None


def _already_queued(media_type: str, media_id: int, season: int | None, episodes: list[int] | None) -> bool:
    with session_scope() as db:
        return QueueRepository(db).is_queued(media_type, media_id, season, episodes[0] if episodes and len(episodes) == 1 else None)


def _missing_episode_numbers(series_id: int, season: int) -> list[int]:
    today = date.today().isoformat()
    with session_scope() as db:
        return list(
            db.scalars(
                select(models.Episode.episode_number).where(
                    models.Episode.series_id == series_id,
                    models.Episode.season_number == season,
                    models.Episode.monitored.is_(True),
                    models.Episode.has_file.is_(False),
                    (models.Episode.air_date.is_(None)) | (models.Episode.air_date <= today),
                )
            )
        )


async def search_missing(limit: int = 50, progress=None) -> dict:
    """Sweep monitored, missing items – least recently searched first."""
    today = date.today().isoformat()

    def collect():
        with session_scope() as db:
            movies = list(
                db.scalars(
                    select(models.Movie.id)
                    .where(models.Movie.monitored.is_(True), models.Movie.has_file.is_(False))
                    .order_by(models.Movie.last_search_at.is_not(None), models.Movie.last_search_at)
                    .limit(limit)
                )
            )
            eps = db.execute(
                EpisodeRepository(db).wanted_missing(today)
                .with_only_columns(models.Episode.series_id, models.Episode.season_number, models.Episode.episode_number)
                .order_by(models.Episode.last_search_at.is_not(None), models.Episode.last_search_at)
                .limit(limit)
            ).all()
            return movies, eps

    movie_ids, episodes = await asyncio.to_thread(collect)
    total, done, grabbed = len(movie_ids) + len(episodes), 0, 0
    for movie_id in movie_ids:
        done += 1
        try:
            if (await automatic("movie", movie_id)).get("grabbed"):
                grabbed += 1
        except Exception as exc:
            log.warning("Missing search failed for movie %s: %s", movie_id, exc)
        if progress:
            progress(done * 100 / max(total, 1), f"Searched {done}/{total}")
    for series_id, season, episode in episodes:
        done += 1
        try:
            if (await automatic("tv", series_id, season, [episode])).get("grabbed"):
                grabbed += 1
        except Exception as exc:
            log.warning("Missing search failed for S%sE%s of series %s: %s", season, episode, series_id, exc)
        if progress:
            progress(done * 100 / max(total, 1), f"Searched {done}/{total}")
    return {"searched": total, "grabbed": grabbed}


# --------------------------------------------------------------------------- RSS sync
async def rss_sync() -> dict:
    releases = await indexer_service.fetch_rss()
    if not releases:
        return {"releases": 0, "grabbed": 0}
    matches = await asyncio.to_thread(_match_rss, releases)
    grabbed = 0
    best_per_target: dict[tuple, Decision] = {}
    for key, (target, blocked, release) in matches.items():
        decision = evaluate(release, target, blocked)
        if decision.approved:
            tkey = key[:3]
            if tkey not in best_per_target or decision.score > best_per_target[tkey].score:
                best_per_target[tkey] = decision
    for (media_type, media_id, season), decision in best_per_target.items():
        try:
            lock = await _lock_for(media_type, media_id, season, decision.episodes)
            async with lock:
                if await asyncio.to_thread(_already_queued, media_type, media_id, season, decision.episodes):
                    continue
                await download_service.grab(decision.release, media_type=media_type, media_id=media_id, season=season, episodes=decision.episodes)
                grabbed += 1
        except Exception as exc:
            log.warning("RSS grab failed for %s: %s", decision.release.title, exc)
    return {"releases": len(releases), "matched": len(matches), "grabbed": grabbed}


def _match_rss(releases: list[ReleaseInfo]) -> dict:
    """Match RSS releases to monitored library items that want a file (missing or upgradable)."""
    today = date.today().isoformat()
    out: dict[tuple, tuple[Target, set[str], ReleaseInfo]] = {}
    with session_scope() as db:
        movies: dict[str, list[models.Movie]] = {}
        for m in db.scalars(select(models.Movie).where(models.Movie.monitored.is_(True))):
            movies.setdefault(normalize_title(m.title), []).append(m)
        series: dict[str, models.Series] = {normalize_title(s.title): s for s in db.scalars(select(models.Series).where(models.Series.monitored.is_(True)))}
        seeders = _min_seeders(db)

        for release in releases:
            ep = parse_episode(release.title)
            if ep and ep.season is not None:
                s = series.get(normalize_title(ep.series_title))
                if not s:
                    continue
                eps = [e for e in db.scalars(select(models.Episode).where(
                    models.Episode.series_id == s.id, models.Episode.season_number == ep.season,
                    models.Episode.monitored.is_(True))) if (ep.full_season or e.episode_number in ep.episodes) and (e.air_date is None or e.air_date <= today)]
                if not eps:
                    continue
                have = [parse_quality(e.quality or e.file_path or "") for e in eps if e.has_file]
                all_have = len(have) == len(eps)
                target = Target("tv", s.id, s.title, s.year, spec_for(db, s.quality_profile_id), season=ep.season,
                                episodes=[] if ep.full_season else ep.episodes,
                                current_quality=min(have, key=lambda q: q.quality.weight) if all_have and have else None,
                                has_file=all_have, minimum_seeders=seeders)
                out[("tv", s.id, ep.season, tuple(ep.episodes), release.guid)] = (target, _blocklist(db, "tv", s.id), release)
                continue
            pm = parse_movie(release.title)
            for m in movies.get(normalize_title(pm.title), []):
                if pm.year and m.year and abs(pm.year - m.year) > 1:
                    continue
                current = parse_quality(m.quality or m.file_path or "") if m.has_file else None
                target = Target("movie", m.id, m.title, m.year, spec_for(db, m.quality_profile_id), current_quality=current,
                                has_file=m.has_file, minimum_seeders=seeders)
                out[("movie", m.id, None, (), release.guid)] = (target, _blocklist(db, "movie", m.id), release)
    return out
