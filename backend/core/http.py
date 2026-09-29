"""Outbound HTTP with a retry policy (exponential backoff + jitter, honours Retry-After)."""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

log = logging.getLogger("maniac.http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
USER_AGENT = "ManiacMedia/2.1 (+https://github.com/maniac-media)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def client(timeout: float = 20.0, **kwargs) -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.AsyncClient(timeout=timeout, headers=headers, **kwargs)


async def request(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.0,
    raise_for_status: bool = True,
    **kwargs,
) -> httpx.Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await http.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS and attempt <= retries:
                delay = _retry_after(resp) or backoff * (2 ** (attempt - 1))
                await asyncio.sleep(delay + random.uniform(0, backoff / 2))
                continue
            if raise_for_status and resp.status_code >= 400:
                raise HttpError(f"HTTP {resp.status_code} from {httpx.URL(url).host}", resp.status_code)
            return resp
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt > retries:
                raise HttpError(f"Network error contacting {httpx.URL(url).host}: {type(exc).__name__}") from exc
            await asyncio.sleep(backoff * (2 ** (attempt - 1)) + random.uniform(0, backoff / 2))


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return min(float(value), 60.0)
    return None
