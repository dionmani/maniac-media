"""The inspection jobs exactly as the worker runs them: real handlers, real DB, stubbed ffprobe."""
from __future__ import annotations

import asyncio

import pytest

PAYLOAD = {
    "format": {"format_name": "matroska,webm", "duration": "5400", "bit_rate": "8000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
    ],
}


class FakeCtx:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.reports: list[tuple[float, str | None]] = []

    def progress(self, percent, message=None):
        self.reports.append((percent, message))


@pytest.fixture
def stub_ffprobe(monkeypatch):
    from backend.services import inspection_service

    probed = []

    async def fake_probe(path, timeout=30):
        probed.append(str(path))
        return PAYLOAD

    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(inspection_service, "probe", fake_probe)
    return probed


@pytest.fixture
def movie_files(database, library, make_video):
    """One movie with a real file on disk and one whose file has gone missing."""
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import media_file_service

    folder = library["movies"] / "Inspection Probe (2001)"
    real = make_video(folder / "Inspection.Probe.2001.1080p.BluRay.x264-GRP.mkv")
    with session_scope() as db:
        movie = models.Movie(title="Inspection Probe", year=2001, root_folder=str(library["movies"]), path=str(folder))
        db.add(movie)
        db.flush()
        ok = media_file_service.attach_movie_file(db, movie, real, None)
        gone = media_file_service.attach_movie_file(db, movie, folder / "gone.mkv", None, make_primary=False)
        ids = {"movie": movie.id, "ok": ok.id, "gone": gone.id}
    yield ids
    with session_scope() as db:
        db.delete(db.get(models.Movie, ids["movie"]))
    real.unlink(missing_ok=True)
    folder.rmdir()


def test_backlog_job_inspects_files_and_does_not_loop_on_failures(stub_ffprobe, movie_files, monkeypatch):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.workers import tasks
    from backend.workers.queue import job_queue

    requeued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda name, *a, **kw: requeued.append(name))

    ctx = FakeCtx()
    stats = asyncio.run(tasks.media_inspection(ctx))
    assert stats["status"] == "complete" and requeued == []
    assert ctx.reports and all(0 < p <= 100 and isinstance(m, str) for p, m in ctx.reports)
    with session_scope() as db:
        ok, gone = db.get(models.MediaFile, movie_files["ok"]), db.get(models.MediaFile, movie_files["gone"])
        assert ok.inspected_at is not None and ok.video_resolution == "1080p" and ok.audio_codec == "ac3" and ok.inspection_error is None
        assert gone.inspected_at is None and gone.inspection_error == "File not found on disk"

    # a second pass has nothing left to do: the failed file waits until it changes instead of being retried forever
    stub_ffprobe.clear()
    assert asyncio.run(tasks.media_inspection(FakeCtx()))["processed"] == 0
    assert stub_ffprobe == []


def test_single_file_job(stub_ffprobe, movie_files):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.workers import tasks

    result = asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))
    assert result == {"status": "inspected", "error": None}
    with session_scope() as db:
        assert db.get(models.MediaFile, movie_files["ok"]).video_codec == "h264"


def test_backlog_does_not_requeue_forever_when_a_changed_file_fails(stub_ffprobe, movie_files, monkeypatch):
    """A previously inspected file that changed but can no longer be probed must leave the backlog."""
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import inspection_service
    from backend.workers import tasks
    from backend.workers.queue import job_queue

    requeued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda name, *a, **kw: requeued.append(name))
    asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))
    with session_scope() as db:
        db.get(models.MediaFile, movie_files["ok"]).size_bytes += 1  # changed on disk since it was inspected

    async def broken_probe(path, timeout=30):
        raise RuntimeError("ffprobe failed: boom")

    monkeypatch.setattr(inspection_service, "probe", broken_probe)
    stats = asyncio.run(tasks.media_inspection(FakeCtx()))
    assert stats["status"] == "complete" and requeued == []
    assert asyncio.run(tasks.media_inspection(FakeCtx()))["processed"] == 0


