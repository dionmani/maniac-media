"""Integration tests for API keys functionality (F3)."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.api.deps import User, allow_api_key_query
from backend.db import models
from backend.db.session import session_scope


def test_api_key_crud_lifecycle(client, admin_headers):
    # 1. Fetch presets
    r = client.get("/api/v1/api-keys/presets", headers=admin_headers)
    assert r.status_code == 200
    presets = r.json()
    assert len(presets) >= 4
    preset_names = [p["name"] for p in presets]
    assert "Read-only dashboard" in preset_names
    assert "Webhook receiver" in preset_names

    # 2. Create API key
    payload = {
        "name": "Overseerr Integration",
        "permissions": ["media:read", "media:write", "search:execute"],
        "expires_in_days": 30,
        "allow_query_param": False,
    }
    r = client.post("/api/v1/api-keys", json=payload, headers=admin_headers)
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Overseerr Integration"
    assert created["prefix"].startswith("mm_")
    assert "api_key" in created
    plaintext_key = created["api_key"]
    assert plaintext_key.startswith("mm_")
    key_id = created["id"]

    # 3. List API keys - plaintext must NOT be returned
    r = client.get("/api/v1/api-keys", headers=admin_headers)
    assert r.status_code == 200
    keys = r.json()
    assert any(k["id"] == key_id for k in keys)
    target = next(k for k in keys if k["id"] == key_id)
    assert target.get("api_key") != plaintext_key
    assert target["prefix"] == created["prefix"]

    # 4. Use X-Api-Key to make authenticated API requests
    api_key_headers = {"X-Api-Key": plaintext_key}
    r = client.get("/api/v1/movies", headers=api_key_headers)
    assert r.status_code == 200

    # 5. Revoke API key
    r = client.post(f"/api/v1/api-keys/{key_id}/revoke", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["revoked_at"] is not None

    # 6. Revoked key gets 401
    r = client.get("/api/v1/movies", headers=api_key_headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "API key revoked"

    # 7. Delete API key (hard delete after revoke)
    r = client.delete(f"/api/v1/api-keys/{key_id}", headers=admin_headers)
    assert r.status_code == 200

    # Key is gone
    r = client.get("/api/v1/api-keys", headers=admin_headers)
    assert not any(k["id"] == key_id for k in r.json())


def test_api_key_permissions_and_scoping(client, admin_headers):
    # Create key with only media:read permission
    payload = {
        "name": "Read Only Key",
        "permissions": ["media:read"],
        "allow_query_param": False,
    }
    r = client.post("/api/v1/api-keys", json=payload, headers=admin_headers)
    assert r.status_code == 201
    key_data = r.json()
    read_only_headers = {"X-Api-Key": key_data["api_key"]}

    # GET /movies (requires media:read) succeeds
    r = client.get("/api/v1/movies", headers=read_only_headers)
    assert r.status_code == 200

    # GET /users (requires users:manage) fails with 403
    r = client.get("/api/v1/users", headers=read_only_headers)
    assert r.status_code == 403

    # API key cannot mint API keys (interactive=True guard)
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "SubKey", "permissions": ["media:read"]},
        headers=read_only_headers,
    )
    assert r.status_code == 403
    assert "Interactive (JWT) session required" in r.json()["detail"]


def test_api_key_effective_permissions_shrink_with_owner_demotion(client, admin_headers):
    # Create a power user
    r = client.post(
        "/api/v1/users",
        json={"username": "keyowner", "password": "Password123!", "role": "power_user"},
        headers=admin_headers,
    )
    assert r.status_code == 201
    owner_id = r.json()["id"]

    # Login as owner to issue key
    r = client.post("/api/v1/auth/login", json={"username": "keyowner", "password": "Password123!"})
    owner_jwt = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Issue API key with media:read and media:write
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Owner Key", "permissions": ["media:read", "media:write"]},
        headers=owner_jwt,
    )
    assert r.status_code == 201
    key_headers = {"X-Api-Key": r.json()["api_key"]}

    # Demote owner from power_user to read_only
    r = client.patch(f"/api/v1/users/{owner_id}", json={"role": "read_only"}, headers=admin_headers)
    assert r.status_code == 200

    # Effective permissions immediately shrink: media:read works
    r = client.get("/api/v1/movies", headers=key_headers)
    assert r.status_code == 200


def test_api_key_cannot_exceed_creator_role_permissions(client, admin_headers):
    # Create a read_only user
    client.post(
        "/api/v1/users",
        json={"username": "readonlyuser", "password": "Password123!", "role": "read_only"},
        headers=admin_headers,
    )
    r = client.post("/api/v1/auth/login", json={"username": "readonlyuser", "password": "Password123!"})
    ro_jwt = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Attempt to create API key with media:write (exceeds read_only role)
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Escalation Key", "permissions": ["media:read", "media:write"]},
        headers=ro_jwt,
    )
    assert r.status_code == 422
    assert "exceeds owner permissions" in r.json()["detail"]


def test_api_key_expired(client, admin_headers):
    # Create key and backdate expires_at in DB
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Expiring Key", "permissions": ["media:read"], "expires_in_days": 1},
        headers=admin_headers,
    )
    assert r.status_code == 201
    data = r.json()
    key_id = data["id"]
    plaintext = data["api_key"]

    with session_scope() as db:
        key_row = db.get(models.ApiKey, key_id)
        key_row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)

    r = client.get("/api/v1/movies", headers={"X-Api-Key": plaintext})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_query_param_apikey_opt_in(client, admin_headers):
    # 1. Create key WITHOUT allow_query_param
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Header Only Key", "permissions": ["media:read"], "allow_query_param": False},
        headers=admin_headers,
    )
    header_only_key = r.json()["api_key"]

    # 2. Create key WITH allow_query_param
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Query Param Key", "permissions": ["media:read"], "allow_query_param": True},
        headers=admin_headers,
    )
    query_key = r.json()["api_key"]

    # Query param on normal route (NOT opted-in) fails
    r = client.get(f"/api/v1/movies?apikey={query_key}")
    assert r.status_code == 401

    # Header-only key on opted-in route via query param fails
    r = client.get(f"/api/v1/api-keys/test-query-optin?apikey={header_only_key}")
    assert r.status_code == 401

    # Query key on opted-in route via query param succeeds
    r = client.get(f"/api/v1/api-keys/test-query-optin?apikey={query_key}")
    assert r.status_code == 200
    assert r.json()["ok"] is True



def test_delete_unrevoked_api_key_fails(client, admin_headers):
    r = client.post(
        "/api/v1/api-keys",
        json={"name": "Active Key", "permissions": ["media:read"]},
        headers=admin_headers,
    )
    key_id = r.json()["id"]

    # Delete without revoking first
    r = client.delete(f"/api/v1/api-keys/{key_id}", headers=admin_headers)
    assert r.status_code == 400
    assert "must be revoked" in r.json()["detail"].lower()

