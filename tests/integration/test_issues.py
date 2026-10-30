"""Structured library issues: stable identity, lifecycle, incremental-safe reconciliation, API."""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from backend.db import migrator, models
from backend.db.session import create_db_engine, session_scope
from backend.services import issue_service, scanner_service
from backend.services.audit import SYSTEM
from backend.services.issue_service import IssueActionError

import sqlalchemy as sa


@pytest.fixture(autouse=True)
def _isolate(database, library):
    """The DB and library folders are session-wide: remove whatever each test created."""
    import shutil

    with session_scope() as db:
        movies = set(db.scalars(select(models.Movie.id)))
        series = set(db.scalars(select(models.Series.id)))
    before = {root: {p.name for p in library[root].iterdir()} for root in ("movies", "tv")}
    yield
    with session_scope() as db:
        for m in db.scalars(select(models.Movie).where(models.Movie.id.not_in(movies) if movies else sa.true())):
            db.delete(m)
        for sr in db.scalars(select(models.Series).where(models.Series.id.not_in(series) if series else sa.true())):
            db.delete(sr)
        db.flush()
        for issue in db.scalars(select(models.LibraryIssue)):
            owner = models.Movie if issue.media_type == "movie" else models.Series
            if db.get(owner, issue.media_id) is None:
                db.delete(issue)
    for root, names in before.items():
        for p in library[root].iterdir():
            if p.name not in names:
                shutil.rmtree(p, ignore_errors=True)


def _body(seed: int, size: int = 10000) -> bytes:
    return b"\x1a\x45\xdf\xa3" + bytes([seed]) * size


def _issues(title: str, issue_type: str | None = None, status: str | None = None) -> list[models.LibraryIssue]:
    with session_scope() as db:
        movie = db.scalar(select(models.Movie).where(models.Movie.title == title))
        stmt = select(models.LibraryIssue).where(models.LibraryIssue.media_type == "movie", models.LibraryIssue.media_id == movie.id)
        if issue_type:
            stmt = stmt.where(models.LibraryIssue.issue_type == issue_type)
        if status:
            stmt = stmt.where(models.LibraryIssue.status == status)
        rows = list(db.scalars(stmt))
        db.expunge_all()
        return rows


def test_duplicate_lifecycle_dismiss_sticks_and_reopens(database, library, make_video):
    folder = library["movies"] / "Heat (1995)"
    make_video(folder / "Heat.1995.1080p.BluRay.mkv", _body(1))
    second = make_video(folder / "Heat.1995.720p.WEB.mkv", _body(2, 9000))

    scanner_service.scan("movies", incremental=False)
    rows = _issues("Heat", "duplicate_file")
    assert len(rows) == 1 and rows[0].status == "open" and rows[0].severity == "warning"
    assert len(rows[0].data["candidates"]) == 2
    first_seen, issue_id = rows[0].first_detected_at, rows[0].id

    # dismiss -> rescan: still one row, still dismissed, first_detected_at untouched
    with session_scope() as db:
        issue_service.resolve(db, db.get(models.LibraryIssue, issue_id), "dismiss", None, SYSTEM)
    time.sleep(0.01)
    scanner_service.scan("movies", incremental=False)
    rows = _issues("Heat", "duplicate_file")
    assert len(rows) == 1
    assert rows[0].status == "dismissed" and rows[0].resolved is True and rows[0].resolution == "dismissed"
    assert rows[0].first_detected_at == first_seen
    assert rows[0].last_seen_at > first_seen

    # a third file changes the evidence signature -> reopens
    make_video(folder / "Heat.1995.2160p.UHD.mkv", _body(3, 11000))
    scanner_service.scan("movies", incremental=False)
    rows = _issues("Heat", "duplicate_file")
    assert len(rows) == 1 and rows[0].status == "open" and rows[0].resolved is False
    assert len(rows[0].data["candidates"]) == 3

    # remove the extras -> auto-resolved
    second.unlink()
    (folder / "Heat.1995.2160p.UHD.mkv").unlink()
    scanner_service.scan("movies", incremental=False)
    rows = _issues("Heat", "duplicate_file")
    assert len(rows) == 1
    assert rows[0].status == "resolved" and rows[0].resolution == "auto" and rows[0].resolved_at is not None
    assert rows[0].first_detected_at == first_seen


