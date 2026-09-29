"""Secret management.

Design
------
* **Master key** – a 256-bit key that never lives next to the data it protects.
  Resolution order (first hit wins):

  1. ``MANIAC_MASTER_KEY`` environment variable (Docker / CI / headless servers).
  2. The operating-system secret store via ``keyring``
     (Windows Credential Manager, macOS Keychain, Linux Secret Service).
  3. A legacy ``data/master.key`` file from v1 – migrated into the OS store and
     removed once the migration has been verified.
  4. Freshly generated – stored in the OS store, or, when none exists, in a key file
     *outside* the data directory (``MANIAC_KEY_FILE`` or ``~/.maniac-media/master.key``).
     That last case is reported as a health warning.

* **Cipher** – AES-256-GCM (authenticated encryption). Ciphertexts are versioned
  (``v2:<base64>``) and bound to their purpose via associated data, so a ciphertext
  copied from one field cannot be replayed into another. Legacy Fernet tokens written
  by v1 are still readable.

* **Secret store** – ``encrypted_config.db``: a tiny SQLite key/value database that only
  ever holds ciphertext. Values are decrypted on demand at runtime and never cached in
  plaintext on disk, never logged and never returned by the API.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets as _pysecrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.core import paths

log = logging.getLogger("maniac.secrets")

KEYRING_SERVICE = "maniac-media"
KEYRING_USERNAME = "master-key"
ENV_MASTER_KEY = "MANIAC_MASTER_KEY"
ENV_KEY_FILE = "MANIAC_KEY_FILE"
CIPHER_PREFIX = "v2:"
_KDF_SALT = b"maniac-media/master-key/v2"


class SecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class MasterKeyInfo:
    source: str  # env | os-keyring | key-file | legacy-file
    secure: bool
    detail: str


def _keyring_backend():
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 0) <= 0:
            return None
        return keyring
    except Exception:  # pragma: no cover - platform dependent
        return None


def _normalise_key(raw: str | bytes) -> bytes:
    """Accept a Fernet/urlsafe-base64 32 byte key or derive one from a passphrase."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    raw = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    # Passphrase: stretch it. 600k iterations follows current OWASP guidance for PBKDF2-SHA256.
    return hashlib.pbkdf2_hmac("sha256", raw.encode(), _KDF_SALT, 600_000, dklen=32)


def _fallback_key_file() -> Path:
    return Path(os.environ.get(ENV_KEY_FILE) or Path.home() / ".maniac-media" / "master.key")


class MasterKeyProvider:
    def __init__(self) -> None:
        self._key: bytes | None = None
        self._info: MasterKeyInfo | None = None
        self._lock = threading.Lock()

    @property
    def info(self) -> MasterKeyInfo:
        self.key()
        assert self._info is not None
        return self._info

    def key(self) -> bytes:
        with self._lock:
            if self._key is None:
                self._key, self._info = self._resolve()
            return self._key

    def _resolve(self) -> tuple[bytes, MasterKeyInfo]:
        env = os.environ.get(ENV_MASTER_KEY)
        if env:
            return _normalise_key(env), MasterKeyInfo("env", True, f"{ENV_MASTER_KEY} environment variable")

        kr = _keyring_backend()
        if kr is not None:
            try:
                stored = kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                if stored:
                    legacy = paths.LEGACY_MASTER_KEY_FILE
                    if legacy.exists() and _normalise_key(legacy.read_bytes()) == _normalise_key(stored):
                        legacy.unlink()  # already migrated; don't leave a copy beside the data
                    return _normalise_key(stored), MasterKeyInfo("os-keyring", True, _backend_name(kr))
            except Exception as exc:  # pragma: no cover
                log.warning("OS secret store unavailable: %s", exc)
                kr = None

        # v1 kept the key inside the data directory. Migrate it out.
        legacy = paths.LEGACY_MASTER_KEY_FILE
        if legacy.exists():
            raw = legacy.read_bytes().strip()
            if kr is not None and self._store_in_keyring(kr, raw.decode()):
                try:
                    legacy.unlink()
                except OSError:
                    pass
                log.info("Migrated legacy master key into the OS secret store")
                return _normalise_key(raw), MasterKeyInfo("os-keyring", True, _backend_name(kr) + " (migrated from master.key)")
            return _normalise_key(raw), MasterKeyInfo(
                "legacy-file", False, "Key stored in data/master.key next to the encrypted data. Set MANIAC_MASTER_KEY."
            )

        fallback = _fallback_key_file()
        if fallback.exists():
            return _normalise_key(fallback.read_text()), MasterKeyInfo(
                "key-file", False, f"Key file {fallback} (no OS secret store available)"
            )

        new_key = Fernet.generate_key().decode()
        if kr is not None and self._store_in_keyring(kr, new_key):
            return _normalise_key(new_key), MasterKeyInfo("os-keyring", True, _backend_name(kr))

        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(new_key)
        try:
            os.chmod(fallback, 0o600)
        except OSError:  # pragma: no cover - windows
            pass
        log.warning("No OS secret store and no %s; generated key file at %s", ENV_MASTER_KEY, fallback)
        return _normalise_key(new_key), MasterKeyInfo("key-file", False, f"Key file {fallback}")

    @staticmethod
    def _store_in_keyring(kr, value: str) -> bool:
        try:
            kr.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
            return kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME) == value
        except Exception as exc:  # pragma: no cover
            log.warning("Could not write to OS secret store: %s", exc)
            return False


