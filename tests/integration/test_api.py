"""API integration tests against a real (temporary) SQLite database."""
from __future__ import annotations

import pytest

from backend.domain.types import EpisodeMetadata, MediaMetadata

API = "/api/v1"


def test_liveness(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_everything_requires_auth(client, database):
    for path in ("/movies", "/series", "/queue", "/settings", "/dashboard", "/system/audit"):
        assert client.get(API + path).status_code == 401


def test_setup_only_once(client, admin_headers):
    r = client.post(f"{API}/auth/setup", json={"username": "evil", "password": "Another-Passw0rd"})
    assert r.status_code == 409
    assert client.get(f"{API}/auth/status").json() == {"setup_required": False}


def test_openapi_is_versioned(client):
    spec = client.get("/api/openapi.json").json()
    assert all(p.startswith("/api/v1") or p in ("/api/health",) for p in spec["paths"])


def test_rbac_and_audit(client, admin_headers):
    r = client.post(f"{API}/users", json={"username": "reader", "password": "Reader-Passw0rd", "role": "read_only"}, headers=admin_headers)
    assert r.status_code == 201
    token = client.post(f"{API}/auth/login", json={"username": "reader", "password": "Reader-Passw0rd"}).json()["access_token"]
    ro = {"Authorization": f"Bearer {token}"}
    assert client.get(f"{API}/movies", headers=ro).status_code == 200
    assert client.post(f"{API}/indexers", json={"name": "x", "implementation": "demo"}, headers=ro).status_code == 403
    audit = client.get(f"{API}/system/audit", params={"category": "security"}, headers=admin_headers).json()
    assert any(a["action"] == "permission.denied" for a in audit["items"])


def test_password_change_revokes_tokens(client, admin_headers):
    client.post(f"{API}/users", json={"username": "temp", "password": "Temp-Passw0rd!", "role": "power_user"}, headers=admin_headers)
    token = client.post(f"{API}/auth/login", json={"username": "temp", "password": "Temp-Passw0rd!"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.post(f"{API}/auth/password", json={"current_password": "Temp-Passw0rd!", "new_password": "Temp-Passw0rd-2"}, headers=h).status_code == 200
    assert client.get(f"{API}/auth/me", headers=h).status_code == 401


def test_lockout_after_failed_logins(client, admin_headers):
    client.post(f"{API}/users", json={"username": "victim", "password": "Victim-Passw0rd", "role": "read_only"}, headers=admin_headers)
    from backend.core.ratelimit import api_limiter

    for _ in range(5):
        client.post(f"{API}/auth/login", json={"username": "victim", "password": "nope"})
    api_limiter.reset()
    r = client.post(f"{API}/auth/login", json={"username": "victim", "password": "Victim-Passw0rd"})
    assert r.status_code == 401 and "locked" in r.json()["detail"]


def test_secrets_never_returned(client, admin_headers):
    r = client.post(f"{API}/indexers", json={"name": "Prowlarr", "implementation": "torznab", "url": "http://localhost:9696/1",
                                              "api_key": "SUPERSECRETKEY", "enabled": False}, headers=admin_headers)
    assert r.status_code == 201 and r.json()["has_api_key"] is True
    assert "SUPERSECRETKEY" not in client.get(f"{API}/indexers", headers=admin_headers).text
    from sqlalchemy import text

    from backend.db.session import db_manager

    with db_manager.engine.connect() as conn:
        stored = conn.execute(text("SELECT api_key FROM indexers WHERE name='Prowlarr'")).scalar()
    assert stored.startswith("v2:") and "SUPERSECRETKEY" not in stored
    assert "SUPERSECRETKEY" not in client.get(f"{API}/settings", headers=admin_headers).text


def test_movie_crud_pagination_filtering(client, admin_headers, monkeypatch):
    from backend.services import metadata_service

    async def fake_fetch(**kw):
        return MediaMetadata(media_type="movie", provider="test", title="Arrival", year=2016, imdb_id="tt2543164", genres=["Sci-Fi"], runtime_minutes=116)

    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)
    r = client.post(f"{API}/movies", json={"title": "Arrival", "year": 2016, "imdb_id": "tt2543164"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    movie = r.json()
    assert movie["genres"] == "Sci-Fi" and movie["path"].endswith("Arrival (2016)")
    assert client.post(f"{API}/movies", json={"title": "Arrival", "imdb_id": "tt2543164"}, headers=admin_headers).status_code == 409

    page = client.get(f"{API}/movies", params={"search": "arriv", "page_size": 1, "sort": "year", "order": "desc"}, headers=admin_headers).json()
    assert page["total"] >= 1 and page["page_size"] == 1 and page["items"][0]["title"] == "Arrival"
    assert client.get(f"{API}/movies", params={"has_file": True, "search": "arrival"}, headers=admin_headers).json()["total"] == 0
    assert client.get(f"{API}/movies", params={"page_size": 9999}, headers=admin_headers).status_code == 422

    assert client.patch(f"{API}/movies/{movie['id']}", json={"monitored": False}, headers=admin_headers).json()["monitored"] is False
    assert client.delete(f"{API}/movies/{movie['id']}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/movies/{movie['id']}", headers=admin_headers).status_code == 404


def test_series_add_with_episodes_and_monitoring(client, admin_headers, monkeypatch):
    from backend.services import metadata_service

    async def fake_series(**kw):
        return MediaMetadata(media_type="tv", provider="test", title="Severance", year=2022, tvmaze_id=44933, season_count=2,
                             episodes=[EpisodeMetadata(s, e, f"Ep {s}x{e}", air_date="2022-02-18") for s in (1, 2) for e in (1, 2, 3)])

    monkeypatch.setattr(metadata_service, "fetch_series", fake_series)
    r = client.post(f"{API}/series", json={"title": "Severance", "tvmaze_id": 44933, "monitor": "latest"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["episode_count"] == 6
    seasons = {x["season_number"]: x for x in s["seasons"]}
    assert seasons[1]["monitored"] is False and seasons[2]["monitored"] is True

    assert client.put(f"{API}/series/{s['id']}/seasons/1/monitor", json={"monitored": True}, headers=admin_headers).status_code == 200
    assert client.post(f"{API}/series/{s['id']}/seasons", json={"season_number": 3, "episode_count": 2}, headers=admin_headers).status_code == 201
    assert client.post(f"{API}/series/{s['id']}/episodes", json={"season_number": 3, "episode_number": 3, "title": "Bonus"}, headers=admin_headers).status_code == 201
    detail = client.get(f"{API}/series/{s['id']}", headers=admin_headers).json()
    assert detail["episode_count"] == 9
    listing = client.get(f"{API}/series", params={"search": "sever"}, headers=admin_headers).json()
    assert listing["items"][0]["episode_count"] == 9 and "seasons" not in listing["items"][0]


def test_naming_validation(client, admin_headers):
    assert client.put(f"{API}/settings", json={"movie_format": "{Title.__class__}"}, headers=admin_headers).status_code == 422
    preview = client.post(f"{API}/settings/naming/preview", json={"template": "{Title} ({Year})", "kind": "movie"}, headers=admin_headers).json()
    assert preview["example"] == "Avatar (2009)"


def test_quality_profile_validation(client, admin_headers):
    bad = client.post(f"{API}/quality-profiles", json={"name": "x", "items": ["Bluray-1080p"], "cutoff": "Remux-2160p"}, headers=admin_headers)
    assert bad.status_code == 422
    good = client.post(f"{API}/quality-profiles", json={"name": "4K", "items": ["Remux-2160p", "Bluray-2160p"], "cutoff": "Bluray-2160p"}, headers=admin_headers)
    assert good.status_code == 201


def test_dashboard_and_health(client, admin_headers):
    d = client.get(f"{API}/dashboard", headers=admin_headers).json()
    for key in ("movies", "series", "missing_total", "downloads", "storage", "failed_imports", "recent_activity"):
        assert key in d
    checks = client.get(f"{API}/system/health", headers=admin_headers).json()
    assert any(c["source"] == "database" and c["status"] == "ok" for c in checks)


def test_rate_limiting(client, admin_headers):
    from backend.core.ratelimit import api_limiter

    api_limiter.reset()
    codes = [client.post(f"{API}/auth/login", json={"username": "nobody", "password": "x"}).status_code for _ in range(12)]
    api_limiter.reset()
    assert 429 in codes


def test_websocket_requires_token(client, admin_headers):
    token = admin_headers["Authorization"].split()[1]
    with client.websocket_connect(f"{API}/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(Exception):
        with client.websocket_connect(f"{API}/ws?token=bogus") as ws:
            ws.receive_json()
