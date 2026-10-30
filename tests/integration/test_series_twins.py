"""Two library shows with one title: Dark Matter (2015) and Dark Matter (2024) keep their own releases and files."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import ReleaseInfo
from backend.services import import_service, scanner_service, search_service


@pytest.fixture(scope="module")
def twins(database) -> dict[int, int]:
    """{year: series id} for the Syfy (2015) and Apple TV+ (2024) "Dark Matter", each wanting S02E09 and S02E10."""
    ids = {}
    with session_scope() as db:
        for year, network, aired in ((2015, "Syfy", ("2016-08-26", "2016-09-02")), (2024, "Apple TV+", ("2026-10-15", "2026-10-22"))):
            series = models.Series(title="Dark Matter", year=year, network=network, monitored=True, quality_profile_id=1,
                                   status="continuing", series_type="standard")
            db.add(series)
            db.flush()
            season = models.Season(series_id=series.id, season_number=2, monitored=True)
            db.add(season)
            db.flush()
            for n, day in zip((9, 10), aired):
                db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=2, episode_number=n,
                                      title=f"Ep {n}", air_date=day, monitored=True))
            ids[year] = series.id
    return ids


def _release(title: str, posted: str | None = None, **ids) -> ReleaseInfo:
    when = datetime.fromisoformat(posted).replace(tzinfo=timezone.utc) if posted else None
    return ReleaseInfo(title, title, "u", "idx", "usenet", size_bytes=2 * 1024 ** 3, publish_date=when, **ids)


@pytest.mark.parametrize(("title", "posted", "year"), [
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", None, 2024),
    ("Dark.Matter.S02E10.1080p.ATVP.WEB-DL.DUAL.DDP5.1.Atmos.H.264-TURG", "2026-10-23", 2024),  # aired last week, Apple TV+
    ("Dark.Matter.S02E09.720p.HDTV.x264-KILLERS", "2016-08-27", 2015),                          # aired that week, Syfy
])
def test_rss_routes_each_release_to_its_own_show(twins, title, posted, year):
    matches = search_service._match_rss([_release(title, posted)])
    assert {key[1] for key in matches} == {twins[year]}
    (target, _, release), = matches.values()
    decision = search_service.evaluate(release, target)
    assert decision.approved, decision.rejections
    assert decision.series_match.level in {"certain", "likely"}


def test_rss_skips_a_release_nothing_tells_apart(twins):
    assert not search_service._match_rss([_release("Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP")])


def test_search_targets_know_their_twin(twins):
    new, _, _ = search_service.build_target("tv", twins[2024], 2, [9])
    old, _, _ = search_service.build_target("tv", twins[2015], 2, [9])
    assert [t.id for t in new.twins] == [twins[2015]] and new.facts.network == "Apple TV+"
    assert (2, 9) in old.facts.aired and (2, 9) in old.twins[0].aired
    assert not search_service.evaluate(_release("Dark.Matter.2024.S02E09.1080p.WEB-DL-playWEB"), old).approved
    atvp = _release("Dark.Matter.S02E10.1080p.ATVP.WEB-DL-TURG", "2026-10-23")
    old10, _, _ = search_service.build_target("tv", twins[2015], 2, [10])
    new10, _, _ = search_service.build_target("tv", twins[2024], 2, [10])
    assert not search_service.evaluate(atvp, old10).approved
    assert search_service.evaluate(atvp, new10).approved


@pytest.mark.parametrize(("name", "mtime", "year"), [
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB.mkv", None, 2024),
    ("Dark.Matter.S02E09.720p.HDTV.x264-KILLERS.mkv", "2016-08-27", 2015),  # downloaded the week the original aired
    ("Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP.mkv", "2021-01-01", None),  # nothing tells: pick by hand
])
def test_manual_import_matches_the_show_by_evidence(twins, library, make_video, name, mtime, year):
    src = make_video(library["downloads"] / "twins-manual" / name)
    if mtime:
        stamp = datetime.fromisoformat(mtime).timestamp()
        os.utime(src, (stamp, stamp))
    with session_scope() as db:
        candidate = import_service.match(db, import_service.inspect(src, 0))
    assert candidate.media_id == (twins[year] if year else None)


def test_import_refuses_a_release_for_the_other_show(twins, library, make_video):
    release = library["downloads"] / "Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB"
    src = make_video(release / f"{release.name}.mkv")
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Dark Matter", media_type="tv", media_id=twins[2015], release_title=release.name,
                                     season_num=2, episode_numbers=[9], status="completed", download_path=str(release),
                                     protocol="usenet")
        db.add(q)
        db.flush()
        queue_id = q.id
    outcome = import_service.import_queue_item(queue_id)
    assert not outcome.imported
    assert src.exists()
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert (q.status, q.failure_reason) == ("import_failed", "not_matched")
        assert "2024" in q.failure_analysis["summary"]
        assert not db.get(models.Series, twins[2015]).episodes[0].has_file


def test_scanner_keeps_same_titled_folders_apart(twins, library):
    root = library["tv"]
    for name in ("Dark Matter (2015)", "Dark Matter (2024)"):
        os.makedirs(root / name / "Season 02", exist_ok=True)
    scanner_service.scan_series(incremental=False)
    with session_scope() as db:
        paths = {year: db.get(models.Series, sid).path for year, sid in twins.items()}
    assert os.path.basename(paths[2015]) == "Dark Matter (2015)"
    assert os.path.basename(paths[2024]) == "Dark Matter (2024)"


def test_manual_import_never_falls_back_to_a_movie_from_another_year(database, library, make_video):
    with session_scope() as db:
        old = models.Movie(title="Twin Remake", year=1984, monitored=True, quality_profile_id=1)
        db.add(old)
        db.flush()
        old_id = old.id
    src = make_video(library["downloads"] / "twins-movie" / "Twin.Remake.2021.1080p.WEB-DL.DD5.1.H.264-GRP.mkv")
    with session_scope() as db:
        candidate = import_service.match(db, import_service.inspect(src, 0))
    assert candidate.media_id != old_id
    src = make_video(library["downloads"] / "twins-movie" / "Twin.Remake.1984.1080p.BluRay.x264-GRP.mkv")
    with session_scope() as db:
        assert import_service.match(db, import_service.inspect(src, 0)).media_id == old_id


@pytest.fixture(scope="module")
def ghosts(database) -> dict[str, int]:
    """{country: series id}: the British Ghosts (2019) and the American one (2021), both wanting S05E05."""
    ids = {}
    with session_scope() as db:
        for year, country, aired in ((2019, "GB", "2023-10-06"), (2021, "US", "2025-11-13")):
            series = models.Series(title="Ghosts", year=year, country=country, monitored=True, quality_profile_id=1,
                                   status="continuing", series_type="standard")
            db.add(series)
            db.flush()
            season = models.Season(series_id=series.id, season_number=5, monitored=True)
            db.add(season)
            db.flush()
            db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=5, episode_number=5,
                                  title="Ep 5", air_date=aired, monitored=True))
            ids[country] = series.id
    return ids


@pytest.mark.parametrize(("title", "posted", "country"), [
    ("Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", None, "US"),
    ("Ghosts.2021.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", None, "US"),
    ("Ghosts.UK.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", None, "GB"),
    ("Ghosts.S05E05.1080p.iP.WEB-DL.AAC2.0.H.264-GRP", "2023-10-07", "GB"),  # aired that week on the BBC
])
def test_rss_routes_country_tagged_releases(ghosts, title, posted, country):
    matches = search_service._match_rss([_release(title, posted)])
    assert {key[1] for key in matches} == {ghosts[country]}
    (target, _, release), = matches.values()
    assert search_service.evaluate(release, target).approved, search_service.evaluate(release, target).rejections


def test_import_refuses_the_other_countrys_show(ghosts, library, make_video):
    release = library["downloads"] / "Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP"
    src = make_video(release / f"{release.name}.mkv")
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Ghosts", media_type="tv", media_id=ghosts["GB"], release_title=release.name,
                                     season_num=5, episode_numbers=[5], status="completed", download_path=str(release),
                                     protocol="usenet")
        db.add(q)
        db.flush()
        queue_id = q.id
    assert not import_service.import_queue_item(queue_id).imported
    assert src.exists()
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert (q.status, q.failure_reason) == ("import_failed", "not_matched")
        assert "tagged US, this show is GB" in q.failure_analysis["summary"]


def test_metadata_fills_in_the_country(database):
    from backend.domain.types import MediaMetadata
    from backend.services import metadata_service

    with session_scope() as db:
        series = models.Series(title="Country Fill", year=2005, monitored=True, quality_profile_id=1)
        db.add(series)
        db.flush()
        metadata_service.apply_series(db, series, MediaMetadata(media_type="tv", provider="tvdb", title="Country Fill", country="usa"),
                                      create_episodes=False)
        assert series.country == "US"
        metadata_service.apply_series(db, series, MediaMetadata(media_type="tv", provider="tvdb", title="Country Fill"),
                                      create_episodes=False)
        assert series.country == "US"  # a provider that doesn't say keeps what we had


def test_import_refuses_an_untagged_release_the_evidence_puts_in_the_other_show(twins, library, make_video):
    """Grabbed for the 2015 show before scoring existed: an Apple TV+ episode that aired the week before."""
    release = library["downloads"] / "Dark.Matter.S02E10.1080p.ATVP.WEB-DL.DUAL.DDP5.1.Atmos.H.264-TURG"
    src = make_video(release / f"{release.name}.mkv")
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Dark Matter", media_type="tv", media_id=twins[2015], release_title=release.name,
                                     season_num=2, episode_numbers=[10], status="completed", download_path=str(release),
                                     protocol="usenet", added_at=datetime(2026, 10, 23, 9, 0))
        db.add(q)
        db.flush()
        queue_id = q.id
    assert not import_service.import_queue_item(queue_id).imported
    assert src.exists()
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert (q.status, q.failure_reason) == ("import_failed", "not_matched")
        assert "Dark Matter (2024)" in q.error_message
