from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.db.session import session_scope
from backend.domain.custom_formats import (
    CustomFormat,
    FormatMatcher,
    ReleaseFacts,
    Specification,
    SpecKind,
    validate_regex,
    validate_specification,
)
from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, preview, render
from backend.domain.quality import QualityProfileSpec, parse_quality
from backend.domain.types import ReleaseInfo
from backend.services import custom_format_io, decision_engine, quality_service


# ------------------------------------------------------------------ extended parse_quality & corpus
@pytest.mark.parametrize(
    "title,hdr,audio,channels,service",
    [
        (
            "Dune.Part.Two.2024.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR10.H.265-FLUX.mkv",
            "DV",
            "DD+ Atmos",
            "5.1",
            "AMZN",
        ),
        (
            "Oppenheimer.2023.2160p.UHD.BluRay.REMUX.HDR10Plus.HEVC.TrueHD.7.1.Atmos-FGT.mkv",
            "HDR10+",
            "TrueHD Atmos",
            "7.1",
            None,
        ),
        (
            "Blade.Runner.1982.2160p.UHD.BluRay.REMUX.DV.HEVC.TrueHD.7.1-PMHD.mkv",
            "DV",
            "TrueHD",
            "7.1",
            None,
        ),
        (
            "The.Crown.S06E01.2160p.NF.WEB-DL.DDP5.1.HDR.H.265-NTb.mkv",
            "HDR10",
            "DD+",
            "5.1",
            "NF",
        ),
        (
            "Planet.Earth.III.S01E01.2160p.iP.WEB-DL.AAC2.0.HLG.H.265-NTb.mkv",
            "HLG",
            "AAC",
            "2.0",
            "iPlayer",
        ),
        (
            "Master.and.Commander.2003.1080p.BluRay.DTS-HD.MA.5.1.x264-DON.mkv",
            None,
            "DTS-HD MA",
            "5.1",
            None,
        ),
        (
            "Heat.1995.1080p.BluRay.FLAC.2.0.x264-EbP.mkv",
            None,
            "FLAC",
            "2.0",
            None,
        ),
        (
            "Andor.S01E01.2160p.DSNP.WEB-DL.DDP5.1.DV.H.265-FLUX.mkv",
            "DV",
            "DD+",
            "5.1",
            "DSNP",
        ),
        (
            "Severance.S01E01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX.mkv",
            "DV",
            "DD+ Atmos",
            "5.1",
            "ATVP",
        ),
    ],
)
def test_extended_parse_quality(title, hdr, audio, channels, service):
    pq = parse_quality(title)
    assert pq.hdr_format == hdr
    assert pq.audio == audio
    assert pq.audio_channels == channels
    assert pq.streaming_service == service


