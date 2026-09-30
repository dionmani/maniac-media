"""F1 Media Files Foundation integration tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from backend.db import migrator, models
from backend.db.repositories import MediaFileRepository
from backend.db.session import create_db_engine, session_scope
from backend.domain.parser import normalise_path_key, parse_quality
from backend.domain.types import EpisodeMetadata, MediaMetadata
from backend.services import dashboard_service, fileops, import_service, media_file_service, media_service, renamer_service, scanner_service
from backend.services.audit import SYSTEM


def test_migration_and_backfill(tmp_path):
    """Test 0004_media_files migration on a fixture DB with movies, multi-episode files, and path collision."""
    db_path = tmp_path / "test_migration.db"
    engine = create_db_engine(f"sqlite:///{db_path}")

    # Upgrade to 0003 first
    cfg = migrator.alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        from alembic import command
        command.upgrade(cfg, "0003_queue_client_position")

    # Seed pre-0004 data
    with engine.begin() as conn:
        # Movies: 1 normal, 2 colliding
        conn.execute(sa.text(
            "INSERT INTO movies (id, title, year, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (1, 'Movie A', 2020, 1, 1, '/media/movies/movie_a.mkv', 1000000, 'Bluray-1080p')"
        ))
        conn.execute(sa.text(
            "INSERT INTO movies (id, title, year, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (2, 'Movie B', 2021, 1, 1, '/media/movies/collision.mkv', 2000000, 'WEBDL-1080p')"
        ))
        conn.execute(sa.text(
            "INSERT INTO movies (id, title, year, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (3, 'Movie C', 2022, 1, 1, '/media/movies/collision.mkv', 2000000, 'WEBDL-1080p')"
        ))
        conn.execute(sa.text(
            "INSERT INTO movies (id, title, year, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (4, 'Movie No File', 2023, 1, 0, NULL, 0, NULL)"
        ))

        # Series, Season, and Episodes sharing a multi-episode file
        conn.execute(sa.text(
            "INSERT INTO series (id, title, year, monitored) VALUES (1, 'Show A', 2020, 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO seasons (id, series_id, season_number, monitored) VALUES (1, 1, 1, 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO episodes (id, series_id, season_id, season_number, episode_number, title, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (1, 1, 1, 1, 1, 'Pilot', 1, 1, '/media/tv/Show A/S01E01-E02.mkv', 5000000, 'HDTV-720p')"
        ))
        conn.execute(sa.text(
            "INSERT INTO episodes (id, series_id, season_id, season_number, episode_number, title, monitored, has_file, file_path, file_size_bytes, quality) "
            "VALUES (2, 1, 1, 1, 2, 'Part 2', 1, 1, '/media/tv/Show A/S01E01-E02.mkv', 5000000, 'HDTV-720p')"
        ))

    # Now upgrade to head (0004)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")

    assert migrator.current_revision(engine) == "0004_media_files"

    with engine.connect() as conn:
        # Check media_files rows
        mf_rows = conn.execute(sa.text("SELECT id, media_type, movie_id, series_id, path, size_bytes, is_primary FROM media_files ORDER BY id")).fetchall()
        # Expect Movie A (1), Movie B (2) [Movie C skipped due to collision], and 1 row for TV Show A S01E01-E02
        assert len(mf_rows) == 3

        movie_a = next(r for r in mf_rows if r[2] == 1)
        assert movie_a[1] == "movie"
        assert movie_a[4] == "/media/movies/movie_a.mkv"
        assert movie_a[5] == 1000000
        assert movie_a[6] == 1

        tv_file = next(r for r in mf_rows if r[1] == "tv")
        assert tv_file[3] == 1
        assert tv_file[4] == "/media/tv/Show A/S01E01-E02.mkv"
        assert tv_file[5] == 5000000

        # Check episode_files links: both episodes 1 and 2 linked to tv_file[0]
        links = conn.execute(sa.text(f"SELECT episode_id FROM episode_files WHERE media_file_id = {tv_file[0]} ORDER BY episode_id")).fetchall()
        assert [l[0] for l in links] == [1, 2]


def test_single_movie_import_and_upgrade(database, library, make_video):
    with session_scope() as db:
        m = media_service.add_movie(
            db, MediaMetadata(media_type="movie", provider="test", title="Gladiator", year=2000),
            quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM
        )
        movie_id = m.id

    # 1. Initial import (720p)
    low_src = make_video(library["downloads"] / "Gladiator.2000.720p.HDTV.x264" / "Gladiator.2000.720p.HDTV.x264.mkv")
    res = import_service.manual_import([{"path": str(low_src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert res.imported

    with session_scope() as db:
        repo = MediaFileRepository(db)
        files = repo.for_movie(movie_id)
        assert len(files) == 1
        f1 = files[0]
        assert f1.is_primary
        assert f1.quality == "HDTV-720p"
        movie = db.get(models.Movie, movie_id)
        assert movie.has_file
        assert movie.file_path == f1.path
        assert movie.file_size_bytes == f1.size_bytes
        assert movie.quality == "HDTV-720p"
        old_path = f1.path

    # 2. Upgrade import (1080p Bluray)
    high_src = make_video(library["downloads"] / "Gladiator.2000.1080p.BluRay.x264" / "Gladiator.2000.1080p.BluRay.x264.mkv", b"\x1a\x45\xdf\xa3" + b"\x03" * 5000)
    res2 = import_service.manual_import([{"path": str(high_src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert res2.imported

    with session_scope() as db:
        repo = MediaFileRepository(db)
        files = repo.for_movie(movie_id)
        # Old media_file detached on upgrade, replaced with new
        assert len(files) == 1
        f2 = files[0]
        assert f2.is_primary
        assert f2.quality == "Bluray-1080p"
        movie = db.get(models.Movie, movie_id)
        assert movie.has_file
        assert movie.file_path == f2.path
        assert movie.quality == "Bluray-1080p"
        # Old file should no longer exist at original path, should be in recycle bin
        assert not Path(old_path).exists()
        assert any(p.name.startswith("Gladiator (2000) [HDTV-720p]") for p in library["recycle"].rglob("*.mkv"))


def test_multiepisode_import_and_storage_dedup(database, library, make_video):
    with session_scope() as db:
        series = media_service.add_series(
            db, MediaMetadata(
                media_type="tv", provider="test", title="Band of Brothers", year=2001,
                season_count=1,
                episodes=[EpisodeMetadata(1, 1, "Currahee"), EpisodeMetadata(1, 2, "Day of Days")]
            ),
            quality_profile_id=1, root_folder=None, monitored=True, monitor="all", actor=SYSTEM
        )
        series_id = series.id

    ep_src = make_video(library["downloads"] / "Band.of.Brothers.S01E01E02.1080p.BluRay.mkv", b"\x1a\x45\xdf\xa3" + b"\x04" * 10000)
    res = import_service.manual_import([
        {"path": str(ep_src), "media_type": "tv", "media_id": series_id, "season": 1, "episodes": [1, 2]}
    ], "move", SYSTEM)
    assert res.imported

    with session_scope() as db:
        repo = MediaFileRepository(db)
        files = repo.for_series(series_id)
        # One file on disk for both episodes
        assert len(files) == 1
        mf = files[0]
        assert len(mf.episodes) == 2

        # Check total bytes for TV
        tv_bytes = repo.total_bytes("tv")
        assert tv_bytes == mf.size_bytes  # counted only ONCE, not twice

        stats = dashboard_service.stats(db)
        assert stats["storage"]["tv_bytes"] == mf.size_bytes


def test_scanner_extra_files_and_missing_detach(database, library, make_video):
    movie_folder = library["movies"] / "Dune (2021)"
    main_file = make_video(movie_folder / "Dune.2021.2160p.UHD.mkv", b"\x1a\x45\xdf\xa3" + b"\x05" * 10000)
    extra_file = make_video(movie_folder / "Dune.2021.1080p.BluRay.mkv", b"\x1a\x45\xdf\xa3" + b"\x06" * 8000)

    scanner_service.scan("movies", incremental=False)

    with session_scope() as db:
        movie = db.scalar(select(models.Movie).where(models.Movie.title == "Dune"))
        assert movie is not None
        assert movie.has_file
        repo = MediaFileRepository(db)
        files = repo.for_movie(movie.id)
        assert len(files) == 2
        primaries = [f for f in files if f.is_primary]
        non_primaries = [f for f in files if not f.is_primary]
        assert len(primaries) == 1
        assert len(non_primaries) == 1
        assert primaries[0].path == str(main_file)
        assert non_primaries[0].path == str(extra_file)

    # Now delete the files on disk and scan again -> detach and flip has_file
    main_file.unlink()
    extra_file.unlink()

    scanner_service.scan("movies", incremental=False)

    with session_scope() as db:
        movie = db.scalar(select(models.Movie).where(models.Movie.title == "Dune"))
        assert not movie.has_file
        assert movie.file_path is None
        repo = MediaFileRepository(db)
        files = repo.for_movie(movie.id)
        assert len(files) == 0


def test_consistency_checker(database, library, make_video):
    with session_scope() as db:
        m = media_service.add_movie(
            db, MediaMetadata(media_type="movie", provider="test", title="The Matrix", year=1999),
            quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM
        )
        fake_file = library["movies"] / "The Matrix (1999)" / "matrix.mkv"
        make_video(fake_file)
        media_file_service.attach_movie_file(db, m, fake_file, parse_quality("1080p BluRay"), source="scan")

    # Manually introduce drift on legacy columns
    with session_scope() as db:
        movie = db.scalar(select(models.Movie).where(models.Movie.title == "The Matrix"))
        movie.file_size_bytes = 999
        movie.quality = "WrongQuality"

    # Run check_consistency
    with session_scope() as db:
        res = media_file_service.check_consistency(db)
        assert res["repaired"] >= 1

    # Verify repaired
    with session_scope() as db:
        movie = db.scalar(select(models.Movie).where(models.Movie.title == "The Matrix"))
        repo = MediaFileRepository(db)
        primary = repo.for_movie(movie.id)[0]
        assert movie.file_size_bytes == primary.size_bytes
        assert movie.quality == primary.quality


def test_media_files_api(client, admin_headers, library, make_video):
    # Add a movie and attach a file
    with session_scope() as db:
        m = media_service.add_movie(
            db, MediaMetadata(media_type="movie", provider="test", title="Oppenheimer", year=2023),
            quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM
        )
        fake_file = library["movies"] / "Oppenheimer (2023)" / "oppenheimer.2023.2160p.mkv"
        make_video(fake_file)
        mf = media_file_service.attach_movie_file(db, m, fake_file, parse_quality("2160p UHD"), source="scan")
        mf_id = mf.id
        movie_id = m.id

    # 1. GET /movies/{id} includes files[]
    r = client.get(f"/api/v1/movies/{movie_id}", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert "files" in data
    assert len(data["files"]) == 1
    assert data["files"][0]["id"] == mf_id
    assert data["files"][0]["is_primary"] is True

    # 2. GET /media-files/{id} returns detail
    r2 = client.get(f"/api/v1/media-files/{mf_id}", headers=admin_headers)
    assert r2.status_code == 200
    detail = r2.json()
    assert detail["id"] == mf_id
    assert detail["movie"]["id"] == movie_id
    assert detail["movie"]["title"] == "Oppenheimer"

    # 3. GET /media-files?movie_id=...
    r3 = client.get(f"/api/v1/media-files?movie_id={movie_id}", headers=admin_headers)
    assert r3.status_code == 200
    page = r3.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == mf_id
