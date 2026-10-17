"""Milestone 7: probing downloads before import (inspect_before_import) and the quick decode test."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.core.settings import get_settings, settings_manager
from backend.db import models
from backend.db.session import session_scope
from backend.domain.inspection import Finding
from backend.domain.types import MediaMetadata
from backend.services import import_service, inspection_service, media_service
from backend.services.audit import SYSTEM

UHD_PAYLOAD = {
    "format": {"format_name": "matroska,webm", "duration": "7200", "bit_rate": "40000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "color_transfer": "smpte2084",
         "side_data_list": [{"side_data_type": "DOVI configuration record"}], "r_frame_rate": "24000/1001"},
        {"codec_type": "audio", "codec_name": "truehd", "channels": 8, "disposition": {"default": 1}, "tags": {"language": "eng"}},
    ],
}


@pytest.fixture
def pre_import(monkeypatch):
    """Turn inspect_before_import on (restored afterwards) with a stubbed ffprobe."""
    before = get_settings()
    settings_manager.update(inspection_enabled=True, inspect_before_import=True, inspection_decode_test=False,
                            movie_format="{Title} ({Year}) [{Quality}]")
    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    calls = {"probe": [], "decode": []}

    def fake_probe(path, timeout=30):
        calls["probe"].append(Path(path).name)
        return UHD_PAYLOAD

    monkeypatch.setattr(inspection_service, "probe_sync", fake_probe)
    yield calls
    settings_manager.update(inspect_before_import=before.inspect_before_import, inspection_decode_test=before.inspection_decode_test,
                            movie_format=before.movie_format)


def _add_movie(title: str, year: int, runtime: int | None = None) -> int:
    with session_scope() as db:
        m = media_service.add_movie(db, MediaMetadata(media_type="movie", provider="test", title=title, year=year, runtime_minutes=runtime),
                                    quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM)
        return m.id


def _import(src: Path, movie_id: int):
    return import_service.manual_import([{"path": str(src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)


def test_mediainfo_tokens_are_filled_from_the_pre_import_probe(database, library, make_video, pre_import):
    settings_manager.update(movie_format="{Title} ({Year}) [{MediaInfo VideoCodec} {MediaInfo HDR} {MediaInfo AudioCodec} {MediaInfo AudioChannels}]")
    movie_id = _add_movie("Dune Part Two", 2024, runtime=166)
    src = make_video(library["downloads"] / "Dune.Part.Two.2024.2160p.UHD.BluRay" / "Dune.Part.Two.2024.2160p.UHD.BluRay.x265.mkv")
    result = _import(src, movie_id)
    assert result.imported, result.failed
    with session_scope() as db:
        name = Path(db.get(models.Movie, movie_id).file_path).name
    assert name == "Dune Part Two (2024) [x265 DV HDR10 TrueHD 7.1].mkv"
    assert pre_import["probe"] == ["Dune.Part.Two.2024.2160p.UHD.BluRay.x265.mkv"]


def test_unreadable_download_is_rejected_and_left_in_place(database, library, make_video, pre_import, monkeypatch):
    def invalid(path, timeout=30):
        raise RuntimeError("ffprobe failed: file:/x.mkv: Invalid data found when processing input")

    monkeypatch.setattr(inspection_service, "probe_sync", invalid)
    movie_id = _add_movie("Unreadable", 2020)
    src = make_video(library["downloads"] / "Unreadable.2020.1080p.WEB-DL" / "Unreadable.2020.1080p.WEB-DL.mkv")
    result = _import(src, movie_id)
    assert not result.imported and "Pre-import check: ffprobe reported invalid data" in result.failed[0]
    assert result.failed_codes[str(src)] == "corrupt_file"
    assert src.exists()
    with session_scope() as db:
        assert not db.get(models.Movie, movie_id).has_file


def test_probe_that_cannot_run_never_blocks_an_import(database, library, make_video, pre_import, monkeypatch):
    def timeout(path, timeout=30):
        raise RuntimeError("ffprobe timeout")

    monkeypatch.setattr(inspection_service, "probe_sync", timeout)
    movie_id = _add_movie("Slow Disk", 2021)
    src = make_video(library["downloads"] / "Slow.Disk.2021.1080p.WEB-DL" / "Slow.Disk.2021.1080p.WEB-DL.mkv")
    assert _import(src, movie_id).imported


def test_truncated_download_is_rejected_against_the_target_runtime(database, library, make_video, pre_import, monkeypatch):
    short = {**UHD_PAYLOAD, "format": {**UHD_PAYLOAD["format"], "duration": str(20 * 60)}}
    monkeypatch.setattr(inspection_service, "probe_sync", lambda path, timeout=30: short)
    movie_id = _add_movie("Half Film", 2022, runtime=120)
    src = make_video(library["downloads"] / "Half.Film.2022.2160p.UHD.BluRay" / "Half.Film.2022.2160p.UHD.BluRay.mkv")  # a few KB: far too small
    result = _import(src, movie_id)
    assert not result.imported and "Pre-import check: File appears truncated" in result.failed[0]
    assert src.exists()


def test_failing_decode_test_rejects_the_download(database, library, make_video, pre_import, monkeypatch):
    settings_manager.update(inspection_decode_test=True)

    def bad_decode(path, inspection, timeout=180):
        pre_import["decode"].append(Path(path).name)
        return Finding("truncated_file", "error", "Quick decode test: file ends early (79 of ~720 sampled frames decoded)",
                       {"source": "decode", "reason": "decode_short_read"})

    monkeypatch.setattr(inspection_service, "decode_check_sync", bad_decode)
    movie_id = _add_movie("Cut Short", 2023)
    src = make_video(library["downloads"] / "Cut.Short.2023.2160p.UHD.BluRay" / "Cut.Short.2023.2160p.UHD.BluRay.mkv")
    result = _import(src, movie_id)
    assert not result.imported and "file ends early" in result.failed[0]
    assert pre_import["decode"] == ["Cut.Short.2023.2160p.UHD.BluRay.mkv"] and src.exists()


def test_check_is_off_by_default(database, library, make_video, monkeypatch):
    called = []
    monkeypatch.setattr(inspection_service, "probe_sync", lambda *a, **k: called.append(1))
    settings_manager.update(inspect_before_import=False)
    movie_id = _add_movie("Default Off", 2019)
    src = make_video(library["downloads"] / "Default.Off.2019.1080p.WEB-DL" / "Default.Off.2019.1080p.WEB-DL.mkv")
    assert _import(src, movie_id).imported and called == []


# --------------------------------------------------------------------------- decode findings after import
def test_decode_finding_survives_header_only_passes_until_the_content_changes(database, library, make_video, monkeypatch):
    """A backlog pass doesn't decode, so it must not auto-resolve a decode finding for unchanged content."""
    from sqlalchemy import select

    from backend.db.base import utcnow
    from backend.services import media_file_service

    payload = {"format": {"format_name": "matroska,webm", "duration": "600"},
               "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "24/1"},
                           {"codec_type": "audio", "codec_name": "aac", "channels": 2}]}

    async def fake_probe(path, timeout=30):
        return payload

    async def bad_decode(path, inspection, timeout=180):
        return Finding("invalid_media", "error", "Quick decode test: 12 decode error(s)", {"source": "decode", "reason": "decode_errors"})

    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(inspection_service, "probe", fake_probe)
    monkeypatch.setattr(inspection_service, "decode_check", bad_decode)
    settings_manager.update(inspection_enabled=True, inspection_decode_test=True)

    movie_id = _add_movie("Glitchy", 2018)
    video = make_video(library["movies"] / "Glitchy (2018)" / "Glitchy.2018.1080p.mkv")
    with session_scope() as db:
        mf_id = media_file_service.attach_movie_file(db, db.get(models.Movie, movie_id), video, None).id

    def open_issues():
        with session_scope() as db:
            return [i.issue_type for i in db.scalars(select(models.LibraryIssue).where(
                models.LibraryIssue.media_id == movie_id, models.LibraryIssue.media_type == "movie", models.LibraryIssue.status == "open"))]

    try:
        assert asyncio.run(inspection_service.inspect_file(mf_id, force=True)).status == "inspected"
        assert open_issues() == ["invalid_media"]

        # header-only pass on unchanged content (backlog / decode disabled): finding carried forward
        assert asyncio.run(inspection_service.inspect_file(mf_id, force=True, decode=False)).status == "inspected"
        assert open_issues() == ["invalid_media"]

        # the file is replaced: the old content's decode verdict no longer applies
        video.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x07" * 9000)
        with session_scope() as db:
            media_file_service.attach_movie_file(db, db.get(models.Movie, movie_id), video, None)
        assert asyncio.run(inspection_service.inspect_file(mf_id, force=True, decode=False)).status == "inspected"
        assert open_issues() == []
    finally:
        settings_manager.update(inspection_decode_test=False)
        with session_scope() as db:
            db.get(models.MediaFile, mf_id).inspected_at = utcnow()


# --------------------------------------------------------------------------- real binary (opt-in)
@pytest.mark.ffprobe
def test_real_decode_test_catches_a_truncated_mkv(tmp_path):
    """A truncated MKV keeps its full duration in the header; only decoding the end window notices."""
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        pytest.skip("ffmpeg/ffprobe not installed on host")
    good = tmp_path / "good.mkv"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=120:size=640x360:rate=24",
                    "-c:v", "libx264", "-preset", "ultrafast", str(good)], check=True)
    truncated = tmp_path / "truncated.mkv"
    truncated.write_bytes(good.read_bytes()[: int(good.stat().st_size * 0.6)])

    for path, expect_ok in ((good, True), (truncated, False)):
        inspection = inspection_service.parse_ffprobe(inspection_service.probe_sync(path))
        assert inspection.duration_seconds == pytest.approx(120, abs=1)  # header says full length either way
        finding = inspection_service.decode_check_sync(path, inspection)
        assert (finding is None) is expect_ok, finding
    assert finding.kind == "truncated_file" and finding.data["reason"] == "decode_short_read"
