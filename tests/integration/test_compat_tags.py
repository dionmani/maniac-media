"""Request attribution: Seerr's "Tag Requests" tags flowing through the compat API into history and the UI."""
import pytest

from backend.domain.types import EpisodeMetadata, MediaMetadata
from backend.services import metadata_service

API = "/api/v1"
RADARR = "/api/compat/radarr/api/v3"
SONARR = "/api/compat/sonarr/api/v3"


@pytest.fixture
def seerr(client, admin_headers):
    r = client.post(f"{API}/api-keys", headers=admin_headers, json={
        "name": "Seerr", "allow_query_param": True,
        "permissions": ["media:read", "media:write", "media:delete", "search:execute", "queue:read"],
    })
    key = r.json()["api_key"]

    class Seerr:
        def get(self, path, **params):
            return client.get(path, params={"apikey": key, **params})

        def post(self, path, body):
            return client.post(path, params={"apikey": key}, json=body)

        def put(self, path, body):
            return client.put(path, params={"apikey": key}, json=body)

        def user_tag(self, base: str, seerr_user_id: int, name: str) -> int:
            """Seerr's MediaRequestSubscriber: find '<id>-' (or legacy '<id> - ') among tags, else create it."""
            for tag in self.get(f"{base}/tag").json():
                if tag["label"].startswith((f"{seerr_user_id} - ", f"{seerr_user_id}-")):
                    return tag["id"]
            return self.post(f"{base}/tag", {"label": f"{seerr_user_id}-{name}"}).json()["id"]

    return Seerr()


@pytest.fixture
def fake_metadata(monkeypatch):
    async def fetch_movie(*, tmdb_id=None, imdb_id=None, title=None, year=None):
        return MediaMetadata(media_type="movie", provider="tmdb", title=f"Tagged Movie {tmdb_id}", year=2022, tmdb_id=tmdb_id)

    async def fetch_series(*, tvdb_id=None, tmdb_id=None, tvmaze_id=None, imdb_id=None, title=None, year=None):
        episodes = [EpisodeMetadata(season_number=s, episode_number=1, title="Pilot", air_date="2020-01-01") for s in (1, 2)]
        return MediaMetadata(media_type="tv", provider="tvmaze", title=f"Tagged Show {tvdb_id}", year=2020, tvdb_id=tvdb_id,
                             tvmaze_id=tvdb_id - 700000, season_count=2, episodes=episodes)

    monkeypatch.setattr(metadata_service, "fetch_movie", fetch_movie)
    monkeypatch.setattr(metadata_service, "fetch_series", fetch_series)


def _history(client, admin_headers, media_type, media_id):
    items = client.get(f"{API}/history", params={"media_type": media_type, "media_id": media_id}, headers=admin_headers).json()["items"]
    return [h["details"] for h in items if h["event_type"] == "requested"]


def test_tags_are_stored_and_idempotent(seerr):
    alice = seerr.user_tag(RADARR, 41, "alice")
    assert seerr.user_tag(RADARR, 41, "alice") == alice  # found again instead of re-created
    assert seerr.user_tag(SONARR, 41, "alice") == alice  # one tag namespace for both facades
    assert {"id": alice, "label": "41-alice"} in seerr.get(f"{RADARR}/tag").json()
    renamed = seerr.put(f"{RADARR}/tag/{alice}", {"id": alice, "label": "41-alice_smith"})
    assert renamed.status_code == 202 and renamed.json()["label"] == "41-alice_smith"


def test_movie_request_is_attributed(client, admin_headers, seerr, fake_metadata):
    bob = seerr.user_tag(RADARR, 42, "bob")
    root = seerr.get(f"{RADARR}/rootfolder").json()[0]["path"]
    movie = seerr.post(f"{RADARR}/movie", {"tmdbId": 770001, "title": "x", "qualityProfileId": 1, "rootFolderPath": root,
                                           "monitored": True, "tags": [bob, 999999], "addOptions": {"searchForMovie": False}}).json()
    assert movie["tags"] == [bob]  # unknown tag ids are dropped

    detail = client.get(f"{API}/movies/{movie['id']}", headers=admin_headers).json()
    assert detail["requested_by"] == ["bob"]
    assert _history(client, admin_headers, "movie", movie["id"]) == ["Requested by bob via Seerr"]

    # Seerr re-sends the same tags when it re-monitors: no second entry
    seerr.put(f"{RADARR}/movie", {**movie, "monitored": True})
    assert len(_history(client, admin_headers, "movie", movie["id"])) == 1


def test_show_requests_are_attributed_per_season(client, admin_headers, seerr, fake_metadata):
    carol = seerr.user_tag(SONARR, 43, "carol")
    dave = seerr.user_tag(SONARR, 44, "dave")
    root = seerr.get(f"{SONARR}/rootfolder").json()[0]["path"]
    show = seerr.post(f"{SONARR}/series", {
        "tvdbId": 770002, "title": "x", "qualityProfileId": 1, "rootFolderPath": root, "monitored": True, "tags": [carol],
        "seasons": [{"seasonNumber": 1, "monitored": False}, {"seasonNumber": 2, "monitored": True}],
    }).json()
    assert _history(client, admin_headers, "tv", show["id"]) == ["Requested by carol via Seerr (season 2)"]

    # dave asks for season 1: Seerr PUTs the show back with season 1 on and the tags merged
    show["seasons"] = [{**s, "monitored": True} for s in show["seasons"]]
    show["tags"] = [carol, dave]
    seerr.put(f"{SONARR}/series", show)
    assert "Requested by dave via Seerr (season 1)" in _history(client, admin_headers, "tv", show["id"])
    assert client.get(f"{API}/series/{show['id']}", headers=admin_headers).json()["requested_by"] == ["carol", "dave"]
