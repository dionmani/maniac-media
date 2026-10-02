"""Manual-import smart match: file names -> library / metadata candidates."""
from __future__ import annotations

from backend.domain.types import MediaMetadata
from backend.services import identify_service, metadata_service


def test_identify_falls_back_to_provider_and_orders_movies_first(client, admin_headers, monkeypatch):
    calls = []

    async def fake_search(query, media_type, year=None):
        calls.append((query, media_type, year))
        if media_type == "movie":
            return []                                    # nothing as a movie ...
        return [MediaMetadata(media_type="tv", provider="fake", title="Zzyzx Road Show", year=2011, tmdb_id=987654)]

    monkeypatch.setattr(metadata_service, "search", fake_search)
    r = client.post("/api/v1/library/manual-import/identify", headers=admin_headers,
                    json={"paths": ["/dl/Zzyzx.Road.Show.2011.1080p.WEB-DL.x264-GRP.mkv"]})
    assert r.status_code == 200, r.text
    item = r.json()[0]
    assert (item["query"], item["year"]) == ("Zzyzx Road Show", 2011)
    assert [c[1] for c in calls] == ["movie", "movie", "tv"]   # movie (with year), movie (without), then TV
    assert item["candidates"][0]["media_type"] == "tv" and item["candidates"][0]["tmdb_id"] == 987654
    assert item["candidates"][0]["source"] == "tmdb" and item["candidates"][0]["media_id"] is None


def test_identify_tv_marker_searches_series_first(client, admin_headers, monkeypatch):
    seen = []

    async def fake_search(query, media_type, year=None):
        seen.append(media_type)
        return []

    monkeypatch.setattr(metadata_service, "search", fake_search)
    out = client.post("/api/v1/library/manual-import/identify", headers=admin_headers,
                      json={"paths": ["/dl/Qwertyuiop.Show.S01E02.720p.HDTV.mkv"]}).json()
    assert seen[0] == "tv" and out[0]["kind"] == "tv" and (out[0]["season"], out[0]["episodes"]) == (1, [2])
    assert out[0]["candidates"] == []


def test_identify_skips_unnameable_files(client, admin_headers):
    out = client.post("/api/v1/library/manual-import/identify", headers=admin_headers, json={"paths": ["/1080p.mkv"]}).json()
    assert out[0]["query"] == "" and out[0]["candidates"] == []
    assert identify_service.MAX_CANDIDATES >= 1


def test_import_quality_keeps_repack_from_file_name():
    from pathlib import Path
    from backend.services.import_service import _import_quality
    q = _import_quality(Path("/dl/Movie.2020.REPACK.1080p.BluRay.x264-GRP.mkv"), "Bluray-1080p")
    assert q.repack and q.quality.name == "Bluray-1080p"
    # label from the UI wins when the file name says something else (quality came from the folder)
    assert _import_quality(Path("/dl/WEBDL-720p-folder/file.mkv"), "WEBDL-720p").quality.name == "WEBDL-720p"


def test_extras_folders_are_flagged():
    from pathlib import Path
    from backend.services.import_service import _in_extras_dir
    assert _in_extras_dir(Path("/dl/Creed III (2023)/Featurettes/Deleted Scenes.mkv"))
    assert not _in_extras_dir(Path("/dl/Creed.III.2023.1080p.mkv"))


def test_library_title_search_ignores_punctuation(client, admin_headers):
    from backend.services.identify_service import _title_rows
    from backend.db.session import session_scope
    r = client.post("/api/v1/movies", headers=admin_headers, json={"title": "Zork: The Great Test", "year": 1990})
    assert r.status_code in (201, 409), r.text
    with session_scope() as db:
        assert any(m.title == "Zork: The Great Test" for m in _title_rows(db, "movie", "Zork The Great Test"))
