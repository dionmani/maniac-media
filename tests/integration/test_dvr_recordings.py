"""Plex DVR recordings: date-named episodes find their episode by air date, and a name with no quality in it is
labelled from the resolution ffprobe measures ("Show - 2026-10-29 - Title.ts" -> HDTV-1080p)."""
from __future__ import annotations

import asyncio

import pytest

from backend.db import models
from backend.db.repositories import MediaFileRepository
from backend.db.session import session_scope
from backend.services import inspection_service, scanner_service
from backend.workers import tasks

TS_1080 = {
    "format": {"format_name": "mpegts", "duration": "1800", "bit_rate": "9000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "mpeg2video", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
    ],
}


class _Ctx:
    def __init__(self, payload):
        self.payload = payload

    def progress(self, percent, message=None):
        pass


@pytest.fixture
def dvr_show(database, library, make_video, monkeypatch):
    """A daily show with three aired episodes, two of them on the same day."""
    folder = library["tv"] / "DVR Daily (2020)"
    with session_scope() as db:
        series = models.Series(title="DVR Daily", year=2020, monitored=True, quality_profile_id=1, status="continuing",
                               series_type="daily", root_folder=str(library["tv"]), path=str(folder))
        db.add(series)
        db.flush()
        season = models.Season(series_id=series.id, season_number=2026, monitored=True)
        db.add(season)
        db.flush()
        for n, day in ((201, "2026-10-28"), (202, "2026-10-29"), (203, "2026-10-30"), (204, "2026-10-30")):
            db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=2026, episode_number=n,
                                  title=f"Show {n}", air_date=day, monitored=True))
        series_id = series.id
    queued: list[int] = []
    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(scanner_service, "_queue_classification", queued.extend)
    yield series_id, folder, queued
    with session_scope() as db:
        db.delete(db.get(models.Series, series_id))
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()


def _episode_files(series_id: int) -> dict[int, str]:
    with session_scope() as db:
        eps = db.query(models.Episode).filter(models.Episode.series_id == series_id).all()
        return {e.episode_number: e.files[0].path for e in eps if e.files}


def test_dated_recordings_match_their_episode_by_air_date(dvr_show, make_video):
    series_id, folder, _ = dvr_show
    dated = make_video(folder / "DVR Daily (2020) - 2026-10-29 - Show 202.ts")
    make_video(folder / "DVR Daily (2020) - 2026-10-30 - Who Knows.ts")  # two episodes aired that day: no guess

    scanner_service.scan_series(incremental=False, media_id=series_id)
    assert _episode_files(series_id) == {202: str(dated)}


def test_an_unmatched_dated_file_is_retried_on_the_next_scan(dvr_show, make_video):
    series_id, folder, _ = dvr_show
    late = make_video(folder / "DVR Daily (2020) - 2026-11-02 - Not Listed Yet.ts")
    scanner_service.scan_series(incremental=False, media_id=series_id)
    assert _episode_files(series_id) == {}

    with session_scope() as db:  # the metadata refresh catches up
        season = db.query(models.Season).filter_by(series_id=series_id, season_number=2026).one()
        db.add(models.Episode(series_id=series_id, season_id=season.id, season_number=2026, episode_number=205,
                              title="Show 205", air_date="2026-11-02", monitored=True))
    scanner_service.scan_series(incremental=True)  # the folder wasn't marked done, so it isn't skipped
    assert _episode_files(series_id) == {205: str(late)}


def test_a_recording_without_quality_in_its_name_is_labelled_from_its_resolution(dvr_show, make_video, monkeypatch):
    series_id, folder, queued = dvr_show
    bare = make_video(folder / "DVR Daily (2020) - 2026-10-28 - Show 201.ts")
    named = make_video(folder / "DVR.Daily.2026.10.29.1080p.WEB-DL.AAC2.0.H.264-GRP.mkv")
    scanner_service.scan_series(incremental=False, media_id=series_id)

    with session_scope() as db:
        files = MediaFileRepository(db)
        bare_id, named_id = files.by_path(bare).id, files.by_path(named).id
    assert queued == [bare_id]  # the WEB-DL already says what it is

    async def probe(path, timeout=30):
        return TS_1080

    monkeypatch.setattr(inspection_service, "probe", probe)
    asyncio.run(tasks.inspect_file(_Ctx({"media_file_id": bare_id})))
    with session_scope() as db:
        mf = db.get(models.MediaFile, bare_id)
        assert (mf.quality, mf.quality_locked, mf.video_resolution) == ("HDTV-1080p", True, "1080p")
        ep = db.query(models.Episode).filter_by(series_id=series_id, episode_number=201).one()
        assert ep.quality == "HDTV-1080p"
        assert db.get(models.MediaFile, named_id).quality == "WEBDL-1080p"

    scanner_service.scan_series(incremental=False, media_id=series_id)  # a rescan keeps the measured label
    with session_scope() as db:
        assert db.get(models.MediaFile, bare_id).quality == "HDTV-1080p"
