"""Release / file name parsing for movies and episodes."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePath

from backend.domain.quality import ParsedQuality, parse_quality

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm", ".flv"}
PARTIAL_EXTENSIONS = {".part", ".!qb", ".!ut", ".crdownload", ".tmp", ".partial", ".download", ".bc!", ".dlpart"}
SAMPLE_RX = re.compile(r"(^|[\W_])(sample|trailer|featurette|extras?)([\W_]|$)", re.I)

_YEAR = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-9]{2})(?!\d)")
_IMDB = re.compile(r"\b(tt\d{7,9})\b")
_GROUP = re.compile(r"-([A-Za-z0-9]{2,20})(?:\[[^\]]*\])?$")
_EP_PATTERNS = [
    # S01E01, S01E01E02, S01E01-E03, S01E01-03
    re.compile(r"\bS(?P<s>\d{1,2})[ ._-]?E(?P<e>\d{1,3})(?:(?:[ ._-]?E|-E?)(?P<e2>\d{1,3}))*", re.I),
    # 1x01, 1x01-1x02
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{2,3})(?:[-_]?\d{1,2}x(?P<e2>\d{2,3}))?\b", re.I),
]
_SEASON_PACK = re.compile(r"\b(?:S(?P<s>\d{1,2})(?![ ._-]?E\d)|Season[ ._-]?(?P<s2>\d{1,2}))\b", re.I)
_DAILY = re.compile(r"\b(?P<y>(?:19|20)\d{2})[ ._-](?P<m>\d{2})[ ._-](?P<d>\d{2})\b")
_EP_NUMBERS = re.compile(r"(?:E|-)(\d{1,3})", re.I)


def clean_title(raw: str) -> str:
    text = raw.replace("_", " ").replace(".", " ")
    text = re.sub(r"\s+", " ", text).strip(" -[]{}")
    text = text.lstrip("(")
    while text.endswith("(") or (text.endswith(")") and text.count("(") < text.count(")")):
        text = text[:-1].rstrip()
    if text.count("(") > text.count(")"):
        text += ")"
    return text


def normalize_title(title: str) -> str:
    """Canonical key for matching: accents stripped, punctuation dropped, '&' -> 'and'."""
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    t = t.lower().replace("&", " and ")
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def is_video(path: str | PurePath) -> bool:
    return PurePath(path).suffix.lower() in VIDEO_EXTENSIONS


def is_partial(path: str | PurePath) -> bool:
    return PurePath(path).suffix.lower() in PARTIAL_EXTENSIONS


def is_sample(name: str) -> bool:
    return bool(SAMPLE_RX.search(PurePath(name).stem))


def release_group(name: str) -> str | None:
    stem = PurePath(name).stem if is_video(name) else name
    m = _GROUP.search(stem.strip())
    if m and not m.group(1).lower() in {"dl", "web", "rip"}:
        return m.group(1)
    return None


@dataclass
class ParsedMovie:
    title: str
    year: int | None
    quality: ParsedQuality
    imdb_id: str | None = None
    release_group: str | None = None


@dataclass
class ParsedEpisode:
    series_title: str
    season: int | None
    episodes: list[int] = field(default_factory=list)
    full_season: bool = False
    air_date: str | None = None
    quality: ParsedQuality | None = None
    release_group: str | None = None
    year: int | None = None


def _stem(name: str) -> str:
    p = PurePath(name)
    return p.stem if p.suffix.lower() in VIDEO_EXTENSIONS | PARTIAL_EXTENSIONS else p.name


def parse_movie(name: str) -> ParsedMovie:
    stem = _stem(name)
    imdb = _IMDB.search(stem)
    years = list(_YEAR.finditer(stem))
    # the last plausible year that is not at the very start is the release year ("2001 A Space Odyssey 1968")
    year_match = next((m for m in reversed(years) if m.start() > 0), None)
    if year_match:
        title = stem[: year_match.start()]
        year = int(year_match.group(1))
    else:
        cut = re.search(r"[\[(]|\b(2160p|1080p|720p|480p|bluray|web|hdtv|remux|dvdrip)\b", stem, re.I)
        title = stem[: cut.start()] if cut and cut.start() > 0 else stem
        year = None
    return ParsedMovie(
        title=clean_title(title),
        year=year,
        quality=parse_quality(stem),
        imdb_id=imdb.group(1) if imdb else None,
        release_group=release_group(stem),
    )


def parse_episode(name: str) -> ParsedEpisode | None:
    stem = _stem(name)
    for index, rx in enumerate(_EP_PATTERNS):
        m = rx.search(stem)
        if not m:
            continue
        season = int(m.group("s"))
        tail = m.group(0)
        if index == 0:
            ep_part = tail[tail.upper().index("E", 1):]
            nums = [int(x) for x in _EP_NUMBERS.findall(ep_part)]
        else:
            nums = [int(m.group("e"))] + ([int(m.group("e2"))] if m.group("e2") else [])
        if len(nums) == 2 and "-" in tail and nums[1] > nums[0]:
            episodes = list(range(nums[0], nums[1] + 1))  # S01E01-E03 is a range
        else:
            episodes = sorted(set(nums))
        return _finish(stem[: m.start()], season, episodes, False, None, stem)

    d = _DAILY.search(stem)
    if d:
        title = stem[: d.start()]
        return _finish(title, None, [], False, f"{d.group('y')}-{d.group('m')}-{d.group('d')}", stem)

    p = _SEASON_PACK.search(stem)
    if p:
        season = int(p.group("s") or p.group("s2"))
        return _finish(stem[: p.start()], season, [], True, None, stem)
    return None


def _finish(title_part: str, season, episodes, full_season, air_date, stem) -> ParsedEpisode:
    year = None
    ym = _YEAR.search(title_part)
    if ym and ym.start() > 0:
        year = int(ym.group(1))
        title_part = title_part[: ym.start()]
    return ParsedEpisode(
        series_title=clean_title(title_part),
        season=season,
        episodes=episodes,
        full_season=full_season,
        air_date=air_date,
        quality=parse_quality(stem),
        release_group=release_group(stem),
        year=year,
    )


def parse_folder_title(folder_name: str) -> tuple[str, int | None, str | None]:
    """'Avatar (2009) {imdb-tt0499549}' -> ('Avatar', 2009, 'tt0499549')."""
    imdb = _IMDB.search(folder_name)
    # A bracketed year wins: "Blade Runner 2049 (2017)" is the 2017 film "Blade Runner 2049".
    m = re.match(r"^(.*?)[\s._]*[(\[]((?:19|20)\d{2})[)\]]", folder_name)
    if m and m.group(1).strip():
        return clean_title(m.group(1)), int(m.group(2)), imdb.group(1) if imdb else None
    years = [y for y in _YEAR.finditer(folder_name) if y.start() > 0]
    if years:
        last = years[-1]
        return clean_title(folder_name[: last.start()]), int(last.group(1)), imdb.group(1) if imdb else None
    return clean_title(re.sub(r"[\[{(].*$", "", folder_name)) or folder_name, None, imdb.group(1) if imdb else None