def test_import_quality_mismatch_and_auto_resolve(movie_files, monkeypatch):
    """File claiming 1080p produces quality_mismatch when fixture is 720p; auto-resolves when fixed."""
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import inspection_service
    from backend.workers import tasks
    from sqlalchemy import select

    mismatch_payload = {
        "format": {"format_name": "matroska", "duration": "5400", "bit_rate": "4000000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
        ],
    }
    corrected_payload = {
        "format": {"format_name": "matroska", "duration": "5400", "bit_rate": "8000000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
        ],
    }

    current_payload = mismatch_payload

    async def fake_probe(path, timeout=30):
        return current_payload

    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(inspection_service, "probe", fake_probe)

    with session_scope() as db:
        mf = db.get(models.MediaFile, movie_files["ok"])
        mf.quality = "WEBDL-1080p"

    # Step 1: inspect with 720p probe -> generates quality_mismatch issue
    res1 = asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))
    assert res1["status"] == "inspected"

    with session_scope() as db:
        issue = db.scalar(
            select(models.LibraryIssue).where(
                models.LibraryIssue.media_type == "movie",
                models.LibraryIssue.media_id == movie_files["movie"],
                models.LibraryIssue.issue_type == "quality_mismatch",
            )
        )
        assert issue is not None
        assert issue.status == "open"
        assert issue.data.get("actual") == "720p"

    # Step 2: corrected probe -> re-inspect auto-resolves the issue
    current_payload = corrected_payload
    res2 = asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))
    assert res2["status"] == "inspected"

    with session_scope() as db:
        issue = db.scalar(
            select(models.LibraryIssue).where(
                models.LibraryIssue.media_type == "movie",
                models.LibraryIssue.media_id == movie_files["movie"],
                models.LibraryIssue.issue_type == "quality_mismatch",
            )
        )
        assert issue is not None
        assert issue.status == "resolved"


def test_unchanged_mtime_skips_reinspection(stub_ffprobe, movie_files):
    from backend.workers import tasks
    # First inspection
    r1 = asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))
    assert r1["status"] == "inspected"

    # Second inspection without force -> skipped
    r2 = asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": False})))
    assert r2["status"] == "skipped"


