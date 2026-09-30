# Maniac Media – Architecture

This document is the design record for Maniac Media v2. Every major decision is stated together
with *why* it was made and what was traded away. Section numbers follow the deliverables list in
`prompt-v2.md`.

---

## 1. Project architecture

```
             ┌──────────────── Browser (React SPA) ────────────────┐
             │  React Query cache ◀── WebSocket events (/api/v1/ws) │
             └──────────────┬──────────────────────────────────────┘
                            │ HTTPS  JSON  Bearer JWT
┌───────────────────────────▼─────────────────────────────────────────────────┐
│ API layer  backend/api/v1/*        validation (Pydantic v2), auth, RBAC,    │
│                                    pagination, rate limiting, OpenAPI        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Application services  backend/services/*                                    │
│   media · metadata · search · decision engine · download · import · rename  │
│   scanner · notifications · dashboard · health · database wizard · users    │
├───────────────────────┬──────────────────────────┬──────────────────────────┤
│ Domain  backend/domain│ Plugins backend/plugins  │ Workers backend/workers  │
│  quality, parser,     │  metadata · indexers ·   │  APScheduler → job table │
│  naming, DTO types    │  download clients ·      │  → asyncio worker pool,  │
│  (pure, no I/O)       │  notifications (dynamic) │  watchdog folder watcher │
├───────────────────────┴──────────────────────────┴──────────────────────────┤
│ Infrastructure  backend/core (settings, secrets, logging, events, metrics,  │
│                 rate limiting, HTTP retry)  ·  backend/db (models, sessions,│
│                 repositories, Alembic migrations)                          │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Decision – modular monolith, not microservices.** A self-hosted media manager runs on one box
(NAS, mini-PC, Docker host). Microservices would add network hops, deployment complexity and
distributed-transaction problems (import = filesystem + database) for no scaling benefit. Clean
module boundaries (layers above only depend downwards; plugins only on `domain` + `plugins.base`)
keep the option of extracting a service later.

**Decision – dependency direction.** `domain` imports nothing from the app; `plugins` import only
`domain` and `plugins.base`; `services` orchestrate; `api` is a thin adapter. This is Clean
Architecture's dependency rule, and it is why TMDB/qBittorrent specifics never leak into business
logic (requirement "No provider logic should leak into business logic").

## 2. Folder structure

```
backend/
  core/        paths, settings, secrets (AES-GCM + keyring), security (argon2, JWT, RBAC),
               logging (JSON + redaction), events (bus), ratelimit, http (retry), observability
  db/          base (Declarative + EncryptedString), models, session (engine hot-swap),
               repositories, migrator, migrations/ (Alembic env + versions)
  domain/      quality.py, parser.py, naming.py, types.py      ← pure logic, 100% unit-testable
  plugins/     base.py (contracts), registry.py (discovery), metadata/, indexers/,
               download_clients/, notifications/
  services/    one module per use-case family (see §6)
  workers/     queue.py (persistent queue + pool), scheduler.py, tasks.py, watcher.py
  api/         deps.py (auth/paging), schemas.py (contracts), v1/ routers