def test_incremental_scan_does_not_resolve_untouched_folders(database, library, make_video):
    folder = library["movies"] / "Ronin (1998)"
    make_video(folder / "Ronin.1998.1080p.BluRay.mkv", _body(4))
    make_video(folder / "Ronin.1998.720p.WEB.mkv", _body(5, 9000))
    scanner_service.scan("movies", incremental=False)
    assert len(_issues("Ronin", "duplicate_file", "open")) == 1

    stats = scanner_service.scan("movies", incremental=True)["movies"]
    assert stats["skipped"] >= 1
    rows = _issues("Ronin", "duplicate_file")
    assert len(rows) == 1 and rows[0].status == "open"  # folder was skipped, issue must survive


def test_missing_file_auto_resolves_when_restored(database, library, make_video):
    folder = library["movies"] / "Collateral (2004)"
    f = make_video(folder / "Collateral.2004.1080p.BluRay.mkv", _body(6))
    scanner_service.scan("movies", incremental=False)
    f.unlink()
    scanner_service.scan("movies", incremental=False)
    missing = _issues("Collateral", "missing_file")
    assert len(missing) == 1 and missing[0].status == "open" and missing[0].severity == "error"
    make_video(folder / "Collateral.2004.1080p.BluRay.mkv", _body(6))
    scanner_service.scan("movies", incremental=False)
    missing = _issues("Collateral", "missing_file")
    assert len(missing) == 1 and missing[0].status == "resolved" and missing[0].resolution == "auto"


def test_missing_file_names_the_file_and_stays_open_until_it_is_back(database, library, make_video):
    folder = library["movies"] / "Drive (2011)"
    f = make_video(folder / "Drive.2011.1080p.BluRay.mkv", _body(15))
    scanner_service.scan("movies", incremental=False)
    f.unlink()
    scanner_service.scan("movies", incremental=False)
    missing = _issues("Drive", "missing_file")
    assert len(missing) == 1 and missing[0].status == "open" and missing[0].path == str(f)
    issue_id = missing[0].id

    # the movie is detached now; later scans must not close the issue while the file is still gone
    scanner_service.scan("movies", incremental=False)
    scanner_service.scan("movies", incremental=True)
    missing = _issues("Drive", "missing_file")
    assert [(i.id, i.status) for i in missing] == [(issue_id, "open")]

    make_video(folder / "Drive.2011.1080p.BluRay.mkv", _body(15))
    scanner_service.scan("movies", incremental=False)
    missing = _issues("Drive", "missing_file")
    assert len(missing) == 1 and missing[0].status == "resolved" and missing[0].resolution == "auto"


def test_missing_episode_file_names_the_file_and_stays_open(database, library, make_video):
    f = make_video(library["tv"] / "Fargo" / "Season 01" / "Fargo.S01E03.1080p.WEB.mkv", _body(16))
    scanner_service.scan("tv", incremental=False)
    f.unlink()
    for _ in range(3):
        scanner_service.scan("tv", incremental=False)
    with session_scope() as db:
        series = db.scalar(select(models.Series).where(models.Series.title == "Fargo"))
        rows = list(db.scalars(select(models.LibraryIssue).where(models.LibraryIssue.issue_type == "missing_file",
                                                                  models.LibraryIssue.media_id == series.id)))
        assert [(r.status, r.path) for r in rows] == [("open", str(f))]
        assert rows[0].episode_id is not None


@pytest.fixture
def auto_search(monkeypatch):
    """Search-on-missing switched on for one test, capturing the jobs it queues instead of running them."""
    from backend.core.settings import settings_manager
    from backend.workers.queue import job_queue

    queued: list[tuple[str, dict]] = []
    monkeypatch.setattr(job_queue, "enqueue", lambda name, payload=None, **kw: queued.append((name, payload)) or len(queued))
    settings_manager.update(search_missing_files=True)
    yield queued
    settings_manager.update(search_missing_files=False)


def _lose(library, make_video, folder: str, name: str, seed: int):
    f = make_video(library["movies"] / folder / name, _body(seed))
    scanner_service.scan("movies", incremental=False)
    f.unlink()
    scanner_service.scan("movies", incremental=False)


