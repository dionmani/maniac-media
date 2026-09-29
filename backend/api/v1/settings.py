"""Configuration: general settings, naming, quality profiles, indexers, download clients,
notifications, metadata providers, and the database wizard."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Requires
from backend.core.secrets import master_key, metadata_key_name, secret_store
from backend.core.security import Permission
from backend.core.settings import MetadataProviderSettings, get_settings, settings_manager
from backend.db import models
from backend.db.session import build_url, db_manager, mask_url, test_connection
from backend.domain import naming
from backend.domain.quality import QUALITIES
from backend.plugins import registry
from backend.plugins.base import PluginKind
from backend.services import database_service, download_service, indexer_service, notification_service, quality_service
from backend.services.audit import audit

router = APIRouter(tags=["settings"])

SECRET_FIELDS_HIDDEN = {"cors_origins"}


# ------------------------------------------------------------------ general
@router.get("/settings")
def get_general(_: Requires(Permission.SETTINGS_READ)):
    s = get_settings()
    data = s.model_dump(exclude={"metadata_providers"})
    data["db_connection"] = mask_url(db_manager.url)
    return data


@router.put("/settings")
def update_general(req: schemas.GeneralSettings, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    changes = req.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("movie_folder_format", "movie_format"):
        if key in changes and (errors := naming.validate(changes[key], naming.MOVIE_TOKENS)):
            raise HTTPException(422, f"{key}: {'; '.join(errors)}")
    for key in ("series_folder_format", "tv_format"):
        if key in changes and (errors := naming.validate(changes[key], naming.EPISODE_TOKENS)):
            raise HTTPException(422, f"{key}: {'; '.join(errors)}")
    before = get_settings().model_dump()
    new = settings_manager.update(**changes)
    diff = {k: {"from": before.get(k), "to": v} for k, v in changes.items() if before.get(k) != v}
    audit(db, "settings", "settings.update", actor=user.actor, details=diff)
    db.commit()
    if "download_folder" in diff or "watch_download_folder" in diff:
        from backend.workers.watcher import watcher

        watcher.restart()
    return new.model_dump(exclude={"metadata_providers"})


@router.post("/settings/naming/preview")
def naming_preview(req: schemas.NamingPreviewRequest, _: Requires(Permission.SETTINGS_READ)):
    p = naming.preview(req.template, req.kind)
    return {"example": p.example, "errors": p.errors, "tokens": naming.MOVIE_TOKENS if req.kind == "movie" else naming.EPISODE_TOKENS}


# ------------------------------------------------------------------ quality
@router.get("/qualities")
def list_qualities(_: Requires(Permission.SETTINGS_READ)):
    return [{"name": q.name, "resolution": q.resolution.value, "source": q.source.value, "weight": q.weight} for q in QUALITIES if q.weight]


@router.get("/quality-profiles", response_model=list[schemas.QualityProfileOut])
def list_profiles(db: DB, _: Requires(Permission.MEDIA_READ)):
    return list(db.scalars(select(models.QualityProfile).order_by(models.QualityProfile.id)))


def _apply_profile(profile: models.QualityProfile, req: schemas.QualityProfileIn) -> None:
    errors = quality_service.validate_profile(req.items, req.cutoff)
    if errors:
        raise HTTPException(422, "; ".join(errors))
    for key, value in req.model_dump().items():
        setattr(profile, key, value)


@router.post("/quality-profiles", response_model=schemas.QualityProfileOut, status_code=201)
def create_profile(req: schemas.QualityProfileIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    profile = models.QualityProfile()
    _apply_profile(profile, req)
    db.add(profile)
    db.flush()
    audit(db, "settings", "quality_profile.create", actor=user.actor, entity_type="quality_profile", entity_id=profile.id, details={"name": req.name})
    db.commit()
    return profile


@router.put("/quality-profiles/{profile_id}", response_model=schemas.QualityProfileOut)
def update_profile(profile_id: int, req: schemas.QualityProfileIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    profile = db.get(models.QualityProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    _apply_profile(profile, req)
    audit(db, "settings", "quality_profile.update", actor=user.actor, entity_type="quality_profile", entity_id=profile.id, details=req.model_dump())
    db.commit()
    return profile


@router.delete("/quality-profiles/{profile_id}", response_model=schemas.Message)
def delete_profile(profile_id: int, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    profile = db.get(models.QualityProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    in_use = db.scalar(select(models.Movie.id).where(models.Movie.quality_profile_id == profile_id).limit(1)) or db.scalar(
        select(models.Series.id).where(models.Series.quality_profile_id == profile_id).limit(1))
    if in_use:
        raise HTTPException(409, "Profile is in use by library items")
    audit(db, "settings", "quality_profile.delete", actor=user.actor, entity_type="quality_profile", entity_id=profile_id, severity="warning")
    db.delete(profile)
    db.commit()
    return schemas.Message(message="Profile deleted")


# ------------------------------------------------------------------ plugins catalogue
@router.get("/plugins")
def plugins(_: Requires(Permission.SETTINGS_READ)):
    return registry.catalogue()


# ------------------------------------------------------------------ indexers
def _indexer_out(row: models.Indexer) -> schemas.IndexerOut:
    out = schemas.IndexerOut.model_validate(row)
    out.has_api_key = bool(row.api_key)
    return out


@router.get("/indexers", response_model=list[schemas.IndexerOut])
def list_indexers(db: DB, _: Requires(Permission.SETTINGS_READ)):
    return [_indexer_out(r) for r in db.scalars(select(models.Indexer).order_by(models.Indexer.priority, models.Indexer.name))]


def _apply_indexer(row: models.Indexer, req: schemas.IndexerIn) -> None:
    try:
        cls = registry.get(PluginKind.INDEXER, req.implementation)
    except KeyError as exc:
        raise HTTPException(422, str(exc))
    if req.implementation not in ("demo",) and not req.url:
        raise HTTPException(422, "URL is required")
    data = req.model_dump(exclude={"api_key"})
    data["protocol"] = req.protocol or cls.protocol
    for key, value in data.items():
        setattr(row, key, value)
    if req.api_key is not None and req.api_key.get_secret_value():
        row.api_key = req.api_key.get_secret_value()
    row.failure_count, row.disabled_until = 0, None


@router.post("/indexers", response_model=schemas.IndexerOut, status_code=201)
def create_indexer(req: schemas.IndexerIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = models.Indexer(url="")
    _apply_indexer(row, req)
    db.add(row)
    db.flush()
    audit(db, "settings", "indexer.create", actor=user.actor, entity_type="indexer", entity_id=row.id, details={"name": row.name, "implementation": row.implementation})
    db.commit()
    return _indexer_out(row)


@router.put("/indexers/{indexer_id}", response_model=schemas.IndexerOut)
def update_indexer(indexer_id: int, req: schemas.IndexerIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.Indexer, indexer_id)
    if row is None:
        raise HTTPException(404, "Indexer not found")
    _apply_indexer(row, req)
    audit(db, "settings", "indexer.update", actor=user.actor, entity_type="indexer", entity_id=row.id, details={"name": row.name, "api_key_changed": req.api_key is not None})
    db.commit()
    return _indexer_out(row)


@router.delete("/indexers/{indexer_id}", response_model=schemas.Message)
def delete_indexer(indexer_id: int, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.Indexer, indexer_id)
    if row is None:
        raise HTTPException(404, "Indexer not found")
    audit(db, "settings", "indexer.delete", actor=user.actor, entity_type="indexer", entity_id=row.id, details={"name": row.name}, severity="warning")
    db.delete(row)
    db.commit()
    return schemas.Message(message="Indexer deleted")


@router.post("/indexers/test")
async def test_indexer(req: schemas.IndexerIn, db: DB, _: Requires(Permission.SETTINGS_WRITE), indexer_id: int | None = None):
    """Test unsaved settings; when editing, a blank API key falls back to the stored one."""
    row = models.Indexer(url="")
    _apply_indexer(row, req)
    if indexer_id and not (req.api_key and req.api_key.get_secret_value()):
        existing = db.get(models.Indexer, indexer_id)
        row.api_key = existing.api_key if existing else None
    db.expunge_all()
    ok, message = await indexer_service.build(row).test()
    return {"success": ok, "message": message}


# ------------------------------------------------------------------ download clients
def _client_out(row: models.DownloadClient) -> schemas.ClientOut:
    out = schemas.ClientOut.model_validate(row)
    out.has_password, out.has_api_key = bool(row.password), bool(row.api_key)
    try:
        out.protocol = registry.get(PluginKind.DOWNLOAD_CLIENT, row.client_type).protocol  # type: ignore[attr-defined]
    except KeyError:
        out.protocol = None
    return out


@router.get("/download-clients", response_model=list[schemas.ClientOut])
def list_clients(db: DB, _: Requires(Permission.SETTINGS_READ)):
    return [_client_out(r) for r in db.scalars(select(models.DownloadClient).order_by(models.DownloadClient.priority, models.DownloadClient.name))]


def _apply_client(row: models.DownloadClient, req: schemas.ClientIn) -> None:
    try:
        cls = registry.get(PluginKind.DOWNLOAD_CLIENT, req.client_type)
    except KeyError as exc:
        raise HTTPException(422, str(exc))
    for key, value in req.model_dump(exclude={"password", "api_key"}).items():
        setattr(row, key, value)
    if row.port is None:
        row.port = cls.default_port  # type: ignore[attr-defined]
    if req.password is not None and req.password.get_secret_value():
        row.password = req.password.get_secret_value()
    if req.api_key is not None and req.api_key.get_secret_value():
        row.api_key = req.api_key.get_secret_value()


@router.post("/download-clients", response_model=schemas.ClientOut, status_code=201)
def create_client(req: schemas.ClientIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = models.DownloadClient()
    _apply_client(row, req)
    db.add(row)
    db.flush()
    audit(db, "settings", "download_client.create", actor=user.actor, entity_type="download_client", entity_id=row.id, details={"name": row.name, "type": row.client_type})
    db.commit()
    return _client_out(row)


@router.put("/download-clients/{client_id}", response_model=schemas.ClientOut)
def update_client(client_id: int, req: schemas.ClientIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.DownloadClient, client_id)
    if row is None:
        raise HTTPException(404, "Download client not found")
    _apply_client(row, req)
    audit(db, "settings", "download_client.update", actor=user.actor, entity_type="download_client", entity_id=row.id, details={"name": row.name})
    db.commit()
    return _client_out(row)


@router.delete("/download-clients/{client_id}", response_model=schemas.Message)
def delete_client(client_id: int, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.DownloadClient, client_id)
    if row is None:
        raise HTTPException(404, "Download client not found")
    audit(db, "settings", "download_client.delete", actor=user.actor, entity_type="download_client", entity_id=row.id, details={"name": row.name}, severity="warning")
    db.delete(row)
    db.commit()
    return schemas.Message(message="Download client deleted")


@router.post("/download-clients/test")
async def test_client(req: schemas.ClientIn, db: DB, _: Requires(Permission.SETTINGS_WRITE), client_id: int | None = None):
    row = models.DownloadClient()
    _apply_client(row, req)
    if client_id:
        existing = db.get(models.DownloadClient, client_id)
        if existing:
            row.password = row.password or existing.password
            row.api_key = row.api_key or existing.api_key
    db.expunge_all()
    ok, message = await download_service.build(row).test()
    return {"success": ok, "message": message}


# ------------------------------------------------------------------ notifications
def _notification_out(row: models.NotificationChannel) -> schemas.NotificationOut:
    secrets = json.loads(row.secret_settings) if row.secret_settings else {}
    return schemas.NotificationOut(id=row.id, name=row.name, implementation=row.implementation, settings=row.settings or {},
                                   configured_secrets=sorted(secrets), events=row.events or [], enabled=row.enabled)


@router.get("/notifications")
def list_notifications(db: DB, _: Requires(Permission.SETTINGS_READ)):
    return {"channels": [_notification_out(r) for r in db.scalars(select(models.NotificationChannel))], "events": notification_service.NOTIFY_EVENTS}


def _apply_notification(row: models.NotificationChannel, req: schemas.NotificationIn) -> None:
    try:
        plain, secret = notification_service.split_settings(req.implementation, req.settings)
    except KeyError as exc:
        raise HTTPException(422, str(exc))
    unknown = set(req.events) - set(notification_service.NOTIFY_EVENTS)
    if unknown:
        raise HTTPException(422, f"Unknown events: {', '.join(sorted(unknown))}")
    existing = json.loads(row.secret_settings) if row.secret_settings else {}
    row.name, row.implementation, row.events, row.enabled, row.settings = req.name, req.implementation, req.events, req.enabled, plain
    row.secret_settings = json.dumps({**existing, **secret}) if (existing or secret) else None


@router.post("/notifications", status_code=201)
def create_notification(req: schemas.NotificationIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = models.NotificationChannel()
    _apply_notification(row, req)
    db.add(row)
    db.flush()
    audit(db, "settings", "notification.create", actor=user.actor, entity_type="notification", entity_id=row.id, details={"name": row.name})
    db.commit()
    return _notification_out(row)


@router.put("/notifications/{channel_id}")
def update_notification(channel_id: int, req: schemas.NotificationIn, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.NotificationChannel, channel_id)
    if row is None:
        raise HTTPException(404, "Channel not found")
    _apply_notification(row, req)
    audit(db, "settings", "notification.update", actor=user.actor, entity_type="notification", entity_id=row.id)
    db.commit()
    return _notification_out(row)


@router.delete("/notifications/{channel_id}", response_model=schemas.Message)
def delete_notification(channel_id: int, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.NotificationChannel, channel_id)
    if row is None:
        raise HTTPException(404, "Channel not found")
    audit(db, "settings", "notification.delete", actor=user.actor, entity_type="notification", entity_id=row.id, severity="warning")
    db.delete(row)
    db.commit()
    return schemas.Message(message="Channel deleted")


@router.post("/notifications/{channel_id}/test")
async def test_notification(channel_id: int, db: DB, _: Requires(Permission.SETTINGS_WRITE)):
    row = db.get(models.NotificationChannel, channel_id)
    if row is None:
        raise HTTPException(404, "Channel not found")
    plugin = notification_service.plugin_for(row)
    ok, message = await plugin.test()
    return {"success": ok, "message": message}


# ------------------------------------------------------------------ metadata providers
@router.get("/metadata-providers")
def metadata_providers(_: Requires(Permission.SETTINGS_READ)):
    s = get_settings()
    out = []
    for key, cls in registry.available(PluginKind.METADATA).items():
        conf = s.metadata_providers.get(key)
        out.append({**cls.describe(), "enabled": conf.enabled if conf else True, "priority": conf.priority if conf else 100,
                    "has_api_key": secret_store.has(metadata_key_name(key))})
    return sorted(out, key=lambda p: p["priority"])


@router.put("/metadata-providers/{key}")
async def update_metadata_provider(key: str, req: schemas.MetadataProviderUpdate, db: DB, user: Requires(Permission.SETTINGS_WRITE)):
    try:
        cls = registry.get(PluginKind.METADATA, key)
    except KeyError:
        raise HTTPException(404, "Unknown provider")
    s = get_settings()
    conf = s.metadata_providers.get(key) or MetadataProviderSettings()
    if req.enabled is not None:
        conf.enabled = req.enabled
    if req.priority is not None:
        conf.priority = req.priority
    s.metadata_providers[key] = conf
    settings_manager.save(s)
    if req.clear_api_key:
        secret_store.delete(metadata_key_name(key))
    result = {"success": True, "message": "Saved"}
    if req.api_key and req.api_key.get_secret_value():
        ok, message = await cls({"api_key": req.api_key.get_secret_value(), "pin": req.pin.get_secret_value() if req.pin else None}).test()
        if not ok:
            raise HTTPException(400, f"Key rejected: {message}")
        secret_store.set(metadata_key_name(key), req.api_key.get_secret_value())
        if req.pin and req.pin.get_secret_value():
            secret_store.set(f"metadata.{key}.pin", req.pin.get_secret_value())
        result["message"] = message
    audit(db, "settings", "metadata_provider.update", actor=user.actor, entity_id=key,
          details={"enabled": conf.enabled, "priority": conf.priority, "api_key_changed": bool(req.api_key), "api_key_cleared": req.clear_api_key})
    db.commit()
    return result


# ------------------------------------------------------------------ database wizard
def _db_url(req: schemas.DatabaseRequest) -> str:
    try:
        return build_url(req.db_type, host=req.host, port=req.port, username=req.username,
                         password=req.password.get_secret_value() if req.password else None, database=req.database,
                         sqlite_path=req.sqlite_path, options=req.options)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/database")
def database_info(_: Requires(Permission.DATABASE_MANAGE)):
    from backend.db import migrator

    key = master_key.info
    return {
        "engine": db_manager.dialect,
        "connection": mask_url(db_manager.url),
        "revision": migrator.current_revision(db_manager.engine),
        "head": migrator.head_revision(),
        "secret_store": {"source": key.source, "secure": key.secure, "detail": key.detail, "file": "encrypted_config.db"},
        "migration": database_service.state.__dict__,
        "drivers": _driver_status(),
    }


def _driver_status() -> dict[str, bool]:
    import importlib.util

    return {name: importlib.util.find_spec(mod) is not None for name, mod in
            {"postgresql": "psycopg", "mysql": "pymysql", "mariadb": "pymysql", "mssql": "pyodbc", "sqlite": "sqlite3"}.items()}


@router.post("/database/test")
async def database_test(req: schemas.DatabaseRequest, _: Requires(Permission.DATABASE_MANAGE)):
    ok, message = await asyncio.to_thread(test_connection, _db_url(req))
    return {"success": ok, "message": message}


@router.post("/database/migrate", status_code=202)
async def database_migrate(req: schemas.DatabaseRequest, db: DB, user: Requires(Permission.DATABASE_MANAGE)):
    if database_service.state.running:
        raise HTTPException(409, "A migration is already running")
    url = _db_url(req)
    audit(db, "system", "database.migrate.start", actor=user.actor, details={"target": mask_url(url)}, severity="warning")
    db.commit()

    async def run():
        from backend.db.session import session_scope

        try:
            await asyncio.to_thread(database_service.migrate, url, req.db_type, overwrite=req.overwrite)
            outcome, severity = "database.migrate.success", "warning"
        except Exception as exc:  # already recorded in state
            outcome, severity = "database.migrate.failed", "error"
            _ = exc
        with session_scope() as s:
            audit(s, "system", outcome, actor=user.actor, details={"target": mask_url(url), "error": database_service.state.error}, severity=severity,
                  success=outcome.endswith("success"))

    asyncio.create_task(run())
    return {"message": "Migration started", "state": database_service.state.__dict__}


@router.get("/database/migration")
def migration_state(_: Requires(Permission.DATABASE_MANAGE)):
    return database_service.state.__dict__


@router.post("/database/backup")
async def database_backup(db: DB, user: Requires(Permission.DATABASE_MANAGE)):
    path = await asyncio.to_thread(database_service.backup_current)
    audit(db, "system", "database.backup", actor=user.actor, details={"path": path})
    db.commit()
    return {"path": path}
