"""APScheduler front-end: turns ``scheduled_tasks`` rows into queued jobs.

The scheduler never executes work itself – it only enqueues. Next run times are derived from
the persisted ``last_run``, so a restart neither skips nor double-fires a task.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from backend.db import models
from backend.db.base import utcnow
from backend.db.session import session_scope
from backend.workers.queue import job_queue

log = logging.getLogger("maniac.scheduler")


class TaskScheduler:
    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
        self.reload()
        self._scheduler.start()
        log.info("Task scheduler started")

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def reload(self) -> None:
        if not self._scheduler:
            return
        self._scheduler.remove_all_jobs()
        with session_scope() as db:
            tasks = list(db.scalars(select(models.ScheduledTask)))
        now = utcnow()
        for task in tasks:
            if not task.enabled or task.name not in job_queue.definitions:
                continue
            interval = timedelta(minutes=max(1, task.interval_minutes))
            first = (task.last_run + interval) if task.last_run else now + timedelta(seconds=20)
            if first < now:
                first = now + timedelta(seconds=15)
            self._scheduler.add_job(
                self._fire, IntervalTrigger(minutes=max(1, task.interval_minutes), start_date=_utc(first), timezone="UTC"),
                args=[task.name], id=task.name, next_run_time=_utc(first),
            )

    async def _fire(self, name: str) -> None:
        import asyncio

        def record():
            with session_scope() as db:
                task = db.get(models.ScheduledTask, name)
                if task:
                    task.last_run = utcnow()
            job_queue.enqueue(name, trigger="scheduled", dedupe_key=f"scheduled:{name}")

        await asyncio.to_thread(record)

    def run_now(self, name: str, payload: dict | None = None, *, trigger: str = "manual") -> int | None:
        with session_scope() as db:
            task = db.get(models.ScheduledTask, name)
            if task:
                task.last_run = utcnow()
        return job_queue.enqueue(name, payload, trigger=trigger, dedupe_key=f"manual:{name}" if not payload else None)

    def next_runs(self) -> dict[str, str | None]:
        if not self._scheduler:
            return {}
        return {j.id: j.next_run_time.isoformat() if j.next_run_time else None for j in self._scheduler.get_jobs()}


def _utc(dt: datetime):
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc)


scheduler = TaskScheduler()
