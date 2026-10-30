"""Evidence scoring between same-titled shows: which one a release is for, and how sure we are."""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.domain.parser import parse_episode
from backend.domain.quality import QualityProfileSpec
from backend.domain.series_match import AMBIGUOUS, CERTAIN, LIKELY, MISMATCH, ReleaseEvidence, SeriesFacts, best_series, match_series
from backend.domain.types import ReleaseInfo
from backend.services.decision_engine import Target, evaluate


def _weekly(season: int, first: date, count: int) -> dict:
    return {(season, e): first + timedelta(weeks=e - 1) for e in range(1, count + 1)}


DM_2015 = SeriesFacts(1, "Dark Matter", 2015, "CA", tvdb_id=292174, network="Syfy",
                      aired=_weekly(1, date(2015, 6, 12), 13) | _weekly(2, date(2016, 7, 1), 13))
DM_2024 = SeriesFacts(2, "Dark Matter", 2024, "US", tvdb_id=420000, network="Apple TV+",
                      aired=_weekly(1, date(2024, 5, 8), 9) | _weekly(2, date(2026, 8, 27), 10))
GHOSTS_UK = SeriesFacts(3, "Ghosts", 2019, "GB", network="BBC One",
                        aired={k: v for s in range(1, 6) for k, v in _weekly(s, date(2018 + s, 4, 15), 6).items()})
GHOSTS_US = SeriesFacts(4, "Ghosts (US)", 2021, None, tvdb_id=398000, network="CBS",
                        aired={k: v for s in range(1, 7) for k, v in _weekly(s, date(2020 + s, 10, 7), 18).items()})


def evidence(name: str, posted: date | None = None, **ids) -> ReleaseEvidence:
    return ReleaseEvidence.from_release(parse_episode(name), posted=posted, **ids)


@pytest.mark.parametrize(("name", "posted", "target", "others", "level"), [
    # an Apple TV+ episode that aired last week is the 2024 show's, even without a year in the name
    ("Dark.Matter.S02E10.1080p.ATVP.WEB-DL.DUAL.DDP5.1.Atmos.H.264-TURG", date(2026, 10, 30), DM_2024, [DM_2015], LIKELY),
    ("Dark.Matter.S02E10.1080p.ATVP.WEB-DL.DUAL.DDP5.1.Atmos.H.264-TURG", date(2026, 10, 30), DM_2015, [DM_2024], MISMATCH),
    # posted the month the original aired: the original's
    ("Dark.Matter.S01E05.720p.HDTV.x264-KILLERS", date(2015, 7, 10), DM_2015, [DM_2024], LIKELY),
    ("Dark.Matter.S01E05.720p.HDTV.x264-KILLERS", date(2015, 7, 10), DM_2024, [DM_2015], MISMATCH),
    # an explicit year settles it either way
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL-playWEB", date(2026, 10, 30), DM_2024, [DM_2015], CERTAIN),
    ("Dark.Matter.2024.S02E09.1080p.ATVP.WEB-DL-playWEB", date(2026, 10, 30), DM_2015, [DM_2024], MISMATCH),
    # nothing to go on: neither show gets it automatically
    ("Dark.Matter.S02E07.1080p.AMZN.WEB-DL.DDP.5.1.H264-SPWEB", None, DM_2015, [DM_2024], AMBIGUOUS),
    ("Dark.Matter.S02E07.1080p.AMZN.WEB-DL.DDP.5.1.H264-SPWEB", None, DM_2024, [DM_2015], MISMATCH),
    # the British Ghosts never had a season 6
    ("Ghosts.S06E01.NORDiC.1080p.VIAP.WEB-DL.DDP5.1.H.264-ADDICTION", date(2026, 10, 9), GHOSTS_US, [GHOSTS_UK], LIKELY),
    ("Ghosts.S06E01.NORDiC.1080p.VIAP.WEB-DL.DDP5.1.H.264-ADDICTION", date(2026, 10, 9), GHOSTS_UK, [GHOSTS_US], MISMATCH),
    # the country is in the library title itself
    ("Ghosts.US.S05E05.1080p.WEB-DL-GRP", None, GHOSTS_US, [GHOSTS_UK], CERTAIN),
    ("Ghosts.UK.S05E05.1080p.WEB-DL-GRP", None, GHOSTS_UK, [GHOSTS_US], CERTAIN),
])
def test_same_titled_shows(name, posted, target, others, level):
    m = match_series(evidence(name, posted), target, others)
    assert m.level == level, (m.score, m.reasons)


