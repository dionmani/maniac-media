"""Generic RSS feed indexer (torrent or NZB). Search is answered by filtering the feed."""
from __future__ import annotations

from backend.core import http
from backend.domain.parser import normalize_title, parse_episode, parse_movie
from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.plugins.base import FieldSpec, IndexerPlugin, PluginError
from backend.plugins.indexers.newznab import parse_feed
from backend.plugins.registry import register


@register
class RssIndexer(IndexerPlugin):
    key = "rss"
    name = "RSS Feed"
    description = "Any RSS feed of torrent or NZB releases (e.g. private tracker RSS)."
    protocol = "torrent"
    fields = [
        FieldSpec("url", "Feed URL", required=True),
        FieldSpec("feed_protocol", "Protocol", "select", default="torrent", options=["torrent", "usenet"]),
        FieldSpec("cookie", "Cookie header", "password", secret=True, help="Only if the feed requires login"),
    ]

    async def rss(self) -> list[ReleaseInfo]:
        headers = {"Cookie": self.setting("cookie")} if self.setting("cookie") else {}
        async with http.client(timeout=30, follow_redirects=True, headers=headers) as c:
            resp = await http.request(c, "GET", self.setting("url"), raise_for_status=False)
            if resp.status_code >= 400:
                raise PluginError(f"Feed returned HTTP {resp.status_code}")
            return parse_feed(resp.text, self.setting("name", self.name), self.setting("feed_protocol", "torrent"), self.setting("id"))

    async def search(self, criteria: SearchCriteria) -> list[ReleaseInfo]:
        wanted = normalize_title(criteria.title)
        results = []
        for release in await self.rss():
            if criteria.media_type == "movie":
                parsed = parse_movie(release.title)
                if normalize_title(parsed.title) == wanted and (not criteria.year or parsed.year in (None, criteria.year)):
                    results.append(release)
            else:
                ep = parse_episode(release.title)
                if ep and normalize_title(ep.series_title) == wanted:
                    if criteria.season is None or ep.season == criteria.season:
                        if criteria.episode is None or criteria.episode in ep.episodes or ep.full_season:
                            results.append(release)
        return results

    async def test(self) -> tuple[bool, str]:
        try:
            items = await self.rss()
            return True, f"Feed OK – {len(items)} items."
        except Exception as exc:
            return False, str(exc)
