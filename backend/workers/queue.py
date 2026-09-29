"""Persistent job queue + asyncio worker pool.

Scheduler ──enqueue──▶ jobs table ──claim──▶ Worker pool ──▶ handler

* Jobs live in the database, so queued work survives restarts; jobs that were *running* when
  the process died are re-queued on startup (with their attempt counted).
* ``dedupe_key`` prevents piling up identical work (e.g. five RSS syncs queued behind a slow one).
* Failed jobs retry with exponential backoff up to ``max_attempts``.
* Blocking handlers run in threads (``asyncio.to_thread``); the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, func, or_, select, update

from backend.core.events import EventType, bus
from backend.core.observability import JOB_DURATION, JOBS_TOTAL, QUEUE_DEPTH
from backend.db import models
from backend.db.base import utcnow
from backend.db.session import session_scope

log = logging.getLogger("maniac.jobs")


@dataclass
class JobContext:
    job_id: int
    name: str
    payload: dict[str, Any]
    _last_report: float = 0.0

    def progress(self, percent: float, message: str | None = None) -> None:
        """Thread-safe progress reporting (throttled to ~2 writes/second)."""
        now = time.monotonic()
        if now - self._last_report < 0.5 and percent < 100:
            return
        self._last_report = now
        with session_scope() as db:
            db.execute(update(models.Job).where(models.Job.id == self.job_id).values(progress=round(percent, 1), message=message))
        bus.publish(EventType.JOB_UPDATED, id=self.job_id, name=self.name, status="running", progress=round(percent, 1), message=message)


Handler = Callable[[JobContext], Awaitable[Any]]


@dataclass
class JobDefinition:
    name: str
    handler: Handler
    title: str
    priority: int = 5
    max_attempts: int = 3
    exclusive: bool = True  # at most one running instance


class JobQueue:
    def __init__(self) -> None:
        self.definitions: dict[str, JobDefinition] = {}
        self._wakeup: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(self, name: str, title: str, *, priority: int = 5, max_attempts: int = 3, exclusive: bool = True):
        def deco(fn: Handler) -> Handler:
            self.definitions[name] = JobDefinition(name, fn, title, priority, max_attempts, exclusive)
            return fn

        return deco

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._wakeup = asyncio.Event()

    def enqueue(self, name: str, payload: dict | None = None, *, trigger: str = "manual", dedupe_key: str | None = None,
                delay_seconds: int = 0) -> int | None:
        """Enqueue a job; returns its id (or the id of an identical pending job)."""
        if name not in self.definitions:
            raise KeyError(f"Unknown job '{name}'")
        from backend.services.database_service import maintenance

        if maintenance.is_set():  # database migration in progress – scheduled work simply re-runs later
            return None
        definition = self.definitions[name]
        payload = dict(payload or {})
        if dedupe_key is not None:
            payload["_dedupe"] = dedupe_key
        with session_scope() as db:
            stmt = select(models.Job).where(models.Job.name == name, models.Job.status == "queued")
            for existing in db.scalars(stmt):
                if (existing.payload or {}).get("_dedupe") == dedupe_key or (dedupe_key is None and (existing.payload or {}) == payload):
                    return existing.id
            job = models.Job(name=name, payload=payload, status="queued", priority=definition.priority, attempts=0,
                             max_attempts=definition.max_attempts, trigger=trigger, progress=0.0,
                             run_after=utcnow() + timedelta(seconds=delay_seconds) if delay_seconds else None)
            db.add(job)
            db.flush()
            job_id = job.id
        self._notify()
        bus.publish(EventType.JOB_UPDATED, id=job_id, name=name, status="queued")
        return job_id

    def _notify(self) -> None:
        if self._loop and self._wakeup:
            self._loop.call_soon_threadsafe(self._wakeup.set)

    def recover(self) -> int:
        """Re-queue jobs orphaned by a crash/restart."""
        with session_scope() as db:
            result = db.execute(
                update(models.Job).where(models.Job.status == "running").values(status="queued", message="Re-queued after restart")
            )
            return result.rowcount or 0

    def claim(self) -> models.Job | None:
        now = utcnow()
        with session_scope() as db:
            running = set(db.scalars(select(models.Job.name).where(models.Job.status == "running")))
            candidates = db.scalars(
                select(models.Job)
                .where(models.Job.status == "queued", or_(models.Job.run_after.is_(None), models.Job.run_after <= now))
                .order_by(models.Job.priority, models.Job.created_at)
                .limit(20)
            ).all()
            for job in candidates:
                definition = self.definitions.get(job.name)
                if definition is None:
                    job.status, job.error = "failed", "No handler registered"
                    continue
                if definition.exclusive and job.name in running:
                    continue
                # optimistic claim: only succeeds if nobody else took it
                claimed = db.execute(
                    update(models.Job).where(and_(models.Job.id == job.id, models.Job.status == "queued"))
                    .values(status="running", started_at=now, attempts=models.Job.attempts + 1, progress=0.0)
                ).rowcount
                if claimed:
                    db.flush()
                    db.refresh(job)
                    return job
            QUEUE_DEPTH.set(db.scalar(select(func.count(models.Job.id)).where(models.Job.status == "queued")) or 0)
        return None

    async def wait(self, timeout: float) -> None:
        assert self._wakeup is not None
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wakeup.clear()


job_queue = JobQueue()
active_pool: "WorkerPool | None" = None


def running_job_count() -> int:
    with session_scope() as db:
        return db.scalar(select(func.count(models.Job.id)).where(models.Job.status == "running")) or 0


class WorkerPool:
    def __init__(self, queue: JobQueue, size: int = 4) -> None:
        self.queue = queue
        self.size = size
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.paused = False

    async def start(self) -> None:
        global active_pool
        active_pool = self
        self.queue.bind(asyncio.get_running_loop())
        recovered = await asyncio.to_thread(self.queue.recover)
        if recovered:
            log.info("Recovered %d interrupted job(s)", recovered)
        self._stopping.clear()
        self._tasks = [asyncio.create_task(self._worker(i), name=f"maniac-worker-{i}") for i in range(self.size)]
        log.info("Worker pool started with %d workers", self.size)

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _worker(self, index: int) -> None:
        while not self._stopping.is_set():
            if self.paused:
                await asyncio.sleep(1)
                continue
            try:
                job = await asyncio.to_thread(self.queue.claim)
            except Exception:
                log.exception("Failed to claim job")
                await asyncio.sleep(5)
                continue
            if job is None:
                await self.queue.wait(timeout=5)
                continue
            await self._execute(job)

    async def _execute(self, job: models.Job) -> None:
        definition = self.queue.definitions[job.name]
        ctx = JobContext(job.id, job.name, {k: v for k, v in (job.payload or {}).items() if not k.startswith("_")})
        bus.publish(EventType.JOB_UPDATED, id=job.id, name=job.name, status="running", progress=0)
        start = time.perf_counter()
        status, result, error = "completed", None, None
        try:
            result = await definition.handler(ctx)
        except asyncio.CancelledError:
            status, error = "queued", "Interrupted by shutdown"
            raise
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.warning("Job %s #%s failed: %s", job.name, job.id, exc)
            log.debug("".join(traceback.format_exception(exc)))
        finally:
            elapsed = time.perf_counter() - start
            JOB_DURATION.labels(job.name).observe(elapsed)
            JOBS_TOTAL.labels(job.name, status).inc()
            await asyncio.to_thread(self._finish, job.id, status, result, error, elapsed)

    def _finish(self, job_id: int, status: str, result: Any, error: str | None, elapsed: float) -> None:
        with session_scope() as db:
            job = db.get(models.Job, job_id)
            if job is None:
                return
            if status == "failed" and job.attempts < job.max_attempts:
                job.status = "queued"
                job.run_after = utcnow() + timedelta(seconds=30 * (2 ** (job.attempts - 1)))
                job.message = f"Retrying after failure: {error}"
                job.error = error
            else:
                job.status = status
                job.finished_at = utcnow()
                job.progress = 100.0 if status == "completed" else job.progress
                job.error = error
                job.result = result if isinstance(result, (dict, list)) or result is None else {"value": str(result)}
            task = db.get(models.ScheduledTask, job.name)
            if task and job.trigger == "scheduled" and job.status in ("completed", "failed"):
                task.last_duration_ms = int(elapsed * 1000)
                task.last_status = job.status
            final_status, name = job.status, job.name
        bus.publish(EventType.JOB_UPDATED, id=job_id, name=name, status=final_status, error=error)
