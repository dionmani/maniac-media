"""Discover: section date rules (domain) and the TMDB discover mapping (against canned responses)."""
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from backend.domain import discover as rules
from backend.domain.types import CastMember, DiscoverItem, MediaMetadata, WatchProvider

TODAY = date(2026, 10, 5)


# ------------------------------------------------------------------ domain rules
def test_section_registry_and_windows():
    assert list(rules.SECTIONS["movie"]) == ["announced", "in_theaters", "streaming"]
    assert rules.is_section("tv", "new_seasons") and not rules.is_section("tv", "streaming")
    assert rules.window("announced", TODAY) == (date(2026, 10, 6), None)
    assert rules.window("streaming", TODAY) == (date(2026, 9, 5), date(2026, 12, 4))
    assert rules.window("in_theaters", TODAY)[1] == TODAY


def test_season_premiere_finds_upcoming_and_binge_dropped_seasons():
    start, end = rules.window("new_seasons", TODAY)
    upcoming = [(0, "2019-05-01"), (1, "2020-01-10"), (2, "2022-03-01"), (3, "2026-10-12")]
    assert rules.season_premiere(upcoming, start, end) == (3, "2026-10-12")
    binge = [(1, "2024-01-01"), (2, "2026-09-28")]  # whole season dropped a week ago
    assert rules.season_premiere(binge, start, end) == (2, "2026-09-28")


def test_season_premiere_ignores_first_seasons_specials_and_other_dates():
    start, end = rules.window("new_seasons", TODAY)
    assert rules.season_premiere([(1, "2026-10-10")], start, end) is None  # a new show, not a returning one
    assert rules.season_premiere([(0, "2026-10-10")], start, end) is None
    assert rules.season_premiere([(2, "2027-02-01"), (3, None)], start, end) is None
    assert rules.season_premiere([(2, "not a date")], start, end) is None


def test_home_release_prefers_digital_within_the_window():
    start, end = rules.window("streaming", TODAY)
    assert rules.home_release("2026-10-20", "2026-11-10", start, end) == ("digital", "2026-10-20")
    assert rules.home_release("2026-01-01", "2026-11-10", start, end) == ("physical", "2026-11-10")
    assert rules.home_release("2026-10-20T00:00:00.000Z", None, start, end) == ("digital", "2026-10-20")
    assert rules.home_release(None, None, start, end) is None


def test_discover_item_to_dict_is_flat_and_light():
    meta = MediaMetadata(media_type="movie", provider="tmdb", title="Arrival", tmdb_id=329865, cast=[CastMember("Amy Adams")])
    data = DiscoverItem(meta, "streaming", "digital", "2026-10-14", watch_providers=[WatchProvider("Netflix", "logo")]).to_dict()
    assert data["title"] == "Arrival" and data["reason"] == "digital" and data["reason_date"] == "2026-10-14"
    assert data["watch_providers"] == [{"name": "Netflix", "logo_url": "logo"}]
    assert not {"cast", "crew", "episodes"} & data.keys()


# ------------------------------------------------------------------ TMDB discover
@pytest.fixture
def tmdb(monkeypatch):
    """A TMDB provider whose HTTP calls are answered from ``routes`` (path -> body or callable)."""
    from backend.core import http
    from backend.plugins.metadata.tmdb import TmdbProvider

    routes: dict = {}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = routes.get(request.url.path.removeprefix("/3"))
        if callable(body):
            body = body(request)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={})

    real_client = http.client
    monkeypatch.setattr(http, "client", lambda timeout=20.0, **kw: real_client(timeout, transport=httpx.MockTransport(handler), **kw))
    return TmdbProvider({"api_key": "k" * 32}), routes, requests


def _page(results):
    """First page carries the rows; later pages are empty."""
    return lambda request: {"results": results if request.url.params.get("page") == "1" else []}


def _run(provider, media_type, section, languages=()):
    return asyncio.run(provider.discover(media_type, section, region="US", languages=list(languages), today=TODAY))


def test_tmdb_streaming_uses_regional_home_release_dates(tmdb):
    provider, routes, requests = tmdb
    routes["/discover/movie"] = _page([
        {"id": 1, "title": "Digital Movie", "release_date": "2026-08-01", "genre_ids": [28], "popularity": 50.0},
        {"id": 2, "title": "Only Abroad", "release_date": "2026-07-01", "popularity": 40.0},
    ])
    routes["/movie/1"] = {
        "id": 1, "title": "Digital Movie", "release_date": "2026-08-01", "imdb_id": "tt1234567", "genres": [{"name": "Action"}],
        "release_dates": {"results": [
            {"iso_3166_1": "US", "release_dates": [
                {"type": 3, "release_date": "2026-08-01T00:00:00.000Z", "certification": "PG-13"},
                {"type": 4, "release_date": "2026-10-14T00:00:00.000Z"},
                {"type": 5, "release_date": "2026-11-20T00:00:00.000Z"},
            ]},
            {"iso_3166_1": "GB", "release_dates": [{"type": 4, "release_date": "2026-09-01T00:00:00.000Z"}]},
        ]},
        "watch/providers": {"results": {"US": {
            "flatrate": [{"provider_name": "Netflix", "logo_path": "/n.png", "display_priority": 1}],
            "buy": [{"provider_name": "Apple TV", "logo_path": "/a.png"}],
        }}},
    }
    routes["/movie/2"] = {"id": 2, "title": "Only Abroad", "release_dates": {"results": [
        {"iso_3166_1": "GB", "release_dates": [{"type": 4, "release_date": "2026-10-01T00:00:00.000Z"}]}]}}

    items = _run(provider, "movie", "streaming")

    assert [i.meta.tmdb_id for i in items] == [1]  # no US home release -> not "coming to streaming" here
    item = items[0]
    assert (item.reason, item.reason_date, item.theatrical_release_date) == ("digital", "2026-10-14", "2026-08-01")
    assert item.meta.physical_release_date == "2026-11-20" and item.meta.certification == "PG-13"
    assert item.meta.imdb_id == "tt1234567" and item.meta.genres == ["Action"]
    assert [p.name for p in item.watch_providers] == ["Netflix"]  # storefronts (buy/rent) are left out
    listing = next(r for r in requests if r.url.path == "/3/discover/movie")
    assert listing.url.params["region"] == "US" and listing.url.params["with_release_type"] == "4|5"
    assert listing.url.params["release_date.gte"] == "2026-09-05" and listing.url.params["release_date.lte"] == "2026-12-04"


