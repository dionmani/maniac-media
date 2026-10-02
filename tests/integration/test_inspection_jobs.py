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
