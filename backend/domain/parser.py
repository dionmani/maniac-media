"""Release / file name parsing for movies and episodes."""
from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePath

from backend.domain.quality import ParsedQuality, parse_quality

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm", ".flv"}
PARTIAL_EXTENSIONS = {".part", ".!qb", ".!ut", ".crdownload", ".tmp", ".partial", ".download", ".bc!", ".dlpart"}
# Only as the last word of a name ("show-grp-sample", "Movie-trailer"), or "sample" as the first: a word
# inside a title ("Extra Large Medium", "The Trailer Park", the show "Extras") is not a sample.
SAMPLE_RX = re.compile(r"^sample(?:[\W_]|$)|[\W_](?:sample|trailer|featurette|extras?)$", re.I)
_TRAILING_PUNCT = re.compile(r"[\W_]+$")

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
    return bool(SAMPLE_RX.search(_TRAILING_PUNCT.sub("", PurePath(name).stem)))


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


# ---------------------------------------------------------------- title guessing (manual import "smart match")
# Anything from the first of these tokens onward is release noise, not title. Deliberately excludes words that are
# also common in real titles ("Web", "The French Connection", "Cam", "Complete").
_JUNK = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d{3,4}[pi]|[48]k|uhd|"                                                   # resolution
    r"blu-?ray|bdrip|brrip|bdremux|remux|web-?dl|web-?rip|webhd|hdtv|pdtv|dvd-?rip|dvd-?scr|dvd[59r]|hd-?rip|hdcam|telesync|"  # source
    r"[xh]\.?26[45]|hevc|avc|xvid|divx|av1|vp9|10-?bit|8-?bit|"                  # codec
    r"dts(?:-?hd)?(?:-?ma)?|dd[p+]?\d(?:\.\d)?|truehd|atmos|aac\d?(?:\.\d)?|e?-?ac-?3|flac|"   # audio
    r"hdr10\+?|hdr|dolby[ ._-]?vision|sdr|hlg|"                                  # dynamic range
    r"proper|repack|rerip|extended|unrated|uncut|remastered|theatrical|directors?[ ._-]?cut|imax|"
    r"multi|dual|dubbed|vostfr|truefrench|amzn|dsnp|hmax|atvp|pcok"              # edition / language / streaming tags
    r")(?![A-Za-z0-9])",
    re.I,
)
_YEAR_IN_TITLE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?![\dpPiIxX])")
_BRACKET_YEAR = re.compile(r"[\[(]((?:19|20)\d{2})[\])]")
_LEADING_TAG = re.compile(r"^(?:\s*[\[(][^\])]*[\])]\s*|\s*www\.\S+?\.[a-z]{2,4}\s*[-–]\s*)+", re.I)
_SEASON_DIR = re.compile(r"^(?:season[ ._-]*\d+|s\d{1,2}|series[ ._-]*\d+|specials?)$", re.I)
_EXTRAS_DIR = re.compile(r"^(?:extras?|featurettes?|subs?|subtitles?|sample)$", re.I)
_GENERIC_NAME = re.compile(r"^(?:movie|video|film|sample|file|vid|cd ?\d|disc ?\d|part ?\d|\d{1,2})$", re.I)


@dataclass
class TitleGuess:
    query: str
    year: int | None = None
    kind: str = "unknown"            # "tv" when the name carries an episode/season marker or sits in a Season folder
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    imdb_id: str | None = None
    from_folder: bool = False        # the file name had no usable title; it came from a parent folder


def _strip_noise(raw: str) -> tuple[str, int | None]:
    """'The.Matrix.1999.1080p.BluRay.x264-GRP' -> ('The Matrix', 1999)."""
    text = _LEADING_TAG.sub("", raw.replace("_", " "))   # 1:1 replacement keeps indexes valid
    cut = len(text)
    for rx in (_JUNK, re.compile(r"[\[(]"), *_EP_PATTERNS, _SEASON_PACK):
        m = rx.search(text)
        if m:
            cut = min(cut, m.start())
    head = text[:cut]
    year = None
    if m := _BRACKET_YEAR.search(text):          # "Blade Runner 2049 (2017)": the bracketed year is the release year
        year = int(m.group(1))
    else:
        # last year before the noise that isn't the very first token ("2012 2009", "Blade Runner 2049 2017")
        in_head = [y for y in _YEAR_IN_TITLE.finditer(head) if y.start() > 0]
        after = [y for y in _YEAR_IN_TITLE.finditer(text, cut) if y.start() > 0]   # "Movie 1080p 2019"
        if in_head:
            year, head = int(in_head[-1].group(1)), head[: in_head[-1].start()]
        elif after:
            year = int(after[-1].group(1))
    return clean_title(head).strip(" -–.,"), year


