from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import MetaData, Text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from backend.core.secrets import Cipher, cipher

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EncryptedString(TypeDecorator):
    """Column type that stores AES-256-GCM ciphertext and yields plaintext in Python.

    ``purpose`` binds the ciphertext to its column. Plaintext found in a legacy row is
    returned unchanged and re-encrypted on the next write.
    """

    impl = Text
    cache_ok = True

    def __init__(self, purpose: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.purpose = purpose

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        if Cipher.is_encrypted(value):
            return value
        return cipher.encrypt(str(value), purpose=self.purpose)

    def process_result_value(self, value, dialect):
        if not value:
            return value
        if Cipher.is_encrypted(value):
            if value.startswith("gAAAA"):
                return cipher.decrypt(value)
            return cipher.decrypt(value, purpose=self.purpose)
        return value
