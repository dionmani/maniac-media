"""Provider-neutral data transfer objects exchanged between plugins and the core.

Plugins translate their wire formats into these types; business logic never sees a
TMDB/TVDB/qBittorrent-specific structure.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CastMember:
    name: str
    character: str | None = None
    image: str | None = None


@dataclass
class EpisodeMetadata:
    season_number: int
    episode_number: int
    title: str
    overview: str | None = None
    air_date: str | None = None
    runtime_minutes: int | None = None
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    absolute_number: int | None = None


@dataclass
class MediaMetadata:
    """Search result / full record for a movie or series."""

    media_type: str  # movie | tv
    provider: str
    title: str
    year: int | None = None
    overview: str | None = None
    original_title: str | None = None
    tagline: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None
    genres: list[str] = field(default_factory=list)
    runtime_minutes: int | None = None
    rating: float | None = None
    votes: int | None = None
    certification: str | None = None
    status: str | None = None
    studio: str | None = None  # movies: studio, series: network
    release_date: str | None = None
    digital_release_date: str | None = None
    physical_release_date: str | None = None
    cast: list[CastMember] = field(default_factory=list)
    crew: list[dict[str, str]] = field(default_factory=list)
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    season_count: int | None = None
    episodes: list[EpisodeMetadata] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchCriteria:
    media_type: str  # movie | tv
    title: str
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    season: int | None = None
    episode: int | None = None
    aliases: list[str] = field(default_factory=list)

    @property
    def is_season_search(self) -> bool:
        return self.media_type == "tv" and self.season is not None and self.episode is None


@dataclass
class ReleaseInfo:
    guid: str
    title: str
    download_url: str
    indexer: str
    protocol: str  # torrent | usenet
    size_bytes: int = 0
    indexer_id: int | None = None
    info_url: str | None = None
    publish_date: datetime | None = None
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None
    categories: list[int] = field(default_factory=list)
    imdb_id: str | None = None
    tvdb_id: int | None = None
    info_hash: str | None = None
    magnet_url: str | None = None

    @property
    def age_days(self) -> int | None:
        if not self.publish_date:
            return None
        pd = self.publish_date.replace(tzinfo=None)
        return max(0, (datetime.utcnow() - pd).days)


@dataclass
class ClientItem:
    """A download as reported by a download client."""

    id: str
    name: str
    status: str  # queued | downloading | paused | completed | failed | warning
    progress: float = 0.0  # 0..100
    size_bytes: int = 0
    remaining_bytes: int = 0
    speed_bps: int = 0
    eta_seconds: int | None = None
    output_path: str | None = None
    category: str | None = None
    message: str | None = None
    can_remove: bool = True


@dataclass
class NotificationMessage:
    event: str
    title: str
    body: str
    fields: dict[str, str] = field(default_factory=dict)
    image_url: str | None = None
    severity: str = "info"