def test_indexer_ids_point_at_one_show():
    r = evidence("Dark.Matter.S02E07.1080p.AMZN.WEB-DL.DDP.5.1.H264-SPWEB", tvdb_id=292174)
    assert match_series(r, DM_2015, [DM_2024]).level == LIKELY
    assert match_series(r, DM_2024, [DM_2015]).level == MISMATCH
    imdb = SeriesFacts(5, "Severance", 2022, imdb_id="tt11280740")
    assert match_series(evidence("Severance.S02E01.1080p.WEB", imdb_id="tt11280740"), imdb, []).level == CERTAIN


def test_a_show_without_a_twin_is_accepted_unless_something_contradicts_it():
    alone = SeriesFacts(6, "Severance", 2022, "US", tvdb_id=371980)
    assert match_series(evidence("Severance.S02E01.1080p.WEB"), alone, []).level == LIKELY
    assert match_series(evidence("Severance.S02E01.1080p.WEB", tvdb_id=371980), alone, []).level == CERTAIN
    assert match_series(evidence("Severance.S02E01.1080p.WEB", tvdb_id=1), alone, []).level == MISMATCH  # another show's id
    assert match_series(evidence("Severance.UK.S02E01.1080p.WEB"), alone, []).level == MISMATCH


def test_rejection_explains_where_the_release_belongs():
    m = match_series(evidence("Dark.Matter.S02E10.1080p.ATVP.WEB-DL-TURG", date(2026, 10, 30)), DM_2015, [DM_2024])
    assert m.rejection().startswith("Release is for Dark Matter (2024, US)")
    assert any("ATVP on Apple TV+" in r for r in m.reasons)
    ruled_out = match_series(evidence("Dark.Matter.2024.S02E09.1080p-GRP"), DM_2015, [DM_2024])
    assert ruled_out.ruled_out and "named year 2024" in ruled_out.rejection()


def test_best_series_picks_only_a_clear_winner():
    r = evidence("Dark.Matter.S02E10.1080p.ATVP.WEB-DL-TURG", date(2026, 10, 30))
    winner, m = best_series(r, [DM_2015, DM_2024])
    assert winner is DM_2024 and m.level == LIKELY
    assert best_series(evidence("Dark.Matter.S02E07.1080p.AMZN.WEB-DL-SPWEB"), [DM_2015, DM_2024]) == (None, None)


def test_decision_carries_the_series_match():
    profile = QualityProfileSpec("Any", ["WEBDL-1080p"])
    posted = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    title = "Dark.Matter.S02E10.1080p.ATVP.WEB-DL.DUAL.DDP5.1.Atmos.H.264-TURG"
    release = ReleaseInfo(title, title, "u", "idx", "usenet", publish_date=posted)
    wrong = evaluate(release, Target("tv", 1, "Dark Matter", 2015, profile, season=2, episodes=[10], facts=DM_2015, twins=[DM_2024]))
    right = evaluate(release, Target("tv", 2, "Dark Matter", 2024, profile, season=2, episodes=[10], facts=DM_2024, twins=[DM_2015]))
    assert not wrong.approved and wrong.to_dict()["series_match"]["level"] == MISMATCH
    assert right.approved and right.to_dict()["series_match"]["level"] == LIKELY
