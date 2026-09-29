"""Authentication primitives: argon2id password hashing, JWT tokens, role based permissions."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.core.secrets import SECRET_JWT, secret_store

_hasher = PasswordHasher()  # argon2id, library defaults follow RFC 9106 recommendations
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "maniac-media"


class Role(StrEnum):
    ADMIN = "admin"
    POWER_USER = "power_user"
    READ_ONLY = "read_only"


class Permission(StrEnum):
    MEDIA_READ = "media:read"
    MEDIA_WRITE = "media:write"
    MEDIA_DELETE = "media:delete"
    SEARCH = "search:execute"
    QUEUE_READ = "queue:read"
    QUEUE_WRITE = "queue:write"
    LIBRARY_SCAN = "library:scan"
    RENAME = "files:rename"
    IMPORT = "files:import"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    DATABASE_MANAGE = "database:manage"
    USERS_MANAGE = "users:manage"
    SYSTEM_READ = "system:read"
    SYSTEM_ADMIN = "system:admin"
    AUDIT_READ = "audit:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.READ_ONLY: frozenset(
        {Permission.MEDIA_READ, Permission.QUEUE_READ, Permission.SYSTEM_READ, Permission.SETTINGS_READ}
    ),
    Role.POWER_USER: frozenset(
        {
            Permission.MEDIA_READ,
            Permission.MEDIA_WRITE,
            Permission.MEDIA_DELETE,
            Permission.SEARCH,
            Permission.QUEUE_READ,
            Permission.QUEUE_WRITE,
            Permission.LIBRARY_SCAN,
            Permission.RENAME,
            Permission.IMPORT,
            Permission.SETTINGS_READ,
            Permission.SYSTEM_READ,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}


def permissions_for(role: str) -> frozenset[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()


# -- passwords ---------------------------------------------------------------
MIN_PASSWORD_LENGTH = 10


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise ValueError("Password must mix at least three of: lowercase, uppercase, digits, symbols")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# Constant-time dummy verification so unknown usernames take as long as wrong passwords.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def burn_verification_time(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


# -- tokens ------------------------------------------------------------------
def _signing_key() -> str:
    return secret_store.get_or_create(SECRET_JWT, lambda: secrets.token_urlsafe(64))


def create_access_token(user_id: int, username: str, role: str, token_version: int, ttl_minutes: int) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": str(user_id),
        "name": username,
        "role": role,
        "ver": token_version,
        "iat": now,
        "nbf": now,
        "exp": expires,
        "iss": JWT_ISSUER,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _signing_key(), algorithm=JWT_ALGORITHM), expires


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        _signing_key(),
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "sub", "iat", "iss"]},
    )
