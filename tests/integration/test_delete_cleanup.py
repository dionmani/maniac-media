"""Deleting a library entry detaches what pointed at it, so an item that later gets the same id starts clean."""
from __future__ import annotations

from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope

API = "/api/v1"


def _add(client, admin_headers, kind: str, title: str) -> int:
    r = client.post(f"{API}/{kind}", json={"title": title, "year": 2020}, headers=admin_headers)
    assert r.status_code == 201, r.text
    if kind == "movies":
        return r.json()["id"]
    with session_scope() as db:
        return db.scalar(select(models.Series.id).where(models.Series.title == title))


def _attach_refs(media_type: str, media_id: int, *, status: str = "downloading") -> None:
    with session_scope() as db:
        db.add(models.LibraryIssue(issue_type="missing_file", media_type=media_type, media_id=media_id, status="open", details="gone"))
        db.add(models.LibraryIssue(issue_type="duplicate_file", media_type=media_type, media_id=media_id, status="dismissed", resolved=True))
        db.add(models.Blocklist(media_type=media_type, media_id=media_id, release_title="Bad.Release-GRP", reason="test"))
        db.add(models.History(media_type=media_type, media_id=media_id, title="Some.Release-GRP", event_type="grabbed"))
        db.add(models.DownloadQueueItem(title="t", media_type=media_type, media_id=media_id, release_title="Pending.Release-GRP", status=status))
        db.add(models.DownloadQueueItem(title="t", media_type=media_type, media_id=media_id, release_title="Done.Release-GRP", status="imported"))


def _refs(media_type: str, media_id: int) -> dict:
    with session_scope() as db:
        def count(model):
            return db.query(model).filter(model.media_type == media_type, model.media_id == media_id).count()

        queue = {q.release_title: q for q in db.scalars(select(models.DownloadQueueItem).where(
            models.DownloadQueueItem.media_type == media_type, models.DownloadQueueItem.media_id == media_id))}
        db.expunge_all()
        return {"issues": count(models.LibraryIssue), "blocklist": count(models.Blocklist), "history": count(models.History), "queue": queue}


def _delete_queue(media_type: str, media_id: int) -> None:
    with session_scope() as db:
        db.query(models.DownloadQueueItem).filter_by(media_type=media_type, media_id=media_id).delete()


def test_deleting_a_movie_detaches_everything_before_its_id_is_reused(client, admin_headers):
    movie_id = _add(client, admin_headers, "movies", "Purge Cleanup Movie")
    _attach_refs("movie", movie_id)
    assert client.delete(f"{API}/movies/{movie_id}", headers=admin_headers).status_code == 200

    refs = _refs("movie", movie_id)
    assert refs["issues"] == refs["blocklist"] == refs["history"] == 0
    pending, done = refs["queue"]["Pending.Release-GRP"], refs["queue"]["Done.Release-GRP"]
    assert pending.status == "import_failed" and pending.failure_reason == "media_deleted"
    assert any(f["kind"] == "remove" for f in pending.failure_analysis["fixes"])
    assert done.status == "imported"  # finished downloads are left as they were
    with session_scope() as db:  # the log keeps the rows, unlinked
        assert db.query(models.History).filter(models.History.title == "Some.Release-GRP", models.History.media_id.is_(None)).count() == 1
    _delete_queue("movie", movie_id)

    # SQLite reuses the highest id: the new movie must not inherit anything
    again = _add(client, admin_headers, "movies", "Purge Cleanup Movie Again")
    assert again == movie_id
    assert client.get(f"{API}/library/issues", params={"media_id": again, "status": "all"}, headers=admin_headers).json()["items"] == []
    history = client.get(f"{API}/history", params={"media_id": again, "media_type": "movie"}, headers=admin_headers).json()["items"]
    assert not any(h["title"] in ("Some.Release-GRP", "Purge Cleanup Movie") for h in history)
    client.delete(f"{API}/movies/{again}", headers=admin_headers)


