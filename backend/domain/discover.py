"""Discover sections: which titles belong in each, as pure date logic.

A provider fetches candidates for a section; the windows and classification rules here decide what
counts as "in theaters", "coming to streaming" or "a new season starting", so every provider applies
the same definitions and they can be tested without the network.
"""
from __future__ import annotations

from datetime import date, timedelta

# media_type -> section key -> label, in display order
SECTIONS: dict[str, dict[str, str]] = {
    "movie": {
        "announced": "Announced & coming soon",
        "in_theaters": "In theaters",
        "streaming": "Coming to streaming & video",
    },
    "tv": {
        "new_series": "New shows",
        "new_seasons": "New seasons starting",
    },
}

# section -> (days back, days ahead) relative to today; None = open-ended
_WINDOWS: dict[str, tuple[int | None, int | None]] = {
    "announced": (-1, None),  # strictly after today
    "in_theaters": (60, 0),
    "streaming": (30, 60),
    "new_series": (30, 30),
    "new_seasons": (14, 30),
}

# a "returning" show must have premiered at least this long before today
RETURNING_MIN_AGE_DAYS = 180


def is_section(media_type: str, section: str) -> bool:
    return section in SECTIONS.get(media_type, {})


def window(section: str, today: date) -> tuple[date | None, date | None]:
    """Inclusive (start, end) dates a section covers; ``None`` means unbounded on that side."""
    back, ahead = _WINDOWS[section]
    start = today - timedelta(days=back) if back is not None else None
    end = today + timedelta(days=ahead) if ahead is not None else None
    return start, end


def in_window(value: str | None, start: date | None, end: date | None) -> bool:
    day = parse_date(value)
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def season_premiere(seasons: list[tuple[int, str | None]], start: date | None, end: date | None) -> tuple[int, str] | None:
    """The newest returning season (2+) whose premiere falls in the window, as (number, air_date).

    ``seasons`` is (season_number, season air_date). Season-level air dates make binge drops count
    the same as weekly premieres. Specials (0) and first seasons (new shows) are not "returning".
    """
    hits = [(n, d) for n, d in seasons if n >= 2 and d and in_window(d, start, end)]
    return max(hits, key=lambda s: s[0]) if hits else None


def home_release(digital: str | None, physical: str | None, start: date | None, end: date | None) -> tuple[str, str] | None:
    """Which home release puts a movie in "coming to streaming & video": (reason, date).

    Digital wins when both are in the window - it is what most people watch first.
    """
    if in_window(digital, start, end):
        return "digital", digital[:10]  # type: ignore[index]
    if in_window(physical, start, end):
        return "physical", physical[:10]  # type: ignore[index]
    return None
