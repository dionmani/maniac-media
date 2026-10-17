"""System: status, health, dashboard, tasks & jobs, audit log, logs, WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import sys
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from backend import __version__
from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, page_response, resolve_token
from backend.core import paths
from backend.core.events import Event, bus
from backend.core.observability import WS_CLIENTS
from backend.core.security import Permission
from backend.core.settings import get_settings
from backend.db import models
from backend.db.repositories import AuditRepository, JobRepository
from backend.db.session import db_manager, session_scope
from backend.services import dashboard_service, health_service
from backend.workers.queue import job_queue
from backend.workers.scheduler import scheduler
from backend.workers.watcher import watcher

router = APIRouter(tags=["system"])
log = logging.getLogger("maniac.ws")
STARTED_AT = datetime.now()


@router.get("/system/status")
def status(_: Requires(Permission.SYSTEM_READ)):
    settings = get_settings()
    ffprobe_info = {"found": False, "path": None, "version": None}
    if settings.inspection_enabled:
        from backend.services.inspection_service import ffprobe_path, ffprobe_version
        path = ffprobe_path()
        if path:
            ffprobe_info["found"] = True
            ffprobe_info["path"] = path
            ffprobe_info["version"] = ffprobe_version()

    return {
        "app_name": "Maniac Media",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
        "database": db_manager.dialect,
        "data_dir": str(paths.DATA_DIR),
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((datetime.now() - STARTED_AT).total_seconds()),
        "download_watcher": {"active": watcher.active, "path": watcher.path},
        "ffprobe": ffprobe_info,
    }


@router.get("/system/health")
async def health(_: Requires(Permission.SYSTEM_READ)):
    return health_service.as_dicts(await asyncio.to_thread(health_service.run_checks))


@router.get("/dashboard")
def dashboard(db: DB, _: Requires(Permission.MEDIA_READ)):
    return dashboard_service.stats(db)


# ------------------------------------------------------------------ tasks & jobs
@router.get("/system/tasks")
def tasks(db: DB, _: Requires(Permission.SYSTEM_READ)):
    next_runs = scheduler.next_runs()
    return [
        {"name": t.name, "title": job_queue.definitions[t.name].title if t.name in job_queue.definitions else t.name,
         "interval_minutes": t.interval_minutes, "enabled": t.enabled, "last_run": t.last_run, "last_duration_ms": t.last_duration_ms,
         "last_status": t.last_status, "next_run": next_runs.get(t.name)}
        for t in db.scalars(select(models.ScheduledTask).order_by(models.ScheduledTask.name))
    ]


@router.put("/system/tasks/{name}")
def update_task(name: str, req: schemas.TaskUpdate, db: DB, user: Requires(Permission.SYSTEM_ADMIN)):
    task = db.get(models.ScheduledTask, name)
    if task is None:
        raise HTTPException(404, "Task not found")
    if req.interval_minutes is not None:
        task.interval_minutes = req.interval_minutes
    if req.enabled is not None:
        task.enabled = req.enabled
    from backend.services.audit import audit

    audit(db, "system", "task.update", actor=user.actor, entity_id=name, details=req.model_dump(exclude_none=True))
    db.commit()
    scheduler.reload()
    return {"status": "success"}


@router.post("/system/tasks/{name}/run")
def run_task(name: str, user: Requires(Permission.LIBRARY_SCAN)):
    if name not in job_queue.definitions:
        raise HTTPException(404, "Unknown task")
    return {"job_id": scheduler.run_now(name)}


@router.get("/system/jobs")
def jobs(db: DB, _: Requires(Permission.SYSTEM_READ), paging: Paging, status: str | None = None, name: str | None = None):
    stmt = select(models.Job)
    if status:
        stmt = stmt.where(models.Job.status.in_(status.split(",")))
    if name:
        stmt = stmt.where(models.Job.name == name)
    page = JobRepository(db).paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort or "created",
                                      order=paging.order if paging.sort else "desc")
    return page_response(page, [
        {"id": j.id, "name": j.name, "title": job_queue.definitions.get(j.name).title if j.name in job_queue.definitions else j.name,
         "status": j.status, "trigger": j.trigger, "attempts": j.attempts, "progress": j.progress, "message": j.message, "error": j.error,
         "result": j.result, "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at}
        for j in page.items
    ])


@router.post("/system/jobs/{job_id}/cancel", response_model=schemas.Message)
def cancel_job(job_id: int, db: DB, _: Requires(Permission.SYSTEM_ADMIN)):
    job = db.get(models.Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status != "queued":
        raise HTTPException(409, "Only queued jobs can be cancelled")
    job.status = "cancelled"
    db.commit()
    return schemas.Message(message="Job cancelled")


# ------------------------------------------------------------------ audit & logs
@router.get("/system/audit")
def audit_log(db: DB, _: Requires(Permission.AUDIT_READ), paging: Paging, category: str | None = None,
              username: str | None = None, success: bool | None = None, search: str | None = Query(None, max_length=100)):
    stmt = select(models.AuditLog)
    if category:
        stmt = stmt.where(models.AuditLog.category.in_(category.split(",")))
    if username:
        stmt = stmt.where(models.AuditLog.username == username)
    if success is not None:
        stmt = stmt.where(models.AuditLog.success.is_(success))
    if search:
        stmt = stmt.where(models.AuditLog.action.ilike(f"%{search}%"))
    page = AuditRepository(db).paginate(stmt, page=paging.page, page_size=paging.page_size, sort=paging.sort or "timestamp",
                                        order=paging.order if paging.sort else "desc")
    return page_response(page, [
        {"id": a.id, "timestamp": a.timestamp, "username": a.username, "category": a.category, "action": a.action,
         "entity_type": a.entity_type, "entity_id": a.entity_id, "severity": a.severity, "success": a.success,
         "ip_address": a.ip_address, "details": a.details}
        for a in page.items
    ])


@router.get("/system/logs")
def logs(_: Requires(Permission.SYSTEM_ADMIN), lines: int = Query(200, ge=1, le=2000), level: str | None = None):
    path = paths.LOG_DIR / "maniac.log"
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - lines * 600))
        raw = fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    out = []
    for line in raw:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level and rec.get("level") != level.upper():
            continue
        out.append(rec)
    return list(reversed(out))


# ------------------------------------------------------------------ websocket
class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, event: Event) -> None:
        if not self.clients:
            return
        message = json.dumps(event.to_dict(), default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
        WS_CLIENTS.set(len(self.clients))


hub = Hub()
bus.subscribe("*", hub.broadcast)


@router.websocket("/ws")
async def websocket(ws: WebSocket, token: str = Query(...)):
    """Real-time event stream. Auth via ``?token=`` (browsers cannot set WS headers)."""
    try:
        def auth():
            with session_scope() as db:
                return resolve_token(token, db, ws.client.host if ws.client else None)

        await asyncio.to_thread(auth)
    except Exception:
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.clients.add(ws)
    WS_CLIENTS.set(len(hub.clients))
    try:
        await ws.send_text(json.dumps({"type": "hello", "payload": {"version": __version__}}))
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
        WS_CLIENTS.set(len(hub.clients))
