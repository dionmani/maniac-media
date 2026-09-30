"""API keys endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from backend.api import schemas
from backend.api.deps import DB, User, allow_api_key_query
from backend.services import api_key_service

router = APIRouter(tags=["api-keys"])


@router.get("/api-keys/presets", response_model=list[schemas.ApiKeyPreset])
def get_presets(user: User):
    return api_key_service.get_api_key_presets()


@router.get("/api-keys", response_model=list[schemas.ApiKeyOut])
def list_api_keys(user: User, db: DB):
    return api_key_service.list_api_keys(db, user)


@router.post("/api-keys", response_model=schemas.ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(req: schemas.ApiKeyCreate, user: User, db: DB):
    if user.auth_method != "jwt":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Interactive (JWT) session required")
    try:
        key, plaintext = api_key_service.create_api_key(
            db,
            user,
            req.name,
            req.permissions,
            expires_in_days=req.expires_in_days,
            allow_query_param=req.allow_query_param,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    db.commit()
    out = schemas.ApiKeyCreatedResponse.model_validate(key)
    out.api_key = plaintext
    return out


@router.post("/api-keys/{key_id}/revoke", response_model=schemas.ApiKeyOut)
def revoke_api_key(key_id: int, user: User, db: DB):
    if user.auth_method != "jwt":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Interactive (JWT) session required")
    try:
        key = api_key_service.revoke_api_key(db, key_id, user)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    db.commit()
    return key


@router.get("/api-keys/test-query-optin")
@allow_api_key_query
def test_query_optin(user: User):
    return {"ok": True, "user": user.username}


@router.delete("/api-keys/{key_id}", response_model=schemas.Message)
def delete_api_key(key_id: int, user: User, db: DB):

    if user.auth_method != "jwt":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Interactive (JWT) session required")
    try:
        api_key_service.delete_api_key(db, key_id, user)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    db.commit()
    return schemas.Message(message="API key deleted")