def test_missing_file_queues_a_search_and_notes_it(database, library, make_video, auto_search):
    _lose(library, make_video, "Tenet (2020)", "Tenet.2020.1080p.BluRay.mkv", 17)
    issue = _issues("Tenet", "missing_file", "open")[0]
    assert auto_search == [("automatic_search", {"media_type": "movie", "media_id": issue.media_id, "issue_id": issue.id})]
    assert issue.data["search"]["status"] == "queued"

    scanner_service.scan("movies", incremental=False)  # still missing: no second search, and the note survives the rescan
    assert len(auto_search) == 1
    assert _issues("Tenet", "missing_file", "open")[0].data["search"]["status"] == "queued"


def test_unmonitored_missing_file_is_not_searched(database, library, make_video, auto_search):
    f = make_video(library["movies"] / "Dune (2021)" / "Dune.2021.1080p.BluRay.mkv", _body(18))
    scanner_service.scan("movies", incremental=False)
    with session_scope() as db:
        db.scalar(select(models.Movie).where(models.Movie.title == "Dune")).monitored = False
    f.unlink()
    scanner_service.scan("movies", incremental=False)
    note = _issues("Dune", "missing_file", "open")[0].data["search"]
    assert auto_search == [] and note["status"] == "skipped" and "not monitored" in note["reason"]


def test_many_files_missing_at_once_are_not_searched(database, library, make_video, auto_search):
    n = issue_service.AUTO_SEARCH_LIMIT + 1
    files = [make_video(library["movies"] / f"Storm Film {i} (2001)" / f"Storm.Film.{i}.2001.1080p.BluRay.mkv", _body(40 + i)) for i in range(n)]
    scanner_service.scan("movies", incremental=False)
    for f in files:
        f.unlink()
    scanner_service.scan("movies", incremental=False)
    notes = [_issues(f"Storm Film {i}", "missing_file", "open")[0].data["search"] for i in range(n)]
    assert auto_search == []
    assert all(note["status"] == "skipped" and "mounted" in note["reason"] for note in notes)


