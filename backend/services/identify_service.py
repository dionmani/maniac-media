"""Manual-import "smart match": turn scanned file paths into ranked library / TMDb candidates.

For every path the file name is normalised (``domain.parser.guess_title`` strips resolution, source, codec,
release year, ...), then the library is searched first and the metadata providers second. Movies are tried before
series unless the name carries an episode marker (``S01E02``) or lives in a ``Season 01`` folder, in which case
series go first. Nothing is written here – the caller decides which candidate to accept.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from sqlalchemy import select

from backend.db import models
from backend.db.repositories import MovieRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain.parser import TitleGuess, guess_title, normalize_title
from backend.domain.types import MediaMetadata
from backend.services import metadata_service

log = logging.getLogger("maniac.identify")

MAX_CANDIDATES = 5
_CONCURRENCY = 4


def _score(title: str, year: int | None, want_title: str, want_year: int | None) -> int:
    score = 0
    if normalize_title(title) == normalize_title(want_title):
        score += 100
    elif normalize_title(want_title) in normalize_title(title):
        score += 30
    if want_year and year:
        score += 40 if year == want_year else 15 if abs(year - want_year) <= 1 else 0
    return score


def _candidate(source: str, media_type: str, title: str, year: int | None, *, media_id: int | None = None,
               tmdb_id: int | None = None, poster_url: str | None = None, overview: str | None = None,
               score: int = 0) -> dict:
    return {"source": source, "media_type": media_type, "media_id": media_id, "tmdb_id": tmdb_id, "title": title,
            "year": year, "poster_url": poster_url, "overview": (overview or "")[:300] or None, "score": score}


def _library_search(guess: TitleGuess, media_type: str) -> list[dict]:
    with session_scope() as db:
        if media_type == "movie":
            rows = db.scalars(MovieRepository(db).query(search=guess.query).limit(25)).all()
        else:
            rows = db.scalars(SeriesRepository(db).query(search=guess.query).limit(25)).all()
        found = [_candidate("library", media_type, r.title, r.year, media_id=r.id, tmdb_id=r.tmdb_id,
                            score=_score(r.title, r.year, guess.query, guess.year))
                 for r in rows]
    return sorted(found, key=lambda c: -c["score"])[:MAX_CANDIDATES]


def _library_ids_by_tmdb(media_type: str, tmdb_ids: list[int]) -> dict[int, int]:
    if not tmdb_ids:
        return {}
    model = models.Movie if media_type == "movie" else models.Series
    with session_scope() as db:
        return {tid: mid for tid, mid in db.execute(select(model.tmdb_id, model.id).where(model.tmdb_id.in_(tmdb_ids)))}


async def _provider_search(guess: TitleGuess, media_type: str) -> list[dict]:
    """TMDb (or whichever provider is configured): with the year first, then without it."""
    results: list[MediaMetadata] = []
    if media_type == "movie" and guess.year:
        results = await metadata_service.search(guess.query, "movie", guess.year)
    if not results:
        results = await metadata_service.search(guess.query, media_type, None)
    ranked = sorted(results, key=lambda m: -_score(m.title, m.year, guess.query, guess.year))[:MAX_CANDIDATES]
    in_library = await asyncio.to_thread(_library_ids_by_tmdb, media_type, [m.tmdb_id for m in ranked if m.tmdb_id])
    return [
        _candidate("library" if m.tmdb_id in in_library else "tmdb", media_type, m.title, m.year,
                   media_id=in_library.get(m.tmdb_id), tmdb_id=m.tmdb_id, poster_url=m.poster_url, overview=m.overview,
                   score=_score(m.title, m.year, guess.query, guess.year))
        for m in ranked
    ]


async def _identify_one(path: str, sem: asyncio.Semaphore) -> dict:
    guess = guess_title(path)
    out = {**asdict(guess), "path": path, "candidates": []}
    if not guess.query:
        return out
    order = ["tv", "movie"] if guess.kind == "tv" else ["movie", "tv"]
    try:
        for media_type in order:           # library first, for both kinds, so a known title never costs an API call
            if found := await asyncio.to_thread(_library_search, guess, media_type):
                if found[0]["score"] >= 100:   # exact title match – no need to look further
                    out["candidates"] = found
                    return out
        async with sem:
            for media_type in order:
                if found := await _provider_search(guess, media_type):
                    out["candidates"] = found
                    return out
        # nothing remote; fall back to whatever partial library hits there were
        for media_type in order:
            if found := await asyncio.to_thread(_library_search, guess, media_type):
                out["candidates"] = found
                return out
    except Exception as exc:  # one bad file must not fail the whole batch
        log.warning("Identify failed for %s: %s", path, exc)
        out["error"] = str(exc)
    return out


async def identify(paths: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    return await asyncio.gather(*(_identify_one(p, sem) for p in paths))