def _meaningful(title: str) -> bool:
    return len(re.sub(r"[^A-Za-z0-9]", "", title)) >= 2 and not _GENERIC_NAME.match(title.strip())


def guess_title(path: str) -> TitleGuess:
    """Best-effort search query (title + year + episode hints) for a media file path.

    Looks at the file name first; when that has no usable title (``S01E02.mkv``, ``movie.mkv``) it walks up the
    parent folders, skipping ``Season 01`` style directories. A year in the parent folder ("The Matrix (1999)")
    fills in a year the file name lacks.
    """
    p = PurePath(path.replace("\\", "/"))
    parents = [x.name for x in list(p.parents)[:3] if x.name and not x.name.endswith(":")]
    imdb = _IMDB.search(path)
    stem = _stem(p.name)
    title, year = _strip_noise(stem)
    guess = TitleGuess(query=title, year=year, imdb_id=imdb.group(1) if imdb else None)
    if ep := parse_episode(stem):
        guess.kind, guess.season, guess.episodes = "tv", ep.season, ep.episodes
    if not _meaningful(title):
        guess.query = ""
        for name in parents:
            if _SEASON_DIR.match(name) or _EXTRAS_DIR.match(name):
                continue
            t, y = _strip_noise(name)
            if _meaningful(t):
                guess.query, guess.year, guess.from_folder = t, year or y, True
                break
    elif year is None:
        for name in parents[:2]:
            t, y = _strip_noise(name)
            if y and normalize_title(t) == normalize_title(title):
                guess.year = y
                break
    if guess.kind == "unknown" and any(_SEASON_DIR.match(n) for n in parents[:2]):
        guess.kind = "tv"
    return guess


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


_TMDB_URL = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.I)
_TVDB_URL = re.compile(r"thetvdb\.com/.*?(?:[?&]id=|/series/|/dereferrer/series/)(\d+)\b", re.I)
_PREFIXED = re.compile(r"^(imdb|tmdb|tvdb)[\s:=#-]*(tt\d+|\d+)$", re.I)


def parse_external_id(text: str, media_type: str) -> dict[str, int | str]:
    """What the user typed into "Link to…" -> provider ids, e.g. {'imdb_id': 'tt0163651'}.

    Accepts IMDb ids/URLs, TMDB/TVDB URLs, 'tmdb:123'/'tvdb:123', or a bare number – which means
    TMDB for movies and TVDB for series (the id those apps show). Empty dict when nothing matched.
    """
    text = (text or "").strip()
    if not text:
        return {}
    if m := _IMDB.search(text):
        return {"imdb_id": m.group(1)}
    if m := _TMDB_URL.search(text):
        return {"tmdb_id": int(m.group(2))}
    if m := _TVDB_URL.search(text):
        return {"tvdb_id": int(m.group(1))}
    if m := _PREFIXED.match(text):
        source, value = m.group(1).lower(), m.group(2)
        if source == "imdb":
            return {"imdb_id": value if value.startswith("tt") else f"tt{value.zfill(7)}"}
        return {} if value.startswith("tt") else {f"{source}_id": int(value)}
    if text.isdigit():
        return {"tmdb_id" if media_type == "movie" else "tvdb_id": int(text)}
    return {}


def normalise_path_key(path: str) -> str:
    """Canonical key for path deduplication and path_hash:
    normcase + normpath + casefold, normalized to forward slashes.
    Handles Windows drive letters, UNC paths, trailing slashes, and \\?\\ prefixes.
    """
    if not path:
        return ""
    p = str(path).strip()
    if p.startswith(("\\\\?\\UNC\\", "\\\\?\\unc\\", "//?/UNC/", "//?/unc/")):
        p = "\\\\" + p[8:]
    elif p.startswith(("\\\\?\\", "//?/")):
        p = p[4:]

    is_unc = p.startswith(("\\\\", "//")) and not p.startswith(("\\\\\\", "///"))
    p = p.replace("\\", "/")

    if is_unc:
        rest = p[2:]
        while "//" in rest:
            rest = rest.replace("//", "/")
        p = "//" + rest
    else:
        while "//" in p:
            p = p.replace("//", "/")

    p = posixpath.normpath(p).casefold()

    if len(p) > 1 and p.endswith("/"):
        if not (len(p) == 3 and p[1] == ":"):
            p = p[:-1]

    return p