frontend/src/  api/ (client + types), store/ (Zustand), hooks/ (live events), components/, pages/
tests/         unit/, integration/, e2e/ (Playwright)
docs/          this file
```

## 3. Database schema

All v1 tables are preserved (additive migration only):

| Table | Purpose | Notable columns |
|---|---|---|
| `movies` | Movie library | ids (tmdb unique, imdb idx), file info, `quality`, cast/crew JSON, release dates, `metadata_source`, `last_metadata_refresh`, `last_search_at` |
| `series`, `seasons`, `episodes` | TV library | tvdb/tvmaze/imdb ids, `monitor_new_seasons`; episodes indexed by `(series_id, season_number, episode_number)` |
| `quality_profiles` | Ranking | `items` (allowed qualities, best first), `cutoff`, codec preferences/rejections, size limits |
| `indexers`, `download_clients` | Integrations | **encrypted** `api_key`/`password`; health, failure counters, circuit-breaker `disabled_until`, remote path mapping |
| `download_queue` | Grab → import lifecycle | client item id, **encrypted** download URL (contains indexer keys), status machine, retry count, client queue position (Activity mirrors the client's order) |
| `history` | Per-media timeline | event type, quality, JSON data |
| `blocklist` | Never-again releases | |
| `jobs`, `scheduled_tasks` | Persistent background work | status, attempts, progress, result; `last_run` per task |
| `users`, `audit_log` | Security | argon2 hash, `token_version`, lockout; append-only audit |
| `notification_channels` | Notifications | plain settings JSON + **encrypted** secret JSON |
| `scan_state`, `library_issues` | Scanner | folder mtimes; missing / duplicate / corrupt / upgrade issues |

Sizes are `BIGINT` (files > 2 GiB overflow 32-bit `INTEGER` on PostgreSQL/MSSQL). A naming
convention on `MetaData` gives deterministic constraint names across engines.

## 4. Domain model

* **Quality** = Resolution × Source (e.g. `Bluray-2160p`) with a global natural weight; **Codec**
  and PROPER/REPACK are orthogonal attributes (`ParsedQuality`).
* **QualityProfileSpec** is the user-defined ranking: position in `allowed` is the rank, `cutoff`
  stops upgrades, codec preference breaks ties, PROPER beats non-PROPER of the same quality.
  `is_upgrade()` encodes the only rule that matters for automation.
* **ReleaseInfo / SearchCriteria / ClientItem / MediaMetadata** are provider-neutral DTOs – the
  lingua franca between plugins and services.
* **Parser** turns release names into `ParsedMovie` / `ParsedEpisode` (S01E01, multi-episode,
  1x01, daily, season packs); **naming** renders allow-listed templates.

## 5. Plugin architecture

Contracts in `plugins/base.py`: `MetadataProvider`, `IndexerPlugin`, `DownloadClientPlugin`,
`NotificationPlugin`. Each declares a `key` and `fields` (used by the UI to render forms).
`registry.discover()` loads, in order: built-in packages, `data/plugins/*.py` (drop-in), and the
`maniac_media.plugins` entry-point group (pip-installable). A failing plugin is recorded and
skipped; the core never imports a concrete plugin.

Built in: TMDB, TVDB v4, TVmaze, IMDb · Torznab, Newznab, RSS, Demo · qBittorrent, Transmission,
Deluge, SABnzbd, NZBGet, Simulator · Email, Discord, Telegram, Webhook (HMAC-signed).

**Decision – classes + registry over setuptools-only plugins.** Drop-in files make
experimentation trivial for self-hosters, entry points serve packaged plugins; both converge on
the same registry.

## 6. Service layer

| Service | Responsibility |
|---|---|
| `media_service` | add/edit/delete, monitoring presets, add season/episode, recycle on delete |
| `metadata_service` | provider chain by priority, merge richer data over keyless data, batch refresh |
| `indexer_service` | fan-out search (asyncio.gather), per-indexer token bucket, retry/backoff, circuit breaker, health |
| `decision_engine` | accept/reject + score every release with human-readable reasons |
| `search_service` | interactive search (server-side release cache), automatic search, missing sweep, RSS matching |
| `download_service` | grab via protocol-appropriate client, reconcile client state, failure → blocklist + re-search, retry |
| `import_service` | validate → match → duplicate/upgrade/conflict → atomic placement → record; manual import; watch-folder report |
| `renamer_service` | preview, dry-run, bulk rename (files or whole folders) |
| `scanner_service` | full/incremental scans, missing/duplicate/corrupt/upgrade detection |
| `database_service` | migration wizard |
| `notification_service` | event → channel fan-out |

## 7. Repository pattern

`db/repositories.py` holds query construction. `Repository.paginate()` implements pagination and
**whitelisted** sorting (`sortable` maps API sort keys to columns – user input never reaches SQL
text). Specialised repositories expose intention-revealing queries (`wanted_missing`,
`episode_stats` – one aggregate query instead of loading 16k episodes for the series list).

**Decision – sync SQLAlchemy sessions, async everywhere else.** Async drivers are uneven across the
five supported engines (aioodbc for MSSQL is immature). Endpoints that only touch the database are
`def` and run in the threadpool; all network I/O (indexers, clients, metadata, notifications) is
`async` via httpx; heavy filesystem work runs in `asyncio.to_thread` workers. The event loop is
never blocked, which is the actual goal behind "async I/O everywhere possible".

## 8. API specification

* Base path `/api/v1`, OpenAPI at `/api/openapi.json`, Swagger UI `/api/docs`, ReDoc `/api/redoc`.
* Auth: `POST /auth/setup` (first run only), `POST /auth/login` → Bearer JWT; `GET /auth/me`;
  `POST /auth/logout` (revokes all tokens); `POST /auth/password`.
* Resources: `/movies`, `/series`, `/episodes/monitor`, `/calendar`, `/lookup`, `/releases`,
  `/releases/grab`, `/search/automatic`, `/search/missing`, `/queue`, `/history`, `/blocklist`,
  `/wanted/missing`, `/wanted/cutoff`, `/library/scan`, `/library/issues`, `/library/manual-import`,
  `/rename/preview`, `/rename`, `/settings`, `/quality-profiles`, `/indexers`, `/download-clients`,
  `/notifications`, `/metadata-providers`, `/database/*`, `/users`, `/system/*`, `/dashboard`, `/ws`.
* Lists return `{items,total,page,page_size,pages}`; `page`, `page_size ≤ 500`, `sort`, `order`
  plus resource-specific filters.
* Errors: `{"detail": "..."}` with 401/403/404/409/410/422/429/503 semantics.
* Rate limiting: per-client token bucket (default 900/min), 10/min on auth endpoints; `429` +
  `Retry-After`.

## 9. Security architecture

| Threat | Control |
|---|---|
| Credential theft from disk | AES-256-GCM, ciphertext bound to purpose (AAD); `encrypted_config.db` holds only ciphertext; indexer/client secrets encrypted per column (`EncryptedString`) |
| Key stored with data | Master key in OS store (Windows Credential Manager / Keychain / Secret Service) or `MANIAC_MASTER_KEY`; v1's `data/master.key` is migrated out and deleted |
| Secrets in logs | `RedactingFilter` on every handler scrubs URLs with passwords, `apikey=`, bearer tokens, Telegram bot tokens |
| Secrets via API | Write-only fields; responses expose `has_api_key`; interactive search returns opaque tokens instead of download URLs |
| Password attacks | argon2id; constant-time path for unknown users; lockout after 5 failures; 10/min login rate limit |
| Session hijack / revocation | Short-lived JWT (HS256, signing key in secret store); `token_version` revokes on logout, password or role change; sessionStorage in the browser |
| Privilege escalation | Role → permission sets (`admin`, `power_user`, `read_only`); every endpoint declares its permission; denials are audited |
| Template injection / path traversal | Allow-list template engine (no `str.format`), sanitised segments, no `..` |
| Arbitrary file read | Artwork endpoint resolves files by library id only, images only |
| Clickjacking / sniffing | `X-Frame-Options: DENY`, `nosniff`, `no-referrer`; `no-store` on API |
| Untrusted XML | stdlib expat (entity expansion protected), 8 MB response cap |

Audit trail: `audit_log` records user actions, media changes, imports, renames, deletions,
settings changes, security events and system errors – written in the same transaction as the
change it describes.

## 10. Background job system

```
APScheduler (IntervalTrigger per scheduled_tasks row, next run from persisted last_run)
      │ enqueue (dedupe)
      ▼
jobs table (queued → running → completed/failed, attempts, progress, result)
      │ optimistic claim (UPDATE … WHERE status='queued')
      ▼
WorkerPool (4 asyncio workers, exclusive-by-name, exponential retry)
```

*Survives restart:* queued jobs persist; jobs found `running` at startup are re-queued; the
scheduler derives the next run from `last_run`. Event-driven jobs (import after download,
follow-up monitoring every 5 s while downloads are active, watcher-triggered scans) use the same
queue. Tasks: RSS sync, download monitoring, download-folder scan, metadata refresh (self-continuing
backlog), library scan, missing search, health check, housekeeping.

**Decision – DB-backed queue over Redis/Celery.** Zero extra infrastructure for self-hosters,
transactional with the data it touches, and portable across all supported databases.

## 11. Filesystem management

* Atomic placement (`*.maniac-partial` + `os.replace`), cross-volume fallback (copy + fsync +
  rename + delete), long Windows paths (`\\?\`), UNC shares.
* Modes: move, copy, hardlink (default for torrents so seeding continues), symlink.
* `FileTransaction` journals operations; any failure – including the DB commit – replays it backwards.
* Recycle bin instead of deletion; housekeeping purges after N days.
* Scanner uses `os.scandir`, pre-loaded lookups and batched commits; incremental scans skip
  folders with unchanged mtimes (authoritative `os.stat`, since `DirEntry` caches can lag on Windows).

## 12. Observability

`/metrics` (Prometheus): HTTP latency/count by route, job counts/durations, queue depth, active
downloads, imports, indexer results, WebSocket clients. Structured JSON logs with request ids
(`X-Request-ID`), rotating file `data/logs/maniac.log`. OpenTelemetry tracing activates when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set and the optional packages are installed.

## 13. Docker deployment

Multi-stage image (Node build → Python venv → slim runtime), non-root user, read-only root
filesystem, `tini`, healthcheck, `/config` volume. `docker-compose.yml` ships SQLite by default,
with optional `postgres` and `monitoring` profiles. `MANIAC_MASTER_KEY` is required in containers
(no OS keyring there).

## 14. CI/CD pipeline

`.github/workflows/ci.yml`: lint + unit/integration tests with coverage gate on Linux & Windows ×
Python 3.12/3.13; PostgreSQL service job running schema + wizard tests; frontend type-check and
build; Playwright E2E against the built SPA; Docker build with layer cache.

## 15. Testing strategy

* **Unit** (`tests/unit`): domain (quality, parser, naming, profiles), security (cipher, purpose
  binding, tamper detection, legacy tokens, argon2, JWT, RBAC, log redaction), infrastructure
  (plugin discovery incl. broken external plugin, Torznab parsing, torrent hashing, path mapping,
  decision engine, rate limiter, file validation, transfer modes, transaction rollback).
* **Integration** (`tests/integration`): API through FastAPI's TestClient on a real SQLite
  database – auth, RBAC, lockout, token revocation, secret non-disclosure, pagination/filtering,
  series/episode management, validation, WebSocket auth, rate limiting; full pipeline
  (search → grab → simulated download → import → rename), upgrade + recycle, corrupt-file
  rejection, scanner (incremental, multi-episode, missing detection), job recovery, migration
  wizard success and rollback; optional real PostgreSQL/MySQL/MSSQL targets.
* **E2E** (`tests/e2e`, Playwright): first-run setup, every page renders without errors, mode
  switch, naming validation, grab-to-import through the UI, read-only role visibility.

## 16. Migration strategy

* **Schema:** Alembic. On startup the migrator detects a v1 database (tables but no
  `alembic_version`), backs it up with SQLite's online backup API, stamps it at `0001_baseline`
  and upgrades. Revision `0002` is additive only and encrypts any plaintext credentials in place.
  Every later upgrade is also preceded by a backup.
* **Configuration:** v1's encrypted DB URL moves from `config.json` to `encrypted_config.db`;
  plaintext is never re-written.
* **Engines (wizard):** validate → quiesce workers → backup → Alembic on target → streamed copy in
  FK order inside one transaction (sequence reset on PostgreSQL, `IDENTITY_INSERT` on MSSQL) →
  row-count + schema verification → encrypted URL saved → engine hot-swap. Any failure rolls back
  the target and drops tables the wizard created; the app never stops serving.

## 17. MVP roadmap (delivered in v2.1)

- [x] Movies & TV with monitoring at series/season/episode level, calendar, wanted lists
- [x] Metadata: TMDB, TVDB, TVmaze, IMDb with fallback chain & background enrichment
- [x] Indexers: Torznab/Newznab/RSS with health, retries, rate limits, circuit breaker
- [x] Clients: qBittorrent, Transmission, Deluge, SABnzbd, NZBGet (+ simulator)
- [x] Decision engine with quality profiles; interactive/automatic/missing/RSS search
- [x] Import engine (atomic, transactional, upgrades, duplicates, recycle bin), manual import
- [x] Renamer with templates, preview, dry-run, bulk; scanner (full/incremental)
- [x] Users/roles/JWT/argon2, audit log, encrypted secrets with OS keyring
- [x] Persistent job system, WebSocket live UI, Prometheus metrics, structured logs
- [x] DB migration wizard (SQLite ↔ PostgreSQL/MySQL/MariaDB/MSSQL), Docker, CI

## 18. Version 1.0 → next roadmap

1. Custom formats (regex scoring on release names: HDR flavours, audio, editions, groups).
2. Anime absolute numbering & scene mapping (XEM); alternative titles from providers.
3. Import lists (Trakt, IMDb lists, Plex watchlist) and collection tracking.
4. Media inspection via ffprobe (real resolution/codec instead of name-based detection).
5. API keys for integrations (Overseerr/Home Assistant) alongside JWT; OIDC single sign-on.
6. Kubernetes Helm chart; horizontally scaled read replicas for the API tier.
7. Subtitle provider plugins; Plex/Jellyfin/Emby library-refresh notifications.