def test_relabel_action_resolves_quality_mismatch(movie_files, monkeypatch):
    """Applying 'relabel' action updates MediaFile.quality and resolves the issue."""
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import inspection_service, issue_service
    from backend.services.audit import Actor
    from backend.workers import tasks
    from sqlalchemy import delete, select

    mismatch_payload = {
        "format": {"format_name": "matroska", "duration": "5400", "bit_rate": "4000000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
        ],
    }
    monkeypatch.setattr(inspection_service, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(inspection_service, "probe", lambda path, timeout=30: asyncio.sleep(0, result=mismatch_payload))

    with session_scope() as db:
        db.execute(delete(models.LibraryIssue).where(models.LibraryIssue.media_id == movie_files["movie"]))
        mf = db.get(models.MediaFile, movie_files["ok"])
        mf.quality = "WEBDL-1080p"

    asyncio.run(tasks.inspect_file(FakeCtx({"media_file_id": movie_files["ok"], "force": True})))

    with session_scope() as db:
        issue = db.scalar(
            select(models.LibraryIssue).where(
                models.LibraryIssue.media_id == movie_files["movie"],
                models.LibraryIssue.issue_type == "quality_mismatch",
            )
        )
        assert issue is not None and issue.status == "open"
        msg = issue_service.resolve(db, issue, "relabel", {}, Actor.system())
        assert "Relabelled" in msg
        assert issue.status == "resolved"
        db.flush()

        mf = db.get(models.MediaFile, movie_files["ok"])
        assert "720p" in mf.quality


def test_inspection_api_endpoints(stub_ffprobe, movie_files, client, admin_headers, monkeypatch):
    from backend.services import inspection_service
    monkeypatch.setattr(inspection_service, "ffprobe_version", lambda: "7.0.2")

    # GET /system/status
    res = client.get("/api/v1/system/status", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert "ffprobe" in data
    assert data["ffprobe"]["found"] is True
    assert data["ffprobe"]["version"] == "7.0.2"

    # POST /media-files/{id}/inspect
    post_res = client.post(f"/api/v1/media-files/{movie_files['ok']}/inspect", headers=admin_headers)
    assert post_res.status_code == 200
    assert "job_id" in post_res.json()

    # POST /media-files/9999999/inspect -> 404
    missing_res = client.post("/api/v1/media-files/9999999/inspect", headers=admin_headers)
    assert missing_res.status_code == 404

    # POST /library/inspect
    lib_res = client.post("/api/v1/library/inspect", json={"scope": "movies", "only_uninspected": True}, headers=admin_headers)
    assert lib_res.status_code == 200
    assert "job_id" in lib_res.json()

    # GET /movies with video_resolution filter
    movies_res = client.get("/api/v1/movies?video_resolution=1080p", headers=admin_headers)
    assert movies_res.status_code == 200


def test_full_reinspect_continuation_does_not_reset_again(stub_ffprobe, movie_files, monkeypatch):
    """only_uninspected=False resets once; the continuation must not reset again or it re-probes batch 1 forever."""
    from backend.services import inspection_service
    from backend.workers import tasks
    from backend.workers.queue import job_queue

    monkeypatch.setattr(inspection_service, "_pending_ids", lambda limit, scope="all": [movie_files["ok"]])  # always "more"
    requeued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda name, payload, **kw: requeued.append(payload))

    asyncio.run(tasks.media_inspection(FakeCtx({"scope": "movies", "only_uninspected": False})))
    assert requeued == [{"scope": "movies", "only_uninspected": True}]


def test_manual_reinspect_is_not_folded_into_a_queued_post_import_job(movie_files, client, admin_headers):
    from sqlalchemy import select

    from backend.db import models
    from backend.db.session import session_scope
    from backend.workers.queue import job_queue

    mf_id = movie_files["ok"]
    post_import = job_queue.enqueue("inspect_file", {"media_file_id": mf_id}, dedupe_key=f"file:{mf_id}")
    try:
        res = client.post(f"/api/v1/media-files/{mf_id}/inspect", headers=admin_headers)
        assert res.status_code == 200
        forced = res.json()["job_id"]
        assert forced != post_import
        with session_scope() as db:
            assert db.get(models.Job, forced).payload["force"] is True
    finally:
        with session_scope() as db:
            for job in db.scalars(select(models.Job).where(models.Job.name == "inspect_file", models.Job.status == "queued")):
                db.delete(job)


@pytest.mark.parametrize("hdr, stored, expected", [
    ("DV", "DV", True), ("DV", "DV HDR10", True), ("DV", "Dolby Vision", True), ("DV", "HDR10", False),
    ("HDR10", "HDR10", True), ("HDR10", "DV HDR10", True), ("HDR10", "HDR10+", False),
    ("HDR10+", "HDR10+", True), ("HDR10+", "DV HDR10+", True), ("HDR10+", "HDR10", False),
    ("SDR", None, True), ("SDR", "HLG", False), ("ANY_HDR", "HLG", True),
])
def test_movie_hdr_filter(movie_files, hdr, stored, expected):
    from backend.db import models
    from backend.db.base import utcnow
    from backend.db.repositories import MovieRepository
    from backend.db.session import session_scope

    with session_scope() as db:
        mf = db.get(models.MediaFile, movie_files["ok"])
        mf.hdr_format, mf.inspected_at = stored, utcnow()
        db.flush()
        ids = [m.id for m in db.scalars(MovieRepository(db).query(hdr_format=hdr))]
        assert (movie_files["movie"] in ids) is expected


def test_sdr_filter_excludes_uninspected_files(movie_files):
    """hdr_format is NULL both for measured SDR and for never-inspected files; only the former is SDR."""
    from backend.db import models
    from backend.db.repositories import MovieRepository
    from backend.db.session import session_scope

    with session_scope() as db:
        mf = db.get(models.MediaFile, movie_files["ok"])
        mf.hdr_format, mf.inspected_at = None, None
        db.flush()
        assert movie_files["movie"] not in [m.id for m in db.scalars(MovieRepository(db).query(hdr_format="SDR"))]


def test_series_list_filters_and_summarises_measured_episode_files(client, admin_headers, tmp_path):
    """A series matches when any episode file matches; the list shows distinct facts across its files."""
    from backend.db import models
    from backend.db.base import utcnow
    from backend.db.session import session_scope
    from backend.services import media_file_service

    files = {"e1": tmp_path / "Show.S01E01.1080p.mkv", "e2": tmp_path / "Show.S01E02.720p.mkv"}
    for f in files.values():
        f.write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 64)
    with session_scope() as db:
        series = models.Series(title="Filterable Show", year=2020, monitored=True)
        db.add(series)
        db.flush()
        season = models.Season(series_id=series.id, season_number=1, monitored=True)
        db.add(season)
        db.flush()
        eps = [models.Episode(series_id=series.id, season_id=season.id, season_number=1, episode_number=n, title=f"Ep {n}", monitored=True)
               for n in (1, 2)]
        db.add_all(eps)
        db.flush()
        measured = {"e1": ("1080p", "DV HDR10", "hevc", "eac3"), "e2": ("720p", None, "h264", "aac")}
        for ep, key in zip(eps, ("e1", "e2")):
            mf = media_file_service.attach_episode_file(db, series, [ep], files[key], None)
            mf.video_resolution, mf.hdr_format, mf.video_codec, mf.audio_codec = measured[key]
            mf.inspected_at = utcnow()
        sid = series.id

    def ids(**params):
        r = client.get("/api/v1/series", params={"page_size": 200, **params}, headers=admin_headers)
        assert r.status_code == 200
        return {i["id"]: i for i in r.json()["items"]}

    try:
        item = ids()[sid]
        assert item["video_resolutions"] == ["1080p", "720p"]  # best first
        assert item["hdr_formats"] == ["DV HDR10"] and item["video_codecs"] == ["h264", "hevc"]
        assert item["audio_codecs"] == ["aac", "eac3"]

        assert sid in ids(video_resolution="720p") and sid in ids(video_resolution="1080p")
        assert sid not in ids(video_resolution="2160p")
        assert sid in ids(hdr_format="DV") and sid in ids(hdr_format="SDR")  # one DV file, one measured SDR file
        assert sid not in ids(hdr_format="HLG")
        assert sid in ids(video_codec="HEVC") and sid not in ids(video_codec="av1")
        assert sid in ids(audio_codec="aac")
    finally:
        with session_scope() as db:
            db.delete(db.get(models.Series, sid))


@pytest.mark.ffprobe
def test_real_ffprobe_binary(tmp_path):
    """Optional real-binary test when ffmpeg and ffprobe are installed on host."""
    import shutil
    import subprocess
    from backend.services.inspection_service import probe, parse_ffprobe

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        pytest.skip("ffmpeg or ffprobe not installed on host")

    test_file = tmp_path / "test_sample.mkv"
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
        "-c:v", "libx264", "-c:a", "aac",
        str(test_file),
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    raw = asyncio.run(probe(test_file))
    parsed = parse_ffprobe(raw)
    assert parsed.video.width == 320
    assert parsed.video.height == 240
    assert parsed.video.codec in ("h264", "libx264")
    assert len(parsed.audio) >= 1

