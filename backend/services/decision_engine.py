"""Release decision engine – decides whether a release is acceptable for a target and ranks it.

Every rejection carries a human-readable reason; interactive search shows them so users
understand *why* a release would not be grabbed automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.domain.availability import Availability, latest
from backend.domain.custom_formats import FormatMatcher, ReleaseFacts
from backend.domain.custom_formats import score as score_formats
from backend.domain.parser import normalize_title, parse_episode, parse_movie, release_group
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
    current_format_score: int | None = None
    current_scene_name: str | None = None
    has_file: bool = False
    aliases: list[str] = field(default_factory=list)
    minimum_seeders: dict[int, int] = field(default_factory=dict)  # indexer id -> min seeders
    # release gate (domain/availability.py): the movie's, and every in-scope episode's by (season, episode)
    availability: Availability | None = None
    air: dict[tuple[int, int], Availability] = field(default_factory=dict)
    matcher: FormatMatcher | None = None


@dataclass
class Decision:
    release: ReleaseInfo
    quality: ParsedQuality
    approved: bool
    rejections: list[str]
    score: int
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    release_gate: str | None = None  # the reference date that let it through (or held it back)
    custom_formats: list[dict[str, Any]] = field(default_factory=list)
    format_score: int = 0
    score_breakdown: list[dict[str, Any]] = field(default_factory=list)
    sort_key: tuple = field(default_factory=tuple)

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
            "release_gate": self.release_gate,
            "custom_formats": self.custom_formats,
            "format_score": self.format_score,
            "score_breakdown": self.score_breakdown,
        }


def evaluate(
    release: ReleaseInfo,
    target: Target,
    blocklist: set[str] | None = None,
    matcher: FormatMatcher | None = None,
) -> Decision:
    rejections: list[str] = []
    quality = parse_quality(release.title)
    wanted_titles = {normalize_title(t) for t in [target.title, *target.aliases] if t}
    season, episodes, full_season = None, [], False

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
            season, episodes, full_season = ep.season, ep.episodes, ep.full_season
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

    gate = release_gate(target, season, episodes, full_season)
    if gate is not None and not gate.available:
        rejections.append(f"Not released yet: {gate.reason}")

    # Custom formats evaluation
    active_matcher = matcher or target.matcher
    custom_formats_out: list[dict[str, Any]] = []
    fs = 0
    format_breakdown: list[tuple[str, int]] = []
    if active_matcher is not None:
        facts = ReleaseFacts(
            title=release.title,
            group=release_group(release.title),
            quality=quality,
            size_bytes=release.size_bytes or 0,
            episode_count=units,
            indexer_id=release.indexer_id,
            protocol=release.protocol,
        )
        matches = active_matcher.match(facts)
        fs, format_breakdown = score_formats(matches, profile.format_scores)
        custom_formats_out = [
            {
                "id": m.format_id,
                "name": m.name,
                "score": int(profile.format_scores.get(m.format_id, 0)),
            }
            for m in matches
        ]

    if fs < profile.min_format_score:
        rejections.append(f"Custom format score {fs} below minimum {profile.min_format_score}")

    if target.has_file:
        if target.current_scene_name and release.title.strip().lower() == target.current_scene_name.strip().lower():
            rejections.append("Release matches existing file's scene name")
        elif not profile.is_upgrade(target.current_quality, quality, current_fs=target.current_format_score, candidate_fs=fs):
            rejections.append(
                f"Existing file ({target.current_quality.quality.name if target.current_quality else 'unknown'}) is equal or better / cutoff met"
            )

    quality_rank = profile.rank(quality.quality) or 0
    codec_sc = profile.codec_score(quality.codec)
    revision = quality.revision if profile.prefer_proper else 1
    protocol_pref = 1 if release.protocol == "usenet" else 0
    capped_seeders = min(release.seeders or 0, 500)
    age_days = release.age_days or 0

    sort_key = (
        not rejections,
        quality_rank,
        fs,
        codec_sc,
        revision,
        protocol_pref,
        capped_seeders,
        -age_days,
    )

    score_breakdown: list[dict[str, Any]] = [
        {"label": f"Quality ({quality.quality.name})", "value": quality_rank * 1000},
    ]
    for fmt_name, fmt_score in format_breakdown:
        score_breakdown.append({"label": f"Format: {fmt_name}", "value": fmt_score})
    if codec_sc:
        score_breakdown.append({"label": f"Codec ({quality.codec.value})", "value": codec_sc * 10})
    if quality.proper or quality.repack:
        score_breakdown.append({"label": "PROPER / REPACK", "value": revision})
    if release.protocol == "usenet":
        score_breakdown.append({"label": "Usenet protocol", "value": 100})
    elif release.seeders:
        score_breakdown.append({"label": f"Seeders ({release.seeders})", "value": capped_seeders})
    if age_days:
        score_breakdown.append({"label": f"Age ({age_days}d)", "value": -min(age_days, 99)})

    base_score = profile.score(quality)
    display_score = (
        base_score * 1000
        + fs * 10
        + capped_seeders
        + (100 if release.protocol == "usenet" else 0)
        - min(age_days, 99)
    )
    return Decision(
        release=release,
        quality=quality,
        approved=not rejections,
        rejections=rejections,
        score=display_score,
        season=season,
        episodes=episodes,
        release_gate=gate.reason if gate else None,
        custom_formats=custom_formats_out,
        format_score=fs,
        score_breakdown=score_breakdown,
        sort_key=sort_key,
    )


def release_gate(target: Target, season: int | None, episodes: list[int], full_season: bool) -> Availability | None:
    """What decides whether the release's content is out: the movie's home release, or the air time of every
    episode it covers - a season pack needs the whole season to have aired. None when nothing is known to check."""
    if target.media_type == "movie":
        return target.availability
    if season is None:
        return None
    if full_season:
        covered = [a for (s, _), a in target.air.items() if s == season]
        if not covered and target.air:
            return Availability(False, f"no episodes of season {season} are known")
    else:
        covered = [target.air[(season, e)] for e in episodes if (season, e) in target.air]
    held = latest(a for a in covered if not a.available)
    if held and full_season:
        return Availability(False, f"season {season} hasn't finished airing ({held.reason})", held.reference)
    return held or latest(covered)


def rank(decisions: list[Decision]) -> list[Decision]:
    return sorted(decisions, key=lambda d: d.sort_key or (d.approved, d.score), reverse=True)
