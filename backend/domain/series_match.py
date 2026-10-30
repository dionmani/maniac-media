"""Which of several same-titled shows a release is for: evidence scoring with a confidence level.

A title alone can't tell Dark Matter (2015) from Dark Matter (2024), or the British Ghosts from the American one.
Each piece of evidence in or about the release scores for or against each candidate show:

    explicit year / country tag in the name     +100, or rules the show out when it names another
    indexer's TVDB / TVmaze id                  +60 / -60 (IMDb only ever +60: some indexers send the episode's id)
    only this show has the episode              +40 (and -40 for a show without it)
    posted within a month of this show airing   +30
    streaming tag matches the show's network    +15 ("ATVP" and Apple TV+)
    original show, when the name has no tags     +5 (scene convention: the later show carries the year)

The target wins with ``certain`` (100+, or 60+ when no other show shares the title) or ``likely`` (30+ ahead of
every other show); too close to call is ``ambiguous``, and losing or being ruled out is ``mismatch``. Automatic
grabs take only certain/likely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backend.domain.parser import ParsedEpisode, normalize_title, series_identity, split_title_tag, years_conflict

CERTAIN, LIKELY, AMBIGUOUS, MISMATCH = "certain", "likely", "ambiguous", "mismatch"
ACCEPTED = {CERTAIN, LIKELY}

# streaming tag (domain.quality.parse_quality) -> words in a network name that mean the same service
_NETWORK_WORDS = {
    "AMZN": ("amazon", "prime video"), "NF": ("netflix",), "DSNP": ("disney",), "ATVP": ("apple",), "HMAX": ("hbo", "max"),
    "MAX": ("hbo", "max"), "PCOK": ("peacock",), "PMTP": ("paramount",), "HULU": ("hulu",), "STAN": ("stan",),
    "iPlayer": ("bbc",), "CR": ("crunchyroll",),
}
_AIR_WINDOW = timedelta(days=30)
_EARLY_LEAK = timedelta(days=3)
LIKELY_MARGIN = 30


@dataclass
class SeriesFacts:
    """What a library show offers to tell it apart from a same-titled one."""

    id: int
    title: str
    year: int | None = None
    country: str | None = None
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    imdb_id: str | None = None
    network: str | None = None
    aired: dict[tuple[int, int], date | None] = field(default_factory=dict)  # (season, episode) -> air date


@dataclass
class ReleaseEvidence:
    """What a release says about which show it is for."""

    series_title: str
    year: int | None
    season: int | None
    episodes: list[int]
    full_season: bool = False
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    imdb_id: str | None = None
    posted: date | None = None
    streaming: str | None = None

    @classmethod
    def from_release(cls, ep: ParsedEpisode, *, tvdb_id=None, tvmaze_id=None, imdb_id=None,
                     posted: date | datetime | None = None) -> ReleaseEvidence:
        return cls(ep.series_title, ep.year, ep.season, list(ep.episodes), ep.full_season, tvdb_id, tvmaze_id, imdb_id,
                   posted.date() if isinstance(posted, datetime) else posted,
                   ep.quality.streaming_service if ep.quality else None)


@dataclass
class SeriesMatch:
    level: str
    score: int
    reasons: list[str]
    other: str | None = None  # mismatch: the show it is for instead; ambiguous: the show it can't be told from
    other_id: int | None = None
    ruled_out: bool = False  # the name itself contradicts the show (a year or country tag naming another)

    @property
    def accepted(self) -> bool:
        return self.level in ACCEPTED

    def rejection(self) -> str | None:
        why = f" ({'; '.join(self.reasons)})" if self.reasons else ""
        if self.level == MISMATCH:
            return f"Release is for {self.other}{why}" if self.other else f"Release is for another show{why}"
        if self.level == AMBIGUOUS:
            return f"Can't tell this show from {self.other}{why}"
        return None

    def to_dict(self) -> dict:
        return {"level": self.level, "score": self.score, "reasons": self.reasons}


def _label(s: SeriesFacts) -> str:
    extra = [str(x) for x in series_identity(s) if x and f"({x})" not in s.title]
    return f"{s.title} ({', '.join(extra)})" if extra else s.title


def _wanted(r: ReleaseEvidence, s: SeriesFacts) -> list[tuple[int, int]]:
    if r.season is None:
        return []
    if r.full_season or not r.episodes:
        return [k for k in s.aired if k[0] == r.season]
    return [(r.season, e) for e in r.episodes]


def _air_date(r: ReleaseEvidence, s: SeriesFacts) -> date | None:
    """The air date a release of these episodes would follow: the last of them (a season pack waits for the finale)."""
    dates = [d for k in _wanted(r, s) if (d := s.aired.get(k))]
    return max(dates) if dates else None


def _has_episodes(r: ReleaseEvidence, s: SeriesFacts) -> bool:
    if r.season is None:
        return True
    if r.full_season or not r.episodes:
        return any(k[0] == r.season for k in s.aired)
    return all((r.season, e) in s.aired for e in r.episodes)


def _evidence(r: ReleaseEvidence, s: SeriesFacts, others: list[SeriesFacts]) -> tuple[int, list[str], bool]:
    """(score, reasons, ruled out) for one candidate show."""
    score, reasons = 0, []
    year, country = series_identity(s)
    if years_conflict(r.year, year):
        return 0, [f"named year {r.year}, this show is {year}"], True
    if r.year and year:
        score += 100
        reasons.append(f"year {r.year}")
    release_country = split_title_tag(r.series_title, bare=True)[2]
    if release_country and normalize_title(r.series_title) == normalize_title(s.title):
        score += 100  # the tag is part of this show's own title ("Ghosts US" for "Ghosts (US)")
        reasons.append(f"country {release_country} in the title")
    elif release_country:
        if country and release_country != country:
            return 0, [f"tagged {release_country}, this show is {country}"], True
        if country:
            score += 100
            reasons.append(f"country {release_country}")
        else:
            score -= 40
            reasons.append(f"tagged {release_country}, this show's country is unknown")

    if r.tvdb_id and s.tvdb_id:
        same = r.tvdb_id == s.tvdb_id
        score += 60 if same else -60
        reasons.append(f"indexer TVDB {r.tvdb_id}" + ("" if same else f", not {s.tvdb_id}"))
    elif r.tvmaze_id and s.tvmaze_id:
        same = r.tvmaze_id == s.tvmaze_id
        score += 60 if same else -60
        reasons.append(f"indexer TVmaze {r.tvmaze_id}" + ("" if same else f", not {s.tvmaze_id}"))
    if r.imdb_id and s.imdb_id and r.imdb_id == s.imdb_id:
        score += 60
        reasons.append(f"indexer IMDb {r.imdb_id}")

    if others and s.aired and all(o.aired for o in others):
        mine, theirs = _has_episodes(r, s), [_has_episodes(r, o) for o in others]
        if mine and not all(theirs):
            score += 40
            reasons.append(f"only this show has {_episode_label(r)}")
        elif not mine and any(theirs):
            score -= 40
            reasons.append(f"this show has no {_episode_label(r)}")

    aired = _air_date(r, s)
    if r.posted and aired and aired - _EARLY_LEAK <= r.posted <= aired + _AIR_WINDOW:
        score += 30
        reasons.append(f"posted {r.posted.isoformat()}, aired {aired.isoformat()}")

    network = (s.network or "").lower()
    if r.streaming and network and any(w in network for w in _NETWORK_WORDS.get(r.streaming, ())):
        score += 15
        reasons.append(f"{r.streaming} on {s.network}")
    return score, reasons, False


def _episode_label(r: ReleaseEvidence) -> str:
    if r.season is None:
        return "these episodes"
    if r.full_season or not r.episodes:
        return f"season {r.season}"
    return f"S{r.season:02d}" + "".join(f"E{e:02d}" for e in r.episodes)


def match_series(release: ReleaseEvidence, target: SeriesFacts, others: list[SeriesFacts]) -> SeriesMatch:
    """How sure we are that ``release`` is for ``target`` rather than one of the same-titled ``others``."""
    scored = {}
    for s in [target, *others]:
        scored[s.id] = _evidence(release, s, [o for o in [target, *others] if o is not s])
    t_score, t_reasons, t_out = scored[target.id]
    if t_out:
        return SeriesMatch(MISMATCH, 0, t_reasons, ruled_out=True)
    if not release.year and not split_title_tag(release.series_title, bare=True)[2] and others:
        # the untagged-original convention: a nudge only, never enough on its own
        oldest = min([target, *others], key=lambda s: series_identity(s)[0] or 9999)
        if series_identity(oldest)[0]:
            score, reasons, out = scored[oldest.id]
            scored[oldest.id] = (score + 5, [*reasons, "no year or country: the original show"], out)
            t_score, t_reasons, _ = scored[target.id]

    rivals = [(scored[o.id][0], o) for o in others if not scored[o.id][2]]
    best_rival = max(rivals, key=lambda x: x[0], default=None)
    if not others:
        if t_score < 0:
            return SeriesMatch(MISMATCH, t_score, t_reasons)
        return SeriesMatch(CERTAIN if t_score >= 60 else LIKELY, t_score, t_reasons)
    if best_rival is None:
        level = CERTAIN if t_score >= 60 else LIKELY if t_score >= 0 else MISMATCH
        return SeriesMatch(level, t_score, t_reasons + [f"{len(others)} same-titled show(s) ruled out"])
    rival_score, rival = best_rival
    if rival_score > t_score:
        return SeriesMatch(MISMATCH, t_score, scored[rival.id][1], other=_label(rival), other_id=rival.id)
    if t_score >= 100 and t_score > rival_score:
        return SeriesMatch(CERTAIN, t_score, t_reasons)
    if t_score - rival_score >= LIKELY_MARGIN:
        return SeriesMatch(LIKELY, t_score, t_reasons)
    return SeriesMatch(AMBIGUOUS, t_score, [*t_reasons, f"{t_score} vs {rival_score}"], other=_label(rival), other_id=rival.id)


def best_series(release: ReleaseEvidence, candidates: list[SeriesFacts]) -> tuple[SeriesFacts | None, SeriesMatch | None]:
    """The candidate a release is for, with its match (None when no candidate is accepted)."""
    for c in candidates:
        m = match_series(release, c, [o for o in candidates if o is not c])
        if m.accepted:
            return c, m
    return None, None


def parse_air_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None

