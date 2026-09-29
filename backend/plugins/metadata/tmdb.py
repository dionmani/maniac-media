"""The Movie Database (TMDB) – richest movie metadata. Requires a free API key (v3) or read token (v4)."""
from __future__ import annotations

from backend.core import http
from backend.domain.types import CastMember, EpisodeMetadata, MediaMetadata
from backend.plugins.base import FieldSpec, MetadataProvider, PluginError
from backend.plugins.registry import register

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"


def _img(path: str | None, size: str) -> str | None:
    return f"{IMG}{size}{path}" if path else None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


@register
class TmdbProvider(MetadataProvider):
    key = "tmdb"
    name = "TMDB"
    description = "The Movie Database. Full movie & series metadata, cast, release dates, artwork."
    requires_api_key = True
    fields = [FieldSpec("api_key", "API key / read access token", "password", required=True, secret=True)]

    def _auth(self) -> tuple[dict, dict]:
        key = self.setting("api_key")
        if not key:
            raise PluginError("TMDB API key not configured")
        if len(key) > 60:  # v4 read access token (JWT)
            return {"Authorization": f"Bearer {key}"}, {}
        return {}, {"api_key": key}

    async def _get(self, path: str, **params) -> dict:
        headers, auth = self._auth()
        lang = self.setting("language", "en-US")
        async with http.client(timeout=12, headers=headers) as c:
            resp = await http.request(c, "GET", f"{BASE}{path}", params={**auth, "language": lang, **params}, raise_for_status=False)
            if resp.status_code == 401:
                raise PluginError("TMDB rejected the API key")
            if resp.status_code == 404:
                return {}
            if resp.status_code >= 400:
                raise PluginError(f"TMDB error {resp.status_code}")
            return resp.json()

    def _movie(self, m: dict, full: bool = False) -> MediaMetadata:
        meta = MediaMetadata(
            media_type="movie",
            provider=self.key,
            title=m.get("title") or m.get("original_title") or "",
            original_title=m.get("original_title"),
            year=_year(m.get("release_date")),
            overview=m.get("overview") or None,
            tagline=m.get("tagline") or None,
            poster_url=_img(m.get("poster_path"), "w500"),
            backdrop_url=_img(m.get("backdrop_path"), "w1280"),
            genres=[g["name"] for g in m.get("genres", [])],
            runtime_minutes=m.get("runtime"),
            rating=m.get("vote_average"),
            votes=m.get("vote_count"),
            release_date=m.get("release_date") or None,
            tmdb_id=m.get("id"),
            imdb_id=m.get("imdb_id") or (m.get("external_ids") or {}).get("imdb_id"),
            status={"Released": "released", "In Production": "announced", "Post Production": "announced", "Planned": "announced"}.get(
                m.get("status", ""), "released"
            ),
        )
        if m.get("production_companies"):
            meta.studio = m["production_companies"][0].get("name")
        if full:
            credits = m.get("credits") or {}
            meta.cast = [
                CastMember(c["name"], c.get("character"), _img(c.get("profile_path"), "w185")) for c in credits.get("cast", [])[:20]
            ]
            meta.crew = [
                {"name": c["name"], "job": c["job"]} for c in credits.get("crew", []) if c.get("job") in {"Director", "Screenplay", "Writer", "Producer"}
            ][:10]
            for country in (m.get("release_dates") or {}).get("results", []):
                if country.get("iso_3166_1") != "US":
                    continue
                for rd in country.get("release_dates", []):
                    date = (rd.get("release_date") or "")[:10]
                    if rd.get("certification") and not meta.certification:
                        meta.certification = rd["certification"]
                    if rd.get("type") == 4 and not meta.digital_release_date:
                        meta.digital_release_date = date
                    if rd.get("type") == 5 and not meta.physical_release_date:
                        meta.physical_release_date = date
        return meta

    async def search_movies(self, query: str, year: int | None = None) -> list[MediaMetadata]:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        data = await self._get("/search/movie", **params)
        return [self._movie(m) for m in data.get("results", [])]

    async def get_movie(self, *, tmdb_id=None, imdb_id=None) -> MediaMetadata | None:
        if not tmdb_id and imdb_id:
            found = await self._get(f"/find/{imdb_id}", external_source="imdb_id")
            results = found.get("movie_results") or []
            tmdb_id = results[0]["id"] if results else None
        if not tmdb_id:
            return None
        m = await self._get(f"/movie/{tmdb_id}", append_to_response="credits,release_dates,external_ids")
        return self._movie(m, full=True) if m else None

    def _series(self, s: dict) -> MediaMetadata:
        return MediaMetadata(
            media_type="tv",
            provider=self.key,
            title=s.get("name") or s.get("original_name") or "",
            original_title=s.get("original_name"),
            year=_year(s.get("first_air_date")),
            overview=s.get("overview") or None,
            poster_url=_img(s.get("poster_path"), "w500"),
            backdrop_url=_img(s.get("backdrop_path"), "w1280"),
            genres=[g["name"] for g in s.get("genres", [])],
            rating=s.get("vote_average"),
            votes=s.get("vote_count"),
            release_date=s.get("first_air_date") or None,
            tmdb_id=s.get("id"),
            status="ended" if s.get("status") in {"Ended", "Canceled"} else "continuing",
            studio=(s.get("networks") or [{}])[0].get("name") if s.get("networks") else None,
            season_count=s.get("number_of_seasons"),
            runtime_minutes=(s.get("episode_run_time") or [None])[0],
        )

    async def search_series(self, query: str) -> list[MediaMetadata]:
        data = await self._get("/search/tv", query=query)
        return [self._series(s) for s in data.get("results", [])]

    async def get_series(self, *, tvdb_id=None, tmdb_id=None, tvmaze_id=None, imdb_id=None) -> MediaMetadata | None:
        if not tmdb_id and (tvdb_id or imdb_id):
            source, value = ("tvdb_id", tvdb_id) if tvdb_id else ("imdb_id", imdb_id)
            found = await self._get(f"/find/{value}", external_source=source)
            results = found.get("tv_results") or []
            tmdb_id = results[0]["id"] if results else None
        if not tmdb_id:
            return None
        s = await self._get(f"/tv/{tmdb_id}", append_to_response="credits,external_ids,content_ratings")
        if not s:
            return None
        meta = self._series(s)
        ext = s.get("external_ids") or {}
        meta.imdb_id, meta.tvdb_id = ext.get("imdb_id"), ext.get("tvdb_id")
        meta.cast = [CastMember(c["name"], c.get("character"), _img(c.get("profile_path"), "w185")) for c in (s.get("credits") or {}).get("cast", [])[:20]]
        for r in (s.get("content_ratings") or {}).get("results", []):
            if r.get("iso_3166_1") == "US":
                meta.certification = r.get("rating")
        for season in s.get("seasons", []):
            number = season.get("season_number")
            if number is None:
                continue
            detail = await self._get(f"/tv/{tmdb_id}/season/{number}")
            for ep in detail.get("episodes", []):
                meta.episodes.append(
                    EpisodeMetadata(
                        season_number=number,
                        episode_number=ep["episode_number"],
                        title=ep.get("name") or f"Episode {ep['episode_number']}",
                        overview=ep.get("overview") or None,
                        air_date=ep.get("air_date") or None,
                        runtime_minutes=ep.get("runtime"),
                    )
                )
        return meta

    async def test(self) -> tuple[bool, str]:
        try:
            await self._get("/configuration")
            return True, "TMDB API key accepted."
        except Exception as exc:
            return False, str(exc)
