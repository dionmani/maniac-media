"""Radarr v3 compat API, exercised the way Seerr calls it (?apikey=, camelCase bodies)."""
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata
from backend.services import metadata_service

BASE = "/api/compat/radarr/api/v3"
SEERR_PERMS = ["media:read", "media:write", "media:delete", "search:execute", "queue:read"]


def _key(client, admin_headers, *, allow_query=True, perms=SEERR_PERMS) -> str:
    r = client.post("/api/v1/api-keys", json={"name": "Seerr", "permissions": perms, "allow_query_param": allow_query}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


@pytest.fixture
def seerr(client, admin_headers):
    key = _key(client, admin_headers)

    class Seerr:
        def get(self, path, **params):
            return client.get(f"{BASE}{path}", params={"apikey": key, **params})

        def post(self, path, body, **params):
            return client.post(f"{BASE}{path}", params={"apikey": key, **params}, json=body)

        def put(self, path, body):
            return client.put(f"{BASE}{path}", params={"apikey": key}, json=body)

        def delete(self, path, **params):
            return client.delete(f"{BASE}{path}", params={"apikey": key, **params})

    return Seerr()


@pytest.fixture
def fake_tmdb(monkeypatch):
    async def fetch_movie(*, tmdb_id=None, imdb_id=None, title=None, year=None):
        if tmdb_id and tmdb_id >= 990000:
            return MediaMetadata(media_type="movie", provider="tmdb", title=f"Compat Movie {tmdb_id}", year=2021, tmdb_id=tmdb_id,
                                 poster_url="https://image.tmdb.org/t/p/original/x.jpg")
        return None

    monkeypatch.setattr(metadata_service, "fetch_movie", fetch_movie)


def _add(seerr, tmdb_id: int, **extra):
    profiles = seerr.get("/qualityProfile").json()
    root = seerr.get("/rootfolder").json()[0]["path"]
    body = {"title": f"Compat Movie {tmdb_id}", "qualityProfileId": profiles[0]["id"], "profileId": profiles[0]["id"],
            "titleSlug": str(tmdb_id), "minimumAvailability": "released", "tmdbId": tmdb_id, "year": 2021,
            "rootFolderPath": root, "monitored": True, "tags": [], "addOptions": {"searchForMovie": False}, **extra}
    return seerr.post("/movie", body)


def test_auth_requires_query_enabled_key(client, admin_headers):
    assert client.get(f"{BASE}/system/status").status_code == 401
    header_only = _key(client, admin_headers, allow_query=False)
    assert client.get(f"{BASE}/system/status", params={"apikey": header_only}).status_code == 401
    assert client.get(f"{BASE}/system/status", headers={"X-Api-Key": header_only}).status_code == 200


def test_connection_test_calls(seerr):
    status = seerr.get("/system/status").json()
    assert status["version"].startswith("5.")
    assert status["urlBase"] == "/api/compat/radarr"
    profiles = seerr.get("/qualityProfile").json()
    assert profiles and {"id", "name"} <= profiles[0].keys()
    assert seerr.get("/qualityprofile").json() == profiles
    folders = seerr.get("/rootfolder").json()
    assert len(folders) == 1 and folders[0]["id"] == 1
    assert seerr.get("/tag").json() == []
    tag = seerr.post("/tag", {"label": "1 - alice"}).json()
    assert tag["label"] == "1 - alice" and tag["id"] == seerr.post("/tag", {"label": "1 - alice"}).json()["id"]


def test_request_flow_lookup_add_sync(seerr, fake_tmdb):
    looked = seerr.get("/movie/lookup", term="tmdb:990001").json()
    assert len(looked) == 1 and "id" not in looked[0] and looked[0]["hasFile"] is False

    r = _add(seerr, 990001)
    assert r.status_code == 201, r.text
    movie = r.json()
    assert movie["id"] and movie["tmdbId"] == 990001 and movie["monitored"] is True
    assert movie["images"][0]["coverType"] == "poster"

    # Seerr's addMovie re-looks-up first; once added it must see the library id
    assert seerr.get("/movie/lookup", term="tmdb:990001").json()[0]["id"] == movie["id"]
    assert [m["id"] for m in seerr.get("/movie", tmdbId=990001).json()] == [movie["id"]]
    assert seerr.get(f"/movie/{movie['id']}").json()["titleSlug"] == str(movie["id"])

    # availability sync reads the full list
    synced = next(m for m in seerr.get("/movie").json() if m["id"] == movie["id"])
    assert synced["hasFile"] is False and synced["monitored"] is True

    dup = _add(seerr, 990001)
    assert dup.status_code == 400 and dup.json()[0]["propertyName"] == "TmdbId"


def test_add_without_metadata_falls_back_to_request_title(seerr, fake_tmdb):
    looked = seerr.get("/movie/lookup", term="tmdb:12345").json()  # fake_tmdb returns None below 990000
    assert looked[0]["tmdbId"] == 12345 and "id" not in looked[0]
    r = _add(seerr, 12345, title="Fallback Title")
    assert r.status_code == 201 and r.json()["title"] == "Fallback Title"


def test_add_rejects_unknown_root_and_profile(seerr, fake_tmdb):
    assert _add(seerr, 990002, rootFolderPath="/etc").json()[0]["propertyName"] == "RootFolderPath"
    assert _add(seerr, 990002, qualityProfileId=9999, profileId=9999).json()[0]["propertyName"] == "QualityProfileId"


def test_existing_unmonitored_movie_is_remonitored_via_put(seerr, fake_tmdb):
    movie = _add(seerr, 990003, monitored=False).json()
    assert movie["monitored"] is False
    r = seerr.put("/movie", {**movie, "monitored": True, "addOptions": {"searchForMovie": True}})
    assert r.status_code == 202 and r.json()["monitored"] is True


def test_movies_search_command_enqueues_job(seerr, fake_tmdb):
    movie = _add(seerr, 990004).json()
    r = seerr.post("/command", {"name": "MoviesSearch", "movieIds": [movie["id"]]})
    assert r.status_code == 201 and r.json()["name"] == "MoviesSearch"
    with session_scope() as db:
        jobs = db.scalars(select(models.Job).where(models.Job.name == "automatic_search")).all()
        assert any((j.payload or {}).get("media_id") == movie["id"] for j in jobs)
    assert seerr.post("/command", {"name": "RefreshMonitoredDownloads"}).json()["status"] == "completed"


def test_search_command_needs_search_permission(client, admin_headers, fake_tmdb):
    key = _key(client, admin_headers, perms=["media:read"])
    r = client.post(f"{BASE}/command", params={"apikey": key}, json={"name": "MoviesSearch", "movieIds": [1]})
    assert r.status_code == 403


def test_queue_reports_movie_downloads(seerr, fake_tmdb):
    movie = _add(seerr, 990005).json()
    with session_scope() as db:
        db.add(models.DownloadQueueItem(title="Compat", media_type="movie", media_id=movie["id"], release_title="Compat.2021.1080p",
                                        size_bytes=1000, progress=40.0, status="downloading", eta_seconds=3700,
                                        client_item_id="abc123", protocol="torrent"))
    records = seerr.get("/queue", includeEpisode="true").json()["records"]
    rec = next(r for r in records if r["movieId"] == movie["id"])
    assert rec["sizeleft"] == 600 and rec["timeleft"] == "01:01:40"
    assert rec["status"] == "downloading" and rec["downloadId"] == "abc123"


def _with_file(movie_id: int, tmp_path: Path) -> Path:
    video = tmp_path / f"movie-{movie_id}.mkv"
    video.write_bytes(b"x" * 64)
    with session_scope() as db:
        m = db.get(models.Movie, movie_id)
        m.file_path, m.has_file = str(video), True
    return video


def test_delete_keeps_files_unless_opted_in(client, admin_headers, seerr, fake_tmdb, tmp_path):
    kept = _add(seerr, 990006).json()
    video = _with_file(kept["id"], tmp_path)
    assert seerr.delete(f"/movie/{kept['id']}", deleteFiles="true", addImportExclusion="false").status_code == 200
    assert video.exists()
    assert seerr.get(f"/movie/{kept['id']}").status_code == 404

    assert client.put("/api/v1/settings", json={"seerr_allow_file_deletion": True}, headers=admin_headers).status_code == 200
    try:
        recycled = _add(seerr, 990007).json()
        video = _with_file(recycled["id"], tmp_path)
        assert seerr.delete(f"/movie/{recycled['id']}", deleteFiles="true").status_code == 200
        assert not video.exists()  # moved to the recycle bin
    finally:
        client.put("/api/v1/settings", json={"seerr_allow_file_deletion": False}, headers=admin_headers)