def _backend_name(kr) -> str:
    try:
        return type(kr.get_keyring()).__name__
    except Exception:  # pragma: no cover
        return "keyring"


master_key = MasterKeyProvider()


class Cipher:
    """AES-256-GCM with purpose binding; transparently reads v1 Fernet tokens."""

    def __init__(self, provider: MasterKeyProvider) -> None:
        self._provider = provider

    def encrypt(self, plaintext: str, purpose: str = "") -> str:
        nonce = _pysecrets.token_bytes(12)
        ct = AESGCM(self._provider.key()).encrypt(nonce, plaintext.encode("utf-8"), purpose.encode())
        return CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()

    def decrypt(self, token: str, purpose: str = "") -> str:
        if token.startswith(CIPHER_PREFIX):
            blob = base64.urlsafe_b64decode(token[len(CIPHER_PREFIX):])
            try:
                return AESGCM(self._provider.key()).decrypt(blob[:12], blob[12:], purpose.encode()).decode("utf-8")
            except Exception as exc:
                raise SecretError("Unable to decrypt secret (wrong master key or tampered data)") from exc
        if token.startswith("gAAAA"):  # v1 Fernet token
            try:
                return Fernet(base64.urlsafe_b64encode(self._provider.key())).decrypt(token.encode()).decode()
            except InvalidToken as exc:
                raise SecretError("Unable to decrypt legacy secret") from exc
        raise SecretError("Value is not an encrypted token")

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        return bool(value) and (value.startswith(CIPHER_PREFIX) or value.startswith("gAAAA"))


cipher = Cipher(master_key)


class SecretStore:
    """Ciphertext-only key/value store backed by ``encrypted_config.db``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        return conn

    def get(self, name: str) -> str | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
        return cipher.decrypt(row[0], purpose=f"secret:{name}") if row else None

    def set(self, name: str, value: str) -> None:
        token = cipher.encrypt(value, purpose=f"secret:{name}")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO secrets(name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (name, token, now),
            )

    def delete(self, name: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def has(self, name: str) -> bool:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT 1 FROM secrets WHERE name = ?", (name,)).fetchone() is not None

    def names(self) -> list[str]:
        with self._lock, self._conn() as conn:
            return [r[0] for r in conn.execute("SELECT name FROM secrets ORDER BY name")]

    def get_or_create(self, name: str, factory) -> str:
        value = self.get(name)
        if value is None:
            value = factory()
            self.set(name, value)
        return value


secret_store = SecretStore(paths.SECRETS_DB_FILE)

# Well-known secret names
SECRET_DB_URL = "database.url"
SECRET_JWT = "auth.jwt_signing_key"


def metadata_key_name(provider: str) -> str:
    return f"metadata.{provider}.api_key"
