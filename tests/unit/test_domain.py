import pytest

from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, TemplateError, render, sanitize_segment, validate
from backend.domain.parser import normalize_title, parse_episode, parse_folder_title, parse_movie
from backend.domain.quality import Codec, QualityProfileSpec, get_quality, parse_quality


@pytest.mark.parametrize(
    "name,quality,codec",
    [
        ("Avatar.2009.2160p.UHD.BluRay.x265-FRAMESTOR.mkv", "Bluray-2160p", Codec.H265),
        ("Movie.2019.1080p.WEB-DL.DDP5.1.H.264-GRP", "WEBDL-1080p", Codec.H264),
        ("Show.S01E01.720p.HDTV.x264-GRP", "HDTV-720p", Codec.H264),
        ("Film.1999.1080p.BluRay.REMUX.AVC", "Remux-1080p", Codec.H264),
        ("Film.2020.2160p.WEB.AV1", "WEBRip-2160p", Codec.AV1),
        ("Old.Movie.1985.DVDRip.XviD", "DVD", Codec.XVID),
    ],
)
def test_parse_quality(name, quality, codec):
    parsed = parse_quality(name)
    assert parsed.quality.name == quality
    assert parsed.codec == codec


def test_legacy_quality_labels_are_understood():
    assert get_quality("1080p WebDL").name == "WEBDL-1080p"
    assert get_quality("2160p UHD BluRay").name == "Bluray-2160p"
    assert get_quality("720p HDTV").name == "HDTV-720p"


@pytest.mark.parametrize(
    "name,season,episodes,full",
    [
        ("Breaking.Bad.S01E01.Pilot.1080p", 1, [1], False),
        ("Show.S02E03-E05.720p", 2, [3, 4, 5], False),
        ("Show.S02E03E04.720p", 2, [3, 4], False),
        ("The Office (US) 2x04 WEBRip", 2, [4], False),
        ("Show.S03.1080p.WEB-DL", 3, [], True),
        ("Show Season 4 Complete", 4, [], True),
    ],
)
def test_parse_episode(name, season, episodes, full):
    parsed = parse_episode(name)
    assert parsed is not None
    assert (parsed.season, parsed.episodes, parsed.full_season) == (season, episodes, full)


def test_parse_daily_episode():
    parsed = parse_episode("Daily.Show.2024.01.15.720p.WEB")
    assert parsed.air_date == "2024-01-15"


def test_parse_movie_title_and_year():
    m = parse_movie("2001.A.Space.Odyssey.1968.1080p.BluRay.mkv")
    assert (m.title, m.year) == ("2001 A Space Odyssey", 1968)
    assert m.quality.quality.name == "Bluray-1080p"


def test_folder_title_prefers_bracketed_year():
    assert parse_folder_title("Blade Runner 2049 (2017)")[:2] == ("Blade Runner 2049", 2017)
    assert parse_folder_title("Avatar (2009) {imdb-tt0499549}") == ("Avatar", 2009, "tt0499549")


def test_normalize_title():
    assert normalize_title("The Lord of the Rings: The Two Towers") == normalize_title("Lord of the Rings - Two Towers")
    assert normalize_title("Law & Order") == normalize_title("Law and Order")


# ------------------------------------------------------------------ naming
def test_render_examples_from_spec():
    assert render("{Title} ({Year}) [{Quality}]", {"title": "Avatar", "year": 2009, "quality": "Bluray-2160p"}, MOVIE_TOKENS) == \
        "Avatar (2009) [Bluray-2160p]"
    rel = render("Season {season:02d}/{Title} - S{season:02d}E{episode:02d} - {EpisodeTitle}",
                 {"title": "Breaking Bad", "season": 1, "episode": 1, "episodetitle": "Pilot"}, EPISODE_TOKENS)
    assert rel == "Season 01/Breaking Bad - S01E01 - Pilot"


def test_template_injection_is_rejected():
    assert validate("{Title.__class__}", MOVIE_TOKENS)
    assert validate("{Title!r}", MOVIE_TOKENS)
    with pytest.raises(TemplateError):
        render("{Title.__init__.__globals__}", {"title": "x"}, MOVIE_TOKENS)


def test_values_cannot_traverse_paths():
    out = render("{Title}/{EpisodeTitle}", {"title": "../../etc", "episodetitle": "..\\..\\win"}, EPISODE_TOKENS)
    assert ".." not in out.split("/")
    assert len(out.split("/")) == 2


def test_empty_tokens_collapse():
    assert render("{Title} ({Year}) [{Quality}]", {"title": "X", "year": 2000, "quality": ""}, MOVIE_TOKENS) == "X (2000)"


def test_sanitize_segment_windows_rules():
    assert sanitize_segment("CON") == "_CON"
    assert sanitize_segment("Name: Sub?") == "Name - Sub"
    assert not sanitize_segment("trailing. ").endswith((".", " "))


# ------------------------------------------------------------------ profiles
@pytest.fixture
def hd_profile():
    return QualityProfileSpec("HD", ["Remux-1080p", "Bluray-1080p", "WEBDL-1080p", "HDTV-720p"], cutoff="Bluray-1080p", preferred_codecs=["H265", "H264"])


def test_profile_ranking(hd_profile):
    assert hd_profile.rank("Remux-1080p") > hd_profile.rank("WEBDL-1080p")
    assert hd_profile.rank("Bluray-2160p") is None


def test_upgrades_respect_cutoff(hd_profile):
    assert hd_profile.is_upgrade(parse_quality("HDTV 720p"), parse_quality("WEB-DL 1080p"))
    assert not hd_profile.is_upgrade(parse_quality("Bluray 1080p"), parse_quality("Remux 1080p"))  # cutoff met
    assert hd_profile.is_upgrade(parse_quality("Bluray 1080p"), parse_quality("Bluray 1080p PROPER"))
    assert not hd_profile.is_upgrade(None, parse_quality("2160p BluRay"))  # not allowed


def test_codec_preference_breaks_ties(hd_profile):
    assert hd_profile.score(parse_quality("WEB-DL 1080p x265")) > hd_profile.score(parse_quality("WEB-DL 1080p x264"))
