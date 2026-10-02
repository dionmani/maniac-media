"""Import failure diagnosis end to end: a wrong remote path mapping is detected, the correct
mapping is suggested, and applying it fixes and re-imports the affected queue item."""
from __future__ import annotations

import asyncio

from backend.core.settings import get_settings
from backend.db import models
from backend.db.session import session_scope
from backend.domain.diagnosis import FixKind
from backend.services import import_diagnosis, import_service
from backend.services.audit import SYSTEM
from backend.workers.queue import job_queue


def _setup_broken_mapping(make_video) -> tuple[int, int, str]:
    downloads = get_settings().download_folder
    real_folder_name = "Movie.Title.2020.1080p.WEB-DL-GRP"
    real_file = make_video(
        __import__("pathlib").Path(downloads) / real_folder_name / f"{real_folder_name}.mkv"
    )
    reported_path = f"/mnt/remote/complete/{real_folder_name}/{real_folder_name}.mkv"

    with session_scope() as db:
        movie = models.Movie(title="Movie Title", year=2020, quality_profile_id=1, monitored=True, has_file=False)
        db.add(movie)
        db.flush()
        client = models.DownloadClient(name="broken-client", client_type="simulator", enabled=True, priority=1)
        db.add(client)
        db.flush()
        q = models.DownloadQueueItem(
            title=movie.title, media_type="movie", media_id=movie.id, release_title=f"{real_folder_name}",
            status="completed", download_path=reported_path, download_client_id=client.id, protocol="usenet",
        )
        db.add(q)
        db.flush()
        return movie.id, q.id, str(real_file)


def test_remote_path_mapping_is_detected_and_fix_unblocks_every_affected_item(database, make_video, monkeypatch):
    # The real worker pool is running in the background for the test session (started by the
    # `client` fixture elsewhere); avoid racing it by not actually enqueueing the retry job –
    # the test drives the "worker" step itself, like tests/integration/test_downloads.py does.
    monkeypatch.setattr(job_queue, "enqueue", lambda *a, **k: None)
    movie_id, queue_id, real_file = _setup_broken_mapping(make_video)

    result = import_service.import_queue_item(queue_id)
    assert not result.imported

    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert q.status == "import_failed"
        assert q.failure_reason == "remote_path_mapping"
        assert q.failure_signature and q.failure_signature.startswith("remote_path_mapping:")
        analysis = q.failure_analysis
        assert analysis is not None
        fixes = analysis["fixes"]
        assert any(f["kind"] == "apply_path_mapping" and f["bulk_applicable"] for f in fixes)
        fix = next(f for f in fixes if f["kind"] == "apply_path_mapping")

    asyncio.run(import_diagnosis.apply_fix(queue_id, FixKind.APPLY_PATH_MAPPING, fix["params"], SYSTEM, bulk=True))

    with session_scope() as db:
        client = db.get(models.DownloadClient, fix["params"]["client_id"])
        assert client.remote_path == fix["params"]["remote_path"]
        assert client.local_path == fix["params"]["local_path"]
        q = db.get(models.DownloadQueueItem, queue_id)
        # the previously-unmapped path on the queue item is corrected in place
        assert q.download_path.lower() == real_file.lower()

    # simulate the worker running the job the fix enqueued
    result = import_service.import_queue_item(queue_id)
    assert result.imported

    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert q is None or q.status == "imported"
        movie = db.get(models.Movie, movie_id)
        assert movie.has_file is True


def test_quality_rejection_is_diagnosed_with_force_import_fix(database, make_video):
    from pathlib import Path

    from backend.domain.quality import DEFAULT_PROFILES

    downloads = get_settings().download_folder
    folder = "Movie.Other.2021.1080p.WEB-DL-GRP"
    make_video(Path(downloads) / folder / f"{folder}.mkv")

    with session_scope() as db:
        sd_profile = next(p for p in DEFAULT_PROFILES if p["name"] == "SD")  # doesn't allow 1080p at all
        profile = models.QualityProfile(name="SD-only", items=sd_profile["items"], cutoff=sd_profile["cutoff"])
        db.add(profile)
        db.flush()
        movie = models.Movie(title="Movie Other", year=2021, quality_profile_id=profile.id, monitored=True, has_file=False)
        db.add(movie)
        db.flush()
        q = models.DownloadQueueItem(
            title=movie.title, media_type="movie", media_id=movie.id, release_title=folder, status="completed",
            download_path=str(Path(downloads) / folder), protocol="usenet",
        )
        db.add(q)
        db.flush()
        movie_id, queue_id = movie.id, q.id

    result = import_service.import_queue_item(queue_id)
    assert not result.imported
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        assert q.failure_reason == "quality_not_allowed"
        fixes = q.failure_analysis["fixes"]
        assert any(f["kind"] == "force_import" for f in fixes)

    result = import_service.import_queue_item(queue_id, force=True)
    assert result.imported
    with session_scope() as db:
        assert db.get(models.Movie, movie_id).has_file is True
