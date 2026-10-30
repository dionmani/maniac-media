"""Two library shows with one title: Dark Matter (2015) and Dark Matter (2024) keep their own releases and files."""
from __future__ import annotations

import os

import pytest

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import ReleaseInfo
from backend.services import import_service, scanner_service, search_service


@pytest.fixture(scope="module")
def twins(database) -> dict[int, int]:
    """{year: series id} for two monitored "Dark Matter" shows, each with an aired S02E09 that has no file."""
    ids = {}
    with session_scope() as db:
        for year in (2015, 2024):
            series = models.Series(title="Dark Matter", year=year, monitored=True, quality_profile_id=1,
                                   status="continuing", series_type="standard")
            db.add(series)
            db.flush()
            season = models.Season(series_id=series.id, season_number=2, monitored=True)
            db.add(season)
            db.flush()
            db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=2, episode_number=9,
                                  title="Ep 9", air_date="2025-01-01", monitored=True))
            ids[year] = series.id
    return ids


def _release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, "u", "idx", "usenet", size_bytes=2 * 1024 ** 3)


@pytest.mark.parametrize(("title", "year"), [
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", 2024),
    ("Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", 2015),
])
def test_rss_routes_each_release_to_its_own_show(twins, title, year):
    matches = search_service._match_rss([_release(title)])
    assert {key[1] for key in matches} == {twins[year]}
    (target, _, release), = matches.values()
    assert search_service.evaluate(release, target).approved, search_service.evaluate(release, target).rejections


def test_search_target_flags_the_newer_twin(twins):
    new, _, _ = search_service.build_target("tv", twins[2024], 2, [9])
    old, _, _ = search_service.build_target("tv", twins[2015], 2, [9])
    assert new.title_has_older_twin and not old.title_has_older_twin
    assert not search_service.evaluate(_release("Dark.Matter.2024.S02E09.1080p.WEB-DL-playWEB"), old).approved
    assert not search_service.evaluate(_release("Dark.Matter.S02E09.1080p.WEB-DL-GRP"), new).approved


@pytest.mark.parametrize(("name", "year"), [
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB.mkv", 2024),
    ("Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP.mkv", 2015),
])
def test_manual_import_matches_the_show_by_year(twins, library, make_video, name, year):
    src = make_video(library["downloads"] / "twins-manual" / name)
    with session_scope() as db:
        candidate = import_service.match(db, import_service.inspect(src, 0))
    assert (candidate.media_type, candidate.media_id) == ("tv", twins[year])


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
