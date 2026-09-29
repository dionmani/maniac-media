"""Release decision engine – decides whether a release is acceptable for a target and ranks it.

Every rejection carries a human-readable reason; interactive search shows them so users
understand *why* a release would not be grabbed automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.domain.parser import normalize_title, parse_episode, parse_movie
from backend.domain.quality import ParsedQuality, QualityProfileSpec, parse_quality
from backend.domain.types import ReleaseInfo


@dataclass
class Target:
    media_type: str  # movie | tv
    media_id: int
    title: str
    year: int | None
    profile: QualityProfileSpec
    season: int | None = None
    episodes: list[int] = field(default_factory=list)  # empty + season => season pack search
    current_quality: ParsedQuality | None = None  # worst current quality across target episodes
    has_file: bool = False
    aliases: list[str] = field(default_factory=list)
    minimum_seeders: dict[int, int] = field(default_factory=dict)  # indexer id -> min seeders


@dataclass
class Decision:
    release: ReleaseInfo
    quality: ParsedQuality
    approved: bool
    rejections: list[str]
    score: int
    season: int | None = None
    episodes: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        r = self.release
        return {
            "guid": r.guid,
            "title": r.title,
            "indexer": r.indexer,
            "indexer_id": r.indexer_id,
            "protocol": r.protocol,
            "size_bytes": r.size_bytes,
            "seeders": r.seeders,
            "leechers": r.leechers,
            "age_days": r.age_days,
            "info_url": r.info_url,
            "quality": self.quality.quality.name,
            "codec": self.quality.codec.value,
            "proper": self.quality.proper or self.quality.repack,
            "hdr": self.quality.hdr,
            "approved": self.approved,
            "rejections": self.rejections,
            "score": self.score,
            "season": self.season,
            "episodes": self.episodes,
        }


def evaluate(release: ReleaseInfo, target: Target, blocklist: set[str] | None = None) -> Decision:
    rejections: list[str] = []
    quality = parse_quality(release.title)
    wanted_titles = {normalize_title(t) for t in [target.title, *target.aliases] if t}
    season, episodes = None, []

    if target.media_type == "movie":
        parsed = parse_movie(release.title)
        if normalize_title(parsed.title) not in wanted_titles:
            rejections.append(f"Title mismatch ('{parsed.title}')")
        if target.year and parsed.year and abs(parsed.year - target.year) > 1:
            rejections.append(f"Year mismatch ({parsed.year} vs {target.year})")
    else:
        ep = parse_episode(release.title)
        if ep is None:
            rejections.append("Could not parse season/episode")
        else:
            season, episodes = ep.season, ep.episodes
            if normalize_title(ep.series_title) not in wanted_titles:
                rejections.append(f"Series mismatch ('{ep.series_title}')")
            if target.season is not None and ep.season != target.season:
                rejections.append(f"Wrong season (S{ep.season})")
            if target.episodes:
                if ep.full_season:
                    rejections.append("Season pack when single episode was wanted")
                elif not set(target.episodes) & set(ep.episodes):
                    rejections.append(f"Wrong episode ({ep.episodes})")
            elif target.season is not None and not ep.full_season:
                rejections.append("Single episode when season pack was wanted")

    profile = target.profile
    if not profile.is_allowed(quality.quality):
        rejections.append(f"{quality.quality.name} not allowed by profile '{profile.name}'")
    if profile.codec_rejected(quality.codec):
        rejections.append(f"Codec {quality.codec.value} rejected by profile")

    size_mb = (release.size_bytes or 0) / (1024 * 1024)
    units = max(1, len(episodes)) if target.media_type == "tv" and episodes else 1
    if profile.min_size_mb and size_mb and size_mb / units < profile.min_size_mb:
        rejections.append(f"Too small ({size_mb:.0f} MB)")
    if profile.max_size_mb and size_mb / units > profile.max_size_mb and not (target.media_type == "tv" and not target.episodes):
        rejections.append(f"Too large ({size_mb:.0f} MB)")

    if release.protocol == "torrent" and release.seeders is not None:
        min_seeders = target.minimum_seeders.get(release.indexer_id or -1, 1)
        if release.seeders < min_seeders:
            rejections.append(f"Not enough seeders ({release.seeders} < {min_seeders})")

    if blocklist and release.title.lower() in blocklist:
        rejections.append("Release is blocklisted")

    if target.has_file and not profile.is_upgrade(target.current_quality, quality):
        rejections.append(
            f"Existing file ({target.current_quality.quality.name if target.current_quality else 'unknown'}) is equal or better / cutoff met"
        )

    score = profile.score(quality)
    # tie-breakers: availability and freshness
    score = score * 1000 + min(release.seeders or 0, 500) + (100 if release.protocol == "usenet" else 0) - min(release.age_days or 0, 99)
    return Decision(release, quality, not rejections, rejections, score, season, episodes)


def rank(decisions: list[Decision]) -> list[Decision]:
    return sorted(decisions, key=lambda d: (d.approved, d.score), reverse=True)
