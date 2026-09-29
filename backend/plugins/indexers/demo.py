"""Demo indexer – synthesises plausible releases. Opt-in only; useful for trying the pipeline."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.plugins.base import FieldSpec, IndexerPlugin
from backend.plugins.registry import register

TAGS = [
    ("2160p.UHD.BluRay.REMUX.HDR.HEVC", 12.0),
    ("2160p.BluRay.x265.HDR", 7.0),
    ("2160p.WEB-DL.DDP5.1.H.265", 4.5),
    ("1080p.BluRay.x264", 2.2),
    ("1080p.WEB-DL.DDP5.1.H.264", 1.8),
    ("1080p.WEBRip.x265", 1.1),
    ("720p.HDTV.x264", 0.8),
    ("480p.DVDRip.XviD", 0.5),
]


@register
class DemoIndexer(IndexerPlugin):
    key = "demo"
    name = "Demo indexer"
    description = "Generates synthetic releases for testing. Pair it with the Simulator download client."
    protocol = "torrent"
    fields = [FieldSpec("feed_protocol", "Protocol", "select", default="torrent", options=["torrent", "usenet"])]

    def _make(self, base: str, base_gb: float) -> list[ReleaseInfo]:
        protocol = self.setting("feed_protocol", "torrent")
        out = []
        for i, (tag, mult) in enumerate(TAGS):
            title = f"{base}.{tag}-DEMO"
            digest = hashlib.sha1(title.encode()).hexdigest()
            out.append(
                ReleaseInfo(
                    guid=f"demo-{digest}",
                    title=title,
                    download_url=f"demo://{digest}",
                    indexer=self.setting("name", self.name),
                    indexer_id=self.setting("id"),
                    protocol=protocol,
                    size_bytes=int(base_gb * mult * 1024**3),
                    publish_date=datetime.now(timezone.utc) - timedelta(days=i + 1),
                    seeders=(200 - i * 20) if protocol == "torrent" else None,
                    leechers=(10 + i) if protocol == "torrent" else None,
                    info_hash=digest if protocol == "torrent" else None,
                )
            )
        return out

    async def search(self, criteria: SearchCriteria) -> list[ReleaseInfo]:
        name = criteria.title.replace(" ", ".").replace(":", "")
        if criteria.media_type == "movie":
            return self._make(f"{name}.{criteria.year or ''}".rstrip("."), 1.5)
        if criteria.episode is not None:
            return self._make(f"{name}.S{criteria.season:02d}E{criteria.episode:02d}", 0.6)
        if criteria.season is not None:
            return self._make(f"{name}.S{criteria.season:02d}", 6.0)
        return self._make(name, 0.6)

    async def rss(self) -> list[ReleaseInfo]:
        return []
