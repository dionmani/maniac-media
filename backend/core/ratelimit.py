"""Token-bucket rate limiting, used both for inbound API traffic and outbound indexer calls."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class TokenBucket:
    __slots__ = ("capacity", "refill_per_sec", "tokens", "updated")

    def __init__(self, per_minute: int) -> None:
        self.capacity = max(1, per_minute)
        self.refill_per_sec = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now

    def try_take(self) -> tuple[bool, float]:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True, 0.0
        return False, (1 - self.tokens) / self.refill_per_sec


class KeyedLimiter:
    """Bounded LRU of buckets keyed by client identity."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_keys

    def hit(self, key: str, per_minute: int) -> tuple[bool, float]:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.capacity != max(1, per_minute):
                bucket = TokenBucket(per_minute)
                self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
            return bucket.try_take()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


api_limiter = KeyedLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client limits. Authentication endpoints get a much stricter budget."""

    def __init__(self, app, settings_provider) -> None:
        super().__init__(app)
        self._settings = settings_provider

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path.startswith("/api/v1/ws"):
            return await call_next(request)
        settings = self._settings()
        client = request.client.host if request.client else "unknown"
        if path.startswith("/api/v1/auth/login") or path.startswith("/api/v1/auth/setup"):
            ok, retry = api_limiter.hit(f"auth:{client}", settings.login_rate_limit_per_minute)
        else:
            ok, retry = api_limiter.hit(f"api:{client}", settings.rate_limit_per_minute)
        if not ok:
            return JSONResponse(
                {"detail": "Rate limit exceeded. Slow down."},
                status_code=429,
                headers={"Retry-After": str(max(1, int(retry + 0.999)))},
            )
        return await call_next(request)


class AsyncRateLimiter:
    """Awaitable limiter for outbound calls (e.g. one per indexer)."""

    def __init__(self, per_minute: int) -> None:
        self._bucket = TokenBucket(per_minute)
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                ok, wait = self._bucket.try_take()
                if ok:
                    return
                await asyncio.sleep(min(wait, 5.0))
