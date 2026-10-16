"""Search engine: interactive search, automatic search, wanted/missing sweeps and RSS matching."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select

from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, MovieRepository, QueueRepository
from backend.db.session import session_scope
from backend.domain.availability import Availability, episode_availability, latest, movie_availability
from backend.domain.custom_formats import FormatMatcher, ReleaseFacts
from backend.domain.custom_formats import score as score_formats
from backend.domain.parser import normalize_title, parse_episode, parse_movie, release_group
from backend.domain.quality import QualityProfileSpec, get_quality, parse_quality
from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.services import download_service, indexer_service
from backend.services.audit import SYSTEM, Actor
from backend.services.decision_engine import Decision, Target, evaluate, rank, release_gate
from backend.services.quality_service import get_matcher, spec_for

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


def movie_gate(movie: models.Movie) -> Availability:
    """Whether ``movie`` is out per the release gate, and the date that says so."""
    return movie_availability(digital=movie.digital_release_date, physical=movie.physical_release_date, release_date=movie.release_date,
                              year=movie.year, today=date.today(), catalog_days=get_settings().release_gate_catalog_days)


def _air_map(episodes, now) -> dict[tuple[int, int], Availability]:
    return {(e.season_number, e.episode_number): episode_availability(air_date=e.air_date, air_date_utc=e.air_date_utc, now=now) for e in episodes}


def compute_file_format_score(
    db,
    media_type: str,
    media_id: int,
    file_path: str | None,
    profile: QualityProfileSpec,
    matcher: FormatMatcher,
    *,
    episode_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Compute (current_format_score, scene_name) for an existing file.
    For scanned files (`source == 'scan'`), returns `current_fs=None` unless the name looks like
    a scene name (contains a group), preventing infinite upgrade loops on renamed library files."""
    from pathlib import Path

    from backend.db.repositories import MediaFileRepository

    mf = None
    if file_path:
        mf = MediaFileRepository(db).by_path(file_path)
    if mf is None:
        if media_type == "movie":
            mfs = MediaFileRepository(db).for_movie(media_id)
            mf = mfs[0] if mfs else None
        elif episode_id is not None:
            mfs = MediaFileRepository(db).for_episode(episode_id)
            mf = mfs[0] if mfs else None

    candidate_name: str | None = None
    size_bytes = 0
    ep_count = 1
    is_scan = False
    if mf is not None:
        candidate_name = mf.scene_name or Path(mf.path).name
        size_bytes = mf.size_bytes or 0
        is_scan = mf.source == "scan"
        if media_type == "tv":
            ep_count = max(1, len(mf.episodes)) if mf.episodes else 1

    if not candidate_name or (is_scan and not release_group(candidate_name)):
        # Check last imported/upgraded history event for a source name fallback
        hist = db.scalar(
            select(models.History)
            .where(
                models.History.media_type == media_type,
                models.History.media_id == media_id,
                models.History.event_type.in_(("imported", "upgraded")),
            )
            .order_by(models.History.id.desc())
            .limit(1)
        )
        if hist and isinstance(hist.data, dict) and hist.data.get("source"):
            candidate_name = Path(str(hist.data["source"])).name
            is_scan = False

    if not candidate_name:
        return None, None
    if is_scan and not release_group(candidate_name):
        return None, candidate_name

    facts = ReleaseFacts.from_title(candidate_name, size_bytes=size_bytes, episode_count=ep_count)
    matches = matcher.match(facts)
    fs, _ = score_formats(matches, profile.format_scores)
    return fs, candidate_name


