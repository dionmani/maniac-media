"""Queue rows must follow imports that happened outside the queue's own import path."""
from __future__ import annotations

from sqlalchemy import select

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata
from backend.services import import_service, media_service, queue_reconciliation_service as recon
from backend.services.audit import SYSTEM, history


def _movie(title: str, year: int) -> int:
    with session_scope() as db:
        return media_service.add_movie(db, MediaMetadata(media_type="movie", provider="test", title=title, year=year),
                                       quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM).id


def _queue(movie_id: int, path: str, status: str = "import_failed") -> int:
    with session_scope() as db:
        q = models.DownloadQueueItem(title="t", media_type="movie", media_id=movie_id, release_title="rel", status=status,
                                     download_path=path, error_message="Download path not found", failure_reason="path_not_found")
        db.add(q)
        db.flush()
        return q.id


def _status(qid: int) -> str:
    with session_scope() as db:
        return db.get(models.DownloadQueueItem, qid).status


def test_manual_import_settles_owning_queue_entry(database, library, make_video):
    movie_id = _movie("Alien", 1979)
    folder = library["downloads"] / "Alien.1979.1080p.BluRay.x264-GRP"
    src = make_video(folder / "Alien.1979.1080p.BluRay.x264-GRP.mkv")
    qid = _queue(movie_id, str(folder))

    result = import_service.manual_import([{"path": str(src), "media_type": "movie", "media_id": movie_id}], "move", SYSTEM)
    assert result.imported
    assert _status(qid) == "imported"
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, qid)
        assert q.error_message is None and q.failure_reason is None
        h = db.scalar(select(models.History).where(models.History.download_id == qid, models.History.event_type == "imported"))
        assert h is not None and h.data["source"] == str(src)


def test_manual_import_to_other_movie_leaves_queue_entry(database, library, make_video):
    owner, other = _movie("Brazil", 1985), _movie("Zardoz", 1974)
    folder = library["downloads"] / "Brazil.1985.1080p.BluRay.x264-GRP"
    src = make_video(folder / "Brazil.1985.1080p.BluRay.x264-GRP.mkv")
    qid = _queue(owner, str(folder))
    assert import_service.manual_import([{"path": str(src), "media_type": "movie", "media_id": other}], "move", SYSTEM).imported
    assert _status(qid) == "import_failed"


def test_reconcile_legacy_history_needs_unique_ownership(database, library):
    movie_id = _movie("Solaris", 1972)
    folder = str(library["downloads"] / "Solaris.1972.720p")
    qid = _queue(movie_id, folder)
    with session_scope() as db:
        history(db, "movie", movie_id, "Solaris", "imported", data={"source": folder + "/Solaris.mkv"})
    assert recon.reconcile_stale() >= 1
    assert _status(qid) == "imported"
    assert recon.reconcile_stale() == 0  # idempotent


def test_reconcile_ignores_other_media_and_sibling_folders(database, library):
    movie_id, other = _movie("Stalker", 1979), _movie("Mirror", 1975)
    folder = str(library["downloads"] / "Stalker.1979")
    qid = _queue(movie_id, folder)
    with session_scope() as db:
        history(db, "movie", other, "Mirror", "imported", download_id=qid, data={"source": folder + "/x.mkv"})
        history(db, "movie", movie_id, "Stalker", "imported", data={"source": folder + ".Other/x.mkv"})
    recon.reconcile_stale()
    assert _status(qid) == "import_failed"


def test_path_ownership_is_component_wise(library):
    assert recon.path_owns(str(library["downloads"] / "Rel"), library["downloads"] / "Rel" / "a.mkv")
    assert not recon.path_owns(str(library["downloads"] / "Rel"), library["downloads"] / "Rel.Other" / "a.mkv")
    assert not recon.path_owns(str(library["downloads"]), library["downloads"] / "Rel" / "a.mkv")
