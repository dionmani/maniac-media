"""Deleting one library file (keeping the movie / episode) and force-replacing a file via manual import."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from backend.db import models
from backend.db.repositories import MediaFileRepository
from backend.db.session import session_scope
from backend.domain.types import EpisodeMetadata, MediaMetadata
from backend.services import import_service, media_service
from backend.services.audit import SYSTEM

API = "/api/v1"


def _add_movie(title: str, year: int) -> int:
    with session_scope() as db:
        return media_service.add_movie(db, MediaMetadata(media_type="movie", provider="test", title=title, year=year),
                                       quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM).id


def _import(src: Path, movie_id: int, force: bool = False):
    return import_service.manual_import([{"path": str(src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM, force)


def test_delete_movie_file_keeps_the_movie(client, admin_headers, library, make_video):
    movie_id = _add_movie("Blackhat", 2015)
    src = make_video(library["downloads"] / "Blackhat.2015.1080p.BluRay.x264" / "Blackhat.2015.1080p.BluRay.x264.mkv")
    assert _import(src, movie_id).imported
    with session_scope() as db:
        mf = MediaFileRepository(db).for_movie(movie_id)[0]
        file_id, path = mf.id, Path(mf.path)

    r = client.delete(f"{API}/media-files/{file_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert "recycle bin" in r.json()["message"]
    assert not path.exists()
    assert any(p.name == path.name for p in library["recycle"].rglob("*.mkv"))
    with session_scope() as db:
        movie = db.get(models.Movie, movie_id)
        assert movie is not None and movie.monitored and not movie.has_file and not movie.file_path
        assert db.get(models.MediaFile, file_id) is None
        events = {h.event_type for h in db.scalars(select(models.History).where(models.History.media_id == movie_id))}
        assert "file_deleted" in events

    # with the file gone the same quality imports again – no cutoff to clear
    again = make_video(library["downloads"] / "Blackhat.2015.1080p.BluRay.x264-B" / "Blackhat.2015.1080p.BluRay.x264-B.mkv")
    assert _import(again, movie_id).imported

    assert client.delete(f"{API}/media-files/999999", headers=admin_headers).status_code == 404


def test_delete_multi_episode_file_empties_both_episodes(client, admin_headers, library, make_video):
    with session_scope() as db:
        series = media_service.add_series(
            db, MediaMetadata(media_type="tv", provider="test", title="The Pacific", year=2010, season_count=1,
                              episodes=[EpisodeMetadata(1, 1, "Guadalcanal"), EpisodeMetadata(1, 2, "Basilone")]),
            quality_profile_id=1, root_folder=None, monitored=True, monitor="all", actor=SYSTEM)
        series_id = series.id
    src = make_video(library["downloads"] / "The.Pacific.S01E01E02.1080p.BluRay.mkv")
    assert import_service.manual_import([{"path": str(src), "media_type": "tv", "media_id": series_id, "season": 1, "episodes": [1, 2]}],
                                        "move", SYSTEM).imported
    with session_scope() as db:
        file_id = MediaFileRepository(db).for_series(series_id)[0].id

    assert client.delete(f"{API}/media-files/{file_id}", headers=admin_headers).status_code == 200
    with session_scope() as db:
        eps = db.scalars(select(models.Episode).where(models.Episode.series_id == series_id)).all()
        assert len(eps) == 2 and not any(e.has_file for e in eps)
        assert not MediaFileRepository(db).for_series(series_id)


def test_delete_file_needs_media_delete(client, admin_headers):
    r = client.post(f"{API}/users", json={"username": "filereader", "password": "Reader-Passw0rd", "role": "read_only"}, headers=admin_headers)
    assert r.status_code == 201
    token = client.post(f"{API}/auth/login", json={"username": "filereader", "password": "Reader-Passw0rd"}).json()["access_token"]
    assert client.delete(f"{API}/media-files/1", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_force_manual_import_replaces_a_file_that_is_not_an_upgrade(client, admin_headers, library, make_video):
    movie_id = _add_movie("Manhunter", 1986)
    first = make_video(library["downloads"] / "Manhunter.1986.1080p.BluRay.x264" / "Manhunter.1986.1080p.BluRay.x264.mkv")
    assert _import(first, movie_id).imported
    with session_scope() as db:
        old = Path(db.get(models.Movie, movie_id).file_path)

    same = make_video(library["downloads"] / "Manhunter.1986.1080p.BluRay.x264-OTHER" / "Manhunter.1986.1080p.BluRay.x264-OTHER.mkv",
                      b"\x1a\x45\xdf\xa3" + b"\x07" * 3000)
    refused = _import(same, movie_id)
    assert not refused.imported and "Not an upgrade" in refused.failed[0]

    r = client.post(f"{API}/library/manual-import", headers=admin_headers,
                    json={"mode": "move", "force": True, "items": [{"path": str(same), "media_type": "movie", "media_id": movie_id}]})
    assert r.status_code == 200 and r.json()["imported"], r.text
    with session_scope() as db:
        movie = db.get(models.Movie, movie_id)
        assert movie.has_file and "OTHER" in (movie.files[0].scene_name or "")
        assert len(movie.files) == 1
    assert not same.exists()
    assert not old.exists() or old == Path(movie.file_path)
