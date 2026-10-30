"""Newznab (usenet) and Torznab (torrent) indexers – also covers Jackett and Prowlarr feeds."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from backend.core import http
from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.plugins.base import FieldSpec, IndexerPlugin, PluginError
from backend.plugins.registry import register

MOVIE_CATS = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080]
TV_CATS = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080]
MAX_FEED_BYTES = 8 * 1024 * 1024


def parse_feed(xml_text: str, indexer: str, protocol: str, indexer_id: int | None = None) -> list[ReleaseInfo]:
    """Parse an RSS 2.0 feed with newznab/torznab attributes."""
    if len(xml_text) > MAX_FEED_BYTES:
        raise PluginError("Indexer response too large")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PluginError(f"Invalid XML from indexer: {exc}") from exc
    if root.tag == "error":
        raise PluginError(f"Indexer error {root.get('code')}: {root.get('description')}")

    releases: list[ReleaseInfo] = []
    for item in root.iter("item"):
        attrs: dict[str, list[str]] = {}
        for el in item:
            if el.tag.endswith("}attr") or el.tag == "attr":
                attrs.setdefault(el.get("name", ""), []).append(el.get("value", ""))
        title = (item.findtext("title") or "").strip()
        enclosure = item.find("enclosure")
        link = (enclosure.get("url") if enclosure is not None else None) or item.findtext("link") or ""
        magnet = _first(attrs, "magneturl")
        if not link and magnet:
            link = magnet
        if not title or not link:
            continue
        size = _int(_first(attrs, "size")) or _int(item.findtext("size")) or (
            _int(enclosure.get("length")) if enclosure is not None else 0
        )
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        seeders = _int(_first(attrs, "seeders"))
        peers = _int(_first(attrs, "peers"))
        imdb = _first(attrs, "imdb") or _first(attrs, "imdbid")
        releases.append(
            ReleaseInfo(
                guid=(item.findtext("guid") or link).strip(),
                title=title,
                download_url=link,
                indexer=indexer,
                indexer_id=indexer_id,
                protocol=protocol,
                size_bytes=size or 0,
                info_url=item.findtext("comments"),
                publish_date=published,
                seeders=seeders,
                leechers=(peers - seeders) if (peers is not None and seeders is not None) else None,
                grabs=_int(_first(attrs, "grabs")),
                categories=[_int(c) for c in attrs.get("category", []) if _int(c)],
                imdb_id=(f"tt{imdb.zfill(7)}" if imdb and not imdb.startswith("tt") else imdb) or None,
                tvdb_id=_int(_first(attrs, "tvdbid")),
                tvmaze_id=_int(_first(attrs, "tvmazeid")),
                info_hash=_first(attrs, "infohash"),
                magnet_url=magnet,
            )
        )
    return releases


def _first(attrs: dict[str, list[str]], key: str) -> str | None:
    values = attrs.get(key)
    return values[0] if values else None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@register
class NewznabIndexer(IndexerPlugin):
    key = "newznab"
    name = "Newznab"
    description = "Usenet indexers speaking the Newznab API (NZBGeek, DrunkenSlug, NZBPlanet...)."
    protocol = "usenet"
    fields = [
        FieldSpec("url", "URL", required=True, help="e.g. https://api.nzbgeek.info"),
        FieldSpec("api_path", "API path", default="/api"),
        FieldSpec("api_key", "API key", "password", secret=True),
        FieldSpec("movie_categories", "Movie categories", "list", default=MOVIE_CATS),
        FieldSpec("tv_categories", "TV categories", "list", default=TV_CATS),
    ]

    @property
    def endpoint(self) -> str:
        return self.setting("url", "").rstrip("/") + "/" + self.setting("api_path", "/api").lstrip("/")

    def _base_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"extended": 1, "limit": 100}
        if self.setting("api_key"):
            params["apikey"] = self.setting("api_key")
        return params

    async def _query(self, params: dict[str, Any]) -> list[ReleaseInfo]:
        async with http.client(timeout=30, follow_redirects=True) as c:
            resp = await http.request(c, "GET", self.endpoint, params={**self._base_params(), **params}, raise_for_status=False)
            if resp.status_code in (401, 403):
                raise PluginError("Indexer rejected the API key")
            if resp.status_code >= 400:
                raise http.HttpError(f"Indexer HTTP {resp.status_code}", resp.status_code)
            return parse_feed(resp.text, self.setting("name", self.name), self.protocol, self.setting("id"))

    async def search(self, criteria: SearchCriteria) -> list[ReleaseInfo]:
        if criteria.media_type == "movie":
            cats = ",".join(str(c) for c in (self.setting("movie_categories") or MOVIE_CATS))
            if criteria.imdb_id:
                releases = await self._query({"t": "movie", "imdbid": criteria.imdb_id.removeprefix("tt"), "cat": cats})
                if releases:
                    return releases
            q = f"{criteria.title} {criteria.year}" if criteria.year else criteria.title
            return await self._query({"t": "movie", "q": q, "cat": cats})

        cats = ",".join(str(c) for c in (self.setting("tv_categories") or TV_CATS))
        params: dict[str, Any] = {"t": "tvsearch", "cat": cats}
        if criteria.season is not None:
            params["season"] = criteria.season
        if criteria.episode is not None:
            params["ep"] = criteria.episode
        if criteria.tvdb_id:
            releases = await self._query({**params, "tvdbid": criteria.tvdb_id})
            if releases:
                return releases
        return await self._query({**params, "q": criteria.title})

    async def rss(self) -> list[ReleaseInfo]:
        cats = [*(self.setting("movie_categories") or MOVIE_CATS), *(self.setting("tv_categories") or TV_CATS)]
        return await self._query({"t": "search", "cat": ",".join(str(c) for c in cats)})

    async def test(self) -> tuple[bool, str]:
        try:
            async with http.client(timeout=15, follow_redirects=True) as c:
                resp = await http.request(c, "GET", self.endpoint, params={**self._base_params(), "t": "caps"}, raise_for_status=False)
            if resp.status_code in (401, 403):
                return False, "Indexer rejected the API key"
            root = ET.fromstring(resp.text)
            if root.tag == "error":
                return False, f"Indexer error: {root.get('description')}"
            server = root.find("server")
            title = server.get("title") if server is not None else None
            return True, f"Connected{f' to {title}' if title else ''}."
        except Exception as exc:
            return False, f"Connection failed: {exc}"


@register
class TorznabIndexer(NewznabIndexer):
    key = "torznab"
    name = "Torznab"
    description = "Torrent indexers via Torznab (Jackett, Prowlarr, or native Torznab trackers)."
    protocol = "torrent"
    fields = [
        FieldSpec("url", "URL", required=True, help="e.g. http://localhost:9117/api/v2.0/indexers/all/results/torznab"),
        FieldSpec("api_path", "API path", default="/api"),
        FieldSpec("api_key", "API key", "password", secret=True),
        FieldSpec("movie_categories", "Movie categories", "list", default=MOVIE_CATS),
        FieldSpec("tv_categories", "TV categories", "list", default=TV_CATS),
        FieldSpec("minimum_seeders", "Minimum seeders", "number", default=1),
    ]
