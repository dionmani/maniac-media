"""Download monitoring: follow-up poll cadence and housekeeping of its routine job rows."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

API = "/api/v1"


def test_follow_up_poll_uses_the_configured_interval(client, admin_headers, monkeypatch):
    from backend.services import download_service
    from backend.workers import tasks
    from backend.workers.queue import job_queue

    queued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda name, *a, **kw: queued.append((name, kw)))

    async def busy():
        return {"clients": 1, "updated": 1, "completed": 0, "failed": 0, "active": 62}

    monkeypatch.setattr(download_service, "sync_clients", busy)
    assert client.put(f"{API}/settings", json={"download_poll_seconds": 3}, headers=admin_headers).status_code == 422
    assert client.put(f"{API}/settings", json={"download_poll_seconds": 45}, headers=admin_headers).status_code == 200
    try:
        asyncio.run(tasks.download_monitor(SimpleNamespace(payload={})))
        assert queued == [("download_monitor", {"trigger": "event", "dedupe_key": "follow-up", "delay_seconds": 45})]

        async def idle():
            return {"clients": 1, "updated": 0, "completed": 0, "failed": 0, "active": 0}

        monkeypatch.setattr(download_service, "sync_clients", idle)
        queued.clear()
        asyncio.run(tasks.download_monitor(SimpleNamespace(payload={})))
        assert queued == []  # nothing downloading: back to the scheduled interval
    finally:
        client.put(f"{API}/settings", json={"download_poll_seconds": 30}, headers=admin_headers)


def test_housekeeping_prunes_routine_download_polls_after_a_day(database):
    from backend.db import models
    from backend.db.base import utcnow
    from backend.db.session import session_scope
    from backend.workers import tasks

    two_days_ago = utcnow() - timedelta(days=2)
    with session_scope() as db:
        rows = {
            "old_poll": models.Job(name="download_monitor", status="completed", trigger="event", created_at=two_days_ago),
            "old_failed_poll": models.Job(name="download_monitor", status="failed", trigger="event", created_at=two_days_ago),
            "recent_poll": models.Job(name="download_monitor", status="completed", trigger="event", created_at=utcnow()),
            "old_other": models.Job(name="library_scan", status="completed", trigger="scheduled", created_at=two_days_ago),
        }
        db.add_all(rows.values())
        db.flush()
        ids = {k: v.id for k, v in rows.items()}

    asyncio.run(tasks.cleanup(SimpleNamespace(payload={}, progress=lambda *a, **kw: None)))
    with session_scope() as db:
        left = {k for k, job_id in ids.items() if db.get(models.Job, job_id) is not None}
        for job_id in ids.values():
            if (row := db.get(models.Job, job_id)) is not None:
                db.delete(row)
    # failures are kept for diagnosis and other jobs keep the normal retention
    assert left == {"old_failed_poll", "recent_poll", "old_other"}
