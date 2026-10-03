"""Third-party-compatible API surfaces (outside ``/api/v1``).

These routers speak another product's wire format so existing tools can talk to Maniac Media
unchanged. They translate only: every action goes through the same services as ``/api/v1``.
"""
from backend.api.compat.radarr import router as radarr_router
from backend.api.compat.sonarr import router as sonarr_router

__all__ = ["radarr_router", "sonarr_router"]
