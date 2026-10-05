"""The Movie Database (TMDB) – richest movie metadata. Requires a free API key (v3) or read token (v4)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import httpx

from backend.core import http
from backend.domain import discover as rules
from backend.domain.types import CastMember, DiscoverItem, EpisodeMetadata, MediaMetadata, WatchProvider
from backend.plugins.base import FieldSpec, MetadataProvider, PluginError
from backend.plugins.registry import register

log = logging.getLogger("maniac.metadata.tmdb")

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"

# list endpoints only carry genre ids; these are TMDB's fixed movie + TV genre ids
GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary", 18: "Drama",
    10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western", 10759: "Action & Adventure",
    10762: "Kids", 10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk",
    10768: "War & Politics",
}
# daily programming (news, soaps, talk shows) would otherwise crowd out actual new series
TV_NOISE_GENRES = frozenset({10763, 10766, 10767})
DISCOVER_PAGES = {"announced": 2, "in_theaters": 2, "streaming": 2, "new_series": 2, "new_seasons": 3}
DETAIL_CONCURRENCY = 8
RELEASE_TYPES = {2: "limited", 3: "theatrical", 4: "digital", 5: "physical"}


def _img(path: str | None, size: str) -> str | None:
    return f"{IMG}{size}{path}" if path else None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _release_info(m: dict, region: str) -> dict[str, str | None]:
    """Earliest theatrical/digital/physical date and the certification for one country."""
    info: dict[str, str | None] = {"limited": None, "theatrical": None, "digital": None, "physical": None, "certification": None}
    for country in (m.get("release_dates") or {}).get("results", []):
        if country.get("iso_3166_1") != region:
            continue
        for rd in country.get("release_dates", []):
            day = (rd.get("release_date") or "")[:10] or None
            if rd.get("certification") and not info["certification"]:
                info["certification"] = rd["certification"]
            kind = RELEASE_TYPES.get(rd.get("type"))
            if kind and day and (info[kind] is None or day < info[kind]):  # type: ignore[operator]
                info[kind] = day
    info["theatrical"] = info["theatrical"] or info["limited"]
    return info


def _watch_providers(m: dict, region: str) -> list[WatchProvider]:
    """Subscription/free streaming services in ``region`` (JustWatch data); rent/buy storefronts are left out."""
    offers = ((m.get("watch/providers") or {}).get("results") or {}).get(region) or {}
    seen: set[str] = set()
    result = []
    for kind in ("flatrate", "free", "ads"):
        for p in sorted(offers.get(kind, []), key=lambda p: p.get("display_priority", 99)):
            name = p.get("provider_name")
            if name and name not in seen:
                seen.add(name)
                result.append(WatchProvider(name, _img(p.get("logo_path"), "w92")))
    return result[:6]


@register
class TmdbProvider(MetadataProvider):
    key = "tmdb"
    name = "TMDB"
    description = "The Movie Database. Full movie & series metadata, cast, release dates, artwork."
    requires_api_key = True
    supports_discover = True
    fields = [FieldSpec("api_key", "API key / read access token", "password", required=True, secret=True)]

    def _auth(self) -> tuple[dict, dict]:
        key = self.setting("api_key")
        if not key:
            raise PluginError("TMDB API key not configured")
        if len(key) > 60:  # v4 read access token (JWT)
            return {"Authorization": f"Bearer {key}"}, {}
        return {}, {"api_key": key}

    def _session(self) -> httpx.AsyncClient:
        headers, _ = self._auth()
        return http.client(timeout=12, headers=headers)

    async def _get(self, path: str, *, session: httpx.AsyncClient | None = None, **params) -> dict:
        """GET ``path``; pass ``session`` (from :meth:`_session`) to reuse one connection across many calls."""
        if session is None:
            async with self._session() as c:
                return await self._get(path, session=c, **params)
        _, auth = self._auth()
        lang = self.setting("language", "en-US")
        resp = await http.request(session, "GET", f"{BASE}{path}", params={k: v for k, v in {**auth, "language": lang, **params}.items() if v is not None},
                                  raise_for_status=False)
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
            genres=[g["name"] for g in m.get("genres", [])] or [GENRES[g] for g in m.get("genre_ids", []) if g in GENRES],
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
            release = _release_info(m, "US")
            meta.certification = release["certification"]
            meta.digital_release_date, meta.physical_release_date = release["digital"], release["physical"]
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
            genres=[g["name"] for g in s.get("genres", [])] or [GENRES[g] for g in s.get("genre_ids", []) if g in GENRES],
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

    # ------------------------------------------------------------------ discover
    async def discover(self, media_type: str, section: str, *, region: str, languages: list[str], today: date) -> list[DiscoverItem]:
        start, end = rules.window(section, today)
        async with self._session() as session:
            if section == "announced":
                return await self._announced(session, start, languages)
            if section == "in_theaters":
                return await self._in_theaters(session, region, languages)
            if section == "streaming":
                return await self._streaming(session, region, start, end, languages)
            if section in ("new_series", "new_seasons"):
                return await self._new_tv(session, section, region, start, end, today, languages)
        return []

    async def _list(self, session: httpx.AsyncClient, path: str, section: str, languages: list[str], skip_genres: frozenset[int] = frozenset(),
                    **params) -> list[dict]:
        """Results across the section's pages, de-duplicated, in TMDB's (popularity) order.

        Language and genre exclusions are also applied here: /movie/now_playing has no such filters, and
        TMDB doesn't document OR-lists for ``with_original_language``, so several languages filter locally.
        """
        if len(languages) == 1 and path.startswith("/discover/"):
            params["with_original_language"] = languages[0]
        if skip_genres and path.startswith("/discover/"):
            params["without_genres"] = ",".join(str(g) for g in sorted(skip_genres))
        pages = await asyncio.gather(*(self._get(path, session=session, page=n, **params) for n in range(1, DISCOVER_PAGES[section] + 1)))
        seen: set[int] = set()
        rows = []
        for data in pages:
            for row in data.get("results", []):
                if not row.get("id") or row["id"] in seen or row.get("adult"):
                    continue
                if languages and row.get("original_language") not in languages:
                    continue
                if skip_genres & set(row.get("genre_ids") or []):
                    continue
                seen.add(row["id"])
                rows.append(row)
        return rows

    async def _details(self, session: httpx.AsyncClient, kind: str, ids: list[int], append: str) -> dict[int, dict]:
        """Full records for list rows, a few at a time; a title that fails to load just keeps its list row."""
        limit = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def one(tmdb_id: int) -> tuple[int, dict]:
            async with limit:
                try:
                    return tmdb_id, await self._get(f"/{kind}/{tmdb_id}", session=session, append_to_response=append)
                except Exception as exc:
                    log.debug("TMDB %s %s detail failed: %s", kind, tmdb_id, exc)
                    return tmdb_id, {}

        return dict(await asyncio.gather(*(one(i) for i in ids)))

    async def _announced(self, session: httpx.AsyncClient, start: date | None, languages: list[str]) -> list[DiscoverItem]:
        rows = await self._list(session, "/discover/movie", "announced", languages, sort_by="popularity.desc", include_adult="false",
                                include_video="false", **{"primary_release_date.gte": start.isoformat() if start else None})
        items = []
        for row in rows:
            meta = self._movie(row)
            meta.status = "announced"
            items.append(DiscoverItem(meta, "announced", "announced", reason_date=meta.release_date, popularity=row.get("popularity")))
        return items

    async def _movie_items(self, session: httpx.AsyncClient, rows: list[dict], region: str) -> list[DiscoverItem]:
        details = await self._details(session, "movie", [r["id"] for r in rows], "release_dates,watch/providers,external_ids")
        result = []
        for row in rows:
            full = details.get(row["id"]) or row
            meta = self._movie(full)
            release = _release_info(full, region)
            meta.certification = release["certification"]
            meta.digital_release_date, meta.physical_release_date = release["digital"], release["physical"]
            item = DiscoverItem(meta, "", "", theatrical_release_date=release["theatrical"] or row.get("release_date") or None,
                                watch_providers=_watch_providers(full, region), popularity=row.get("popularity"))
            result.append(item)
        return result

    async def _in_theaters(self, session: httpx.AsyncClient, region: str, languages: list[str]) -> list[DiscoverItem]:
        rows = await self._list(session, "/movie/now_playing", "in_theaters", languages, region=region)
        items = []
        for item in await self._movie_items(session, rows, region):
            item.section = item.reason = "in_theaters"
            item.reason_date = item.theatrical_release_date
            item.meta.status = "in_cinemas"
            items.append(item)
        return items

    async def _streaming(self, session: httpx.AsyncClient, region: str, start: date | None, end: date | None,
                         languages: list[str]) -> list[DiscoverItem]:
        rows = await self._list(
            session, "/discover/movie", "streaming", languages, region=region, with_release_type="4|5", sort_by="popularity.desc",
            include_adult="false", include_video="false",
            **{"release_date.gte": start.isoformat() if start else None, "release_date.lte": end.isoformat() if end else None},
        )
        items = []
        for item in await self._movie_items(session, rows, region):
            hit = rules.home_release(item.meta.digital_release_date, item.meta.physical_release_date, start, end)
            if hit is None:
                continue  # TMDB matched on another release than the ones we read; keep the definition strict
            item.section = "streaming"
            item.reason, item.reason_date = hit
            items.append(item)
        return items

    async def _new_tv(self, session: httpx.AsyncClient, section: str, region: str, start: date | None, end: date | None, today: date,
                      languages: list[str]) -> list[DiscoverItem]:
        params: dict = {"sort_by": "popularity.desc", "include_adult": "false",
                        "include_null_first_air_dates": "false"}
        if section == "new_series":
            params |= {"first_air_date.gte": start.isoformat() if start else None, "first_air_date.lte": end.isoformat() if end else None}
        else:
            params |= {"air_date.gte": start.isoformat() if start else None, "air_date.lte": end.isoformat() if end else None,
                       "first_air_date.lte": (today - timedelta(days=rules.RETURNING_MIN_AGE_DAYS)).isoformat()}
        rows = await self._list(session, "/discover/tv", section, languages, TV_NOISE_GENRES, **params)
        details = await self._details(session, "tv", [r["id"] for r in rows], "external_ids,watch/providers")
        items = []
        for row in rows:
            full = details.get(row["id"]) or {}
            meta = self._series(full or row)
            ext = full.get("external_ids") or {}
            meta.imdb_id, meta.tvdb_id = ext.get("imdb_id") or None, ext.get("tvdb_id") or None
            item = DiscoverItem(meta, section, "", watch_providers=_watch_providers(full, region), popularity=row.get("popularity"))
            if section == "new_series":
                item.reason, item.reason_date, item.season_number = "series_premiere", meta.release_date, 1
            else:
                hit = rules.season_premiere([(s.get("season_number") or 0, s.get("air_date")) for s in full.get("seasons", [])], start, end)
                if hit is None:
                    continue  # an episode airs in the window, but it isn't a season premiere
                item.reason = "season_premiere"
                item.season_number, item.reason_date = hit
            items.append(item)
        return items

    async def test(self) -> tuple[bool, str]:
        try:
            await self._get("/configuration")
            return True, "TMDB API key accepted."
        except Exception as exc:
            return False, str(exc)