def test_search_outcome_lists_every_release_and_indexer_tried(database, library, make_video, auto_search, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from backend.domain.types import ReleaseInfo
    from backend.services import download_service, indexer_service
    from backend.workers import tasks

    _lose(library, make_video, "Memento (2000)", "Memento.2000.1080p.BluRay.mkv", 19)
    other = "Some.Other.Film.1999.1080p.BluRay.x264-GRP"

    async def search(criteria):
        return [ReleaseInfo(other, other, "u", "Idx", "usenet", size_bytes=2 * 1024**3)], [
            {"indexer": "Idx", "results": 1, "error": None},
            {"indexer": "Broken", "results": 0, "error": "Client error '401' for url 'https://idx.example/api?t=movie&apikey=SECRET123'"},
        ]

    async def grab(*args, **kwargs):
        raise AssertionError("nothing acceptable, nothing grabbed")

    monkeypatch.setattr(indexer_service, "search", search)
    monkeypatch.setattr(download_service, "grab", grab)
    asyncio.run(tasks.automatic_search(SimpleNamespace(payload=auto_search[0][1])))

    note = _issues("Memento", "missing_file", "open")[0].data["search"]
    assert note["status"] == "not_found" and note["candidates"] == 1
    assert [r["title"] for r in note["releases"]] == [other] and note["releases"][0]["rejections"]
    assert [i["indexer"] for i in note["indexers"]] == ["Idx", "Broken"] and note["indexers"][1]["error"]
    assert "SECRET123" not in str(note)


def test_tv_upgrade_issue_recorded_with_episode(database, library, make_video):
    make_video(library["tv"] / "Mindhunter" / "Season 01" / "Mindhunter.S01E02.DVDRip.mkv", _body(7))
    scanner_service.scan("tv", incremental=False)
    with session_scope() as db:
        series = db.scalar(select(models.Series).where(models.Series.title == "Mindhunter"))
        ep = db.scalar(select(models.Episode).where(models.Episode.series_id == series.id, models.Episode.episode_number == 2))
        rows = list(db.scalars(select(models.LibraryIssue).where(models.LibraryIssue.issue_type == "upgrade_available",
                                                                  models.LibraryIssue.media_id == series.id)))
        assert len(rows) == 1
        assert rows[0].episode_id == ep.id and rows[0].severity == "info" and rows[0].data["current"]


def test_resolve_actions_validated(database, library, make_video):
    make_video(library["movies"] / "Sicario (2015)" / "Sicario.2015.1080p.BluRay.mkv", _body(8))
    make_video(library["movies"] / "Sicario (2015)" / "Sicario.2015.720p.WEB.mkv", _body(9, 9000))
    scanner_service.scan("movies", incremental=False)
    issue_id = _issues("Sicario", "duplicate_file")[0].id
    with session_scope() as db:
        issue = db.get(models.LibraryIssue, issue_id)
        with pytest.raises(IssueActionError):
            issue_service.resolve(db, issue, "delete_file", None, SYSTEM)  # not allowed for duplicates
        with pytest.raises(IssueActionError):
            issue_service.resolve(db, issue, "nonsense", None, SYSTEM)
        issue_service.resolve(db, issue, "dismiss", None, SYSTEM)
        issue_service.resolve(db, issue, "reopen", None, SYSTEM)
        assert issue.status == "open" and issue.resolved is False and issue.resolved_at is None


def test_issue_api(client, admin_headers, database, library, make_video):
    make_video(library["movies"] / "Prisoners (2013)" / "Prisoners.2013.1080p.BluRay.mkv", _body(10))
    make_video(library["movies"] / "Prisoners (2013)" / "Prisoners.2013.720p.WEB.mkv", _body(11, 9000))
    scanner_service.scan("movies", incremental=False)
    movie_id = _issues("Prisoners")[0].media_id

    types = client.get("/api/v1/library/issue-types", headers=admin_headers).json()
    assert {t["type"] for t in types} >= {"missing_file", "duplicate_file", "upgrade_available"}
    assert all({"label", "severity", "actions"} <= set(t) for t in types)

    r = client.get("/api/v1/library/issues", params={"media_id": movie_id, "issue_type": "duplicate_file"}, headers=admin_headers)
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["status"] == "open" and item["severity"] == "warning" and item["label"] == "Duplicate files"
    assert item["data"]["candidates"] and "dismiss" in item["actions"]
    issue_id = item["id"]

    assert client.get("/api/v1/library/issues", params={"media_id": movie_id, "severity": "error"}, headers=admin_headers).json()["items"] == []

    # unknown / disallowed action -> 422
    bad = client.post(f"/api/v1/library/issues/{issue_id}/resolve", json={"action": "delete_file"}, headers=admin_headers)
    assert bad.status_code == 422

    # no body == dismiss (backward compatible)
    ok = client.post(f"/api/v1/library/issues/{issue_id}/resolve", headers=admin_headers)
    assert ok.status_code == 200, ok.text
    assert client.get("/api/v1/library/issues", params={"media_id": movie_id}, headers=admin_headers).json()["items"] == []
    dismissed = client.get("/api/v1/library/issues", params={"media_id": movie_id, "status": "dismissed"}, headers=admin_headers).json()["items"]
    assert [i["id"] for i in dismissed] == [issue_id]

    summary = client.get("/api/v1/library/issues/summary", headers=admin_headers).json()
    assert summary["by_type"]["duplicate_file"]["dismissed"] >= 1 and "by_severity" in summary

    # bulk reopen
    bulk = client.post("/api/v1/library/issues/bulk", json={"ids": [issue_id, 999999], "action": "reopen"}, headers=admin_headers)
    assert bulk.status_code == 200 and bulk.json()["processed"] == 1 and bulk.json()["failed"][0]["id"] == 999999
    assert client.post("/api/v1/library/issues/bulk", json={"ids": list(range(1, 502)), "action": "dismiss"}, headers=admin_headers).status_code == 422


def test_issue_list_names_the_movie_or_series(client, admin_headers, database, library, make_video):
    make_video(library["movies"] / "Thief (1981)" / "Thief.1981.1080p.BluRay.mkv", _body(12))
    make_video(library["movies"] / "Thief (1981)" / "Thief.1981.720p.WEB.mkv", _body(13, 9000))
    make_video(library["tv"] / "Severance" / "Season 01" / "Severance.S01E01.DVDRip.mkv", _body(14))
    scanner_service.scan("movies", incremental=False)
    scanner_service.scan("tv", incremental=False)

    items = client.get("/api/v1/library/issues", params={"page_size": 500}, headers=admin_headers).json()["items"]
    titles = {(i["media_type"], i["title"]) for i in items}
    assert {("movie", "Thief"), ("tv", "Severance")} <= titles


def _alembic(engine, revision: str) -> None:
    from alembic import command

    cfg = migrator.alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def test_migration_backfills_status_and_fingerprint(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'old.db'}")
    _alembic(engine, "0004_media_files")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO movies (id, title, year) VALUES (1, 'A', 2000), (2, 'B', 2000)"))  # 0015 drops orphans
        conn.execute(sa.text(
            "INSERT INTO library_issues (issue_type, media_type, media_id, path, details, detected_at, resolved) VALUES "
            "('missing_file','movie',1,'C:\\Movies\\A.mkv','gone','2026-01-01 00:00:00',0),"
            "('missing_file','movie',1,'c:/movies/a.mkv','gone (older copy)','2026-01-02 00:00:00',0),"
            "('duplicate_file','movie',2,'C:\\Movies\\B','dup','2026-01-03 00:00:00',1)"))
    _alembic(engine, "head")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, status, resolved, severity, fingerprint, first_detected_at, last_seen_at, resolution FROM library_issues ORDER BY id")).all()
    assert [r.status for r in rows] == ["open", "resolved", "resolved"]  # legacy twin of an open issue is closed
    assert [bool(r.resolved) for r in rows] == [False, True, True]
    assert rows[0].severity == "error" and rows[2].severity == "warning"
    assert rows[0].fingerprint == rows[1].fingerprint and len(rows[0].fingerprint) == 64
    assert rows[0].first_detected_at == rows[0].last_seen_at is not None
    assert rows[2].resolution == "auto"


def test_scan_and_inspection_do_not_resolve_each_others_issues(database, library, make_video):
    """corrupt_file belongs to the scanner's header check; truncated_file/invalid_media to ffprobe inspection."""
    from backend.services.inspection_service import _IssueBatch, _reconcile

    good = make_video(library["movies"] / "Sicario (2015)" / "Sicario.2015.1080p.BluRay.mkv", _body(6))
    bad = make_video(library["movies"] / "Arrival (2016)" / "Arrival.2016.1080p.BluRay.mkv", b"\x00" * 10000)
    scanner_service.scan("movies", incremental=False)
    assert len(_issues("Arrival", "corrupt_file", "open")) == 1

    with session_scope() as db:
        sicario = db.scalar(select(models.Movie).where(models.Movie.title == "Sicario")).id
        arrival = db.scalar(select(models.Movie).where(models.Movie.title == "Arrival")).id
    _reconcile(_IssueBatch(reports=[{"issue_type": "truncated_file", "media_type": "movie", "media_id": sicario,
                                     "path": str(good), "details": "File appears truncated"}]))

    # a full scan of a file with a valid header must not close ffprobe's finding
    scanner_service.scan("movies", incremental=False)
    assert len(_issues("Sicario", "truncated_file", "open")) == 1

    # a clean inspection pass closes its own finding but leaves the scanner's corrupt_file alone
    _reconcile(_IssueBatch(touched=[("movie", sicario), ("movie", arrival)],
                           touched_paths=[("movie", str(good)), ("movie", str(bad))]))
    assert _issues("Sicario", "truncated_file")[0].status == "resolved"
    assert len(_issues("Arrival", "corrupt_file", "open")) == 1


def test_migration_moves_inspection_findings_off_corrupt_file(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'old.db'}")
    _alembic(engine, "0011_custom_formats")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO media_files (media_type, path, path_hash, size_bytes, revision, inspected_at, inspection_error) VALUES "
            "('movie','/m/trunc.mkv','h1',1,1,'2026-01-01 00:00:00',NULL),"
            "('movie','/m/bad.mkv','h2',1,1,NULL,'ffprobe failed: Invalid data found when processing input'),"
            "('movie','/m/header.mkv','h3',1,1,'2026-01-01 00:00:00',NULL)"))
        conn.execute(sa.text("INSERT INTO movies (id, title, year) VALUES (1, 'A', 2000), (3, 'C', 2000)"))  # 0015 drops orphans
        conn.execute(sa.text(
            "INSERT INTO library_issues (issue_type, media_type, media_id, path, details, status, resolved, severity, detected_at) VALUES "
            "('corrupt_file','movie',1,'/m/trunc.mkv','File appears truncated: duration is 10.0 min, expected 100 min','open',0,'error','2026-01-01'),"
            "('corrupt_file','movie',3,'/m/header.mkv','Missing container header','open',0,'error','2026-01-01')"))
    _alembic(engine, "head")
    with engine.connect() as conn:
        issues = {r.path: r for r in conn.execute(sa.text("SELECT path, status, resolution FROM library_issues"))}
        files = {r.path: r for r in conn.execute(sa.text("SELECT path, inspected_at, inspection_error FROM media_files"))}
    assert issues["/m/trunc.mkv"].status == "resolved" and issues["/m/trunc.mkv"].resolution == "auto"
    assert issues["/m/header.mkv"].status == "open"  # the scanner's own finding is untouched
    assert files["/m/trunc.mkv"].inspected_at is None  # re-queued for re-inspection under the new rules
    assert files["/m/bad.mkv"].inspection_error is None
    assert files["/m/header.mkv"].inspected_at is not None