def test_release_names_corpus():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "release_names.txt"
    lines = [
        ln.strip()
        for ln in fixture.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(lines) >= 190
    hdr_seen = set()
    audio_seen = set()
    services_seen = set()
    for title in lines:
        pq = parse_quality(title)
        assert pq.quality is not None
        if pq.hdr_format:
            hdr_seen.add(pq.hdr_format)
        if pq.audio:
            audio_seen.add(pq.audio)
        if pq.streaming_service:
            services_seen.add(pq.streaming_service)
    assert {"DV", "HDR10+", "HDR10", "HLG"} <= hdr_seen
    assert {"TrueHD Atmos", "TrueHD", "DTS-HD MA", "DD+ Atmos", "DD+", "AAC", "FLAC"} <= audio_seen
    assert {"AMZN", "NF", "ATVP", "DSNP", "MAX", "HULU"} <= services_seen


# ------------------------------------------------------------------ specifications & semantics
def _facts(
    title: str = "Movie.2024.1080p.BluRay.x265-DON",
    size_bytes: int = 8 * 1024**3,
    indexer_id: int = 1,
    protocol: str = "torrent",
) -> ReleaseFacts:
    return ReleaseFacts.from_title(title, size_bytes=size_bytes, indexer_id=indexer_id, protocol=protocol)


def test_all_spec_kinds_and_negate():
    facts = _facts("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.TrueHD.7.1.Atmos.x265-FraMeSToR", size_bytes=60 * 1024**3, indexer_id=7)
    cases = [
        (Specification("title", SpecKind.RELEASE_TITLE, value=r"\bTrueHD\b"), True),
        (Specification("group", SpecKind.RELEASE_GROUP, value=r"^framestor$"), True),
        (Specification("source", SpecKind.SOURCE, value="Remux"), True),
        (Specification("res", SpecKind.RESOLUTION, value="2160p"), True),
        (Specification("codec", SpecKind.CODEC, value="H265"), True),
        (Specification("mod", SpecKind.QUALITY_MODIFIER, value="remux"), True),
        (Specification("size", SpecKind.SIZE, value={"min_gb": 40.0, "max_gb": 80.0}), True),
        (Specification("idx", SpecKind.INDEXER, value=7), True),
        (Specification("proto", SpecKind.PROTOCOL, value="torrent"), True),
        (Specification("neg", SpecKind.PROTOCOL, value="usenet", negate=True), True),
        (Specification("neg_fail", SpecKind.PROTOCOL, value="torrent", negate=True), False),
    ]
    for spec, expected in cases:
        fmt = CustomFormat(id=1, name=spec.name, specifications=(spec,))
        matched = FormatMatcher([fmt]).match(facts)
        assert (len(matched) == 1) is expected, f"Failed for {spec}"


def test_radarr_boolean_semantics_or_within_kind_and_across_kinds():
    # Two optional source specs (bluray OR webdl) + one required resolution spec (1080p)
    fmt = CustomFormat(
        id=1,
        name="1080p Disc or Web",
        specifications=(
            Specification("BluRay", SpecKind.SOURCE, value="BluRay", required=False),
            Specification("WEBDL", SpecKind.SOURCE, value="WEBDL", required=False),
            Specification("1080p", SpecKind.RESOLUTION, value="1080p", required=True),
        ),
    )
    matcher = FormatMatcher([fmt])
    assert matcher.match(_facts("Movie.2024.1080p.BluRay.x264-GRP"))
    assert matcher.match(_facts("Movie.2024.1080p.WEB-DL.H264-GRP"))
    assert not matcher.match(_facts("Movie.2024.1080p.HDTV.x264-GRP"))
    assert not matcher.match(_facts("Movie.2024.2160p.BluRay.x265-GRP"))

    # Two required specs of the same kind must BOTH pass
    both_req = CustomFormat(
        id=2,
        name="DV + HDR10",
        specifications=(
            Specification("DV", SpecKind.RELEASE_TITLE, value=r"\b(DV|DoVi)\b", required=True),
            Specification("HDR10", SpecKind.RELEASE_TITLE, value=r"\bHDR10\b", required=True),
        ),
    )
    m2 = FormatMatcher([both_req])
    assert m2.match(_facts("Movie.2024.2160p.WEB-DL.DV.HDR10.H.265-FLUX"))
    assert not m2.match(_facts("Movie.2024.2160p.WEB-DL.DV.H.265-FLUX"))


# ------------------------------------------------------------------ validate_regex
@pytest.mark.parametrize(
    "bad_pattern",
    [
        "",
        "   ",
        "(a+)+$",
        "(.*)*",
        "([a-z]+)+",
        "[unclosed",
        "a" * 501,
    ],
)
def test_validate_regex_rejects_unsafe_or_invalid_patterns(bad_pattern):
    assert validate_regex(bad_pattern) is not None


def test_validate_specification_reports_errors():
    bad_spec = Specification("bad", SpecKind.RELEASE_TITLE, value="(a+)+$")
    assert validate_specification(bad_spec)
    bad_size = Specification("bad_size", SpecKind.SIZE, value={"min_gb": 50.0, "max_gb": 10.0})
    assert validate_specification(bad_size)


# ------------------------------------------------------------------ TRaSH JSON round-trip
SAMPLE_TRASH_FORMATS = [
    {
        "trash_id": "dc98083864ea246d05a42df0d05f81cc",
        "name": "x265 (HD)",
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            {
                "name": "x265/HEVC",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"[xh]\.?265|hevc"},
            },
            {
                "name": "Not 2160p",
                "implementation": "ResolutionSpecification",
                "negate": True,
                "required": True,
                "fields": {"value": 2160},
            },
        ],
    },
    {
        "trash_id": "e23edd2482476e595fb990b12e7c609c",
        "name": "DV HDR10",
        "includeCustomFormatWhenRenaming": True,
        "specifications": [
            {
                "name": "DV",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"\b(dv|dovi|dolby[ .]?vision)\b"},
            },
            {
                "name": "HDR10",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"\bHDR10?\b"},
            },
        ],
    },
    {
        "trash_id": "496f355514737f7d83bf7aa4d24f8169",
        "name": "TrueHD Atmos",
        "includeCustomFormatWhenRenaming": True,
        "specifications": [
            {
                "name": "TrueHD",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"\bTrue[ .-]?HD\b"},
            },
            {
                "name": "Atmos",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"\bAtmos\b"},
            },
        ],
    },
    {
        "trash_id": "90a6f9a284dff5103f6346090e6280c8",
        "name": "LQ",
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            {
                "name": "YTS",
                "implementation": "ReleaseGroupSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": r"^(YTS|YIFY|RARBG|MeGusta)$"},
            },
            {
                "name": "Tigole",
                "implementation": "ReleaseGroupSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": r"^(Tigole|QxR)$"},
            },
        ],
    },
    {
        "trash_id": "b3b3a6ac74ecbd56bcdbefa4799fb9df",
        "name": "AMZN",
        "includeCustomFormatWhenRenaming": True,
        "specifications": [
            {
                "name": "AMZN",
                "implementation": "ReleaseTitleSpecification",
                "negate": False,
                "required": True,
                "fields": {"value": r"\b(AMZN|Amazon)\b"},
            },
            {
                "name": "WEBDL",
                "implementation": "SourceSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": 3},
            },
            {
                "name": "WEBRip",
                "implementation": "SourceSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": 4},
            },
            {
                "name": "Unsupported Flag",
                "implementation": "IndexerFlagSpecification",
                "negate": False,
                "required": False,
                "fields": {"value": 1},
            },
        ],
    },
]