def build_target(media_type: str, media_id: int, season: int | None = None, episodes: list[int] | None = None) -> tuple[Target, SearchCriteria, set[str]]:
    with session_scope() as db:
        matcher = get_matcher(db)
        if media_type == "movie":
            movie = db.get(models.Movie, media_id)
            if movie is None:
                raise SearchError("Movie not found")
            profile = spec_for(db, movie.quality_profile_id)
            current = parse_quality(movie.quality or movie.file_path or "") if movie.has_file else None
            if current and current.quality.name == "Unknown" and movie.quality:
                current = parse_quality(get_quality(movie.quality).name)
            current_fs, scene_name = (
                compute_file_format_score(db, "movie", movie.id, movie.file_path, profile, matcher)
                if movie.has_file
                else (None, None)
            )
            target = Target("movie", movie.id, movie.title, movie.year, profile,
                            current_quality=current, current_format_score=current_fs, current_scene_name=scene_name,
                            has_file=movie.has_file, aliases=[movie.original_title or ""],
                            minimum_seeders=_min_seeders(db), availability=movie_gate(movie), matcher=matcher)
            criteria = SearchCriteria("movie", movie.title, movie.year, imdb_id=movie.imdb_id, tmdb_id=movie.tmdb_id)
            return target, criteria, _blocklist(db, "movie", movie.id)

        series = db.get(models.Series, media_id)
        if series is None:
            raise SearchError("Series not found")
        profile = spec_for(db, series.quality_profile_id)
        stmt = select(models.Episode).where(models.Episode.series_id == series.id)
        if season is not None:
            stmt = stmt.where(models.Episode.season_number == season)
        if episodes:
            stmt = stmt.where(models.Episode.episode_number.in_(episodes))
        eps = list(db.scalars(stmt))
        with_files = [parse_quality(e.quality or e.file_path or "") for e in eps if e.has_file]
        all_have = bool(eps) and all(e.has_file for e in eps)
        worst = min(with_files, key=lambda q: q.quality.weight) if with_files and all_have else None
        current_fs = None
        scene_name = None
        if all_have:
            scores_and_names = [
                compute_file_format_score(db, "tv", series.id, e.file_path, profile, matcher, episode_id=e.id)
                for e in eps
            ]
            known_scores = [fs for fs, _ in scores_and_names if fs is not None]
            if len(known_scores) == len(eps):
                current_fs = min(known_scores)
            if len(eps) == 1:
                scene_name = scores_and_names[0][1]
        target = Target("tv", series.id, series.title, series.year, profile,
                        season=season, episodes=list(episodes or []), current_quality=worst,
                        current_format_score=current_fs, current_scene_name=scene_name, has_file=all_have,
                        aliases=[series.original_title or ""], minimum_seeders=_min_seeders(db), air=_air_map(eps, utcnow()),
                        matcher=matcher)
        criteria = SearchCriteria("tv", series.title, series.year, imdb_id=series.imdb_id, tvdb_id=series.tvdb_id,
                                  tvmaze_id=series.tvmaze_id, season=season, episode=episodes[0] if episodes and len(episodes) == 1 else None)
        return target, criteria, _blocklist(db, "tv", series.id)


