"""Shared API dependencies: authentication, authorisation, pagination."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from backend.core.security import Permission, permissions_for
from backend.db import models
from backend.db.repositories import Page
from backend.db.session import get_db
from backend.services.audit import Actor

bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_db)]


@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    ip: str | None
    auth_method: str = "jwt"  # "jwt" | "api_key"
    api_key_id: int | None = None
    key_permissions: frozenset[Permission] | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        role_perms = permissions_for(self.role)
        if self.auth_method == "api_key" and self.key_permissions is not None:
            return self.key_permissions & role_perms
        return role_perms

    @property
    def actor(self) -> Actor:
        return Actor(self.id, self.username, self.ip)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def allow_api_key_query(func):
    """Decorator to allow ?apikey= query param auth on a route."""
    func.api_key_query = True
    return func


def resolve_token(token: str, db: Session, ip: str | None) -> CurrentUser:
    from backend.core.security import decode_access_token

    try:
        claims = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired", headers={"WWW-Authenticate": "Bearer"})
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(models.User, int(claims["sub"]))
    # token_version lets password changes / role changes revoke every outstanding token
    if user is None or not user.is_active or user.token_version != claims.get("ver"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token revoked", headers={"WWW-Authenticate": "Bearer"})
    return CurrentUser(user.id, user.username, user.role, ip, auth_method="jwt")


def resolve_api_key(
    raw_key: str,
    db: Session,
    ip: str | None,
    *,
    allow_query_param_required: bool = False,
) -> CurrentUser:
    from sqlalchemy import select

    from backend.core.ratelimit import api_limiter
    from backend.core.security import Permission, hash_api_key
    from backend.db.base import utcnow

    key_hash = hash_api_key(raw_key)
    key = db.scalars(select(models.ApiKey).where(models.ApiKey.key_hash == key_hash)).first()
    if key is None:
        if ip:
            api_limiter.hit(f"auth:{ip}", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})

    if allow_query_param_required and not key.allow_query_param:
        if ip:
            api_limiter.hit(f"auth:{ip}", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key not allowed in query parameter", headers={"WWW-Authenticate": "Bearer"})

    if key.revoked_at is not None:
        if ip:
            api_limiter.hit(f"auth:{ip}", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key revoked", headers={"WWW-Authenticate": "Bearer"})

    now = utcnow()
    if key.expires_at is not None and key.expires_at < now:
        if ip:
            api_limiter.hit(f"auth:{ip}", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key expired", headers={"WWW-Authenticate": "Bearer"})

    owner = db.get(models.User, key.user_id)
    if owner is None or not owner.is_active or (owner.locked_until and owner.locked_until > now):
        if ip:
            api_limiter.hit(f"auth:{ip}", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User account inactive or locked", headers={"WWW-Authenticate": "Bearer"})

    if key.last_used_at is None or (now - key.last_used_at).total_seconds() >= 60:
        key.last_used_at = now
        key.last_used_ip = ip
        db.commit()

    raw_perms = key.permissions or []
    key_perms = frozenset({Permission(p) for p in raw_perms if p in Permission._value2member_map_})

    return CurrentUser(
        id=owner.id,
        username=owner.username,
        role=owner.role,
        ip=ip,
        auth_method="api_key",
        api_key_id=key.id,
        key_permissions=key_perms,
    )


def current_user(request: Request, db: DB, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    ip = client_ip(request)
    # 1. Authorization: Bearer <jwt>
    if creds is not None and creds.scheme.lower() == "bearer":
        user = resolve_token(creds.credentials, db, ip)
        request.state.user = user
        return user

    # 2. X-Api-Key: mm_...
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        user = resolve_api_key(x_api_key, db, ip)
        request.state.user = user
        return user

    # 3. ?apikey=...
    apikey_param = request.query_params.get("apikey")
    if apikey_param:
        endpoint = request.scope.get("endpoint")
        route_allows_query = getattr(endpoint, "api_key_query", False) if endpoint else False
        if route_allows_query:
            user = resolve_api_key(apikey_param, db, ip, allow_query_param_required=True)
            request.state.user = user
            return user

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})


User = Annotated[CurrentUser, Depends(current_user)]


def require(permission: Permission, *, interactive: bool = False):
    def checker(user: User, request: Request) -> CurrentUser:
        if interactive and user.auth_method != "jwt":
            from backend.db.session import session_scope
            from backend.services.audit import audit

            with session_scope() as db:
                audit(
                    db,
                    "security",
                    "permission.denied",
                    actor=user.actor,
                    severity="warning",
                    success=False,
                    details={
                        "permission": permission.value,
                        "path": request.url.path,
                        "method": request.method,
                        "reason": "interactive_required",
                    },
                )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Interactive (JWT) session required")

        if permission not in user.permissions:
            from backend.db.session import session_scope
            from backend.services.audit import audit

            with session_scope() as db:
                audit(
                    db,
                    "security",
                    "permission.denied",
                    actor=user.actor,
                    severity="warning",
                    success=False,
                    details={"permission": permission.value, "path": request.url.path, "method": request.method},
                )
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {permission.value}")
        return user

    return checker


def Requires(permission: Permission, *, interactive: bool = False):  # noqa: N802 - reads like a type in signatures
    return Annotated[CurrentUser, Depends(require(permission, interactive=interactive))]



@dataclass
class PageParams:
    page: int
    page_size: int
    sort: str | None
    order: str


def page_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort: str | None = Query(None, max_length=32),
    order: str = Query("asc", pattern="^(asc|desc)$"),
) -> PageParams:
    return PageParams(page, page_size, sort, order)


Paging = Annotated[PageParams, Depends(page_params)]


def page_response(page: Page, items: list) -> dict:
    return {"items": items, "total": page.total, "page": page.page, "page_size": page.page_size, "pages": page.pages}


def guard_maintenance(conn: HTTPConnection) -> None:
    """Applies to HTTP and WebSocket routes alike; only blocks mutating HTTP requests."""
    from backend.services.database_service import maintenance

    method = conn.scope.get("method")
    if maintenance.is_set() and method and method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database migration in progress – changes are temporarily disabled")
