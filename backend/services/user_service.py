"""Local users: first-run setup, authentication with lockout, management."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import security
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.services.audit import Actor, audit


class AuthError(Exception):
    pass


def user_count(db: Session) -> int:
    return db.scalar(select(func.count(models.User.id))) or 0


def create_user(db: Session, username: str, password: str, role: str, actor: Actor | None = None) -> models.User:
    username = username.strip()
    if not username or len(username) > 64:
        raise ValueError("Username must be 1-64 characters")
    security.validate_password_strength(password)
    security.Role(role)  # validates
    if db.scalar(select(models.User).where(func.lower(models.User.username) == username.lower())):
        raise ValueError("Username already taken")
    user = models.User(username=username, password_hash=security.hash_password(password), role=role, is_active=True, token_version=1, failed_logins=0)
    db.add(user)
    db.flush()
    audit(db, "security", "user.create", actor=actor, entity_type="user", entity_id=user.id, details={"username": username, "role": role})
    return user


def authenticate(db: Session, username: str, password: str, ip: str | None) -> models.User:
    settings = get_settings()
    user = db.scalar(select(models.User).where(func.lower(models.User.username) == username.strip().lower()))
    actor = Actor(user.id if user else None, username[:64], ip)
    if user is None:
        security.burn_verification_time(password)
        audit(db, "security", "login.failed", actor=actor, details={"reason": "unknown user"}, severity="warning", success=False)
        raise AuthError("Invalid username or password")
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        audit(db, "security", "login.locked", actor=actor, entity_type="user", entity_id=user.id, severity="warning", success=False)
        raise AuthError("Account temporarily locked after repeated failures")
    if not user.is_active or not security.verify_password(user.password_hash, password):
        user.failed_logins = (user.failed_logins or 0) + 1
        details = {"attempts": user.failed_logins}
        if user.failed_logins >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            user.failed_logins = 0
            details["locked_minutes"] = settings.lockout_minutes
        audit(db, "security", "login.failed", actor=actor, entity_type="user", entity_id=user.id, details=details, severity="warning", success=False)
        raise AuthError("Invalid username or password")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login = now
    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)
    audit(db, "security", "login.success", actor=actor, entity_type="user", entity_id=user.id)
    return user


def issue_token(user: models.User) -> tuple[str, str]:
    token, expires = security.create_access_token(user.id, user.username, user.role, user.token_version, get_settings().access_token_ttl_minutes)
    return token, expires.isoformat()


def change_password(db: Session, user: models.User, current: str | None, new: str, actor: Actor, *, admin_reset: bool = False) -> None:
    if not admin_reset and not security.verify_password(user.password_hash, current or ""):
        audit(db, "security", "password.change_failed", actor=actor, entity_type="user", entity_id=user.id, severity="warning", success=False)
        raise AuthError("Current password is incorrect")
    security.validate_password_strength(new)
    user.password_hash = security.hash_password(new)
    user.token_version += 1  # revoke every outstanding token
    audit(db, "security", "password.reset" if admin_reset else "password.change", actor=actor, entity_type="user", entity_id=user.id)


def admins_remaining(db: Session, excluding: int) -> int:
    return db.scalar(select(func.count(models.User.id)).where(models.User.role == "admin", models.User.is_active.is_(True), models.User.id != excluding)) or 0
