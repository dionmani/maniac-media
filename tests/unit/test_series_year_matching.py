"""Same-titled shows (Dark Matter 2015 vs 2024) never take each other's releases."""
from types import SimpleNamespace

import pytest

from backend.domain.parser import has_older_twin, parse_episode, pick_series, years_conflict
from backend.domain.quality import QualityProfileSpec
from backend.domain.types import ReleaseInfo
from backend.services.decision_engine import Target, evaluate

PROFILE = QualityProfileSpec("Any", ["WEBDL-1080p", "WEBDL-2160p"])
OLD = SimpleNamespace(title="Dark Matter", year=2015)
NEW = SimpleNamespace(title="Dark Matter", year=2024)


def release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, "u", "idx", "usenet")


@pytest.mark.parametrize(("release_year", "media_year", "conflict"), [
    (2024, 2015, True),
    (2015, 2015, False),
    (2004, 2003, False),  # premiere-year drift between metadata sources
    (None, 2015, False),
    (2024, None, False),
    (2024, 0, False),     # series with an unknown year
])
def test_years_conflict(release_year, media_year, conflict):
    assert years_conflict(release_year, media_year) is conflict


def test_parser_reads_the_series_year():
    ep = parse_episode("Dark.Matter.2024.S02E09.How.to.Build.the.Box.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB")
    assert (ep.series_title, ep.year, ep.season, ep.episodes) == ("Dark Matter", 2024, 2, [9])


def test_pick_series_uses_the_release_year():
    assert pick_series([OLD, NEW], 2024) is NEW
    assert pick_series([NEW, OLD], 2015) is OLD
    assert pick_series([OLD, NEW], None) is OLD  # the later show carries the year tag
    assert pick_series([OLD], 2024) is None
    assert pick_series([], None) is None
    unknown = SimpleNamespace(title="Dark Matter", year=0)
    assert pick_series([unknown, NEW], 2024) is NEW
    assert pick_series([unknown], 2024) is unknown


def test_has_older_twin():
    assert has_older_twin(NEW, [OLD, NEW])
    assert not has_older_twin(OLD, [OLD, NEW])
    assert not has_older_twin(NEW, [NEW])


@pytest.mark.parametrize(("year", "twin", "title", "approved"), [
    (2015, False, "Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", False),
    (2015, False, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (2024, True, "Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", True),
    (2024, True, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (2024, False, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", True),  # no same-titled show in the library
])
def test_episode_search_respects_the_series_year(year, twin, title, approved):
    target = Target("tv", 1, "Dark Matter", year, PROFILE, season=2, episodes=[9], title_has_older_twin=twin)
    decision = evaluate(release(title), target)
    assert decision.approved is approved, decision.rejections


def test_season_pack_from_the_other_show_is_rejected():
    target = Target("tv", 1, "Dark Matter", 2015, PROFILE, season=2)
    decision = evaluate(release("Dark.Matter.2024.S02.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DoVi.HDR.H.265-playWEB"), target)
    assert "Series year mismatch (2024 vs 2015)" in decision.rejections


@pytest.mark.parametrize(("title", "year", "release_title", "approved"), [
    ("Ghosts", 2019, "Ghosts.2021.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),   # UK show vs the US remake
    ("Ghosts", 2021, "Ghosts.2021.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    ("Scrubs", 2001, "Scrubs.2026.S01E01.1080p.WEB-DL.DD5.1.H.264-GRP", False),   # original vs the revival
    ("Scrubs", 2026, "Scrubs.2026.S01E01.1080p.WEB-DL.DD5.1.H.264-GRP", True),
])
def test_rule_is_not_specific_to_one_show(title, year, release_title, approved):
    ep = parse_episode(release_title)
    target = Target("tv", 1, title, year, PROFILE, season=ep.season, episodes=ep.episodes)
    assert evaluate(release(release_title), target).approved is approved


@pytest.mark.parametrize(("year", "release_title", "approved"), [
    (1984, "Dune.2021.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (2021, "Dune.2021.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (1982, "The.Thing.2011.1080p.WEB-DL.DD5.1.H.264-GRP", False),
])
def test_movie_remakes_keep_their_own_releases(year, release_title, approved):
    title = "Dune" if release_title.startswith("Dune") else "The Thing"
    assert evaluate(release(release_title), Target("movie", 1, title, year, PROFILE)).approved is approved
