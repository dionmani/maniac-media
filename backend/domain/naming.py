"""Template-driven naming engine.

Why not ``str.format``? v1 fed user templates straight into ``str.format``, which permits
attribute traversal (``{Title.__class__.__mro__}``) – a template-injection hole. This engine
only understands an allow-list of tokens, optional numeric padding (``{season:02d}``) and
sanitises every value so it can never introduce path separators or traversal segments.

Supported syntax
  {Token}            value of the token
  {token:02d}        zero padded integer
  /                  directory separator inside a template
Empty tokens collapse surrounding brackets and separators: "Movie [{Edition}]" -> "Movie".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_RX = re.compile(r"\{(?P<name>[A-Za-z][A-Za-z ]*?)(?::(?P<fmt>0?\d{0,2}d))?\}")
ILLEGAL_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MOVIE_TOKENS = {
    "title": "Movie title",
    "year": "Release year",
    "quality": "Quality name, e.g. Bluray-2160p",
    "resolution": "e.g. 1080p",
    "source": "e.g. BluRay",
    "codec": "e.g. H265",
    "imdbid": "IMDb id (tt...)",
    "tmdbid": "TMDB id",
    "releasegroup": "Release group",
    "edition": "Edition (Director's Cut...)",
    "originalname": "Original release name",
    "mediainfo_videocodec": "e.g. x264",
    "mediainfo_audiocodec": "e.g. EAC3",
    "mediainfo_audiochannels": "e.g. 5.1",
    "mediainfo_hdr": "e.g. HDR10",
    "mediainfo_resolution": "e.g. 1080p",
    "customformats": "Matched custom formats (e.g. DV HDR10 TrueHD Atmos)",
}
EPISODE_TOKENS = {
    "title": "Series title",
    "year": "Series year",
    "season": "Season number (use {season:02d})",
    "episode": "Episode number (use {episode:02d})",
    "episodetitle": "Episode title",
    "airdate": "Air date YYYY-MM-DD",
    "quality": "Quality name",
    "resolution": "e.g. 1080p",
    "source": "e.g. WEB-DL",
    "codec": "e.g. H264",
    "tvdbid": "TVDB id",
    "imdbid": "IMDb id",
    "releasegroup": "Release group",
    "originalname": "Original release name",
    "mediainfo_videocodec": "e.g. x264",
    "mediainfo_audiocodec": "e.g. EAC3",
    "mediainfo_audiochannels": "e.g. 5.1",
    "mediainfo_hdr": "e.g. HDR10",
    "mediainfo_resolution": "e.g. 1080p",
    "customformats": "Matched custom formats (e.g. DV HDR10)",
}
ALIASES = {"movietitle": "title", "seriestitle": "title", "episodename": "episodetitle", "group": "releasegroup", "customformat": "customformats"}


class TemplateError(ValueError):
    pass


def _key(name: str) -> str:
    k = name.replace(" ", "").lower()
    return ALIASES.get(k, k)


def sanitize_segment(value: str) -> str:
    value = value.replace(":", " -").replace("/", "-").replace("\\", "-")
    value = ILLEGAL_CHARS.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(". ")  # no hidden files; Windows forbids trailing dots/spaces
    if value in {".", ".."}:
        value = ""
    if value.split(".")[0].upper() in RESERVED_WINDOWS:
        value = f"_{value}"
    return value[:240]


def validate(template: str, allowed: dict[str, str]) -> list[str]:
    errors = []
    if not template.strip():
        errors.append("Template must not be empty")
    for m in TOKEN_RX.finditer(template):
        if _key(m.group("name")) not in allowed:
            errors.append(f"Unknown token {{{m.group('name')}}}")
    stripped = TOKEN_RX.sub("", template)
    if "{" in stripped or "}" in stripped:
        errors.append("Unbalanced or malformed braces")
    if template.startswith(("/", "\\")) or ".." in template.replace("...", ""):
        errors.append("Template must be relative and must not contain '..'")
    return errors


def render(template: str, values: dict[str, object], allowed: dict[str, str]) -> str:
    """Render ``template`` into a relative path (``/`` separated)."""
    errors = validate(template, allowed)
    if errors:
        raise TemplateError("; ".join(errors))
    normalised = {_key(k): v for k, v in values.items()}

    def replace(m: re.Match) -> str:
        value = normalised.get(_key(m.group("name")))
        fmt = m.group("fmt")
        if value is None or value == "":
            return ""
        if fmt:
            try:
                return format(int(value), fmt)
            except (TypeError, ValueError):
                return str(value)
        return str(value).replace("/", "-").replace("\\", "-")

    segments = []
    for raw_segment in re.split(r"[/\\]", template):
        rendered = TOKEN_RX.sub(replace, raw_segment)
        rendered = re.sub(r"\[\s*\]|\(\s*\)|\{\s*\}", "", rendered)  # empty brackets
        rendered = re.sub(r"(\s-\s*)+$", "", rendered)
        rendered = re.sub(r"\s-\s-\s", " - ", rendered)
        segment = sanitize_segment(rendered)
        if segment:
            segments.append(segment)
    if not segments:
        raise TemplateError("Template rendered to an empty path")
    return "/".join(segments)


@dataclass
class NamingPreview:
    template: str
    example: str | None
    errors: list[str]


def preview(template: str, kind: str) -> NamingPreview:
    if kind == "movie":
        allowed, sample = MOVIE_TOKENS, {
            "title": "Avatar", "year": 2009, "quality": "Bluray-2160p", "resolution": "2160p",
            "source": "BluRay", "codec": "H265", "imdbid": "tt0499549", "tmdbid": 19995, "releasegroup": "FRAMESTOR",
            "customformats": "DV HDR10 TrueHD Atmos",
        }
    else:
        allowed, sample = EPISODE_TOKENS, {
            "title": "Breaking Bad", "year": 2008, "season": 1, "episode": 1, "episodetitle": "Pilot",
            "airdate": "2008-01-20", "quality": "Bluray-1080p", "resolution": "1080p", "source": "BluRay",
            "codec": "H264", "tvdbid": 81189, "imdbid": "tt0903747", "releasegroup": "DEMAND",
            "customformats": "x264",
        }
    errors = validate(template, allowed)
    example = None if errors else render(template, sample, allowed)
    return NamingPreview(template, example, errors)
