"""Link ("fix match"): re-point a mis-parsed, on-disk movie/series at the title the user identified by id."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.domain.types import EpisodeMetadata, MediaMetadata

API = "/api/v1"


@pytest.fixture
def cleanup(client, admin_headers, library):
    """The DB and library folders are session-wide; leave neither rows nor files behind for other tests' totals."""
    yield
    for kind in ("movies", "series"):
        for item in client.get(f"{API}/{kind}", params={"page_size": 500}, headers=admin_headers).json()["items"]:
            if any(x in (item["title"] or "") for x in ("American Pie", "Goldfinger", "Breaking Bad", "Dr No", "Dr. No")):
                client.delete(f"{API}/{kind}/{item['id']}", headers=admin_headers)
    for root in (library["movies"], library["tv"]):
        for name in ("www.Torrenting.com - American Pie", "American Pie (1999)", "www.UIndex.org - American Pie Again",
                     "www Torrenting com - Goldfinger", "www.UIndex.org - Breaking Bad", "Breaking Bad",
                     "Dr. No (1962)", "www.UIndex.org - Dr No (1962) [1080p]", "www.UIndex.org - Dr No Extras"):
            shutil.rmtree(root / name, ignore_errors=True)


def test_link_movie_preview_apply_and_rename(client, admin_headers, monkeypatch, library, make_video, cleanup):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import metadata_service, scanner_service

    seen = []

    async def fake_fetch(**kw):
        seen.append(kw)
        if kw.get("imdb_id") == "tt0163651" or kw.get("tmdb_id") == 2075:
            return MediaMetadata(media_type="movie", provider="test", title="American Pie", year=1999, tmdb_id=2075,
                                 imdb_id="tt0163651", overview="Four teens.", genres=["Comedy"])
        return None

    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)
    folder = library["movies"] / "www.Torrenting.com - American Pie"
    video = make_video(folder / "www.Torrenting.com - American.Pie.1999.1080p.BluRay.x264-GRP.mkv")
    (folder / "poster.jpg").write_bytes(b"art")
    scanner_service.scan_movies(incremental=False)
    with session_scope() as db:
        movie = db.query(models.Movie).filter(models.Movie.path == str(folder)).one()
        mid, scanned_title = movie.id, movie.title
    assert "Torrenting" in scanned_title

    # garbage in -> 422, unknown id -> 404, a TVDB id for a movie -> 422 with a hint
    assert client.post(f"{API}/movies/{mid}/link", json={"external_id": "American Pie"}, headers=admin_headers).status_code == 422
    assert client.post(f"{API}/movies/{mid}/link", json={"external_id": "tt9999999"}, headers=admin_headers).status_code == 404
    assert "TV shows" in client.post(f"{API}/movies/{mid}/link", json={"external_id": "tvdb:123"}, headers=admin_headers).json()["detail"]

    # preview (the default) changes nothing
    r = client.post(f"{API}/movies/{mid}/link", json={"external_id": "https://www.imdb.com/title/tt0163651/"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["applied"] is False and preview["existing"] is None
    assert preview["match"]["title"] == "American Pie" and preview["match"]["tmdb_id"] == 2075
    assert seen[-1] == {"imdb_id": "tt0163651"}
    assert client.get(f"{API}/movies/{mid}", headers=admin_headers).json()["title"] == scanned_title

    r = client.post(f"{API}/movies/{mid}/link", json={"external_id": "tt0163651", "dry_run": False}, headers=admin_headers)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["applied"] is True and done["renamed"] == 1 and done["rename_errors"] == []

    movie = client.get(f"{API}/movies/{mid}", headers=admin_headers).json()
    assert (movie["title"], movie["year"], movie["imdb_id"], movie["tmdb_id"], movie["genres"]) == ("American Pie", 1999, "tt0163651", 2075, "Comedy")
    new_folder = library["movies"] / "American Pie (1999)"
    assert movie["path"] == str(new_folder)
    assert movie["file_path"] == str(new_folder / "American Pie (1999) [Bluray-1080p].mkv")
    assert Path(movie["file_path"]).exists() and (new_folder / "poster.jpg").exists()
    assert not folder.exists() and not video.exists()
    assert movie["files"][0]["path"] == movie["file_path"]

    # a rescan keeps the link (found by its new folder) instead of re-creating the mis-parsed entry
    scanner_service.scan_movies(incremental=False)
    with session_scope() as db:
        assert db.query(models.Movie).filter(models.Movie.title.like("%Torrenting%")).count() == 0
        assert db.get(models.Movie, mid).title == "American Pie"

    # a second copy pointed at the same id is flagged as a duplicate, not linked
    dupe = make_video(library["movies"] / "www.UIndex.org - American Pie Again" / "American.Pie.mkv")
    scanner_service.scan_movies(incremental=False)
    with session_scope() as db:
        other = db.query(models.Movie).filter(models.Movie.path == str(dupe.parent)).one().id
    preview = client.post(f"{API}/movies/{other}/link", json={"external_id": "tmdb:2075"}, headers=admin_headers).json()
    assert preview["existing"] == {"id": mid, "title": "American Pie", "year": 1999}
    assert client.post(f"{API}/movies/{other}/link", json={"external_id": "tmdb:2075", "dry_run": False}, headers=admin_headers).status_code == 409


def test_link_movie_without_rename_keeps_files(client, admin_headers, monkeypatch, library, make_video, cleanup):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import metadata_service, scanner_service

    async def fake_fetch(**kw):
        return MediaMetadata(media_type="movie", provider="test", title="Goldfinger", year=1964, tmdb_id=658)  # no imdb id echoed back

    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)
    video = make_video(library["movies"] / "www Torrenting com - Goldfinger" / "Goldfinger.mkv")
    scanner_service.scan_movies(incremental=False)
    with session_scope() as db:
        mid = db.query(models.Movie).filter(models.Movie.path == str(video.parent)).one().id
    done = client.post(f"{API}/movies/{mid}/link", json={"external_id": "tt0058150", "dry_run": False, "rename": False},
                       headers=admin_headers).json()
    assert done["applied"] is True and done["renamed"] == 0
    movie = client.get(f"{API}/movies/{mid}", headers=admin_headers).json()
    assert movie["title"] == "Goldfinger" and movie["imdb_id"] == "tt0058150"  # the id the user typed is kept
    assert movie["file_path"] == str(video) and video.exists()


def test_link_series_matches_episodes_and_renames(client, admin_headers, monkeypatch, library, make_video, cleanup):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import metadata_service, scanner_service

    async def fake_series(**kw):
        assert kw == {"tvdb_id": 81189}
        return MediaMetadata(media_type="tv", provider="test", title="Breaking Bad", year=2008, tvdb_id=81189, imdb_id="tt0903747",
                             episodes=[EpisodeMetadata(1, e, "Pilot" if e == 1 else f"Ep {e}") for e in (1, 2, 3)])

    monkeypatch.setattr(metadata_service, "fetch_series", fake_series)
    folder = library["tv"] / "www.UIndex.org - Breaking Bad"
    make_video(folder / "Season 01" / "Breaking.Bad.S01E01.720p.HDTV.x264-GRP.mkv")
    (folder / "poster.jpg").write_bytes(b"art")
    scanner_service.scan_series(incremental=False)
    with session_scope() as db:
        sid = db.query(models.Series).filter(models.Series.path == str(folder)).one().id

    r = client.post(f"{API}/series/{sid}/link", json={"external_id": "https://thetvdb.com/?tab=series&id=81189"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["match"]["episode_count"] == 3 and r.json()["applied"] is False

    done = client.post(f"{API}/series/{sid}/link", json={"external_id": "81189", "dry_run": False}, headers=admin_headers).json()
    assert done["applied"] is True and done["rename_errors"] == [] and done["renamed"] == 2  # folder + one episode file

    s = client.get(f"{API}/series/{sid}", headers=admin_headers).json()
    new_folder = library["tv"] / "Breaking Bad"
    assert (s["title"], s["tvdb_id"], s["path"]) == ("Breaking Bad", 81189, str(new_folder))
    eps = {e["episode_number"]: e for season in s["seasons"] for e in season["episodes"]}
    assert set(eps) == {1, 2, 3} and eps[1]["title"] == "Pilot"
    expected = new_folder / "Season 01" / "Breaking Bad - S01E01 - Pilot [HDTV-720p].mkv"
    assert eps[1]["file_path"] == str(expected) and expected.exists()
    assert (new_folder / "poster.jpg").exists() and not folder.exists()


def _dr_no(monkeypatch):
    from backend.services import metadata_service

    async def fake_fetch(**kw):
        return MediaMetadata(media_type="movie", provider="test", title="Dr. No", year=1962, tmdb_id=646, imdb_id="tt0055928")

    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)


def _movie_at(folder: Path) -> int:
    from backend.db import models
    from backend.db.session import session_scope

    with session_scope() as db:
        return db.query(models.Movie).filter(models.Movie.path == str(folder)).one().id


def test_merge_duplicate_into_existing_keeps_the_picked_file(client, admin_headers, monkeypatch, library, make_video, cleanup):
    from backend.db import models
    from backend.db.session import session_scope
    from backend.services import scanner_service

    _dr_no(monkeypatch)
    good = library["movies"] / "Dr. No (1962)"
    old = make_video(good / "Dr.No.1962.720p.HDTV.x264-GRP.mkv")
    dupe_dir = library["movies"] / "www.UIndex.org - Dr No (1962) [1080p]"
    new = make_video(dupe_dir / "Dr.No.1962.1080p.BluRay.x264-GRP.mkv", bytes.fromhex("1A45DFA3") + b"0" * 64)  # bigger
    (new.with_suffix(".en.srt")).write_text("subs")
    (dupe_dir / "www.UIndex.org.txt").write_text("junk")
    scanner_service.scan_movies(incremental=False)
    target, source = _movie_at(good), _movie_at(dupe_dir)
    client.post(f"{API}/movies/{target}/link", json={"external_id": "tmdb:646", "dry_run": False, "rename": False}, headers=admin_headers)
    with session_scope() as db:  # the source carries some history to be folded in
        db.add(models.History(media_type="movie", media_id=source, title="dupe", event_type="grabbed"))

    preview = client.post(f"{API}/movies/{source}/link", json={"external_id": "tmdb:646"}, headers=admin_headers).json()
    assert preview["existing"]["id"] == target
    files = {f["path"]: f for f in preview["merge"]["files"]}
    assert set(files) == {str(old), str(new)} and files[str(new)]["owner"] == "this" and files[str(new)]["recommended"]
    assert preview["merge"]["cleanup_folder"] == str(dupe_dir)
    assert client.get(f"{API}/movies/{source}", headers=admin_headers).status_code == 200  # preview changed nothing

    # two files and no pick -> refused; a path that isn't part of the merge -> refused
    body = {"external_id": "tmdb:646", "dry_run": False, "merge": True, "rename": False}
    assert client.post(f"{API}/movies/{source}/link", json=body, headers=admin_headers).status_code == 422
    assert client.post(f"{API}/movies/{source}/link", json={**body, "keep_path": "/etc/passwd"}, headers=admin_headers).status_code == 422

    r = client.post(f"{API}/movies/{source}/link", json={**body, "keep_path": str(new)}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["merged_into"] == target and r.json()["applied"] is True

    assert client.get(f"{API}/movies/{source}", headers=admin_headers).status_code == 404
    movie = client.get(f"{API}/movies/{target}", headers=admin_headers).json()
    kept = good / new.name
    assert movie["file_path"] == str(kept) and kept.exists() and (good / "Dr.No.1962.1080p.BluRay.x264-GRP.en.srt").exists()
    assert [f["path"] for f in movie["files"]] == [str(kept)]
    assert not old.exists() and not dupe_dir.exists()  # the 720p and the duplicate's folder went to the recycle bin
    assert any(p.name == old.name for p in library["recycle"].rglob("*"))
    with session_scope() as db:
        assert db.query(models.History).filter(models.History.media_id == source, models.History.media_type == "movie").count() == 0

    scanner_service.scan_movies(incremental=False)  # nothing left on disk to re-create the duplicate from
    with session_scope() as db:
        assert db.query(models.Movie).filter(models.Movie.tmdb_id == 646).count() == 1
        assert db.query(models.Movie).filter(models.Movie.title.like("%UIndex%")).count() == 0


def test_merge_fileless_duplicate_just_removes_it(client, admin_headers, monkeypatch, library, make_video, cleanup):
    from backend.services import scanner_service

    _dr_no(monkeypatch)
    good = library["movies"] / "Dr. No (1962)"
    video = make_video(good / "Dr.No.1962.1080p.BluRay.x264-GRP.mkv")
    dupe_dir = library["movies"] / "www.UIndex.org - Dr No Extras"
    dupe_dir.mkdir(parents=True)
    (dupe_dir / "poster.jpg").write_bytes(b"art")
    scanner_service.scan_movies(incremental=False)
    target, source = _movie_at(good), _movie_at(dupe_dir)
    client.post(f"{API}/movies/{target}/link", json={"external_id": "tmdb:646", "dry_run": False, "rename": False}, headers=admin_headers)

    preview = client.post(f"{API}/movies/{source}/link", json={"external_id": "tmdb:646"}, headers=admin_headers).json()
    assert [f["path"] for f in preview["merge"]["files"]] == [str(video)]
    r = client.post(f"{API}/movies/{source}/link", json={"external_id": "tmdb:646", "dry_run": False, "merge": True, "rename": False},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/movies/{source}", headers=admin_headers).status_code == 404
    assert video.exists() and not dupe_dir.exists()
    assert client.get(f"{API}/movies/{target}", headers=admin_headers).json()["file_path"] == str(video)
