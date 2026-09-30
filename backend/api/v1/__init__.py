from fastapi import APIRouter, Depends

from backend.api.deps import guard_maintenance
from backend.api.v1 import activity, api_keys, auth, library, media, settings, system

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(guard_maintenance)])
for module in (auth, api_keys, media, activity, library, settings, system):
    api_router.include_router(module.router)