async def interactive(media_type: str, media_id: int, season: int | None = None, episode: int | None = None) -> dict:
    target, criteria, blocked = await asyncio.to_thread(build_target, media_type, media_id, season, [episode] if episode else None)
    releases, report = await indexer_service.search(criteria)
    decisions = rank([evaluate(r, target, blocked) for r in releases])
    await asyncio.to_thread(_touch_search, media_type, media_id, season, [episode] if episode else None)
    hold = _hold(target)
    return {
        "releases": [{**d.to_dict(), "download_url_token": _release_cache_put(d.release)} for d in decisions],
        "indexers": report,
        # a user may still override, but automatic search won't touch it until then
        "held": f"Not released yet: {hold.reason}" if hold else None,
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
        hold = _hold(target)
        if hold:
            log.info("Not searching %s %s (season %s, episodes %s): not released yet - %s", media_type, media_id, season, episodes, hold.reason)
            return {"grabbed": None, "held": True, "reason": f"Not released yet: {hold.reason}"}
        if media_type == "tv" and season is not None and not episodes:
            # season search: prefer a pack (only once the whole season has aired), fall back to individual episodes
            pack_gate = release_gate(target, season, [], True)
            if pack_gate is None or pack_gate.available:
                releases, _ = await indexer_service.search(criteria)
                best = _best(releases, target, blocked)
                if best:
                    qid = await download_service.grab(best.release, media_type="tv", media_id=media_id, season=season,
                                                        episodes=best.episodes, actor=actor, retry_count=retry_count,
                                                        release_gate=best.release_gate, custom_formats=best.custom_formats,
                                                        format_score=best.format_score)
                    await asyncio.to_thread(_touch_search, media_type, media_id, season, episodes)
                    return {"grabbed": best.release.title, "queue_id": qid}
            missing = await asyncio.to_thread(_missing_episode_numbers, media_id, season)
            grabbed = []
            for number in missing:
                # each episode is a distinct lock key, so recursing here does not self-deadlock
                result = await automatic("tv", media_id, season, [number], actor, retry_count=retry_count)
                if result.get("grabbed"):
                    grabbed.append(result["grabbed"])
            out = {"grabbed": grabbed or None, "reason": None if grabbed else "No acceptable releases found"}
            if pack_gate is not None and not pack_gate.available:
                out["season_pack"] = f"Not searched: {pack_gate.reason}"
            return out

        releases, report = await indexer_service.search(criteria)
        best = _best(releases, target, blocked)
        await asyncio.to_thread(_touch_search, media_type, media_id, season, episodes)
        if not best:
            return {"grabbed": None, "reason": "No acceptable releases found", "indexers": report, "candidates": len(releases)}
        qid = await download_service.grab(best.release, media_type=media_type, media_id=media_id, season=season if season is not None else best.season,
                                          episodes=episodes or best.episodes, actor=actor, retry_count=retry_count,
                                          release_gate=best.release_gate, custom_formats=best.custom_formats,
                                          format_score=best.format_score)
        return {"grabbed": best.release.title, "queue_id": qid, "quality": best.quality.quality.name, "release_gate": best.release_gate}


def _hold(target: Target) -> Availability | None:
    """Why the target must not be searched for at all yet, or None.

    A movie is held until its home release; specific episodes, or a season, until at least one of them has
    aired (releases covering the rest are still rejected one by one by the decision engine).
    """
    if target.media_type == "movie":
        return target.availability if target.availability and not target.availability.available else None
    if target.season is None:
        return None
    if target.episodes:
        known = [target.air[(target.season, e)] for e in target.episodes if (target.season, e) in target.air]
    else:
        known = [a for (s, _), a in target.air.items() if s == target.season]
    if known and not any(a.available for a in known):
        return latest(known)
    return None


def _best(releases: list[ReleaseInfo], target: Target, blocked: set[str]) -> Decision | None:
    decisions = [d for d in rank([evaluate(r, target, blocked) for r in releases]) if d.approved]
    return decisions[0] if decisions else None


def _already_queued(media_type: str, media_id: int, season: int | None, episodes: list[int] | None) -> bool:
    with session_scope() as db:
        return QueueRepository(db).is_queued(media_type, media_id, season, episodes[0] if episodes and len(episodes) == 1 else None)


def _missing_episode_numbers(series_id: int, season: int) -> list[int]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(models.Episode.episode_number).where(
                    models.Episode.series_id == series_id,
                    models.Episode.season_number == season,
                    models.Episode.monitored.is_(True),
                    models.Episode.has_file.is_(False),
                    EpisodeRepository.aired(utcnow()),
                )
            )
        )


