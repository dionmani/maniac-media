"""Discover API: sections from a (fake) provider, library annotation, exclusions, caching and first-seen tracking."""
from __future__ import annotations

import pytest

from backend.domain.types import DiscoverItem, MediaMetadata
from backend.plugins.base import PluginError

API = "/api/v1"


def _movie(tmdb_id: int, title: str, **kw) -> MediaMetadata:
    return MediaMetadata(media_type="movie", provider="fake", title=title, year=2026, tmdb_id=tmdb_id, **kw)


class FakeDiscover:
    key = "fake"
    name = "Fake"
    supports_discover = True

    def __init__(self, sections: dict[tuple[str, str], list[DiscoverItem] | Exception]):
        self.sections = sections
        self.calls = 0

    async def discover(self, media_type, section, *, region, languages, today):
        self.calls += 1
        result = self.sections.get((media_type, section), [])
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_provider(monkeypatch):
    from backend.services import discover_service, metadata_service

    def install(sections):
        provider = FakeDiscover(sections)
        monkeypatch.setattr(metadata_service, "providers", lambda media_type: [provider])
        return provider

    discover_service.invalidate()
    yield install
    discover_service.invalidate()


def _section(body: dict, key: str) -> dict:
    return next(s for s in body["sections"] if s["key"] == key)


def test_discover_without_a_capable_provider_is_unconfigured(client, admin_headers, monkeypatch):
    from backend.services import discover_service, metadata_service

    monkeypatch.setattr(metadata_service, "providers", lambda media_type: [])
    discover_service.invalidate()
    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    assert body["configured"] is False and body["sections"] == []


def test_discover_movie_sections_library_flags_and_exclusions(client, admin_headers, fake_provider, monkeypatch):
    from backend.services import metadata_service

    provider = fake_provider({
        ("movie", "announced"): [DiscoverItem(_movie(9001, "Sequel"), "announced", "announced", "2027-05-01"),
                                 DiscoverItem(_movie(9002, "Original"), "announced", "announced", "2027-06-01")],
        ("movie", "in_theaters"): PluginError("TMDB error 503"),
        ("movie", "streaming"): [DiscoverItem(_movie(9003, "Home Release", imdb_id="tt9000003"), "streaming", "digital", "2026-10-14")],
    })

    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    assert body["configured"] is True and body["region"] == "US"
    assert [s["key"] for s in body["sections"]] == ["announced", "in_theaters", "streaming"]
    assert _section(body, "in_theaters")["error"] == "TMDB error 503"  # one failing section doesn't sink the page
    announced = _section(body, "announced")
    assert announced["total"] == 2 and [i["tmdb_id"] for i in announced["items"]] == [9001, 9002]
    assert all(i["first_seen_at"] and i["in_library"] is False for i in announced["items"])
    first_seen = announced["items"][0]["first_seen_at"]

    # served from cache: the provider isn't asked again (the failed section is retried)
    calls = provider.calls
    limited = client.get(f"{API}/discover/movie", params={"limit": 1}, headers=admin_headers).json()
    assert provider.calls == calls + 1
    assert len(_section(limited, "announced")["items"]) == 1 and _section(limited, "announced")["total"] == 2

    # adding a title flags it as in the library (matched by TMDB id, or IMDb id)
    async def fake_fetch(**kw):
        if kw.get("tmdb_id") == 9001:
            return _movie(9001, "Sequel")
        return MediaMetadata(media_type="movie", provider="fake", title="Home Release", year=2026, imdb_id="tt9000003")

    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)
    added = client.post(f"{API}/movies", json={"title": "Sequel", "tmdb_id": 9001}, headers=admin_headers).json()
    client.post(f"{API}/movies", json={"title": "Home Release", "imdb_id": "tt9000003"}, headers=admin_headers)
    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    sequel = _section(body, "announced")["items"][0]
    assert sequel["in_library"] is True and sequel["library_id"] == added["id"]
    assert _section(body, "streaming")["items"][0]["in_library"] is True

    # "not interested" hides a title everywhere until it is restored
    hide = {"media_type": "movie", "tmdb_id": 9002, "title": "Original", "year": 2027}
    r = client.post(f"{API}/discover/exclusions", json=hide, headers=admin_headers)
    assert r.status_code == 201
    assert client.post(f"{API}/discover/exclusions", json=hide, headers=admin_headers).json()["id"] == r.json()["id"]  # idempotent
    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    assert [i["tmdb_id"] for i in _section(body, "announced")["items"]] == [9001]
    hidden = client.get(f"{API}/discover/exclusions", params={"media_type": "movie"}, headers=admin_headers).json()
    assert [(h["tmdb_id"], h["title"]) for h in hidden] == [(9002, "Original")]
    assert client.delete(f"{API}/discover/exclusions/movie/9002", headers=admin_headers).status_code == 200
    assert client.delete(f"{API}/discover/exclusions/movie/9002", headers=admin_headers).status_code == 404
    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    assert [i["tmdb_id"] for i in _section(body, "announced")["items"]] == [9001, 9002]

    # a refresh re-fetches, but a title keeps the date it was first seen
    calls = provider.calls
    assert client.post(f"{API}/discover/movie/refresh", headers=admin_headers).status_code == 200
    body = client.get(f"{API}/discover/movie", headers=admin_headers).json()
    assert provider.calls == calls + 3
    assert _section(body, "announced")["items"][0]["first_seen_at"] == first_seen


