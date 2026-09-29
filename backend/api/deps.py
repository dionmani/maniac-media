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

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    @property
    def actor(self) -> Actor:
        return Actor(self.id, self.username, self.ip)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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
    return CurrentUser(user.id, user.username, user.role, ip)


def current_user(request: Request, db: DB, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    user = resolve_token(creds.credentials, db, client_ip(request))
    request.state.user = user
    return user


User = Annotated[CurrentUser, Depends(current_user)]


def require(permission: Permission):
    def checker(user: User, request: Request) -> CurrentUser:
        if permission not in user.permissions:
            from backend.db.session import session_scope
            from backend.services.audit import audit

            with session_scope() as db:
                audit(db, "security", "permission.denied", actor=user.actor, severity="warning", success=False,
                      details={"permission": permission.value, "path": request.url.path, "method": request.method})
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {permission.value}")
        return user

    return checker


def Requires(permission: Permission):  # noqa: N802 - reads like a type in signatures
    return Annotated[CurrentUser, Depends(require(permission))]


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