def test_tmdb_new_seasons_keeps_only_returning_season_premieres(tmdb):
    provider, routes, requests = tmdb
    routes["/discover/tv"] = _page([
        {"id": 10, "name": "Returning", "first_air_date": "2020-01-01", "popularity": 80.0, "genre_ids": [18]},
        {"id": 11, "name": "Mid Season", "first_air_date": "2025-01-01", "popularity": 70.0},
        {"id": 12, "name": "Late Night Talk", "first_air_date": "2015-01-01", "popularity": 90.0, "genre_ids": [10767]},
    ])
    routes["/tv/10"] = {
        "id": 10, "name": "Returning", "first_air_date": "2020-01-01", "networks": [{"name": "HBO"}], "number_of_seasons": 3,
        "seasons": [{"season_number": 0, "air_date": "2026-10-01"}, {"season_number": 1, "air_date": "2020-01-01"},
                    {"season_number": 3, "air_date": "2026-10-12"}],
        "external_ids": {"tvdb_id": 999, "imdb_id": "tt7654321"},
    }
    routes["/tv/11"] = {"id": 11, "name": "Mid Season", "seasons": [{"season_number": 1, "air_date": "2025-01-01"},
                                                                   {"season_number": 2, "air_date": "2026-06-01"}]}

    items = _run(provider, "tv", "new_seasons")

    assert [i.meta.tmdb_id for i in items] == [10]
    item = items[0]
    assert (item.reason, item.season_number, item.reason_date) == ("season_premiere", 3, "2026-10-12")
    assert item.meta.tvdb_id == 999 and item.meta.imdb_id == "tt7654321" and item.meta.studio == "HBO"
    params = next(r for r in requests if r.url.path == "/3/discover/tv").url.params
    assert params["first_air_date.lte"] == "2026-04-08" and params["air_date.gte"] == "2026-09-21"
    assert params["without_genres"] == "10763,10766,10767"
    assert not any(r.url.path == "/3/tv/12" for r in requests)  # talk shows are dropped before any detail lookup


def test_tmdb_announced_filters_languages_and_sends_no_empty_params(tmdb):
    provider, routes, requests = tmdb
    routes["/discover/movie"] = _page([
        {"id": 5, "title": "Big Sequel", "release_date": "2027-05-01", "original_language": "en", "popularity": 99.0},
        {"id": 6, "title": "Foreign Film", "release_date": "2027-01-01", "original_language": "ko", "popularity": 90.0},
    ])

    items = _run(provider, "movie", "announced", languages=["en"])

    assert [(i.meta.title, i.reason, i.reason_date, i.meta.status) for i in items] == [("Big Sequel", "announced", "2027-05-01", "announced")]
    params = requests[0].url.params
    assert params["with_original_language"] == "en" and params["primary_release_date.gte"] == "2026-10-06"
    assert "primary_release_date.lte" not in params
    assert not any(r.url.path.startswith("/3/movie/") for r in requests)  # no per-title lookups for this section


def test_tmdb_in_theaters_falls_back_to_the_list_row_when_details_fail(tmdb):
    provider, routes, _ = tmdb
    routes["/movie/now_playing"] = _page([{"id": 7, "title": "Opening Weekend", "release_date": "2026-10-02", "popularity": 60.0}])
    # no "/movie/7" route -> 404 -> the list row is all we have

    items = _run(provider, "movie", "in_theaters")

    assert [(i.meta.title, i.reason, i.reason_date) for i in items] == [("Opening Weekend", "in_theaters", "2026-10-02")]


def test_tmdb_several_languages_are_filtered_locally(tmdb):
    provider, routes, requests = tmdb
    routes["/discover/movie"] = _page([
        {"id": 5, "title": "English", "release_date": "2027-05-01", "original_language": "en"},
        {"id": 6, "title": "Spanish", "release_date": "2027-05-01", "original_language": "es"},
        {"id": 7, "title": "Korean", "release_date": "2027-05-01", "original_language": "ko"},
    ])

    items = _run(provider, "movie", "announced", languages=["en", "es"])

    assert [i.meta.title for i in items] == ["English", "Spanish"]
    assert "with_original_language" not in requests[0].url.params  # TMDB documents no OR-list for this filter
