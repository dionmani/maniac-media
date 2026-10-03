"""Application composition root."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import __build__, __version__
from backend.api import compat
from backend.api.v1 import api_router
from backend.core import paths
from backend.core.events import bus
from backend.core.logging import configure_logging
from backend.core.observability import RequestContextMiddleware, metrics_response, setup_opentelemetry
from backend.core.ratelimit import RateLimitMiddleware
from backend.core.settings import get_settings

log = logging.getLogger("maniac")


def bootstrap_database() -> dict:
    """Configure the engine, migrate the schema, seed defaults. Safe to call repeatedly."""
    from backend.db import migrator
    from backend.db.session import db_manager, session_scope
    from backend.services.quality_service import ensure_default_profiles
    from backend.workers.tasks import ensure_scheduled_tasks

    db_manager.configure()
    summary = migrator.upgrade(db_manager.engine)
    with session_scope() as db:
        ensure_default_profiles(db)
    ensure_scheduled_tasks()
    return summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    paths.ensure_dirs()
    from backend.plugins import registry
    from backend.services import notification_service
    from backend.workers import tasks  # noqa: F401  (registers job handlers)
    from backend.workers.queue import WorkerPool, job_queue
    from backend.workers.scheduler import scheduler
    from backend.workers.watcher import watcher

    summary = await asyncio.to_thread(bootstrap_database)
    if summary.get("changed"):
        log.info("Schema migrated %s -> %s (backup: %s)", summary["from"], summary["to"], summary["backup"])
    registry.discover()
    bus.bind_loop(asyncio.get_running_loop())
    notification_service.install()

    pool = WorkerPool(job_queue, size=4)
    await pool.start()
    scheduler.start()
    watcher.start()
    app.state.pool = pool
    log.info("Maniac Media %s ready", __version__)
    try:
        yield
    finally:
        watcher.stop()
        scheduler.stop()
        await pool.stop()
        from backend.db.session import db_manager

        db_manager.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Maniac Media API",
        version=__version__,
        description="Unified movie & TV automation. All endpoints live under `/api/v1` and require a Bearer token "
                    "obtained from `/api/v1/auth/login` (or `/auth/setup` on first run).",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(RateLimitMiddleware, settings_provider=get_settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Api-Key"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/docs"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(api_router)
    app.include_router(compat.radarr_router)
    app.include_router(compat.sonarr_router)

    @app.get("/api/health", tags=["system"])
    def liveness():
        """Unauthenticated liveness probe for Docker/Kubernetes."""
        return {"status": "ok", "version": __version__, "build": __build__}

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        if get_settings().metrics_require_auth:
            from backend.api.deps import resolve_token
            from backend.db.session import session_scope

            auth = request.headers.get("Authorization", "")
            try:
                with session_scope() as db:
                    resolve_token(auth.removeprefix("Bearer ").strip(), db, None)
            except Exception:
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return metrics_response()

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    setup_opentelemetry(app)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    dist = paths.FRONTEND_DIST
    if not dist.exists():
        return
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


app = create_app()
