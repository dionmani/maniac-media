"""TVmaze – free, keyless, excellent episode data. Series only."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from backend.core import http
from backend.domain.types import CastMember, EpisodeMetadata, MediaMetadata
from backend.plugins.base import MetadataProvider
from backend.plugins.registry import register

BASE = "https://api.tvmaze.com"


def _strip_html(text: str | None) -> str | None:
    return re.sub(r"<[^>]+>", "", text).strip() if text else None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _airstamp(value: str | None) -> datetime | None:
    """TVmaze's ``airstamp`` (ISO 8601 with offset) as naive UTC."""
    try:
        stamp = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if stamp is None or stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


@register
class TvMazeProvider(MetadataProvider):
    key = "tvmaze"
    name = "TVmaze"
    description = "Free TV metadata with full episode lists. No API key required."
    supports_movies = False
    provides_air_times = True

    def _convert(self, show: dict, embedded: bool = False) -> MediaMetadata:
        image = show.get("image") or {}
        externals = show.get("externals") or {}
        network = (show.get("network") or show.get("webChannel") or {}).get("name")
        rating = (show.get("rating") or {}).get("average")
        meta = MediaMetadata(
            media_type="tv",
            provider=self.key,
            title=show.get("name") or "",
            year=_year(show.get("premiered")),
            overview=_strip_html(show.get("summary")),
            poster_url=image.get("original") or image.get("medium"),
            backdrop_url=None,
            genres=show.get("genres") or [],
            runtime_minutes=show.get("averageRuntime") or show.get("runtime"),
            rating=rating,
            status="ended" if show.get("status") == "Ended" else "continuing",
            studio=network,
            release_date=show.get("premiered"),
            tvmaze_id=show.get("id"),
            tvdb_id=externals.get("thetvdb"),
            imdb_id=externals.get("imdb"),
        )
        if embedded:
            emb = show.get("_embedded") or {}
            for ep in emb.get("episodes") or []:
                if ep.get("season") is None or ep.get("number") is None:
                    continue  # specials without numbers
                meta.episodes.append(
                    EpisodeMetadata(
                        season_number=ep["season"],
                        episode_number=ep["number"],
                        title=ep.get("name") or f"Episode {ep['number']}",
                        overview=_strip_html(ep.get("summary")),
                        air_date=ep.get("airdate") or None,
                        air_date_utc=_airstamp(ep.get("airstamp")) if ep.get("airdate") else None,
                        runtime_minutes=ep.get("runtime"),
                        tvmaze_id=ep.get("id"),
                    )
                )
            for c in (emb.get("cast") or [])[:20]:
                person, character = c.get("person") or {}, c.get("character") or {}
                meta.cast.append(
                    CastMember(person.get("name", ""), character.get("name"), (person.get("image") or {}).get("medium"))
                )
            meta.season_count = max((e.season_number for e in meta.episodes), default=None)
            images = [i for i in emb.get("images") or [] if i.get("type") == "background"]
            if images:
                meta.backdrop_url = images[0]["resolutions"]["original"]["url"]
        return meta

    async def search_series(self, query: str) -> list[MediaMetadata]:
        async with http.client(timeout=10) as c:
            resp = await http.request(c, "GET", f"{BASE}/search/shows", params={"q": query})
            return [self._convert(item["show"]) for item in resp.json() if item.get("show")]

    async def get_series(self, *, tvdb_id=None, tmdb_id=None, tvmaze_id=None, imdb_id=None) -> MediaMetadata | None:
        async with http.client(timeout=15, follow_redirects=True) as c:
            if not tvmaze_id:
                params = {"thetvdb": tvdb_id} if tvdb_id else {"imdb": imdb_id} if imdb_id else None
                if not params:
                    return None
                resp = await http.request(c, "GET", f"{BASE}/lookup/shows", params=params, raise_for_status=False)
                if resp.status_code != 200:
                    return None
                tvmaze_id = resp.json().get("id")
            resp = await http.request(
                c,
                "GET",
                f"{BASE}/shows/{tvmaze_id}",
                params=[("embed[]", "episodes"), ("embed[]", "cast"), ("embed[]", "images")],
                raise_for_status=False,
            )
            if resp.status_code != 200:
                return None
            return self._convert(resp.json(), embedded=True)

    async def test(self) -> tuple[bool, str]:
        try:
            await self.search_series("Breaking Bad")
            return True, "TVmaze reachable."
        except Exception as exc:
            return False, str(exc)
