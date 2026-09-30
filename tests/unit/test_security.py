import base64

import pytest
from cryptography.fernet import Fernet

from backend.core import security
from backend.core.logging import redact
from backend.core.secrets import Cipher, SecretError, SecretStore, cipher, master_key


def test_cipher_roundtrip_and_format():
    token = cipher.encrypt("postgresql://u:p@h/db", purpose="database.url")
    assert token.startswith("v2:") and "p@h" not in token
    assert cipher.decrypt(token, purpose="database.url") == "postgresql://u:p@h/db"


def test_ciphertext_is_bound_to_purpose():
    token = cipher.encrypt("secret", purpose="indexers.api_key")
    with pytest.raises(SecretError):
        cipher.decrypt(token, purpose="download_clients.password")


def test_tampering_is_detected():
    token = cipher.encrypt("secret", purpose="x")
    raw = bytearray(base64.urlsafe_b64decode(token[3:]))
    raw[-1] ^= 0x01
    with pytest.raises(SecretError):
        cipher.decrypt("v2:" + base64.urlsafe_b64encode(bytes(raw)).decode(), purpose="x")


def test_legacy_fernet_tokens_are_readable():
    legacy = Fernet(base64.urlsafe_b64encode(master_key.key())).encrypt(b"sqlite:///old.db").decode()
    assert Cipher.is_encrypted(legacy)
    assert cipher.decrypt(legacy) == "sqlite:///old.db"


def test_secret_store_holds_only_ciphertext(tmp_path):
    store = SecretStore(tmp_path / "encrypted_config.db")
    store.set("database.url", "mysql://root:hunter2@db/app")
    assert store.get("database.url") == "mysql://root:hunter2@db/app"
    assert b"hunter2" not in (tmp_path / "encrypted_config.db").read_bytes()


def test_master_key_source_is_reported():
    assert master_key.info.source == "env"


def test_password_hashing_is_argon2():
    h = security.hash_password("Correct-Horse-9")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "Correct-Horse-9")
    assert not security.verify_password(h, "wrong")


@pytest.mark.parametrize("pw", ["short1A!", "alllowercaseletters", "NoDigitsOrSymbols"])
def test_weak_passwords_rejected(pw):
    with pytest.raises(ValueError):
        security.validate_password_strength(pw)


def test_jwt_roundtrip():
    token, _ = security.create_access_token(7, "neo", "power_user", 3, 5)
    claims = security.decode_access_token(token)
    assert claims["sub"] == "7" and claims["role"] == "power_user" and claims["ver"] == 3


def test_role_permissions_are_hierarchical():
    ro, pu, admin = (security.permissions_for(r) for r in ("read_only", "power_user", "admin"))
    assert ro < pu < admin
    assert security.Permission.USERS_MANAGE not in pu
    assert security.Permission.MEDIA_DELETE not in ro


@pytest.mark.parametrize(
    "text,leak",
    [
        ("connect postgresql://admin:S3cret@db:5432/x", "S3cret"),
        ("GET /api?t=search&apikey=abcdef123456", "abcdef123456"),
        ('{"password": "topsecret"}', "topsecret"),
        ("Authorization: Bearer eyJhbGciOi.xyz.abc", "eyJhbGciOi"),
        ("https://api.telegram.org/bot123456:ABC-DEF/sendMessage", "ABC-DEF"),
        ("X-Api-Key: mm_1234567890abcdefghijklmnopqrstuvwxyz", "mm_1234567890abcdefghijklmnopqrstuvwxyz"),
        ("key is mm_1234567890abcdefghijklmnopqrstuvwxyz in line", "mm_1234567890abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_logs_never_contain_credentials(text, leak):
    assert leak not in redact(text)


def test_api_key_generation_and_hashing():
    plaintext, key_hash, prefix = security.generate_api_key()
    assert plaintext.startswith("mm_")
    assert prefix == plaintext[:9]
    assert len(key_hash) == 64
    assert security.hash_api_key(plaintext) == key_hash
    assert security.hash_api_key(plaintext) == security.hash_api_key(plaintext)