def test_trash_json_import_match_export_round_trip(database):
    from sqlalchemy import delete

    from backend.db import models

    sample_names = {"x265 (HD)", "DV HDR10", "TrueHD Atmos", "LQ", "AMZN"}
    with session_scope() as db:
        res = custom_format_io.import_formats_data(db, SAMPLE_TRASH_FORMATS)
        assert set(res["imported"]) | set(res["updated"]) == sample_names
        assert any("IndexerFlagSpecification" in w for w in res["warnings"])
        matcher = quality_service.get_matcher(db)

        # Match checks
        m_lq = matcher.match(_facts("The.Matrix.1999.1080p.BluRay.x265-RARBG"))
        assert {m.name for m in m_lq if m.name in sample_names} == {"x265 (HD)", "LQ"}

        m_4k = matcher.match(_facts("Dune.Part.Two.2024.2160p.AMZN.WEB-DL.TrueHD.7.1.Atmos.DV.HDR10.H.265-FLUX"))
        assert {m.name for m in m_4k if m.name in sample_names} == {"DV HDR10", "TrueHD Atmos", "AMZN"}

        exported = custom_format_io.export_formats(db)
        exported_by_name = {item["name"]: item for item in exported}
        assert sample_names <= set(exported_by_name)
        assert exported_by_name["DV HDR10"]["includeCustomFormatWhenRenaming"] is True
        assert exported_by_name["AMZN"]["trash_id"] == "b3b3a6ac74ecbd56bcdbefa4799fb9df"

        db.execute(delete(models.CustomFormat).where(models.CustomFormat.name.in_(sample_names)))
        db.flush()
        quality_service.bump_formats_version()


