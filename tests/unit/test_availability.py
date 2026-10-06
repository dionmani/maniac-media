"""Release gate: movies wait for a home release, episodes for their air time, and undated titles are held."""
from datetime import date, datetime

import pytest

from backend.domain.availability import (
    Availability,
    episode_availability,
    latest,
    movie_availability,
    wants_air_times,
)
from backend.domain.quality import QualityProfileSpec
from backend.domain.types import ReleaseInfo
from backend.plugins.metadata.tvmaze import _airstamp
from backend.services.decision_engine import Target, evaluate, release_gate

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 18, 0)


def movie(**kw):
    args = {"digital": None, "physical": None, "release_date": None, "year": None, "today": TODAY} | kw
    return movie_availability(**args)


# ------------------------------------------------------------------ movies
def test_movie_is_out_on_its_digital_or_physical_date():
    assert movie(digital="2026-10-06", release_date="2026-08-01") == Availability(True, "Digital release 2026-10-06", "2026-10-06")
    out = movie(digital="2026-12-01", physical="2026-09-15", release_date="2026-07-01")
    assert out.available and out.reason == "Physical release 2026-09-15"


def test_theatrical_release_alone_never_counts():
    held = movie(release_date="2026-09-12", year=2026)
    assert not held.available
    assert held.reason == "No digital or physical release date yet (first released 2026-09-12)"


def test_future_home_release_is_held_with_its_date():
    held = movie(digital="2026-11-20", physical="2027-01-05", release_date="2026-09-01")
    assert held == Availability(False, "Digital release is 2026-11-20", "2026-11-20")


def test_back_catalogue_fallback_and_switching_it_off():
    old = movie(release_date="1999-03-31", year=1999)
    assert old.available and "Back catalogue" in old.reason and "1999-03-31" in old.reason
    assert movie(year=2014).available  # no dates at all (e.g. a scanned movie), but clearly old
    assert not movie(year=2025).available  # could still be in cinemas
    assert not movie(release_date="2025-10-07").available  # 364 days ago
    assert movie(release_date="2025-10-06").available  # exactly 365
    assert not movie(release_date="1999-03-31", catalog_days=0).available
    # a long-released film whose only listed home date is a future re-release is still out
    assert movie(digital="2027-02-01", release_date="1985-07-03").available


def test_movie_without_any_date_is_held():
    assert movie() == Availability(False, "No release date on record", None)
    assert not movie(year=0).available


# ------------------------------------------------------------------ episodes
def test_episode_with_air_time_is_out_from_that_minute():
    before = episode_availability(air_date="2026-10-06", air_date_utc=datetime(2026, 10, 6, 18, 1), now=NOW)
    at = episode_availability(air_date="2026-10-06", air_date_utc=datetime(2026, 10, 6, 18, 0), now=NOW)
    assert not before.available and before.reason == "Airs 2026-10-06 18:01 UTC"
    assert at.available and at.reason == "Aired 2026-10-06 18:00 UTC"


def test_date_only_episode_waits_until_the_date_is_over_everywhere():
    def aired(day: str, now: datetime) -> bool:
        return episode_availability(air_date=day, air_date_utc=None, now=now).available

    assert not aired("2026-10-06", NOW)
    assert not aired("2026-10-05", datetime(2026, 10, 6, 11, 59))
    assert aired("2026-10-05", datetime(2026, 10, 6, 12, 0))  # 36 h after the date began in UTC
    assert aired("2026-09-30", NOW)


def test_undated_episode_is_held():
    assert episode_availability(air_date=None, air_date_utc=None, now=NOW) == Availability(False, "Air date TBA", None)


def test_latest_and_air_time_window():
    a, b = Availability(True, "a", "2026-10-01"), Availability(True, "b", "2026-10-03 01:00")
    assert latest([a, b]) is b and latest([]) is None
    assert wants_air_times(["2010-01-01", "2026-10-05"], TODAY)
    assert not wants_air_times(["2010-01-01", None, "2026-10-03"], TODAY)


def test_tvmaze_airstamp_becomes_naive_utc():
    assert _airstamp("2026-10-06T21:00:00-04:00") == datetime(2026, 10, 7, 1, 0)
    assert _airstamp("2026-10-06T21:00:00+00:00") == datetime(2026, 10, 6, 21, 0)
    assert _airstamp(None) is None and _airstamp("not a date") is None


# ------------------------------------------------------------------ decision engine
PROFILE = QualityProfileSpec("Any", ["WEBDL-1080p"])


def release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, "u", "idx", "usenet")


def test_unreleased_movie_is_rejected_with_the_reference_date():
    target = Target("movie", 1, "Arrival", 2026, PROFILE, availability=movie(digital="2026-11-20"))
    decision = evaluate(release("Arrival.2026.1080p.WEB-DL"), target)
    assert not decision.approved
    assert "Not released yet: Digital release is 2026-11-20" in decision.rejections

    target.availability = movie(digital="2026-09-01")
    decision = evaluate(release("Arrival.2026.1080p.WEB-DL"), target)
    assert decision.approved and decision.release_gate == "Digital release 2026-09-01"


def _air(**days: str | None) -> dict[tuple[int, int], Availability]:
    """``e1="2026-09-01"`` -> season 1 episode 1 aired on that date (date-only)."""
    return {(1, int(k[1:])): episode_availability(air_date=v, air_date_utc=None, now=NOW) for k, v in days.items()}


@pytest.mark.parametrize(("title", "episodes", "approved"), [
    ("Show.S01E01.1080p.WEB-DL", [1], True),
    ("Show.S01E03.1080p.WEB-DL", [3], False),  # airs next week
    ("Show.S01E04.1080p.WEB-DL", [4], False),  # TBA
    ("Show.S01E02E03.1080p.WEB-DL", [2], False),  # multi-episode release that includes an unaired episode
])
def test_episode_releases_wait_for_every_episode_they_contain(title, episodes, approved):
    target = Target("tv", 1, "Show", 2026, PROFILE, season=1, episodes=episodes,
                    air=_air(e1="2026-09-01", e2="2026-09-08", e3="2026-10-13", e4=None))
    decision = evaluate(release(title), target)
    assert decision.approved is approved, decision.rejections
    if not approved:
        assert any(r.startswith("Not released yet:") for r in decision.rejections)


def test_season_pack_needs_the_whole_season_aired():
    airing = Target("tv", 1, "Show", 2026, PROFILE, season=1, air=_air(e1="2026-09-01", e2="2026-10-13"))
    decision = evaluate(release("Show.S01.1080p.WEB-DL"), airing)
    assert not decision.approved
    assert "Not released yet: season 1 hasn't finished airing (Airs 2026-10-13 " in " ".join(decision.rejections)

    done = Target("tv", 1, "Show", 2026, PROFILE, season=1, air=_air(e1="2026-09-01", e2="2026-09-08"))
    decision = evaluate(release("Show.S01.1080p.WEB-DL"), done)
    assert decision.approved and decision.release_gate == "Aired 2026-09-08"


def test_gate_is_silent_when_nothing_is_known():
    target = Target("tv", 1, "Show", 2026, PROFILE, season=1, episodes=[1])
    assert release_gate(target, 1, [1], False) is None
    assert evaluate(release("Show.S01E01.1080p.WEB-DL"), target).approved
