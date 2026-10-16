"""Integration tests for Custom Formats CRUD, tester, TRaSH import/export, search ranking, and format upgrades."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata, ReleaseInfo
from backend.services import import_service, media_service, search_service
from backend.services.audit import SYSTEM

API = "/api/v1"


def test_custom_formats_crud_tester_import_export_and_profile_cleanup(client, admin_headers):
    # 1. Invalid regex rejected with 422
    bad = client.post(
        f"{API}/custom-formats",
        json={
            "name": "Bad Regex",
            "include_in_rename": False,
            "specifications": [
                {"name": "ReDoS", "kind": "release_title", "value": "(a+)+$", "negate": False, "required": True}
            ],
        },
        headers=admin_headers,
    )
    assert bad.status_code == 422

    # 2. Create custom format
    created = client.post(
        f"{API}/custom-formats",
        json={
            "name": "x265 HD Test",
            "include_in_rename": True,
            "specifications": [
                {"name": "x265", "kind": "codec", "value": "H265", "negate": False, "required": True},
                {"name": "Not 2160p", "kind": "resolution", "value": "2160p", "negate": True, "required": True},
            ],
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    cf = created.json()
    cf_id = cf["id"]

    # Duplicate name -> 409 or 422
    dup = client.post(
        f"{API}/custom-formats",
        json={"name": "x265 HD Test", "include_in_rename": False, "specifications": cf["specifications"]},
        headers=admin_headers,
    )
    assert dup.status_code in (409, 422)

    # 3. Assign score in a quality profile; unknown format ID -> 422
    bad_prof = client.post(
        f"{API}/quality-profiles",
        json={
            "name": "CF Invalid Profile",
            "items": ["Bluray-1080p", "WEBDL-1080p"],
            "cutoff": "Bluray-1080p",
            "format_scores": {"999999": 50},
        },
        headers=admin_headers,
    )
    assert bad_prof.status_code == 422

    prof_res = client.post(
        f"{API}/quality-profiles",
        json={
            "name": "CF HD Profile",
            "items": ["Bluray-1080p", "WEBDL-1080p"],
            "cutoff": "Bluray-1080p",
            "format_scores": {str(cf_id): 120},
            "min_format_score": 0,
            "upgrade_until_format_score": 200,
            "min_format_upgrade_delta": 10,
        },
        headers=admin_headers,
    )
    assert prof_res.status_code == 201, prof_res.text
    prof = prof_res.json()
    assert prof["format_scores"] == {str(cf_id): 120}

    # 4. Tester endpoint
    test_res = client.post(
        f"{API}/custom-formats/test",
        json={
            "title": "Blade.Runner.1982.1080p.BluRay.x265-GRP",
            "profile_id": prof["id"],
        },
        headers=admin_headers,
    )
    assert test_res.status_code == 200, test_res.text
    tdata = test_res.json()
    assert any(m["id"] == cf_id for m in tdata["matches"])
    assert any(f["format_id"] == cf_id and f["matched"] is True for f in tdata["formats"])
    assert tdata["total_score"] == 120

    # 5. Import & Export TRaSH JSON
    imp = client.post(
        f"{API}/custom-formats/import",
        json={
            "formats": [
                {
                    "name": "Imported Atmos",
                    "includeCustomFormatWhenRenaming": True,
                    "specifications": [
                        {
                            "name": "Atmos",
                            "implementation": "ReleaseTitleSpecification",
                            "negate": False,
                            "required": True,
                            "fields": {"value": r"\bAtmos\b"},
                        }
                    ],
                }
            ]
        },
        headers=admin_headers,
    )
    assert imp.status_code == 200, imp.text
    assert imp.json()["imported"] == ["Imported Atmos"]

    exp = client.get(f"{API}/custom-formats/export", params={"ids": str(cf_id)}, headers=admin_headers)
    assert exp.status_code == 200
    assert len(exp.json()) == 1 and exp.json()[0]["name"] == "x265 HD Test"

    # 6. Deleting the custom format strips it from quality_profiles.format_scores
    del_res = client.delete(f"{API}/custom-formats/{cf_id}", headers=admin_headers)
    assert del_res.status_code == 200
    prof_after = client.get(f"{API}/quality-profiles", headers=admin_headers).json()
    updated_prof = next(p for p in prof_after if p["id"] == prof["id"])
    assert str(cf_id) not in updated_prof["format_scores"]


def test_interactive_and_automatic_search_with_custom_formats_and_upgrade_lifecycle(
    client, admin_headers, database, library, make_video, monkeypatch
):
    # Create LQ (-10000) and x265 (+100) formats
    lq_id = client.post(
        f"{API}/custom-formats",
        json={
            "name": "LQ Groups",
            "include_in_rename": False,
            "specifications": [
                {"name": "YTS", "kind": "release_group", "value": r"^YTS$", "negate": False, "required": True}
            ],
        },
        headers=admin_headers,
    ).json()["id"]

    x265_id = client.post(
        f"{API}/custom-formats",
        json={
            "name": "x265 Preferred",
            "include_in_rename": True,
            "specifications": [
                {"name": "H265", "kind": "codec", "value": "H265", "negate": False, "required": True}
            ],
        },
        headers=admin_headers,
    ).json()["id"]

    prof_id = client.post(
        f"{API}/quality-profiles",
        json={
            "name": "Bluray 1080p CF Upgrade",
            "items": ["Bluray-1080p", "WEBDL-1080p"],
            "cutoff": "Bluray-1080p",
            "format_scores": {str(lq_id): -10000, str(x265_id): 100},
            "min_format_score": 0,
            "upgrade_until_format_score": 100,
            "min_format_upgrade_delta": 50,
        },
        headers=admin_headers,
    ).json()["id"]

    with session_scope() as db:
        if not db.scalar(select(models.Indexer).where(models.Indexer.implementation == "demo")):
            db.add(models.Indexer(name="Demo", implementation="demo", url="", protocol="torrent", enabled=True, priority=1))
            db.add(models.DownloadClient(name="Sim", client_type="simulator", enabled=True, category="test", priority=1))
        m = media_service.add_movie(
            db,
            MediaMetadata(media_type="movie", provider="test", title="Solaris", year=1972),
            quality_profile_id=prof_id,
            root_folder=None,
            monitored=True,
            actor=SYSTEM,
        )
        movie_id = m.id

    now = datetime.now(UTC)
    fake_candidates = [
        ReleaseInfo(
            guid="sol-1",
            title="Solaris.1972.1080p.BluRay.x264-YTS",
            size_bytes=2 * 1024**3,
            download_url="sim://Solaris.1972.1080p.BluRay.x264-YTS",
            indexer="Demo",
            protocol="torrent",
            indexer_id=1,
            seeders=500,
            publish_date=now,
        ),
        ReleaseInfo(
            guid="sol-2",
            title="Solaris.1972.1080p.BluRay.x264-DON",
            size_bytes=8 * 1024**3,
            download_url="sim://Solaris.1972.1080p.BluRay.x264-DON",
            indexer="Demo",
            protocol="torrent",
            indexer_id=1,
            seeders=100,
            publish_date=now,
        ),
        ReleaseInfo(
            guid="sol-3",
            title="Solaris.1972.1080p.BluRay.x265-FraMeSToR",
            size_bytes=7 * 1024**3,
            download_url="sim://Solaris.1972.1080p.BluRay.x265-FraMeSToR",
            indexer="Demo",
            protocol="torrent",
            indexer_id=1,
            seeders=20,
            publish_date=now,
        ),
    ]

    async def fake_search(criteria):
        return list(fake_candidates), [{"indexer": "Demo", "ok": True, "count": 3}]

    monkeypatch.setattr(search_service.indexer_service, "search", fake_search)

    # 1. Interactive search via GET /api/v1/releases
    rel_res = client.get(f"{API}/releases", params={"media_type": "movie", "media_id": movie_id}, headers=admin_headers)
    assert rel_res.status_code == 200, rel_res.text
    releases = rel_res.json()["releases"]
    assert len(releases) == 3
    # Top release is x265-FraMeSToR with format_score=100
    assert releases[0]["title"] == "Solaris.1972.1080p.BluRay.x265-FraMeSToR"
    assert releases[0]["approved"] is True
    assert releases[0]["format_score"] == 100
    assert any(b["label"].startswith("Format:") for b in releases[0]["score_breakdown"])
    # YTS is rejected due to format score -10000 < 0
    yts_rel = next(r for r in releases if "YTS" in r["title"])
    assert yts_rel["approved"] is False
    assert yts_rel["format_score"] == -10000
    assert any("below minimum" in reason for reason in yts_rel["rejections"])

    # 2. Import an initial x264 Bluray-1080p file (quality cutoff met, but format score = 0 < 100)
    x264_src = make_video(library["downloads"] / "Solaris.1972.1080p.BluRay.x264-DON" / "Solaris.1972.1080p.BluRay.x264-DON.mkv")
    imp_outcome = import_service.manual_import([{"path": str(x264_src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert imp_outcome.imported == ["Solaris (1972)"]

    # Movie should appear in /wanted/cutoff because format score 0 < upgrade_until_format_score 100
    cutoff_page = client.get(f"{API}/wanted/cutoff", headers=admin_headers).json()
    assert any(item["media_type"] == "movie" and item["media_id"] == movie_id for item in cutoff_page["items"])

    # 3. Automatic search upgrades from x264 (score 0) to x265 (score 100) within same quality (Bluray-1080p)
    auto_res = asyncio.run(search_service.automatic("movie", movie_id))
    assert auto_res["grabbed"] == "Solaris.1972.1080p.BluRay.x265-FraMeSToR"

    with session_scope() as db:
        grab_hist = db.scalar(
            select(models.History)
            .where(models.History.media_type == "movie", models.History.media_id == movie_id, models.History.event_type == "grabbed")
            .order_by(models.History.id.desc())
        )
        assert grab_hist is not None
        assert grab_hist.data.get("format_score") == 100
        assert "x265 Preferred" in grab_hist.data.get("formats", [])

    # 4. Import the x265 release -> reaches upgrade_until_format_score (100), disappears from /wanted/cutoff, and stops upgrading
    x265_src = make_video(
        library["downloads"] / "Solaris.1972.1080p.BluRay.x265-FraMeSToR" / "Solaris.1972.1080p.BluRay.x265-FraMeSToR.mkv",
        b"\x1a\x45\xdf\xa3" + b"\x02" * 4000,
    )
    upg_outcome = import_service.manual_import([{"path": str(x265_src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert upg_outcome.imported == ["Solaris (1972)"]

    cutoff_after = client.get(f"{API}/wanted/cutoff", headers=admin_headers).json()
    assert not any(item["media_type"] == "movie" and item["media_id"] == movie_id for item in cutoff_after["items"])

    # Subsequent automatic search does not grab again (at upgrade_until_format_score and same scene_name guard)
    again = asyncio.run(search_service.automatic("movie", movie_id))
    assert again["grabbed"] is None
