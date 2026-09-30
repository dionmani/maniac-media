"""End-to-end automation pipeline: search → grab → download → import → rename, plus scanner and migrations."""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata
from backend.services import download_service, import_service, media_service, renamer_service, scanner_service, search_service
from backend.services.audit import SYSTEM


def _add_movie(title: str, year: int) -> int:
    with session_scope() as db:
        m = media_service.add_movie(db, MediaMetadata(media_type="movie", provider="test", title=title, year=year),
                                    quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM)
        return m.id


def _setup_demo_integrations():
    with session_scope() as db:
        if not db.scalar(select(models.Indexer).where(models.Indexer.implementation == "demo")):
            db.add(models.Indexer(name="Demo", implementation="demo", url="", protocol="torrent", enabled=True, priority=1))
            db.add(models.DownloadClient(name="Sim", client_type="simulator", enabled=True, category="test", priority=1))


def test_full_download_and_import_pipeline(database, library):
    _setup_demo_integrations()
    movie_id = _add_movie("Interstellar", 2014)

    result = asyncio.run(search_service.automatic("movie", movie_id))
    assert result["grabbed"] and "Interstellar" in result["grabbed"]
    queue_id = result["queue_id"]

    import time

    for _ in range(40):  # the simulator finishes any release within 15 s
        asyncio.run(download_service.sync_clients())
        with session_scope() as db:
            status = db.get(models.DownloadQueueItem, queue_id).status
        if status in ("completed", "imported"):
            break
        time.sleep(1)
    assert status in ("completed", "imported")
    if status == "completed":  # a live worker pool may already have auto-imported it
        outcome = import_service.import_queue_item(queue_id)
        if not outcome.imported:  # lost the race: the worker imported between our status read and this call
            with session_scope() as db:
                assert db.get(models.DownloadQueueItem, queue_id).status == "imported", outcome
        else:
            assert outcome.imported == ["Interstellar (2014)"], outcome
    with session_scope() as db:
        movie = db.get(models.Movie, movie_id)
        assert movie.has_file and Path(movie.file_path).exists()
        assert Path(movie.file_path).name.startswith("Interstellar (2014) [")
        assert Path(movie.file_path).parent.name == "Interstellar (2014)"
        assert db.get(models.DownloadQueueItem, queue_id).status == "imported"
        events = {h.event_type for h in db.scalars(select(models.History).where(models.History.media_id == movie_id))}
        assert {"added", "grabbed", "downloaded", "imported"} <= events

    # same file again is not an upgrade -> rejected, library untouched
    again = asyncio.run(search_service.automatic("movie", movie_id))
    assert again["grabbed"] is None or "Remux" in again["grabbed"] or "2160p" in again["grabbed"]