def _report(movie_title: str, issue_type: str, path, data: dict | None = None) -> int:
    from backend.services.inspection_service import _IssueBatch, _reconcile

    with session_scope() as db:
        movie_id = db.scalar(select(models.Movie).where(models.Movie.title == movie_title)).id
    _reconcile(_IssueBatch(reports=[{"issue_type": issue_type, "media_type": "movie", "media_id": movie_id,
                                     "path": str(path), "details": "test", "data": data}]))
    return _issues(movie_title, issue_type, "open")[0].id


def test_relabel_survives_rescan_until_the_file_changes(database, library, make_video):
    from backend.db.repositories import MediaFileRepository

    video = make_video(library["movies"] / "Dune (2021)" / "Dune.2021.1080p.BluRay.mkv", _body(7))
    scanner_service.scan("movies", incremental=False)
    issue_id = _report("Dune", "quality_mismatch", video, {"claimed": "1080p", "actual": "720p"})

    with session_scope() as db:
        assert "720p" in issue_service.resolve(db, db.get(models.LibraryIssue, issue_id), "relabel", None, SYSTEM)

    scanner_service.scan("movies", incremental=False)  # re-parses "1080p" from the filename
    with session_scope() as db:
        mf = MediaFileRepository(db).by_path(video)
        assert mf.quality == "Bluray-720p" and mf.quality_locked is True
        assert db.get(models.Movie, mf.movie_id).quality == "Bluray-720p"

    video.write_bytes(_body(8, 12000))  # replaced content: the relabel no longer applies
    scanner_service.scan("movies", incremental=False)
    with session_scope() as db:
        mf = MediaFileRepository(db).by_path(video)
        assert mf.quality == "Bluray-1080p" and mf.quality_locked is False