def test_deleting_a_series_detaches_everything(client, admin_headers):
    series_id = _add(client, admin_headers, "series", "Purge Cleanup Show")
    _attach_refs("tv", series_id, status="completed")
    assert client.delete(f"{API}/series/{series_id}", headers=admin_headers).status_code == 200
    refs = _refs("tv", series_id)
    assert refs["issues"] == refs["blocklist"] == refs["history"] == 0
    assert refs["queue"]["Pending.Release-GRP"].status == "import_failed"
    _delete_queue("tv", series_id)


def test_removing_episodes_clears_their_issues(client, admin_headers):
    from backend.services import media_service
    from backend.services.audit import SYSTEM

    series_id = _add(client, admin_headers, "series", "Purge Cleanup Episodes")
    with session_scope() as db:
        series = db.get(models.Series, series_id)
        gone = media_service.add_episode(db, series, 1, 1, "Pilot", None, SYSTEM)
        kept = media_service.add_episode(db, series, 1, 2, "Second", None, SYSTEM)
        for ep in (gone, kept):
            db.add(models.LibraryIssue(issue_type="missing_file", media_type="tv", media_id=series_id, episode_id=ep.id, status="open"))
        gone_id, kept_id = gone.id, kept.id
    r = client.delete(f"{API}/series/{series_id}/episodes/{gone_id}", params={"dry_run": False}, headers=admin_headers)
    assert r.status_code == 200, r.text
    with session_scope() as db:
        left = [i.episode_id for i in db.scalars(select(models.LibraryIssue).where(models.LibraryIssue.media_type == "tv",
                                                                                  models.LibraryIssue.media_id == series_id))]
    assert left == [kept_id]
    client.delete(f"{API}/series/{series_id}", headers=admin_headers)


def test_migration_clears_existing_leftovers(database):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from backend.db.session import db_manager

    migration = importlib.import_module("backend.db.migrations.versions.0015_purge_orphaned_media_refs")
    with session_scope() as db:
        series = db.scalar(select(models.Series))
        live_movie = models.Movie(title="Purge Cleanup Live", year=2020, monitored=True, quality_profile_id=1, has_file=False, file_size_bytes=0)
        db.add(live_movie)
        db.flush()
        live = live_movie.id
        ghost = live + 1000
        db.add(models.LibraryIssue(issue_type="missing_file", media_type="movie", media_id=ghost, status="open"))
        db.add(models.LibraryIssue(issue_type="missing_file", media_type="movie", media_id=live, status="open"))
        if series is not None:  # an issue for an episode that was removed while its series stayed
            db.add(models.LibraryIssue(issue_type="missing_file", media_type="tv", media_id=series.id, episode_id=10**6, status="open"))
        db.add(models.Blocklist(media_type="movie", media_id=ghost, release_title="Ghost.Release-GRP"))
        db.add(models.History(media_type="movie", media_id=ghost, title="Ghost.History", event_type="grabbed"))
        db.add(models.DownloadQueueItem(title="t", media_type="movie", media_id=ghost, release_title="Ghost.Queue-GRP", status="downloading"))

    with db_manager.engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

    with session_scope() as db:
        assert db.query(models.LibraryIssue).filter_by(media_type="movie", media_id=ghost).count() == 0
        assert db.query(models.LibraryIssue).filter_by(media_type="movie", media_id=live).count() == 1
        assert db.query(models.LibraryIssue).filter_by(episode_id=10**6).count() == 0
        assert db.query(models.Blocklist).filter_by(release_title="Ghost.Release-GRP").count() == 0
        assert db.query(models.History).filter_by(title="Ghost.History").one().media_id is None
        q = db.query(models.DownloadQueueItem).filter_by(release_title="Ghost.Queue-GRP").one()
        assert q.status == "import_failed" and q.failure_reason == "media_deleted"
        db.delete(q)
        db.query(models.LibraryIssue).filter_by(media_type="movie", media_id=live).delete()
        db.delete(db.get(models.Movie, live))