def test_import_rejects_corrupt_file_and_rolls_back(database, library, make_video):
    movie_id = _add_movie("Memento", 2000)
    src = make_video(library["downloads"] / "Memento.2000.1080p.BluRay.x264-GRP" / "memento.mkv", b"garbage")
    result = import_service.manual_import([{"path": str(src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert not result.imported and "Invalid Matroska header" in result.failed[0]
    assert src.exists()


def test_upgrade_replaces_and_recycles(database, library, make_video):
    movie_id = _add_movie("Heat", 1995)
    low = make_video(library["downloads"] / "Heat.1995.720p.HDTV.x264" / "Heat.1995.720p.HDTV.x264.mkv")
    assert import_service.manual_import([{"path": str(low), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM).imported
    with session_scope() as db:
        old_path = Path(db.get(models.Movie, movie_id).file_path)
    high = make_video(library["downloads"] / "Heat.1995.1080p.WEB-DL" / "Heat.1995.1080p.WEB-DL.mkv", b"\x1a\x45\xdf\xa3" + b"\x01" * 4000)
    assert import_service.manual_import([{"path": str(high), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM).imported
    with session_scope() as db:
        movie = db.get(models.Movie, movie_id)
        assert movie.quality == "WEBDL-1080p" and Path(movie.file_path).exists()
    assert not old_path.exists()
    assert any(p.name.startswith("Heat (1995) [HDTV-720p]") for p in library["recycle"].rglob("*.mkv"))

    worse = make_video(library["downloads"] / "Heat.1995.480p" / "Heat.1995.DVDRip.mkv", b"\x1a\x45\xdf\xa3" + b"\x02" * 100)
    result = import_service.manual_import([{"path": str(worse), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert not result.imported and "Not an upgrade" in result.failed[0]
    assert worse.exists()


def test_scanner_and_renamer(database, library, make_video):
    make_video(library["movies"] / "Alien (1979)" / "alien.1979.1080p.bluray.x264.mkv")
    make_video(library["movies"] / "Blade Runner 2049 (2017)" / "br2049.2160p.web-dl.mkv")
    make_video(library["tv"] / "The Expanse" / "Season 01" / "The.Expanse.S01E01.720p.HDTV.mkv")
    make_video(library["tv"] / "The Expanse" / "Season 01" / "The.Expanse.S01E02E03.720p.HDTV.mkv")

    result = scanner_service.scan("all", incremental=True)
    assert result["movies"]["new"] >= 2
    assert result["tv"]["new_episodes"] == 3
    with session_scope() as db:
        br = db.scalar(select(models.Movie).where(models.Movie.title == "Blade Runner 2049"))
        assert br is not None and br.year == 2017 and br.quality == "WEBDL-2160p"

    second = scanner_service.scan("movies", incremental=True)
    assert second["movies"]["skipped"] >= 2 and second["movies"]["scanned"] == 0

    preview = renamer_service.preview("all")
    titles = {i["title"] for i in preview["items"]}
    assert "Alien (1979)" in titles and "The Expanse S01E02-E03" in titles
    dry = renamer_service.execute([{"media_type": i["media_type"], "media_id": i["media_id"]} for i in preview["items"]],
                                  dry_run=True, include_folders=False, actor=SYSTEM)
    assert dry["renamed"] == 0 and dry["planned"]
    done = renamer_service.execute([{"media_type": i["media_type"], "media_id": i["media_id"]} for i in preview["items"]],
                                   dry_run=False, include_folders=False, actor=SYSTEM)
    assert done["renamed"] == len(preview["items"]), done["errors"]
    assert (library["movies"] / "Alien (1979)" / "Alien (1979) [Bluray-1080p].mkv").exists()
    assert (library["tv"] / "The Expanse" / "Season 01" / "The Expanse - S01E02-E03 - Episode 2 + Episode 3 [HDTV-720p].mkv").exists()
    assert renamer_service.preview("all")["items"] == []

    (library["movies"] / "Alien (1979)" / "Alien (1979) [Bluray-1080p].mkv").unlink()
    after = scanner_service.scan("movies", incremental=True)
    assert after["movies"]["missing"] >= 1


def test_job_queue_persists_and_recovers(database):
    from backend.main import app
    from backend.workers import tasks  # noqa: F401
    from backend.workers.queue import job_queue

    pool = getattr(app.state, "pool", None)
    if pool is not None:
        pool.paused = True  # keep live workers from racing this test
    job_id = job_queue.enqueue("cleanup", trigger="manual")
    assert job_queue.enqueue("cleanup", trigger="manual") == job_id  # deduplicated
    claimed = job_queue.claim()
    assert claimed is not None
    assert job_queue.recover() >= 1  # simulated crash while running
    with session_scope() as db:
        assert db.get(models.Job, claimed.id).status == "queued"


def test_database_migration_wizard_to_second_sqlite(database, tmp_path):
    from backend.db.session import build_url, db_manager
    from backend.services import database_service

    original = db_manager.url
    target = build_url("sqlite", sqlite_path=str(tmp_path / "target.db"))
    try:
        database_service.migrate(target, "sqlite")
        assert database_service.state.step == "done"
        assert all(v["source"] == v["target"] for v in database_service.state.tables.values())
        assert db_manager.url == target
        assert database_service.verify_schema(db_manager.engine) == []
    finally:
        from backend.core.secrets import SECRET_DB_URL, secret_store

        secret_store.set(SECRET_DB_URL, original)
        db_manager.configure(original)


def test_database_migration_rolls_back_on_failure(database, tmp_path):
    from backend.db.session import db_manager
    from backend.services import database_service

    original = db_manager.url
    import pytest

    with pytest.raises(Exception):
        database_service.migrate("postgresql+psycopg://u:p@127.0.0.1:1/nope", "postgresql")
    assert database_service.state.step == "failed"
    assert db_manager.url == original
