"""TheTVDB v4 – canonical TV numbering. Requires an API key (and optional subscriber PIN)."""
from __future__ import annotations

import time

from backend.core import http
from backend.domain.types import EpisodeMetadata, MediaMetadata
from backend.plugins.base import FieldSpec, MetadataProvider, PluginError
from backend.plugins.registry import register

BASE = "https://api4.thetvdb.com/v4"
_token_cache: dict[str, tuple[str, float]] = {}


def _year(v) -> int | None:
    s = str(v or "")
    return int(s[:4]) if s[:4].isdigit() else None


@register
class TvdbProvider(MetadataProvider):
    key = "tvdb"
    name = "TheTVDB"
    description = "TheTVDB v4 API. Authoritative season/episode numbering."
    requires_api_key = True
    fields = [
        FieldSpec("api_key", "API key", "password", required=True, secret=True),
        FieldSpec("pin", "Subscriber PIN", "password", secret=True, help="Only for user-supported keys"),
    ]

    async def _token(self, c) -> str:
        key = self.setting("api_key")
        if not key:
            raise PluginError("TVDB API key not configured")
        cached = _token_cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
        body = {"apikey": key}
        if self.setting("pin"):
            body["pin"] = self.setting("pin")
        resp = await http.request(c, "POST", f"{BASE}/login", json=body, raise_for_status=False)
        if resp.status_code != 200:
            raise PluginError("TVDB rejected the API key")
        token = resp.json()["data"]["token"]
        _token_cache[key] = (token, time.time() + 25 * 24 * 3600)
        return token

    async def _get(self, path: str, **params) -> dict:
        async with http.client(timeout=15) as c:
            token = await self._token(c)
            resp = await http.request(c, "GET", f"{BASE}{path}", params=params, headers={"Authorization": f"Bearer {token}"}, raise_for_status=False)
            if resp.status_code == 404:
                return {}
            if resp.status_code >= 400:
                raise PluginError(f"TVDB error {resp.status_code}")
            return resp.json().get("data") or {}

    def _from_search(self, r: dict, media_type: str) -> MediaMetadata:
        remote = {x.get("sourceName"): x.get("id") for x in r.get("remote_ids") or []}
        tvdb = r.get("tvdb_id") or r.get("id", "").split("-")[-1]
        return MediaMetadata(
            media_type=media_type,
            provider=self.key,
            title=r.get("name") or "",
            year=_year(r.get("year") or r.get("first_air_time")),
            overview=r.get("overview"),
            poster_url=r.get("image_url") or r.get("thumbnail"),
            studio=r.get("network"),
            genres=r.get("genres") or [],
            tvdb_id=int(tvdb) if str(tvdb).isdigit() else None,
            imdb_id=remote.get("IMDB"),
            tmdb_id=int(remote["TheMovieDB.com"]) if str(remote.get("TheMovieDB.com", "")).isdigit() else None,
        )

    async def search_series(self, query: str) -> list[MediaMetadata]:
        data = await self._get("/search", query=query, type="series")
        return [self._from_search(r, "tv") for r in data or []]

    async def search_movies(self, query: str, year: int | None = None) -> list[MediaMetadata]:
        params = {"query": query, "type": "movie"}
        if year:
            params["year"] = year
        data = await self._get("/search", **params)
        return [self._from_search(r, "movie") for r in data or []]

    async def get_series(self, *, tvdb_id=None, tmdb_id=None, tvmaze_id=None, imdb_id=None) -> MediaMetadata | None:
        if not tvdb_id:
            if not imdb_id:
                return None
            found = await self._get(f"/search/remoteid/{imdb_id}")
            series = next((x.get("series") for x in found or [] if x.get("series")), None)
            tvdb_id = series.get("id") if series else None
            if not tvdb_id:
                return None
        s = await self._get(f"/series/{tvdb_id}/extended", meta="episodes", short="true")
        if not s:
            return None
        remote = {x.get("sourceName"): x.get("id") for x in s.get("remoteIds") or []}
        meta = MediaMetadata(
            media_type="tv",
            provider=self.key,
            title=s.get("name") or "",
            year=_year(s.get("year") or s.get("firstAired")),
            overview=s.get("overview"),
            poster_url=s.get("image"),
            genres=[g.get("name") for g in s.get("genres") or []],
            runtime_minutes=s.get("averageRuntime"),
            status="ended" if (s.get("status") or {}).get("name") == "Ended" else "continuing",
            studio=(s.get("latestNetwork") or s.get("originalNetwork") or {}).get("name"),
            release_date=s.get("firstAired"),
            tvdb_id=s.get("id"),
            imdb_id=remote.get("IMDB"),
        )
        for art in s.get("artworks") or []:
            if art.get("type") == 3 and not meta.backdrop_url:
                meta.backdrop_url = art.get("image")
        for ep in s.get("episodes") or []:
            if ep.get("seasonNumber") is None or ep.get("number") is None:
                continue
            meta.episodes.append(
                EpisodeMetadata(
                    season_number=ep["seasonNumber"],
                    episode_number=ep["number"],
                    title=ep.get("name") or f"Episode {ep['number']}",
                    overview=ep.get("overview"),
                    air_date=ep.get("aired"),
                    runtime_minutes=ep.get("runtime"),
                    tvdb_id=ep.get("id"),
                    absolute_number=ep.get("absoluteNumber"),
                )
            )
        meta.season_count = max((e.season_number for e in meta.episodes), default=None)
        return meta

    async def test(self) -> tuple[bool, str]:
        try:
            async with http.client(timeout=10) as c:
                await self._token(c)
            return True, "TVDB API key accepted."
        except Exception as exc:
            return False, str(exc)