async def search_missing(limit: int = 50, progress=None) -> dict:
    """Sweep monitored, missing items that are out (domain/availability.py) – least recently searched first."""
    catalog_days = get_settings().release_gate_catalog_days

    def collect():
        with session_scope() as db:
            movies = list(
                db.scalars(
                    select(models.Movie.id)
                    .where(models.Movie.monitored.is_(True), models.Movie.has_file.is_(False),
                           MovieRepository.released(date.today(), catalog_days))
                    .order_by(models.Movie.last_search_at.is_not(None), models.Movie.last_search_at)
                    .limit(limit)
                )
            )
            eps = db.execute(
                EpisodeRepository(db).wanted_missing(utcnow())
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
            if tkey not in best_per_target or decision.sort_key > best_per_target[tkey].sort_key:
                best_per_target[tkey] = decision
    for (media_type, media_id, season), decision in best_per_target.items():
        try:
            lock = await _lock_for(media_type, media_id, season, decision.episodes)
            async with lock:
                if await asyncio.to_thread(_already_queued, media_type, media_id, season, decision.episodes):
                    continue
                await download_service.grab(decision.release, media_type=media_type, media_id=media_id, season=season, episodes=decision.episodes,
                                            release_gate=decision.release_gate, custom_formats=decision.custom_formats,
                                            format_score=decision.format_score)
                grabbed += 1
        except Exception as exc:
            log.warning("RSS grab failed for %s: %s", decision.release.title, exc)
    return {"releases": len(releases), "matched": len(matches), "grabbed": grabbed}


def _match_rss(releases: list[ReleaseInfo]) -> dict:
    """Match RSS releases to monitored library items that want a file (missing or upgradable).

    Targets carry the release gate, so the decision engine rejects anything not out yet (including a season
    pack for a season that is still airing).
    """
    now = utcnow()
    out: dict[tuple, tuple[Target, set[str], ReleaseInfo]] = {}
    with session_scope() as db:
        matcher = get_matcher(db)
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
                profile = spec_for(db, s.quality_profile_id)
                season_eps = list(db.scalars(select(models.Episode).where(models.Episode.series_id == s.id, models.Episode.season_number == ep.season)))
                air = _air_map(season_eps, now)
                eps = [e for e in season_eps if e.monitored and (ep.full_season or e.episode_number in ep.episodes)
                       and air[(e.season_number, e.episode_number)].available]
                if not eps:
                    continue
                have = [parse_quality(e.quality or e.file_path or "") for e in eps if e.has_file]
                all_have = len(have) == len(eps)
                current_fs = None
                scene_name = None
                if all_have:
                    scores_and_names = [
                        compute_file_format_score(db, "tv", s.id, e.file_path, profile, matcher, episode_id=e.id)
                        for e in eps
                    ]
                    known_scores = [fs for fs, _ in scores_and_names if fs is not None]
                    if len(known_scores) == len(eps):
                        current_fs = min(known_scores)
                    if len(eps) == 1:
                        scene_name = scores_and_names[0][1]
                target = Target("tv", s.id, s.title, s.year, profile, season=ep.season,
                                episodes=[] if ep.full_season else ep.episodes,
                                current_quality=min(have, key=lambda q: q.quality.weight) if all_have and have else None,
                                current_format_score=current_fs, current_scene_name=scene_name,
                                has_file=all_have, minimum_seeders=seeders, air=air, matcher=matcher)
                out[("tv", s.id, ep.season, tuple(ep.episodes), release.guid)] = (target, _blocklist(db, "tv", s.id), release)
                continue
            pm = parse_movie(release.title)
            for m in movies.get(normalize_title(pm.title), []):
                if pm.year and m.year and abs(pm.year - m.year) > 1:
                    continue
                profile = spec_for(db, m.quality_profile_id)
                current = parse_quality(m.quality or m.file_path or "") if m.has_file else None
                current_fs, scene_name = (
                    compute_file_format_score(db, "movie", m.id, m.file_path, profile, matcher)
                    if m.has_file
                    else (None, None)
                )
                target = Target("movie", m.id, m.title, m.year, profile, current_quality=current,
                                current_format_score=current_fs, current_scene_name=scene_name,
                                has_file=m.has_file, minimum_seeders=seeders, availability=movie_gate(m), matcher=matcher)
                out[("movie", m.id, None, (), release.guid)] = (target, _blocklist(db, "movie", m.id), release)
    return out
