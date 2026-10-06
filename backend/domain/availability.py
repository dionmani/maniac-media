"""Release gate: nothing is searched for or queued before it is actually out.

Pre-release grabs are CAMs, telesyncs, fakes, placeholder files and malware, so the rule is strict:

* a movie becomes available on its home release (digital/streaming or physical), never on its
  theatrical or festival date;
* an episode becomes available at its own air time;
* anything without a confirmed date is held.

Every answer names the reference date it was based on; search results, job results and the grab's
history entry carry it, so each decision can be audited.

The SQL in ``db/repositories.py`` (``MovieRepository.released``, ``EpisodeRepository.aired``)
expresses the same rules for list queries and is tested against these functions - change both
together.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# A movie whose first release is older than this but has no home-release date on record is back
# catalogue: its home release happened long ago and just isn't listed for the metadata region.
# 0 turns the fallback off, so only a digital or physical date ever counts.
DEFAULT_CATALOG_DAYS = 365

# An air date without a time could be in any timezone. It has certainly aired once that date is
# over everywhere (UTC-12), i.e. 36 hours after it started in UTC.
DATE_ONLY_DELAY = timedelta(hours=36)

# Air times only matter around now: an episode dated longer ago than this is past DATE_ONLY_DELAY.
AIR_TIME_WINDOW = timedelta(days=2)


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str  # always names the reference date, or says there is none
    reference: str | None = None  # ISO date or "YYYY-MM-DD HH:MM" (UTC) the decision was based on


def catalog_cutoff(today: date, catalog_days: int) -> date | None:
    """First releases on or before this date count as back catalogue (None = fallback off)."""
    return today - timedelta(days=catalog_days) if catalog_days > 0 else None


def movie_availability(*, digital: str | None, physical: str | None, release_date: str | None, year: int | None,
                       today: date, catalog_days: int = DEFAULT_CATALOG_DAYS) -> Availability:
    t = today.isoformat()
    home = sorted((d[:10], kind) for kind, d in (("Digital", digital), ("Physical", physical)) if d)
    out = [(day, kind) for day, kind in home if day <= t]
    if out:
        day, kind = out[0]
        return Availability(True, f"{kind} release {day}", day)
    cutoff = catalog_cutoff(today, catalog_days)
    if cutoff:
        if release_date and release_date[:10] <= cutoff.isoformat():
            day = release_date[:10]
            return Availability(True, f"Back catalogue: released {day}, over {catalog_days} days ago", day)
        if not release_date and year and year < cutoff.year:
            return Availability(True, f"Back catalogue: released {year}, over {catalog_days} days ago", str(year))
    if home:
        day, kind = home[0]
        return Availability(False, f"{kind} release is {day}", day)
    if release_date:
        return Availability(False, f"No digital or physical release date yet (first released {release_date[:10]})", None)
    return Availability(False, "No release date on record", None)


def date_only_cutoff(now: datetime) -> str:
    """The latest air date (without a time) that counts as aired at ``now`` (naive UTC)."""
    return (now - DATE_ONLY_DELAY).date().isoformat()


def episode_availability(*, air_date: str | None, air_date_utc: datetime | None, now: datetime) -> Availability:
    """``air_date_utc`` and ``now`` are naive UTC, like every DateTime column."""
    if air_date_utc is not None:
        stamp = f"{air_date_utc:%Y-%m-%d %H:%M}"
        if air_date_utc <= now:
            return Availability(True, f"Aired {stamp} UTC", stamp)
        return Availability(False, f"Airs {stamp} UTC", stamp)
    if air_date:
        day = air_date[:10]
        if day <= date_only_cutoff(now):
            return Availability(True, f"Aired {day}", day)
        return Availability(False, f"Airs {day} (no air time known: held until that date is over everywhere)", day)
    return Availability(False, "Air date TBA", None)


def latest(items: Iterable[Availability]) -> Availability | None:
    """The availability with the latest reference - what a multi-episode grab was waiting on."""
    return max(items, key=lambda a: a.reference or "", default=None)


def wants_air_times(air_dates: Iterable[str | None], today: date) -> bool:
    """True when some episode is dated recently enough (or in the future) that its exact time matters."""
    since = (today - AIR_TIME_WINDOW).isoformat()
    return any(d and d[:10] >= since for d in air_dates)