# ------------------------------------------------------------------ is_upgrade matrix & sort_key
def test_is_upgrade_matrix():
    profile = QualityProfileSpec(
        name="HD+Formats",
        allowed=["Remux-1080p", "Bluray-1080p", "WEBDL-1080p"],
        cutoff="Bluray-1080p",
        format_scores={1: 100, 2: 50},
        min_format_score=0,
        upgrade_until_format_score=100,
        min_format_upgrade_delta=20,
    )
    webdl = parse_quality("Movie.2024.1080p.WEB-DL.H264-GRP")
    bluray = parse_quality("Movie.2024.1080p.BluRay.x264-GRP")
    remux = parse_quality("Movie.2024.1080p.BluRay.REMUX.AVC-GRP")

    # 1. Quality better (below cutoff) -> True regardless of format score
    assert profile.is_upgrade(webdl, bluray, current_fs=100, candidate_fs=0)
    # 2. Cutoff met -> quality upgrade alone does not trigger if format score is not an upgrade
    assert not profile.is_upgrade(bluray, remux, current_fs=100, candidate_fs=100)
    # 3. Cutoff met, but format score is below upgrade_until and improves by >= delta -> True!
    assert profile.is_upgrade(bluray, bluray, current_fs=0, candidate_fs=50)
    # 4. Same quality, format delta below min_format_upgrade_delta (10 < 20) -> False
    assert not profile.is_upgrade(bluray, bluray, current_fs=40, candidate_fs=50)
    # 5. Same quality, already at upgrade_until_format_score (100 >= 100) -> False
    assert not profile.is_upgrade(bluray, bluray, current_fs=100, candidate_fs=150)
    # 6. Unknown current_fs (legacy/scanned file with no scene name) -> never churn on unknown data (§4.2)
    assert not profile.is_upgrade(bluray, bluray, current_fs=None, candidate_fs=50)


def test_decision_engine_sort_key_and_min_format_score():
    fmt_hdr = CustomFormat(
        id=1,
        name="DV",
        specifications=(Specification("DV", SpecKind.RELEASE_TITLE, value=r"\bDV\b", required=True),),
    )
    fmt_lq = CustomFormat(
        id=2,
        name="LQ",
        specifications=(Specification("YTS", SpecKind.RELEASE_GROUP, value=r"^YTS$", required=True),),
    )
    matcher = FormatMatcher([fmt_hdr, fmt_lq])
    profile = QualityProfileSpec(
        name="Test",
        allowed=["Bluray-2160p", "WEBDL-2160p", "Bluray-1080p"],
        cutoff="Bluray-2160p",
        preferred_codecs=["H265"],
        format_scores={1: 100, 2: -10000},
        min_format_score=0,
    )
    target = decision_engine.Target(
        media_type="movie",
        media_id=1,
        title="Dune",
        year=2021,
        profile=profile,
        matcher=matcher,
    )
    now = datetime.now(UTC)
    candidates = [
        ReleaseInfo(guid="1", title="Dune.2021.1080p.BluRay.DV.x265-GRP", size_bytes=5 * 1024**3, download_url="http://x/1", indexer="idx", protocol="torrent", seeders=500, publish_date=now),
        ReleaseInfo(guid="2", title="Dune.2021.2160p.WEB-DL.x264-GRP", size_bytes=10 * 1024**3, download_url="http://x/2", indexer="idx", protocol="torrent", seeders=50, publish_date=now),
        ReleaseInfo(guid="3", title="Dune.2021.2160p.WEB-DL.DV.x264-GRP", size_bytes=10 * 1024**3, download_url="http://x/3", indexer="idx", protocol="torrent", seeders=10, publish_date=now),
        ReleaseInfo(guid="4", title="Dune.2021.2160p.WEB-DL.DV.x265-YTS", size_bytes=10 * 1024**3, download_url="http://x/4", indexer="idx", protocol="torrent", seeders=999, publish_date=now),
    ]
    ranked = decision_engine.rank([decision_engine.evaluate(c, target, set(), matcher=matcher) for c in candidates])
    # 1st: 2160p WEB-DL DV (quality beats 1080p even with DV; DV format score beats non-DV 2160p WEB-DL despite fewer seeders)
    assert ranked[0].release.title == "Dune.2021.2160p.WEB-DL.DV.x264-GRP"
    assert ranked[0].approved is True and ranked[0].format_score == 100
    # 2nd: 2160p WEB-DL non-DV (quality 2160p beats 1080p BluRay even though 1080p has format_score=100)
    assert ranked[1].release.title == "Dune.2021.2160p.WEB-DL.x264-GRP"
    assert ranked[1].approved is True and ranked[1].format_score == 0
    # 3rd: 1080p BluRay DV
    assert ranked[2].release.title == "Dune.2021.1080p.BluRay.DV.x265-GRP"
    assert ranked[2].approved is True and ranked[2].format_score == 100
    # 4th: rejected due to LQ (-9900 < min_format_score 0)
    assert ranked[3].release.title == "Dune.2021.2160p.WEB-DL.DV.x265-YTS"
    assert ranked[3].approved is False
    assert any("below minimum" in r for r in ranked[3].rejections)


