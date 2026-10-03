"""Sonarr v3 compat API, exercised the way Seerr calls it (?apikey=, camelCase bodies)."""
import pytest
from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import EpisodeMetadata, MediaMetadata
from backend.services import metadata_service

BASE = "/api/compat/sonarr/api/v3"
SEERR_PERMS = ["media:read", "media:write", "media:delete", "search:execute", "queue:read"]


@pytest.fixture
def seerr(client, admin_headers):
    r = client.post("/api/v1/api-keys", json={"name": "Seerr TV", "permissions": SEERR_PERMS, "allow_query_param": True}, headers=admin_headers)
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]

    class Seerr:
        def get(self, path, **params):
            return client.get(f"{BASE}{path}", params={"apikey": key, **params})

        def post(self, path, body):
            return client.post(f"{BASE}{path}", params={"apikey": key}, json=body)

        def put(self, path, body):
            return client.put(f"{BASE}{path}", params={"apikey": key}, json=body)

        def delete(self, path, **params):
            return client.delete(f"{BASE}{path}", params={"apikey": key, **params})

    return Seerr()


@pytest.fixture
def fake_tvdb(monkeypatch):
    """Shows with tvdb ids >= 880000 exist: two seasons of three episodes, the last one unaired.
    The TVmaze id is the TVDB id minus 800000, so a library row can be matched by it."""
    async def fetch_series(*, tvdb_id=None, tmdb_id=None, tvmaze_id=None, imdb_id=None, title=None, year=None):
        if not tvdb_id or tvdb_id < 880000:
            return None
        episodes = [EpisodeMetadata(season_number=s, episode_number=e, title=f"S{s}E{e}", air_date=f"2020-01-0{e}")
                    for s in (1, 2) for e in (1, 2, 3)]
        episodes[-1].air_date = "2999-01-01"
        return MediaMetadata(media_type="tv", provider="tvmaze", title=f"Compat Show {tvdb_id}", year=2020, tvdb_id=tvdb_id,
                             tvmaze_id=tvdb_id - 800000, season_count=2, episodes=episodes,
                             poster_url="https://static.tvmaze.com/x.jpg")

    monkeypatch.setattr(metadata_service, "fetch_series", fetch_series)


def _request(seerr, tvdb_id: int, seasons: list[int], **extra):
    """What Seerr's addSeries does for a show not in the library: look up, flip requested seasons on, post."""
    looked = seerr.get("/series/lookup", term=f"tvdb:{tvdb_id}").json()
    assert looked and "id" not in looked[0]
    profile = seerr.get("/qualityProfile").json()[0]["id"]
    root = seerr.get("/rootfolder").json()[0]["path"]
    body = {"tvdbId": tvdb_id, "title": looked[0]["title"], "qualityProfileId": profile, "languageProfileId": None,
            "seasons": [{"seasonNumber": s["seasonNumber"], "monitored": s["seasonNumber"] in seasons} for s in looked[0]["seasons"]],
            "tags": [], "seasonFolder": True, "monitored": True, "monitorNewItems": "all", "rootFolderPath": root,
            "seriesType": "standard", "addOptions": {"ignoreEpisodesWithFiles": True, "searchForMissingEpisodes": False}, **extra}
    return seerr.post("/series", body)


def _season(resource: dict, number: int) -> dict:
    return next(s for s in resource["seasons"] if s["seasonNumber"] == number)


def test_connection_test_calls(seerr):
    status = seerr.get("/system/status").json()
    assert status["version"].startswith("4.")  # >= 4 so Seerr skips language profiles
    assert status["urlBase"] == "/api/compat/sonarr"
    from backend.core.settings import get_settings

    assert seerr.get("/rootfolder").json()[0]["path"] == get_settings().media_root_tv
    assert seerr.get("/qualityProfile").json()
    assert seerr.get("/languageprofile").json()[0]["id"] == 1


