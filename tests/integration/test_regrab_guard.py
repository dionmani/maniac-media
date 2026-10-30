"""Searches never fetch the identical release again after it came back without a file: indexers ban accounts
that download one NZB over and over (a failed import followed by the next missing search used to do exactly that)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import QueueRepository
from backend.db.session import session_scope
from backend.domain.types import MediaMetadata, ReleaseInfo
from backend.services import download_service, indexer_service, media_service, search_service
from backend.services.audit import SYSTEM
from backend.services.decision_engine import Target, evaluate
from backend.services.download_service import ClientRef

FIRST = "Regrab Film 2024 1080p WEB-DL x264-AAA"
OTHER = "Regrab Film 2024 1080p WEB-DL x264-BBB"


class _Sab:
    protocol = "usenet"

    def __init__(self):
        self.added: list[str] = []

    async def add(self, release, category):
        self.added.append(release.title)
        return f"SABnzbd_nzo_{len(self.added)}"


def _release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, f"https://idx/{title}", "Idx", "usenet", size_bytes=2 * 1024**3)


@pytest.fixture
def sab(database, monkeypatch):
    with session_scope() as db:
        row = models.DownloadClient(name="regrab-sab", client_type="sabnzbd", enabled=False, category="movies", priority=99)
        db.add(row)
        db.flush()
        client_id = row.id
    client = _Sab()
    monkeypatch.setattr(download_service, "_clients", lambda protocol=None: [ClientRef(client_id, "sab", "movies", client, False)])
    offered: list[ReleaseInfo] = []

    async def search(criteria):
        return list(offered), []

    monkeypatch.setattr(indexer_service, "search", search)
    return client, offered


def _movie(title: str) -> int:
    with session_scope() as db:
        meta = MediaMetadata(media_type="movie", provider="test", title=title, year=2024, release_date="2024-01-01")
        return media_service.add_movie(db, meta, quality_profile_id=1, root_folder=None, monitored=True, actor=SYSTEM).id


def _fail_import(queue_id: int, reason: str = "not_matched") -> None:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        q.status, q.failure_reason = "import_failed", reason


def test_failed_import_blocks_searching_again_until_cleared(sab):
    client, offered = sab
    offered.append(_release(FIRST))
    movie_id = _movie("Regrab Film")

    first = asyncio.run(search_service.automatic("movie", movie_id))
    assert first["grabbed"] == FIRST
    _fail_import(first["queue_id"])

    again = asyncio.run(search_service.automatic("movie", movie_id))
    assert again == {"grabbed": None, "reason": "Already in the download queue"}
    assert client.added == [FIRST]

    # cleared without a blocklist: a different release is fine, the identical one is not
    with session_scope() as db:
        db.delete(db.get(models.DownloadQueueItem, first["queue_id"]))
    alone = asyncio.run(search_service.automatic("movie", movie_id))
    assert alone["grabbed"] is None
    assert alone["rejected"][0]["rejections"] == ["Already grabbed recently"]
    offered.append(_release(OTHER))
    assert asyncio.run(search_service.automatic("movie", movie_id))["grabbed"] == OTHER
    assert client.added == [FIRST, OTHER]


def test_grabs_older_than_a_week_are_forgotten(sab):
    _, offered = sab
    title = "Regrab Later 2024 1080p WEB-DL x264-AAA"
    offered.append(_release(title))
    movie_id = _movie("Regrab Later")
    first = asyncio.run(search_service.automatic("movie", movie_id))
    with session_scope() as db:
        db.delete(db.get(models.DownloadQueueItem, first["queue_id"]))
        for h in db.query(models.History).filter_by(media_type="movie", media_id=movie_id, event_type="grabbed"):
            h.timestamp = utcnow() - timedelta(days=search_service.RECENT_GRAB_DAYS + 1)
    assert asyncio.run(search_service.automatic("movie", movie_id))["grabbed"] == title


def test_download_orphaned_by_a_deleted_item_holds_nothing(database):
    with session_scope() as db:
        db.add(models.DownloadQueueItem(title="Gone", media_type="movie", media_id=987654, release_title="Gone.2020.1080p",
                                        status="import_failed", failure_reason="media_deleted"))
        db.add(models.DownloadQueueItem(title="Stuck", media_type="movie", media_id=987655, release_title="Stuck.2020.1080p",
                                        status="import_failed", failure_reason="not_matched"))
        db.flush()
        repo = QueueRepository(db)
        assert not repo.is_queued("movie", 987654)
        assert repo.is_queued("movie", 987655)


def test_recently_grabbed_release_is_rejected():
    from backend.domain.quality import QualityProfileSpec

    target = Target("movie", 1, "Regrab Film", 2024, QualityProfileSpec("Any", ["WEBDL-1080p"]), recent_grabs={FIRST.lower()})
    assert evaluate(_release(FIRST), target).rejections == ["Already grabbed recently"]
    assert evaluate(_release(OTHER), target).approved