def test_discover_single_section_and_validation(client, admin_headers, fake_provider):
    fake_provider({("movie", "streaming"): [DiscoverItem(_movie(9101, "Streamer"), "streaming", "digital", "2026-10-20")]})
    body = client.get(f"{API}/discover/movie/streaming", headers=admin_headers).json()
    assert [s["key"] for s in body["sections"]] == ["streaming"] and body["sections"][0]["items"][0]["title"] == "Streamer"
    assert client.get(f"{API}/discover/movie/new_series", headers=admin_headers).status_code == 404
    assert client.get(f"{API}/discover/books", headers=admin_headers).status_code == 422


def test_discover_tv_matches_library_series_by_tvdb_id(client, admin_headers, fake_provider, monkeypatch):
    from backend.services import metadata_service

    show = MediaMetadata(media_type="tv", provider="fake", title="Returning Show", year=2020, tmdb_id=7001, tvdb_id=8001)
    fake_provider({("tv", "new_seasons"): [DiscoverItem(show, "new_seasons", "season_premiere", "2026-10-12", season_number=3)]})

    async def fake_series(**kw):  # the library copy only knows the TVDB id
        return MediaMetadata(media_type="tv", provider="fake", title="Returning Show", year=2020, tvdb_id=8001, season_count=1)

    monkeypatch.setattr(metadata_service, "fetch_series", fake_series)
    assert client.post(f"{API}/series", json={"title": "Returning Show", "tvdb_id": 8001}, headers=admin_headers).status_code == 201
    item = _section(client.get(f"{API}/discover/tv", headers=admin_headers).json(), "new_seasons")["items"][0]
    assert item["in_library"] is True and item["season_number"] == 3 and item["reason"] == "season_premiere"


def test_add_series_rejects_a_duplicate_tmdb_id(client, admin_headers, monkeypatch):
    from backend.services import metadata_service

    async def fake_series(**kw):  # a show TMDB knows that has no TVDB/TVmaze id
        return MediaMetadata(media_type="tv", provider="fake", title="TMDB Only Show", year=2026, tmdb_id=7100, season_count=1)

    monkeypatch.setattr(metadata_service, "fetch_series", fake_series)
    assert client.post(f"{API}/series", json={"title": "TMDB Only Show", "tmdb_id": 7100}, headers=admin_headers).status_code == 201
    assert client.post(f"{API}/series", json={"title": "TMDB Only Show", "tmdb_id": 7100}, headers=admin_headers).status_code == 409


def test_read_only_users_can_browse_but_not_hide(client, admin_headers, fake_provider):
    from backend.core.ratelimit import api_limiter

    fake_provider({})
    client.post(f"{API}/users", json={"username": "discover_reader", "password": "Reader-Passw0rd", "role": "read_only"}, headers=admin_headers)
    api_limiter.reset()
    token = client.post(f"{API}/auth/login", json={"username": "discover_reader", "password": "Reader-Passw0rd"}).json()["access_token"]
    ro = {"Authorization": f"Bearer {token}"}
    assert client.get(f"{API}/discover/tv", headers=ro).status_code == 200
    hide = {"media_type": "tv", "tmdb_id": 1, "title": "Anything"}
    assert client.post(f"{API}/discover/exclusions", json=hide, headers=ro).status_code == 403
    assert client.post(f"{API}/discover/tv/refresh", headers=ro).status_code == 403
