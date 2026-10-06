"""Release gate against a real database: the SQL predicates agree with the domain rules, and automatic
search never contacts an indexer for something that isn't out yet."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, MovieRepository
from backend.db.session import session_scope
from backend.domain.availability import episode_availability, movie_availability
from backend.domain.types import EpisodeMetadata, MediaMetadata, ReleaseInfo
from backend.services import download_service, indexer_service, media_service, metadata_service, search_service
from backend.services.audit import SYSTEM

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 18, 0)

MOVIE_CASES = [  # digital, physical, release_date, year
    ("2026-09-01", None, "2026-06-01", 2026),
    (None, "2026-10-06", None, 2026),
    ("2026-11-20", None, "2026-09-01", 2026),
    (None, None, "2026-09-12", 2026),
    (None, None, "1999-03-31", 1999),
    (None, None, None, 2014),
    (None, None, None, 2025),
    (None, None, None, 0),
    ("", "", "", 2026),
    ("2027-02-01", None, "1985-07-03", 1985),
]

EPISODE_CASES = [  # air_date, air_date_utc
    ("2026-10-06", datetime(2026, 10, 6, 17, 59)),
    ("2026-10-06", datetime(2026, 10, 6, 18, 1)),
    ("2026-10-05", None),
    ("2026-10-04", None),
    ("2026-10-06", None),
    (None, None),
    ("", None),
    ("2026-10-07", datetime(2026, 10, 7, 1, 0)),
]


@pytest.mark.parametrize("catalog_days", [365, 0])
def test_movie_sql_matches_domain(database, catalog_days):
    with session_scope() as db:
        ids = []
        for i, (digital, physical, released, year) in enumerate(MOVIE_CASES):
            m = models.Movie(title=f"Gate Movie {catalog_days}-{i}", year=year, digital_release_date=digital,
                             physical_release_date=physical, release_date=released, has_file=False)
            db.add(m)
            db.flush()
            ids.append(m.id)
        sql = set(db.scalars(select(models.Movie.id).where(models.Movie.id.in_(ids), MovieRepository.released(TODAY, catalog_days))))
        domain = {m.id for m in db.scalars(select(models.Movie).where(models.Movie.id.in_(ids)))
                  if movie_availability(digital=m.digital_release_date, physical=m.physical_release_date, release_date=m.release_date,
                                        year=m.year, today=TODAY, catalog_days=catalog_days).available}
        assert sql == domain
        db.rollback()


def test_episode_sql_matches_domain(database):
    with session_scope() as db:
        series = models.Series(title="Gate Parity Show", year=2026)
        db.add(series)
        db.flush()
        season = models.Season(series_id=series.id, season_number=1)
        db.add(season)
        db.flush()
        for n, (air_date, stamp) in enumerate(EPISODE_CASES, start=1):
            db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=1, episode_number=n, title=f"E{n}",
                                  air_date=air_date, air_date_utc=stamp))
        db.flush()
        rows = list(db.scalars(select(models.Episode).where(models.Episode.series_id == series.id)))
        sql = set(db.scalars(select(models.Episode.id).where(models.Episode.series_id == series.id, EpisodeRepository.aired(NOW))))
        domain = {e.id for e in rows if episode_availability(air_date=e.air_date, air_date_utc=e.air_date_utc, now=NOW).available}
        assert sql == domain == {rows[0].id, rows[2].id, rows[3].id}
        db.rollback()


# ------------------------------------------------------------------ automatic search
@pytest.fixture
def no_indexers(monkeypatch):
    """Fails the test if anything is searched; ``allow`` opts specific searches back in."""
    calls, allowed = [], {}

    async def search(criteria):
        calls.append(criteria)
        key = (criteria.season, criteria.episode)
        assert key in allowed, f"searched although held: {criteria}"
        return allowed[key], []

    grabs = []

    async def grab(release, **kwargs):
        grabs.append((release.title, kwargs))
        return 1

    monkeypatch.setattr(indexer_service, "search", search)
    monkeypatch.setattr(download_service, "grab", grab)
    return calls, allowed, grabs


def _release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, "u", "Idx", "usenet", size_bytes=2 * 1024**3)


def _movie(title: str, **dates) -> int:
    with session_scope() as db:
        meta = MediaMetadata(media_type="movie", provider="test", title=title, year=2026, **dates)
        return media_service.add_movie(db, meta, quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM).id


def test_unreleased_movie_is_held_without_searching(database, no_indexers):
    calls, _, grabs = no_indexers
    movie_id = _movie("Gate Future Film", release_date="2026-09-25", digital_release_date="2099-01-01")
    result = asyncio.run(search_service.automatic("movie", movie_id))
    assert result == {"grabbed": None, "held": True, "reason": "Not released yet: Digital release is 2099-01-01"}
    assert calls == [] and grabs == []


def test_released_movie_grab_records_the_reference_date(database, no_indexers):
    _, allowed, grabs = no_indexers
    allowed[(None, None)] = [_release("Gate Streaming Film 2026 1080p WEB-DL x264-GRP")]
    movie_id = _movie("Gate Streaming Film", release_date="2026-07-01", digital_release_date="2026-09-01")
    result = asyncio.run(search_service.automatic("movie", movie_id))
    assert result["grabbed"] and result["release_gate"] == "Digital release 2026-09-01"
    assert grabs[0][1]["release_gate"] == "Digital release 2026-09-01"


def test_missing_sweep_skips_unreleased_movies(database, no_indexers):
    calls, _, _ = no_indexers
    _movie("Gate Sweep Film", release_date=(date.today() - timedelta(days=10)).isoformat())
    asyncio.run(search_service.search_missing(limit=1000))
    assert all(c.title != "Gate Sweep Film" for c in calls)


def test_episodes_wait_for_air_time_and_packs_for_the_whole_season(database, no_indexers):
    calls, allowed, grabs = no_indexers
    now = utcnow()
    episodes = [
        EpisodeMetadata(1, 1, "Pilot", air_date=(now - timedelta(days=7)).date().isoformat()),
        EpisodeMetadata(1, 2, "Next", air_date=(now + timedelta(days=1)).date().isoformat(), air_date_utc=now + timedelta(days=1)),
    ]
    with session_scope() as db:
        series = media_service.add_series(db, MediaMetadata(media_type="tv", provider="test", title="Gate Show", year=2026, episodes=episodes),
                                          quality_profile_id=1, root_folder=None, monitored=True, monitor="all", actor=SYSTEM)
        series_id = series.id

    held = asyncio.run(search_service.automatic("tv", series_id, 1, [2]))
    assert held["held"] and held["reason"].startswith("Not released yet: Airs ") and held["reason"].endswith(" UTC")
    assert calls == []

    # season search: no pack search while E02 is unaired, only the aired episode is searched
    allowed[(1, 1)] = [_release("Gate.Show.S01E01.1080p.WEB-DL.x264-GRP")]
    result = asyncio.run(search_service.automatic("tv", series_id, 1))
    assert [(c.season, c.episode) for c in calls] == [(1, 1)]
    assert result["grabbed"] == ["Gate.Show.S01E01.1080p.WEB-DL.x264-GRP"]
    assert result["season_pack"].startswith("Not searched: season 1 hasn't finished airing")
    assert grabs[0][1]["release_gate"] == f"Aired {episodes[0].air_date}"


def test_reschedule_without_a_time_drops_the_stale_air_time(database):
    with session_scope() as db:
        series = models.Series(title="Gate Reschedule Show", year=2026)
        db.add(series)
        db.flush()
        stamp = datetime(2026, 10, 7, 1, 0)
        metadata_service.merge_episodes(db, series, MediaMetadata(media_type="tv", provider="tvmaze", title="x", episodes=[
            EpisodeMetadata(1, 1, "E1", air_date="2026-10-06", air_date_utc=stamp)]))
        db.flush()
        ep = db.scalar(select(models.Episode).where(models.Episode.series_id == series.id))
        assert ep.air_date_utc == stamp
        metadata_service.merge_episodes(db, series, MediaMetadata(media_type="tv", provider="tmdb", title="x", episodes=[
            EpisodeMetadata(1, 1, "E1", air_date="2026-10-06")]))
        assert ep.air_date_utc == stamp  # same date from a date-only provider keeps the known time
        metadata_service.merge_episodes(db, series, MediaMetadata(media_type="tv", provider="tmdb", title="x", episodes=[
            EpisodeMetadata(1, 1, "E1", air_date="2026-10-13")]))
        assert ep.air_date_utc is None and ep.air_date == "2026-10-13"
        db.rollback()


def test_fill_air_times_borrows_matching_times_only(monkeypatch):
    stamp = datetime(2026, 10, 7, 1, 0)
    soon = (date.today() + timedelta(days=1)).isoformat()
    best = MediaMetadata(media_type="tv", provider="tmdb", title="x", tvdb_id=5, episodes=[
        EpisodeMetadata(1, 1, "a", air_date=soon), EpisodeMetadata(1, 2, "b", air_date=soon)])
    tvmaze = MediaMetadata(media_type="tv", provider="tvmaze", title="x", tvmaze_id=9, episodes=[
        EpisodeMetadata(1, 1, "a", air_date=soon, air_date_utc=stamp),
        EpisodeMetadata(1, 2, "b", air_date="2026-12-25", air_date_utc=stamp)])  # disagrees on the date: not taken

    class FakeTvMaze:
        key, name, provides_air_times = "tvmaze", "TVmaze", True

        async def get_series(self, **ids):
            assert ids["tvdb_id"] == 5
            return tvmaze

    monkeypatch.setattr(metadata_service, "providers", lambda media_type: [FakeTvMaze()])
    asyncio.run(metadata_service._fill_air_times(best, [best]))
    assert [e.air_date_utc for e in best.episodes] == [stamp, None]
    assert best.tvmaze_id == 9


def test_radarr_facade_reports_the_gate_to_seerr(database):
    from backend.api.compat.radarr import movie_resource

    future = _movie("Gate Seerr Future", release_date="2026-09-25", digital_release_date="2099-01-01")
    out = _movie("Gate Seerr Out", release_date="2026-06-01", digital_release_date="2026-08-01")
    with session_scope() as db:
        assert movie_resource(db.get(models.Movie, future))["isAvailable"] is False
        assert movie_resource(db.get(models.Movie, out))["isAvailable"] is True
