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
