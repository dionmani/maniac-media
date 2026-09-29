"""IMDb references via the public suggestion endpoint (keyless).

Provides IMDb ids, posters, year and principal cast. Used as the zero-configuration movie
provider and as a cross-reference source for the others.
"""
from __future__ import annotations

import re

from backend.core import http
from backend.domain.types import CastMember, MediaMetadata
from backend.plugins.base import MetadataProvider
from backend.plugins.registry import register

BASE = "https://v3.sg.media-imdb.com/suggestion"
MOVIE_TYPES = {"movie", "tvMovie", "video"}
SERIES_TYPES = {"tvSeries", "tvMiniSeries"}


def _slug(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")


@register
class ImdbProvider(MetadataProvider):
    key = "imdb"
    name = "IMDb (suggest)"
    description = "IMDb ids, posters and principal cast. No API key required."

    async def _suggest(self, query: str) -> list[dict]:
        slug = query if re.fullmatch(r"tt\d+", query) else _slug(query)
        if not slug:
            return []
        async with http.client(timeout=10, headers={"Accept-Language": "en-US"}) as c:
            resp = await http.request(c, "GET", f"{BASE}/{slug[0]}/{slug}.json", raise_for_status=False)
            if resp.status_code != 200:
                return []
            return resp.json().get("d", [])

    def _convert(self, d: dict, media_type: str) -> MediaMetadata:
        image = (d.get("i") or {}).get("imageUrl")
        if image and "._V1_" in image:
            image = image.split("._V1_")[0] + "._V1_SX600.jpg"
        stars = [s.strip() for s in (d.get("s") or "").split(",") if s.strip()]
        return MediaMetadata(
            media_type=media_type,
            provider=self.key,
            title=d.get("l") or "",
            year=d.get("y"),
            overview=None,
            poster_url=image,
            imdb_id=d.get("id"),
            cast=[CastMember(name) for name in stars],
        )

    async def search_movies(self, query: str, year: int | None = None) -> list[MediaMetadata]:
        results = [self._convert(d, "movie") for d in await self._suggest(query) if d.get("qid") in MOVIE_TYPES]
        if year:
            results.sort(key=lambda r: abs((r.year or 0) - year))
        return results

    async def search_series(self, query: str) -> list[MediaMetadata]:
        return [self._convert(d, "tv") for d in await self._suggest(query) if d.get("qid") in SERIES_TYPES]

    async def get_movie(self, *, tmdb_id=None, imdb_id=None) -> MediaMetadata | None:
        if not imdb_id:
            return None
        for d in await self._suggest(imdb_id):
            if d.get("id") == imdb_id:
                return self._convert(d, "movie")
        return None

    async def test(self) -> tuple[bool, str]:
        try:
            ok = bool(await self._suggest("inception"))
            return ok, "IMDb reachable." if ok else "IMDb returned no data."
        except Exception as exc:
            return False, str(exc)
