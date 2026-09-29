"""System health checks surfaced on the System page and pushed as notifications when they change."""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass

from sqlalchemy import select, text

from backend.core.events import EventType, bus
from backend.core.secrets import master_key
from backend.core.settings import get_settings
from backend.db import models
from backend.db.session import db_manager, session_scope
from backend.plugins import registry

LOW_DISK_BYTES = 10 * 1024**3


@dataclass
class HealthCheck:
    source: str
    status: str  # ok | warning | error
    message: str
    wiki: str | None = None


_last_problems: set[str] = set()


def run_checks() -> list[HealthCheck]:
    settings = get_settings()
    checks: list[HealthCheck] = []

    try:
        with db_manager.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(HealthCheck("database", "ok", f"{db_manager.dialect} reachable"))
    except Exception as exc:
        checks.append(HealthCheck("database", "error", f"Database unreachable: {type(exc).__name__}"))

    info = master_key.info
    checks.append(HealthCheck("secret_store", "ok" if info.secure else "warning",
                              f"Master key source: {info.detail}" if info.secure else f"Insecure key storage – {info.detail}"))

    for label, path in (("movies_root", settings.media_root_movies), ("tv_root", settings.media_root_tv), ("download_folder", settings.download_folder)):
        if not path or not os.path.isdir(path):
            checks.append(HealthCheck(label, "error", f"Folder not found or not accessible: {path}"))
            continue
        if not os.access(path, os.W_OK):
            checks.append(HealthCheck(label, "warning", f"Folder is not writable: {path}"))
            continue
        try:
            free = shutil.disk_usage(path).free
            status = "warning" if free < LOW_DISK_BYTES else "ok"
            checks.append(HealthCheck(label, status, f"{path} – {free / 1024**3:.1f} GB free"))
        except OSError as exc:
            checks.append(HealthCheck(label, "warning", f"Could not read disk usage for {path}: {exc}"))

    with session_scope() as db:
        indexers = list(db.scalars(select(models.Indexer).where(models.Indexer.enabled.is_(True))))
        clients = list(db.scalars(select(models.DownloadClient).where(models.DownloadClient.enabled.is_(True))))
        if not indexers:
            checks.append(HealthCheck("indexers", "warning", "No indexers enabled – automatic search and RSS sync are inactive"))
        for i in indexers:
            if i.health_status in ("error", "warning"):
                checks.append(HealthCheck(f"indexer:{i.id}", i.health_status, f"Indexer '{i.name}': {i.health_message}"))
        if not clients:
            checks.append(HealthCheck("download_clients", "warning", "No download clients enabled – releases cannot be grabbed"))
        for c in clients:
            if c.health_status == "error":
                checks.append(HealthCheck(f"download_client:{c.id}", "error", f"Download client '{c.name}': {c.health_message}"))
        failed = db.scalar(select(models.DownloadQueueItem.id).where(models.DownloadQueueItem.status == "import_failed").limit(1))
        if failed:
            checks.append(HealthCheck("imports", "warning", "One or more downloads failed to import – see Activity"))

    import importlib.util

    if not any(importlib.util.find_spec(m) for m in ("websockets", "wsproto")):
        checks.append(HealthCheck("websockets", "warning", "No WebSocket library installed – live updates disabled. pip install 'uvicorn[standard]'"))

    errors = registry.catalogue()["errors"]
    if errors:
        checks.append(HealthCheck("plugins", "warning", f"{len(errors)} plugin(s) failed to load"))

    problems = {f"{c.source}:{c.message}" for c in checks if c.status != "ok"}
    global _last_problems
    for p in problems - _last_problems:
        source, message = p.split(":", 1)
        bus.publish(EventType.HEALTH_ISSUE, source=source, message=message)
    _last_problems = problems
    return checks


def as_dicts(checks: list[HealthCheck]) -> list[dict]:
    return [asdict(c) for c in checks]
