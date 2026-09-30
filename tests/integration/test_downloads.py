"""Download monitoring: reconciling the queue with what download clients report."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from backend.db import models
from backend.db.base import utcnow
from backend.db.session import session_scope
from backend.domain.types import ClientItem
from backend.services import download_service
from backend.services.download_service import ClientRef


class _StubClient:
    def __init__(self, listed=(), findable=()):
        self.listed, self.findable, self.lookups = list(listed), list(findable), []

    async def get_items(self, category):
        return self.listed

    async def find_items(self, item_ids):
        self.lookups.append(item_ids)
        return [i for i in self.findable if i.id in item_ids]


def _setup(nzo_id: str, added_minutes_ago: int = 30) -> tuple[int, int]:
    with session_scope() as db:
        client = models.DownloadClient(name=f"stub-{nzo_id}", client_type="sabnzbd", enabled=False, category="tv", priority=99)
        db.add(client)
        db.flush()
        q = models.DownloadQueueItem(title="Show", media_type="tv", media_id=1, release_title=f"Show.S01E01.{nzo_id}",
                                     status="queued", download_client_id=client.id, client_item_id=nzo_id, protocol="usenet",
                                     added_at=utcnow() - timedelta(minutes=added_minutes_ago))
        db.add(q)
        db.flush()
        return client.id, q.id


def _sync(client_id: int, stub: _StubClient, monkeypatch) -> None:
    monkeypatch.setattr(download_service, "_clients", lambda protocol=None: [ClientRef(client_id, "stub", "tv", stub, False)])
    asyncio.run(download_service.sync_clients())


def _status(queue_id: int) -> tuple[str, int]:
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        blocked = db.scalar(select(func.count(models.Blocklist.id)).where(models.Blocklist.release_title == q.release_title))
        return q.status, blocked


def _failed_out(queue_id: int, release_title: str) -> tuple[bool, int, int]:
    """(still in the queue, blocklist rows, download_failed history rows) for a download that should have failed."""
    with session_scope() as db:
        queued = db.get(models.DownloadQueueItem, queue_id) is not None
        blocked = db.scalar(select(func.count(models.Blocklist.id)).where(models.Blocklist.release_title == release_title))
        failed = db.scalar(select(func.count(models.History.id)).where(models.History.title == release_title,
                                                                        models.History.event_type == "download_failed"))
        return queued, blocked, failed


def test_item_outside_the_listing_is_found_by_id(database, monkeypatch):
    client_id, queue_id = _setup("nzo_hidden")
    stub = _StubClient(findable=[ClientItem(id="nzo_hidden", name="x", status="downloading", progress=42.0)])
    _sync(client_id, stub, monkeypatch)
    assert stub.lookups == [["nzo_hidden"]]
    assert _status(queue_id) == ("downloading", 0)


def test_vanished_item_is_not_failed_immediately_nor_blocklisted(database, monkeypatch):
    client_id, queue_id = _setup("nzo_gone")
    retries = []
    monkeypatch.setattr("backend.workers.queue.job_queue.enqueue", lambda *a, **k: retries.append(a))
    _sync(client_id, _StubClient(), monkeypatch)
    assert _status(queue_id) == ("queued", 0)  # first miss only starts the grace period

    download_service._missing_since[queue_id] = utcnow() - timedelta(seconds=download_service.MISSING_GRACE_SECONDS + 1)
    _sync(client_id, _StubClient(), monkeypatch)
    assert _failed_out(queue_id, "Show.S01E01.nzo_gone") == (False, 0, 1)
    assert not [a for a in retries if a and a[0] == "automatic_search"]


def test_failed_download_leaves_the_queue_but_stays_in_history(database, monkeypatch):
    client_id, queue_id = _setup("nzo_broken")
    monkeypatch.setattr("backend.workers.queue.job_queue.enqueue", lambda *a, **k: None)
    stub = _StubClient(listed=[ClientItem(id="nzo_broken", name="x", status="failed", message="Aborted, cannot be completed")])
    _sync(client_id, stub, monkeypatch)
    assert _failed_out(queue_id, "Show.S01E01.nzo_broken") == (False, 1, 1)
    with session_scope() as db:
        entry = db.scalar(select(models.History).where(models.History.title == "Show.S01E01.nzo_broken"))
        assert entry.details == "Aborted, cannot be completed"


def test_leftover_failed_rows_are_hidden_from_the_queue(database):
    from backend.db.repositories import QueueRepository

    _, queue_id = _setup("nzo_legacy")
    with session_scope() as db:
        db.get(models.DownloadQueueItem, queue_id).status = "failed"
    with session_scope() as db:
        repo = QueueRepository(db)
        assert queue_id not in [q.id for q in repo.for_display()]
        assert queue_id not in [q.id for q in repo.for_display(include_finished=True)]


def test_miss_timer_resets_when_item_reappears(database, monkeypatch):
    client_id, queue_id = _setup("nzo_flaky")
    _sync(client_id, _StubClient(), monkeypatch)
    assert queue_id in download_service._missing_since
    _sync(client_id, _StubClient(listed=[ClientItem(id="nzo_flaky", name="x", status="queued")]), monkeypatch)
    assert queue_id not in download_service._missing_since


def test_queue_mirrors_client_order_with_active_download_first(database, monkeypatch):
    from backend.db.repositories import QueueRepository

    client_id, first = _setup("nzo_pos_a", added_minutes_ago=30)
    ids = {"nzo_pos_a": first}
    with session_scope() as db:
        for nzo, minutes in (("nzo_pos_b", 20), ("nzo_pos_c", 10), ("nzo_pos_d", 1)):
            q = models.DownloadQueueItem(title="Show", media_type="tv", media_id=1, release_title=f"Show.{nzo}", status="queued",
                                         download_client_id=client_id, client_item_id=nzo, protocol="usenet",
                                         added_at=utcnow() - timedelta(minutes=minutes))
            db.add(q)
            db.flush()
            ids[nzo] = q.id
    # newest grab is downloading, the oldest sits at the back of the client's queue
    stub = _StubClient(listed=[
        ClientItem(id="nzo_pos_d", name="d", status="downloading", progress=12.0, position=0),
        ClientItem(id="nzo_pos_c", name="c", status="queued", position=1),
        ClientItem(id="nzo_pos_b", name="b", status="queued", position=2),
        ClientItem(id="nzo_pos_a", name="a", status="queued", position=3),
    ])
    _sync(client_id, stub, monkeypatch)
    with session_scope() as db:
        order = [q.client_item_id for q in QueueRepository(db).for_display() if q.id in ids.values()]
    assert order == ["nzo_pos_d", "nzo_pos_c", "nzo_pos_b", "nzo_pos_a"]
