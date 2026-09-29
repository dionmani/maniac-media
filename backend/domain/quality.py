"""Quality model: resolution x source, codecs, parsing and user-defined ranking.

Pure domain logic – no I/O, no ORM – so it is trivially unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Resolution(StrEnum):
    UNKNOWN = "unknown"
    SD = "SD"
    R720 = "720p"
    R1080 = "1080p"
    R2160 = "2160p"


class Source(StrEnum):
    UNKNOWN = "unknown"
    DVD = "DVD"
    HDTV = "HDTV"
    WEBRIP = "WEBRip"
    WEBDL = "WEB-DL"
    BLURAY = "BluRay"
    REMUX = "Remux"


class Codec(StrEnum):
    UNKNOWN = "unknown"
    XVID = "XviD"
    H264 = "H264"
    H265 = "H265"
    AV1 = "AV1"


@dataclass(frozen=True)
class Quality:
    name: str
    resolution: Resolution
    source: Source
    weight: int  # global natural order, higher = better


QUALITIES: list[Quality] = [
    Quality("Unknown", Resolution.UNKNOWN, Source.UNKNOWN, 0),
    Quality("SDTV", Resolution.SD, Source.HDTV, 10),
    Quality("DVD", Resolution.SD, Source.DVD, 20),
    Quality("WEBRip-480p", Resolution.SD, Source.WEBRIP, 25),
    Quality("WEBDL-480p", Resolution.SD, Source.WEBDL, 30),
    Quality("HDTV-720p", Resolution.R720, Source.HDTV, 40),
    Quality("WEBRip-720p", Resolution.R720, Source.WEBRIP, 45),
    Quality("WEBDL-720p", Resolution.R720, Source.WEBDL, 50),
    Quality("Bluray-720p", Resolution.R720, Source.BLURAY, 60),
    Quality("HDTV-1080p", Resolution.R1080, Source.HDTV, 70),
    Quality("WEBRip-1080p", Resolution.R1080, Source.WEBRIP, 75),
    Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80),
    Quality("Bluray-1080p", Resolution.R1080, Source.BLURAY, 90),
    Quality("Remux-1080p", Resolution.R1080, Source.REMUX, 100),
    Quality("HDTV-2160p", Resolution.R2160, Source.HDTV, 110),
    Quality("WEBRip-2160p", Resolution.R2160, Source.WEBRIP, 115),
    Quality("WEBDL-2160p", Resolution.R2160, Source.WEBDL, 120),
    Quality("Bluray-2160p", Resolution.R2160, Source.BLURAY, 130),
    Quality("Remux-2160p", Resolution.R2160, Source.REMUX, 140),
]
QUALITY_BY_NAME = {q.name.lower(): q for q in QUALITIES}
_BY_RES_SOURCE = {(q.resolution, q.source): q for q in QUALITIES}
UNKNOWN = QUALITIES[0]


def get_quality(name: str | None) -> Quality:
    if not name:
        return UNKNOWN
    q = QUALITY_BY_NAME.get(name.lower())
    return q if q else parse_quality(name).quality


@dataclass(frozen=True)
class ParsedQuality:
    quality: Quality
    codec: Codec
    proper: bool = False
    repack: bool = False
    hdr: bool = False

    @property
    def revision(self) -> int:
        return 2 if (self.proper or self.repack) else 1


_RES = [
    (re.compile(r"\b(2160p|4k|uhd)\b", re.I), Resolution.R2160),
    (re.compile(r"\b1080[pi]\b", re.I), Resolution.R1080),
    (re.compile(r"\b720p\b", re.I), Resolution.R720),
    (re.compile(r"\b(480p|576p|480i|576i|sd)\b", re.I), Resolution.SD),
]
_SRC = [
    (re.compile(r"\b(remux|bd[ .-]?remux)\b", re.I), Source.REMUX),
    (re.compile(r"\b(blu[ .-]?ray|bdrip|brrip|bd25|bd50)\b", re.I), Source.BLURAY),
    (re.compile(r"\b(web[ .-]?dl|amzn|nf|dsnp|hmax|atvp|itunes)\b", re.I), Source.WEBDL),
    (re.compile(r"\b(web[ .-]?rip|web)\b", re.I), Source.WEBRIP),
    (re.compile(r"\b(hdtv|pdtv|dsr|tvrip|sdtv)\b", re.I), Source.HDTV),
    (re.compile(r"\b(dvd[ .-]?rip|dvd|dvdr|dvd5|dvd9)\b", re.I), Source.DVD),
]
_CODEC = [
    (re.compile(r"\b(av1)\b", re.I), Codec.AV1),
    (re.compile(r"\b([xh][ .]?265|hevc)\b", re.I), Codec.H265),
    (re.compile(r"\b([xh][ .]?264|avc)\b", re.I), Codec.H264),
    (re.compile(r"\b(xvid|divx)\b", re.I), Codec.XVID),
]
_PROPER = re.compile(r"\bproper\b", re.I)
_REPACK = re.compile(r"\b(repack|rerip)\b", re.I)
_HDR = re.compile(r"\b(hdr10?\+?|hdr|dv|dovi|dolby[ .]?vision)\b", re.I)
# v1 stored free-form labels such as "1080p WebDL" or "2160p UHD BluRay"
_SPLIT = re.compile(r"[._\[\]()\s-]+")


def _first(patterns, text, default):
    for rx, value in patterns:
        if rx.search(text):
            return value
    return default


def parse_quality(text: str) -> ParsedQuality:
    normalised = _SPLIT.sub(" ", text or "")
    exact = QUALITY_BY_NAME.get((text or "").strip().lower())
    codec = _first(_CODEC, normalised, Codec.UNKNOWN)
    if exact:
        return ParsedQuality(exact, codec)

    res = _first(_RES, normalised, Resolution.UNKNOWN)
    src = _first(_SRC, normalised, Source.UNKNOWN)

    if src == Source.REMUX and res == Resolution.UNKNOWN:
        res = Resolution.R1080
    if src == Source.REMUX and res not in (Resolution.R1080, Resolution.R2160):
        src = Source.BLURAY
    if src == Source.DVD:
        res = Resolution.SD
    if res != Resolution.UNKNOWN and src == Source.UNKNOWN:
        src = Source.WEBDL if res == Resolution.R2160 else Source.HDTV
    if res == Resolution.UNKNOWN and src in (Source.HDTV, Source.WEBRIP, Source.WEBDL):
        res = Resolution.SD
    if res == Resolution.UNKNOWN and src == Source.BLURAY:
        res = Resolution.R720
    if res == Resolution.SD and src == Source.BLURAY:
        src = Source.DVD

    quality = _BY_RES_SOURCE.get((res, src))
    if quality is None and res == Resolution.SD:
        quality = QUALITY_BY_NAME["sdtv"]
    return ParsedQuality(
        quality or UNKNOWN,
        codec,
        proper=bool(_PROPER.search(normalised)),
        repack=bool(_REPACK.search(normalised)),
        hdr=bool(_HDR.search(normalised)),
    )


# --------------------------------------------------------------------------- profiles
@dataclass
class QualityProfileSpec:
    """Domain view of a quality profile. ``allowed`` is ordered best-first."""

    name: str
    allowed: list[str]
    cutoff: str | None = None
    upgrade_allowed: bool = True
    preferred_codecs: list[str] = field(default_factory=list)
    rejected_codecs: list[str] = field(default_factory=list)
    min_size_mb: int = 0
    max_size_mb: int = 0
    prefer_proper: bool = True

    def rank(self, quality: Quality | str) -> int | None:
        name = quality if isinstance(quality, str) else quality.name
        lowered = [a.lower() for a in self.allowed]
        try:
            return len(lowered) - lowered.index(name.lower())
        except ValueError:
            return None

    def is_allowed(self, quality: Quality) -> bool:
        return self.rank(quality) is not None

    def codec_score(self, codec: Codec) -> int:
        lowered = [c.lower() for c in self.preferred_codecs]
        if codec.value.lower() in lowered:
            return len(lowered) - lowered.index(codec.value.lower())
        return 0

    def codec_rejected(self, codec: Codec) -> bool:
        return codec.value.lower() in {c.lower() for c in self.rejected_codecs}

    def cutoff_met(self, quality: Quality | str | None) -> bool:
        if quality is None:
            return False
        current = self.rank(quality if isinstance(quality, str) else quality.name)
        if current is None:
            return False
        cutoff = self.rank(self.cutoff) if self.cutoff else None
        if cutoff is None:
            cutoff = max(1, len(self.allowed))
        return current >= cutoff

    def score(self, parsed: ParsedQuality) -> int:
        rank = self.rank(parsed.quality) or 0
        score = rank * 1000 + self.codec_score(parsed.codec) * 10
        if self.prefer_proper:
            score += parsed.revision
        return score

    def is_upgrade(self, current: ParsedQuality | None, candidate: ParsedQuality) -> bool:
        if not self.is_allowed(candidate.quality):
            return False
        if current is None or current.quality == UNKNOWN:
            return True
        if not self.upgrade_allowed:
            return False
        if self.cutoff_met(current.quality):
            # Cutoff reached: only a PROPER/REPACK of the same quality is still worth taking.
            return self.rank(candidate.quality) == self.rank(current.quality) and candidate.revision > current.revision
        return self.score(candidate) > self.score(current)


DEFAULT_PROFILES: list[dict] = [
    {
        "name": "Any",
        "cutoff": "WEBDL-1080p",
        "items": [q.name for q in sorted(QUALITIES, key=lambda q: -q.weight) if q.weight > 0],
        "preferred_codecs": ["H265", "AV1", "H264"],
    },
    {
        "name": "HD-1080p",
        "cutoff": "Bluray-1080p",
        "items": ["Remux-1080p", "Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p", "HDTV-1080p", "Bluray-720p", "WEBDL-720p", "WEBRip-720p", "HDTV-720p"],
        "preferred_codecs": ["H265", "H264"],
    },
    {
        "name": "Ultra-HD",
        "cutoff": "Remux-2160p",
        "items": ["Remux-2160p", "Bluray-2160p", "WEBDL-2160p", "WEBRip-2160p", "HDTV-2160p"],
        "preferred_codecs": ["H265", "AV1"],
    },
    {
        "name": "SD",
        "cutoff": "DVD",
        "items": ["WEBDL-480p", "WEBRip-480p", "DVD", "SDTV"],
        "preferred_codecs": [],
    },
]