# ------------------------------------------------------------------ naming token {CustomFormats}
def test_custom_formats_naming_token():
    out = render(
        "{Title} ({Year}) [{Quality}] [{CustomFormats}]",
        {"title": "Dune", "year": 2021, "quality": "Bluray-2160p", "customformats": "DV HDR10 TrueHD Atmos"},
        MOVIE_TOKENS,
    )
    assert out == "Dune (2021) [Bluray-2160p] [DV HDR10 TrueHD Atmos]"

    # Empty customformats collapses cleanly without leaving empty brackets or double spaces
    empty = render(
        "{Title} ({Year}) [{Quality}] [{CustomFormats}]",
        {"title": "Dune", "year": 2021, "quality": "Bluray-2160p", "customformats": ""},
        MOVIE_TOKENS,
    )
    assert empty == "Dune (2021) [Bluray-2160p]"

    # Space alias {Custom Formats} also works in episode templates and preview()
    ep = render(
        "Season {season:02d}/{Title} - S{season:02d}E{episode:02d} [{Quality}] [{Custom Formats}]",
        {"title": "Dark", "season": 1, "episode": 1, "quality": "WEBDL-1080p", "customformats": "x265"},
        EPISODE_TOKENS,
    )
    assert ep == "Season 01/Dark - S01E01 [WEBDL-1080p] [x265]"
    assert "DV HDR10" in (preview("{Title} ({Year}) [{CustomFormats}]", "movie").example or "")


# ------------------------------------------------------------------ benchmark (1,000 titles x 100 formats < 200ms)
def test_matcher_benchmark_1000_titles_x_100_formats():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "release_names.txt"
    base_titles = [
        ln.strip()
        for ln in fixture.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    titles = (base_titles * ((1000 // len(base_titles)) + 1))[:1000]
    facts_list = [
        ReleaseFacts.from_title(t, size_bytes=5 * 1024**3, indexer_id=1, protocol="torrent")
        for t in titles
    ]
    formats = [
        CustomFormat(
            id=i + 1,
            name=f"Format-{i}",
            specifications=(
                Specification("title", SpecKind.RELEASE_TITLE, value=r"\b(DV|HDR10|Atmos|TrueHD|x265|REMUX)\b", required=True),
                Specification("res", SpecKind.RESOLUTION, value="2160p" if i % 2 == 0 else "1080p", required=False),
                Specification("src", SpecKind.SOURCE, value="BluRay" if i % 3 == 0 else "WEBDL", required=False),
            ),
        )
        for i in range(100)
    ]
    matcher = FormatMatcher(formats)
    t0 = time.perf_counter()
    for facts in facts_list:
        matcher.match(facts)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 200.0, f"Benchmark took {elapsed_ms:.1f} ms (limit 200 ms)"
