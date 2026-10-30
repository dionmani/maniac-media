"""Same-titled shows (Dark Matter 2015 vs 2024) never take each other's releases."""
from types import SimpleNamespace

import pytest

from backend.domain.parser import country_code, parse_episode, pick_series, split_title_tag, years_conflict
from backend.domain.quality import QualityProfileSpec
from backend.domain.series_match import SeriesFacts
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


@pytest.mark.parametrize(("year", "twin", "title", "approved"), [
    (2015, False, "Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", False),
    (2015, False, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (2024, True, "Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL.DD+5.1.Atmos.H.264-playWEB", True),
    (2024, True, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (2024, False, "Dark.Matter.S02E09.1080p.WEB-DL.DD5.1.H.264-GRP", True),  # no same-titled show in the library
])
def test_episode_search_respects_the_series_year(year, twin, title, approved):
    twins = [SeriesFacts(2, "Dark Matter", 2015)] if twin else []
    target = Target("tv", 1, "Dark Matter", year, PROFILE, season=2, episodes=[9], twins=twins)
    decision = evaluate(release(title), target)
    assert decision.approved is approved, decision.rejections


def test_season_pack_from_the_other_show_is_rejected():
    target = Target("tv", 1, "Dark Matter", 2015, PROFILE, season=2)
    decision = evaluate(release("Dark.Matter.2024.S02.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DoVi.HDR.H.265-playWEB"), target)
    assert any("named year 2024, this show is 2015" in r for r in decision.rejections), decision.rejections


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


# ------------------------------------------------------------------ country tags (Ghosts UK/US, The Office UK/US)
@pytest.mark.parametrize(("title", "bare", "expected"), [
    ("Ghosts (US)", False, ("Ghosts", None, "US")),
    ("The Office (UK)", False, ("The Office", None, "GB")),
    ("Dark Matter (2024)", False, ("Dark Matter", 2024, None)),
    ("Ghosts US", True, ("Ghosts", None, "US")),
    ("Ghosts US", False, ("Ghosts US", None, None)),  # library titles only carry it in brackets
    ("This Is Us", True, ("This Is Us", None, None)),  # a real word, not an upper-case tag
    ("Law and Order (Special Victims Unit)", False, ("Law and Order (Special Victims Unit)", None, None)),
    ("US", True, ("US", None, None)),
])
def test_split_title_tag(title, bare, expected):
    assert split_title_tag(title, bare=bare) == expected


@pytest.mark.parametrize(("value", "code"), [("US", "US"), ("gb", "GB"), ("UK", "GB"), ("usa", "US"), ("gbr", "GB"),
                                            ("xyz", None), (None, None), ("", None)])
def test_country_code(value, code):
    assert country_code(value) == code


def _ghosts(year, country, twin=False, title="Ghosts"):
    twins = [SeriesFacts(2, "Ghosts", 2019, "GB")] if twin else []
    return Target("tv", 1, title, year, PROFILE, season=5, episodes=[5], country=country, twins=twins)


@pytest.mark.parametrize(("target", "title", "approved"), [
    (_ghosts(2021, "US", twin=True), "Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2021, "US", twin=True), "Ghosts.UK.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (_ghosts(2021, "US", twin=True), "Ghosts.2021.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2021, "US", twin=True), "Ghosts.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),  # the British one's
    (_ghosts(2019, "GB"), "Ghosts.UK.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2019, "GB"), "Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (_ghosts(2019, "GB"), "Ghosts.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2019, None), "Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),  # unknown country: no guessing
    (_ghosts(2021, None, title="Ghosts (US)"), "Ghosts.US.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2021, None, title="Ghosts (US)"), "Ghosts.2021.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
    (_ghosts(2021, None, title="Ghosts (US)"), "Ghosts.UK.S05E05.1080p.WEB-DL.DD5.1.H.264-GRP", False),
    (Target("tv", 1, "This Is Us", 2016, PROFILE, season=6, episodes=[5]), "This.Is.Us.S06E05.1080p.WEB-DL.DD5.1.H.264-GRP", True),
])
def test_country_tag_decides_between_same_titled_shows(target, title, approved):
    decision = evaluate(release(title), target)
    assert decision.approved is approved, decision.rejections


def test_pick_series_by_country():
    uk = SimpleNamespace(title="Ghosts", year=2019, country="GB")
    us = SimpleNamespace(title="Ghosts", year=2021, country="US")
    tagged = SimpleNamespace(title="Ghosts (US)", year=2021, country=None)
    assert pick_series([uk, us], None, "US") is us
    assert pick_series([uk, us], None, "GB") is uk
    assert pick_series([uk, us], None, None) is uk
    assert pick_series([uk, tagged], None, "US") is tagged
    assert pick_series([SimpleNamespace(title="Ghosts", year=2019, country=None)], None, "US") is None


def test_providers_report_the_origin_country():
    from backend.plugins.metadata.tmdb import TmdbProvider
    from backend.plugins.metadata.tvdb import TvdbProvider
    from backend.plugins.metadata.tvmaze import TvMazeProvider

    tvmaze = TvMazeProvider({})._convert({"id": 1, "name": "Ghosts", "network": {"name": "CBS", "country": {"code": "US"}}})
    tmdb = TmdbProvider({})._series({"id": 1, "name": "Ghosts", "origin_country": ["GB"]})
    tvdb = TvdbProvider({})._from_search({"tvdb_id": "1", "name": "Ghosts", "country": "gbr"}, "tv")
    assert (tvmaze.country, tmdb.country, country_code(tvdb.country)) == ("US", "GB", "GB")