def test_request_some_seasons_then_more(seerr, fake_tvdb):
    r = _request(seerr, 880001, seasons=[2])
    assert r.status_code == 201, r.text
    show = r.json()
    assert show["id"] and show["tvdbId"] == 880001 and show["monitorNewItems"] == "all"
    assert _season(show, 1)["monitored"] is False and _season(show, 2)["monitored"] is True
    assert _season(show, 1)["statistics"]["totalEpisodeCount"] == 3
    assert _season(show, 2)["statistics"]["episodeCount"] == 2  # monitored + aired; the unaired one doesn't count

    # once added, Seerr's lookup and tvdbId filter see the library id
    assert seerr.get("/series/lookup", term="tvdb:880001").json()[0]["id"] == show["id"]
    assert [s["id"] for s in seerr.get("/series", tvdbId=880001).json()] == [show["id"]]
    assert any(s["id"] == show["id"] for s in seerr.get("/series").json())

    # second request for season 1: PUT the series back with the flag flipped, then re-monitor episodes
    show["seasons"] = [{**s, "monitored": s["seasonNumber"] in (1, 2)} for s in show["seasons"]]
    updated = seerr.put("/series", show)
    assert updated.status_code == 202 and _season(updated.json(), 1)["monitored"] is True
    episodes = seerr.get("/episode", seriesId=show["id"]).json()
    assert len(episodes) == 6 and all(e["monitored"] for e in episodes if e["seasonNumber"] == 1)
    assert seerr.put("/episode/monitor", {"episodeIds": [episodes[0]["id"]], "monitored": True}).status_code == 202

    # files arrive: availability is per-season episodeFileCount vs totalEpisodeCount
    with session_scope() as db:
        for ep in db.scalars(select(models.Episode).where(models.Episode.series_id == show["id"], models.Episode.season_number == 1)):
            ep.has_file, ep.file_size_bytes = True, 100
    stats = _season(seerr.get(f"/series/{show['id']}").json(), 1)["statistics"]
    assert stats["episodeFileCount"] == stats["totalEpisodeCount"] == 3 and stats["sizeOnDisk"] == 300


def test_duplicate_add_is_rejected(seerr, fake_tvdb):
    assert _request(seerr, 880003, seasons=[1]).status_code == 201
    dup = seerr.post("/series", {"tvdbId": 880003, "title": "x", "seasons": []})
    assert dup.status_code == 400 and dup.json()[0]["propertyName"] == "TvdbId"


def test_library_show_without_tvdb_id_is_matched_not_duplicated(seerr, fake_tvdb):
    with session_scope() as db:
        series = models.Series(title="Already Here", year=2020, tvmaze_id=880004 - 800000, monitored=True)
        db.add(series)
        db.flush()
        existing_id = series.id
    looked = seerr.get("/series/lookup", term="tvdb:880004").json()
    assert looked[0]["id"] == existing_id
    with session_scope() as db:
        assert db.get(models.Series, existing_id).tvdb_id == 880004  # backfilled, so Seerr's sync can match it


def test_lookup_without_metadata_returns_nothing(seerr, fake_tvdb):
    # no season list to offer: Seerr reports "Series not found" and the request can be retried later
    assert seerr.get("/series/lookup", term="tvdb:123").json() == []


def test_search_commands_enqueue_season_searches(seerr, fake_tvdb):
    show = _request(seerr, 880005, seasons=[1, 2]).json()
    r = seerr.post("/command", {"name": "MissingEpisodeSearch", "seriesId": show["id"]})
    assert r.status_code == 201 and r.json()["status"] == "queued"
    episode = seerr.get("/episode", seriesId=show["id"]).json()[0]
    assert seerr.post("/command", {"name": "EpisodeSearch", "episodeIds": [episode["id"]]}).status_code == 201
    with session_scope() as db:
        payloads = [j.payload or {} for j in db.scalars(select(models.Job).where(models.Job.name == "automatic_search"))]
    mine = [p for p in payloads if p.get("media_type") == "tv" and p.get("media_id") == show["id"]]
    assert {p["season"] for p in mine if not p.get("episodes")} == {1, 2}
    assert any(p.get("episodes") == [episode["episodeNumber"]] for p in mine)
    assert seerr.post("/command", {"name": "RefreshMonitoredDownloads"}).json()["status"] == "completed"


def test_queue_lists_a_download_once_per_episode(seerr, fake_tvdb):
    show = _request(seerr, 880006, seasons=[1]).json()
    with session_scope() as db:
        db.add(models.DownloadQueueItem(title="Compat Show", media_type="tv", media_id=show["id"], season_num=1, episode_numbers=[1, 2],
                                        release_title="Compat.Show.S01E01E02.1080p", size_bytes=2000, progress=50.0,
                                        status="downloading", eta_seconds=60, client_item_id="tv1", protocol="torrent"))
    records = [r for r in seerr.get("/queue", includeEpisode="true").json()["records"] if r["seriesId"] == show["id"]]
    assert sorted(r["episode"]["episodeNumber"] for r in records) == [1, 2]
    assert all(r["sizeleft"] == 1000 and r["timeleft"] == "00:01:00" and r["episodeId"] for r in records)


def test_remove_keeps_files_by_default(seerr, fake_tvdb):
    show = _request(seerr, 880007, seasons=[1]).json()
    assert seerr.delete(f"/series/{show['id']}", deleteFiles="true", addImportExclusion="false").status_code == 200
    assert seerr.get(f"/series/{show['id']}").status_code == 404