def test_detached_file_retires_its_file_scoped_issues(database, library, make_video):
    from backend.db.repositories import MediaFileRepository
    from backend.services import media_file_service

    video = make_video(library["movies"] / "Tenet (2020)" / "Tenet.2020.1080p.BluRay.mkv", _body(9))
    scanner_service.scan("movies", incremental=False)
    _report("Tenet", "runtime_mismatch", video)
    _report("Tenet", "upgrade_available", video)  # media-level, not about this file's contents

    with session_scope() as db:
        media_file_service.detach_file(db, MediaFileRepository(db).by_path(video), reason="upgraded")
    runtime = _issues("Tenet", "runtime_mismatch")[0]
    assert runtime.status == "resolved" and runtime.resolution == "auto"
    assert _issues("Tenet", "upgrade_available")[0].status == "open"


def test_moved_file_carries_its_issues_to_the_new_path(database, library, make_video):
    from backend.db.repositories import MediaFileRepository
    from backend.domain.issues import fingerprint
    from backend.services import media_file_service
    from backend.services.inspection_service import _IssueBatch, _reconcile

    folder = library["movies"] / "Nope (2022)"
    video = make_video(folder / "Nope.2022.1080p.BluRay.mkv", _body(10))
    scanner_service.scan("movies", incremental=False)
    _report("Nope", "quality_mismatch", video)
    with session_scope() as db:
        dismissed = db.get(models.LibraryIssue, _report("Nope", "language_missing", video))
        issue_service.resolve(db, dismissed, "dismiss", None, SYSTEM)

    renamed = folder / "Nope (2022) Bluray-1080p.mkv"
    video.rename(renamed)
    with session_scope() as db:
        mf = MediaFileRepository(db).by_path(video)
        media_file_service.move_file(db, mf, renamed)

    moved = {r.issue_type: r for r in _issues("Nope")}
    assert moved["quality_mismatch"].path == str(renamed) and moved["quality_mismatch"].status == "open"
    assert moved["quality_mismatch"].fingerprint == fingerprint("quality_mismatch", "movie", moved["quality_mismatch"].media_id, str(renamed))
    assert moved["language_missing"].path == str(renamed) and moved["language_missing"].status == "dismissed"

    # a clean re-inspection at the new path now resolves it (before, it was stranded on the old path)
    _reconcile(_IssueBatch(touched_paths=[("movie", str(renamed))]))
    assert _issues("Nope", "quality_mismatch")[0].status == "resolved"


