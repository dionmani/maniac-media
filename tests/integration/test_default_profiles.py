"""Default quality profiles: movies and TV shows each get their own profile when one isn't picked."""
from __future__ import annotations

import shutil

import pytest

from backend.db import models
from backend.db.session import session_scope

API = "/api/v1"
COMPAT = "/api/compat/radarr/api/v3"
TITLES = ("Default Profile Movie", "Default Profile Show", "Default Profile Scan", "Default Profile Seerr")


def _profile(client, admin_headers, name: str) -> int:
    r = client.post(f"{API}/quality-profiles", json={"name": name, "items": ["WEBDL-1080p"], "cutoff": "WEBDL-1080p"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def profiles(client, admin_headers, library):
    """Two fresh profiles; settings, library rows and folders are session-wide, so put everything back afterwards."""
    from backend.core.settings import settings_manager

    ids = {"movie": _profile(client, admin_headers, "Roku Movies"), "tv": _profile(client, admin_headers, "Roku TV")}
    yield ids
    # update() skips None values, so clear the defaults by saving a copy
    settings_manager.save(settings_manager.get().model_copy(update={"default_movie_profile_id": None, "default_tv_profile_id": None}))
    for kind, media_type in (("movies", "movie"), ("series", "tv")):
        for item in client.get(f"{API}/{kind}", params={"page_size": 500}, headers=admin_headers).json()["items"]:
            if (item["title"] or "").startswith(TITLES):
                client.delete(f"{API}/{kind}/{item['id']}", headers=admin_headers)
                # deleting media leaves its issues behind, and SQLite hands the id to the next test's item
                with session_scope() as db:
                    db.query(models.LibraryIssue).filter_by(media_type=media_type, media_id=item["id"]).delete()
    for pid in ids.values():
        assert client.delete(f"{API}/quality-profiles/{pid}", headers=admin_headers).status_code == 200
    shutil.rmtree(library["movies"] / "Default Profile Scan (2020)", ignore_errors=True)
    shutil.rmtree(library["tv"] / "Default Profile Scan", ignore_errors=True)


def test_defaults_fall_back_to_lowest_profile(client, admin_headers, profiles):
    lowest = min(p["id"] for p in client.get(f"{API}/quality-profiles", headers=admin_headers).json())
    assert client.get(f"{API}/quality-profiles/defaults", headers=admin_headers).json() == {"movie": lowest, "tv": lowest}


def test_each_type_has_its_own_default_for_new_items(client, admin_headers, profiles):
    r = client.put(f"{API}/quality-profiles/defaults", json={"movie": profiles["movie"]}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["movie"] == profiles["movie"]
    r = client.put(f"{API}/quality-profiles/defaults", json={"tv": profiles["tv"]}, headers=admin_headers)
    assert r.json() == {"movie": profiles["movie"], "tv": profiles["tv"]}  # setting one leaves the other alone
    assert client.put(f"{API}/quality-profiles/defaults", json={"tv": 99999}, headers=admin_headers).status_code == 422

    movie = client.post(f"{API}/movies", json={"title": "Default Profile Movie", "year": 2020}, headers=admin_headers)
    assert movie.status_code == 201, movie.text
    assert movie.json()["quality_profile_id"] == profiles["movie"]
    show = client.post(f"{API}/series", json={"title": "Default Profile Show", "year": 2020}, headers=admin_headers)
    assert show.status_code == 201, show.text
    with session_scope() as db:
        series = db.query(models.Series).filter(models.Series.title == "Default Profile Show").one()
        assert series.quality_profile_id == profiles["tv"]

    # an explicit pick still wins
    picked = client.post(f"{API}/movies", json={"title": "Default Profile Movie 2", "year": 2021, "quality_profile_id": profiles["tv"]},
                         headers=admin_headers)
    assert picked.json()["quality_profile_id"] == profiles["tv"]


def test_default_profile_cannot_be_deleted(client, admin_headers, profiles):
    client.put(f"{API}/quality-profiles/defaults", json={"movie": profiles["movie"]}, headers=admin_headers)
    assert client.delete(f"{API}/quality-profiles/{profiles['movie']}", headers=admin_headers).status_code == 409


def test_scanner_and_seerr_use_the_defaults(client, admin_headers, profiles, library, make_video):
    from backend.services import scanner_service

    client.put(f"{API}/quality-profiles/defaults", json=profiles, headers=admin_headers)
    make_video(library["movies"] / "Default Profile Scan (2020)" / "Default.Profile.Scan.2020.1080p.WEB-DL.mkv")
    make_video(library["tv"] / "Default Profile Scan" / "Season 01" / "Default.Profile.Scan.S01E01.1080p.WEB-DL.mkv")
    scanner_service.scan_movies(incremental=False)
    scanner_service.scan_series(incremental=False)
    with session_scope() as db:
        assert db.query(models.Movie).filter(models.Movie.title == "Default Profile Scan").one().quality_profile_id == profiles["movie"]
        assert db.query(models.Series).filter(models.Series.title == "Default Profile Scan").one().quality_profile_id == profiles["tv"]

    key = client.post(f"{API}/api-keys", json={"name": "Seerr default", "permissions": ["media:read", "media:write"], "allow_query_param": True},
                      headers=admin_headers).json()["api_key"]
    root = client.get(f"{COMPAT}/rootfolder", params={"apikey": key}).json()[0]["path"]
    r = client.post(f"{COMPAT}/movie", params={"apikey": key}, json={
        "title": "Default Profile Seerr", "tmdbId": 12399, "year": 2020, "rootFolderPath": root, "monitored": True,
        "addOptions": {"searchForMovie": False},
    })
    assert r.status_code == 201, r.text
    assert r.json()["qualityProfileId"] == profiles["movie"]
