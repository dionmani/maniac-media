"""Indexer engine: fan-out search, rate limiting, circuit breaking and health tracking."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from backend.core.observability import INDEXER_REQUESTS
from backend.db import models
from backend.db.base import utcnow
from backend.db.session import session_scope
from backend.core.ratelimit import AsyncRateLimiter
from backend.domain.types import ReleaseInfo, SearchCriteria
from backend.plugins import registry
from backend.plugins.base import IndexerPlugin, PluginKind

log = logging.getLogger("maniac.indexers")

_limiters: dict[int, tuple[int, AsyncRateLimiter]] = {}
MAX_BACKOFF = timedelta(hours=6)


@dataclass
class IndexerRef:
    id: int
    name: str
    implementation: str
    priority: int
    plugin: IndexerPlugin
    rate_limit: int


def plugin_settings(row: models.Indexer) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "api_path": row.api_path or "/api",
        "api_key": row.api_key,
        "movie_categories": row.movie_categories,
        "tv_categories": row.tv_categories,
        "minimum_seeders": row.minimum_seeders,
        "feed_protocol": row.protocol,
    }


def build(row: models.Indexer) -> IndexerPlugin:
    cls = registry.get(PluginKind.INDEXER, row.implementation or "torznab")
    return cls(plugin_settings(row))  # type: ignore[return-value]


def load(purpose: str) -> list[IndexerRef]:
    """Snapshot of usable indexers; the DB session is released before any network I/O."""
    now = utcnow()
    refs = []
    with session_scope() as db:
        for row in db.scalars(select(models.Indexer).where(models.Indexer.enabled.is_(True)).order_by(models.Indexer.priority)):
            if purpose == "search" and row.enable_search is False:
                continue
            if purpose == "rss" and row.enable_rss is False:
                continue
            if row.disabled_until and row.disabled_until > now:
                continue
            try:
                plugin = build(row)
            except KeyError as exc:
                log.error("%s", exc)
                continue
            if purpose == "search" and not plugin.supports_search:
                continue
            if purpose == "rss" and not plugin.supports_rss:
                continue
            refs.append(IndexerRef(row.id, row.name, row.implementation or "", row.priority or 25, plugin, row.rate_limit_per_minute or 30))
    return refs


def _limiter(ref: IndexerRef) -> AsyncRateLimiter:
    current = _limiters.get(ref.id)
    if current is None or current[0] != ref.rate_limit:
        current = (ref.rate_limit, AsyncRateLimiter(ref.rate_limit))
        _limiters[ref.id] = current
    return current[1]


def record_result(indexer_id: int, ok: bool, message: str | None = None) -> None:
    with session_scope() as db:
        row = db.get(models.Indexer, indexer_id)
        if row is None:
            return
        row.last_health_check = utcnow()
        if ok:
            row.failure_count = 0
            row.disabled_until = None
            row.health_status = "ok"
            row.health_message = message
        else:
            row.failure_count = (row.failure_count or 0) + 1
            backoff = min(timedelta(minutes=5) * (2 ** (row.failure_count - 1)), MAX_BACKOFF)
            # circuit breaker: only trip after repeated failures so a single blip doesn't disable search
            if row.failure_count >= 3:
                row.disabled_until = utcnow() + backoff
            row.health_status = "error" if row.failure_count >= 3 else "warning"
            row.health_message = message


async def _run(ref: IndexerRef, coro_factory) -> tuple[IndexerRef, list[ReleaseInfo], str | None]:
    await _limiter(ref).acquire()
    try:
        releases = await asyncio.wait_for(coro_factory(ref.plugin), timeout=60)
        INDEXER_REQUESTS.labels(ref.name, "ok").inc()
        await asyncio.to_thread(record_result, ref.id, True, f"{len(releases)} results")
        return ref, releases, None
    except Exception as exc:
        INDEXER_REQUESTS.labels(ref.name, "error").inc()
        message = str(exc) or type(exc).__name__
        log.warning("Indexer %s failed: %s", ref.name, message)
        await asyncio.to_thread(record_result, ref.id, False, message)
        return ref, [], message


async def search(criteria: SearchCriteria) -> tuple[list[ReleaseInfo], list[dict]]:
    refs = await asyncio.to_thread(load, "search")
    results = await asyncio.gather(*(_run(r, lambda p: p.search(criteria)) for r in refs))
    releases, report = [], []
    for ref, items, error in results:
        for item in items:
            item.indexer_id = ref.id
            item.indexer = ref.name
        releases.extend(items)
        report.append({"indexer": ref.name, "results": len(items), "error": error})
    return _dedupe(releases), report


async def fetch_rss() -> list[ReleaseInfo]:
    refs = await asyncio.to_thread(load, "rss")
    results = await asyncio.gather(*(_run(r, lambda p: p.rss()) for r in refs))
    releases = []
    for ref, items, _ in results:
        for item in items:
            item.indexer_id, item.indexer = ref.id, ref.name
        releases.extend(items)

    def mark():
        with session_scope() as db:
            for ref in refs:
                row = db.get(models.Indexer, ref.id)
                if row:
                    row.last_rss_sync = utcnow()

    await asyncio.to_thread(mark)
    return _dedupe(releases)


def _dedupe(releases: list[ReleaseInfo]) -> list[ReleaseInfo]:
    seen, out = set(), []
    for r in releases:
        key = (r.info_hash or r.guid or r.title).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


async def health_check_all() -> list[dict]:
    with session_scope() as db:
        rows = [(r.id, r.name, build(r)) for r in db.scalars(select(models.Indexer).where(models.Indexer.enabled.is_(True)))]
    out = []
    for indexer_id, name, plugin in rows:
        ok, message = await plugin.test()
        await asyncio.to_thread(record_result, indexer_id, ok, message)
        out.append({"id": indexer_id, "name": name, "ok": ok, "message": message})
    return out
