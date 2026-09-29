from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from backend.api import schemas
from backend.api.deps import DB, Paging, Requires, User, client_ip, page_response
from backend.core.security import Permission, permissions_for
from backend.db import models
from backend.db.repositories import UserRepository
from backend.services import user_service
from backend.services.audit import Actor, audit

router = APIRouter(tags=["auth"])


def _user_out(u: models.User) -> schemas.UserOut:
    out = schemas.UserOut.model_validate(u)
    out.permissions = sorted(p.value for p in permissions_for(u.role))
    return out


@router.get("/auth/status")
def auth_status(db: DB):
    """Public: tells the UI whether first-run setup is required."""
    return {"setup_required": user_service.user_count(db) == 0}


@router.post("/auth/setup", response_model=schemas.TokenResponse)
def setup(req: schemas.SetupRequest, request: Request, db: DB):
    """Create the first administrator. Only possible while no users exist."""
    if user_service.user_count(db) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup already completed")
    try:
        user = user_service.create_user(db, req.username, req.password.get_secret_value(), "admin",
                                        Actor(None, req.username, client_ip(request)))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    db.commit()
    token, expires = user_service.issue_token(user)
    return schemas.TokenResponse(access_token=token, expires_at=expires, user=_user_out(user))


@router.post("/auth/login", response_model=schemas.TokenResponse)
def login(req: schemas.LoginRequest, request: Request, db: DB):
    try:
        user = user_service.authenticate(db, req.username, req.password.get_secret_value(), client_ip(request))
    except user_service.AuthError as exc:
        db.commit()  # persist the failed-attempt audit + counters
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    db.commit()
    token, expires = user_service.issue_token(user)
    return schemas.TokenResponse(access_token=token, expires_at=expires, user=_user_out(user))


@router.get("/auth/me", response_model=schemas.UserOut)
def me(user: User, db: DB):
    return _user_out(db.get(models.User, user.id))


@router.post("/auth/logout", response_model=schemas.Message)
def logout(user: User, db: DB):
    """Revokes every token for this user (token_version bump)."""
    row = db.get(models.User, user.id)
    row.token_version += 1
    audit(db, "security", "logout", actor=user.actor, entity_type="user", entity_id=user.id)
    db.commit()
    return schemas.Message(message="Signed out on all devices")


@router.post("/auth/password", response_model=schemas.Message)
def change_password(req: schemas.PasswordChange, user: User, db: DB):
    row = db.get(models.User, user.id)
    try:
        user_service.change_password(db, row, req.current_password.get_secret_value(), req.new_password.get_secret_value(), user.actor)
    except (user_service.AuthError, ValueError) as exc:
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    db.commit()
    return schemas.Message(message="Password changed. Please sign in again.")


# ------------------------------------------------------------------ user management
@router.get("/users")
def list_users(db: DB, _: Requires(Permission.USERS_MANAGE), paging: Paging):
    page = UserRepository(db).paginate(select(models.User), page=paging.page, page_size=paging.page_size, sort=paging.sort, order=paging.order)
    return page_response(page, [_user_out(u) for u in page.items])


@router.post("/users", response_model=schemas.UserOut, status_code=201)
def create_user(req: schemas.UserCreate, db: DB, admin: Requires(Permission.USERS_MANAGE)):
    try:
        user = user_service.create_user(db, req.username, req.password.get_secret_value(), req.role, admin.actor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    db.commit()
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=schemas.UserOut)
def update_user(user_id: int, req: schemas.UserUpdate, db: DB, admin: Requires(Permission.USERS_MANAGE)):
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    demoting = (req.role and req.role != "admin") or req.is_active is False
    if user.role == "admin" and demoting and user_service.admins_remaining(db, user.id) == 0:
        raise HTTPException(400, "At least one active administrator is required")
    changes = {}
    if req.role and req.role != user.role:
        user.role, changes["role"] = req.role, req.role
        user.token_version += 1
    if req.is_active is not None and req.is_active != user.is_active:
        user.is_active, changes["is_active"] = req.is_active, req.is_active
        user.token_version += 1
    if req.password:
        try:
            user_service.change_password(db, user, None, req.password.get_secret_value(), admin.actor, admin_reset=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        changes["password"] = "reset"
    audit(db, "security", "user.update", actor=admin.actor, entity_type="user", entity_id=user.id, details=changes)
    db.commit()
    return _user_out(user)


@router.delete("/users/{user_id}", response_model=schemas.Message)
def delete_user(user_id: int, db: DB, admin: Requires(Permission.USERS_MANAGE)):
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "You cannot delete your own account")
    if user.role == "admin" and user_service.admins_remaining(db, user.id) == 0:
        raise HTTPException(400, "At least one active administrator is required")
    audit(db, "security", "user.delete", actor=admin.actor, entity_type="user", entity_id=user.id, details={"username": user.username}, severity="warning")
    db.delete(user)
    db.commit()
    return schemas.Message(message=f"User '{user.username}' deleted")