def test_relabel_rescores_custom_formats_at_the_measured_resolution(database, library, make_video, client, admin_headers):
    """A format keyed on resolution must follow the relabel – for the stored score and for upgrade decisions."""
    from backend.db.repositories import MediaFileRepository
    from backend.services import quality_service, search_service

    cf = client.post("/api/v1/custom-formats", json={"name": "Is 1080p (relabel test)", "include_in_rename": False, "specifications": [
        {"name": "1080p", "kind": "resolution", "value": "1080p", "negate": False, "required": True}]}, headers=admin_headers)
    assert cf.status_code == 201, cf.text
    cf_id = cf.json()["id"]
    prof = client.post("/api/v1/quality-profiles", json={
        "name": "Relabel CF Profile", "items": ["Bluray-720p", "Bluray-1080p"], "cutoff": "Bluray-1080p",
        "format_scores": {str(cf_id): 100}}, headers=admin_headers)
    assert prof.status_code in (200, 201), prof.text
    prof_id = prof.json()["id"]
    try:
        video = make_video(library["movies"] / "Blade Runner (1982)" / "Blade.Runner.1982.1080p.BluRay.x264-GRP.mkv", _body(11))
        scanner_service.scan("movies", incremental=False)
        with session_scope() as db:
            movie = db.scalar(select(models.Movie).where(models.Movie.title == "Blade Runner"))
            movie.quality_profile_id = prof_id
        scanner_service.scan("movies", incremental=False)  # re-score under the new profile
        with session_scope() as db:
            assert MediaFileRepository(db).by_path(video).custom_format_score == 100

        issue_id = _report("Blade Runner", "quality_mismatch", video, {"claimed": "1080p", "actual": "720p"})
        with session_scope() as db:
            issue_service.resolve(db, db.get(models.LibraryIssue, issue_id), "relabel", None, SYSTEM)
        with session_scope() as db:
            assert MediaFileRepository(db).by_path(video).custom_format_score == 0  # re-scored on relabel

        scanner_service.scan("movies", incremental=False)
        with session_scope() as db:
            mf = MediaFileRepository(db).by_path(video)
            assert mf.custom_format_score == 0 and mf.custom_formats == []  # and a rescan keeps it that way
            movie = db.get(models.Movie, mf.movie_id)
            current_fs, _ = search_service.compute_file_format_score(
                db, "movie", movie.id, movie.file_path, quality_service.spec_for(db, prof_id), quality_service.get_matcher(db))
            assert current_fs == 0  # upgrade decisions compare against the measured quality too
    finally:
        with session_scope() as db:
            for m in db.scalars(select(models.Movie).where(models.Movie.quality_profile_id == prof_id)):
                m.quality_profile_id = 1
        client.delete(f"/api/v1/quality-profiles/{prof_id}", headers=admin_headers)
        client.delete(f"/api/v1/custom-formats/{cf_id}", headers=admin_headers)
