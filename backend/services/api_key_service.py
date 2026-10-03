"""API key management service."""
from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api import schemas
from backend.api.deps import CurrentUser
from backend.core.security import Permission, generate_api_key
from backend.db import models
from backend.db.base import utcnow
from backend.services.audit import audit

PRESETS: list[schemas.ApiKeyPreset] = [
    schemas.ApiKeyPreset(
        name="Read-only dashboard",
        description="Read-only access to media, queue, settings and system info",
        permissions=["media:read", "queue:read", "settings:read", "system:read"],
        allow_query_param=False,
    ),
    schemas.ApiKeyPreset(
        name="Request manager (Seerr/Overseerr)",
        description="Lets Seerr use Maniac Media as its Radarr and Sonarr server (URL bases /api/compat/radarr and /api/compat/sonarr)",
        permissions=["media:read", "media:write", "media:delete", "search:execute", "queue:read"],
        allow_query_param=True,  # Seerr only ever sends ?apikey=
    ),
    schemas.ApiKeyPreset(
        name="Webhook receiver",
        description="Inbound webhook events from media servers",
        permissions=["media:read", "media:write", "library:scan"],
        allow_query_param=True,
    ),
    schemas.ApiKeyPreset(
        name="Calendar feed",
        description="iCal calendar subscription feed",
        permissions=["media:read"],
        allow_query_param=True,
    ),
]


def get_api_key_presets() -> list[schemas.ApiKeyPreset]:
    return PRESETS


def list_api_keys(db: Session, requesting_user: CurrentUser) -> Sequence[models.ApiKey]:
    if Permission.USERS_MANAGE in requesting_user.permissions:
        stmt = select(models.ApiKey).order_by(models.ApiKey.created_at.desc())
    else:
        stmt = select(models.ApiKey).where(models.ApiKey.user_id == requesting_user.id).order_by(models.ApiKey.created_at.desc())
    return db.scalars(stmt).all()


def create_api_key(
    db: Session,
    user: CurrentUser,
    name: str,
    requested_permissions: list[str],
    expires_in_days: int | None = None,
    allow_query_param: bool = False,
) -> tuple[models.ApiKey, str]:
    user_perms = {p.value for p in user.permissions}
    for p in requested_permissions:
        if p not in Permission._value2member_map_:
            raise ValueError(f"Invalid permission: '{p}'")
        if p not in user_perms:
            raise ValueError(f"Permission '{p}' exceeds owner permissions")

    plaintext, key_hash, prefix = generate_api_key()
    expires_at = utcnow() + timedelta(days=expires_in_days) if expires_in_days is not None else None

    api_key = models.ApiKey(
        name=name,
        key_hash=key_hash,
        prefix=prefix,
        user_id=user.id,
        permissions=list(requested_permissions),
        allow_query_param=allow_query_param,
        created_at=utcnow(),
        expires_at=expires_at,
    )
    db.add(api_key)
    db.flush()

    audit(
        db,
        "security",
        "api_key.create",
        actor=user.actor,
        entity_type="api_key",
        entity_id=api_key.id,
        details={"name": name, "prefix": prefix, "permissions": requested_permissions, "allow_query_param": allow_query_param},
    )
    return api_key, plaintext


def revoke_api_key(db: Session, key_id: int, user: CurrentUser) -> models.ApiKey:
    key = db.get(models.ApiKey, key_id)
    if key is None:
        raise KeyError("API key not found")
    if key.user_id != user.id and Permission.USERS_MANAGE not in user.permissions:
        raise PermissionError("Not authorized to revoke this key")

    if key.revoked_at is None:
        key.revoked_at = utcnow()
        audit(
            db,
            "security",
            "api_key.revoke",
            actor=user.actor,
            entity_type="api_key",
            entity_id=key.id,
            details={"name": key.name, "prefix": key.prefix},
        )
    return key


def delete_api_key(db: Session, key_id: int, user: CurrentUser) -> None:
    key = db.get(models.ApiKey, key_id)
    if key is None:
        raise KeyError("API key not found")
    if key.user_id != user.id and Permission.USERS_MANAGE not in user.permissions:
        raise PermissionError("Not authorized to delete this key")
    if key.revoked_at is None:
        raise ValueError("API key must be revoked before deletion")

    audit(
        db,
        "security",
        "api_key.delete",
        actor=user.actor,
        entity_type="api_key",
        entity_id=key_id,
        details={"name": key.name, "prefix": key.prefix},
    )
    db.delete(key)
