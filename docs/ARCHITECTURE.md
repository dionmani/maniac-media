# Maniac Media – Architecture

This document is the design record for Maniac Media v2. Every major decision is stated together
with *why* it was made and what was traded away. Section numbers originally followed the
deliverables list in `prompt-v2.md` (historical; the code is the source of truth where they differ).

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
frontend/src/  api/ (client + types), store/ (Zustand: auth, ui), hooks/ (useLiveEvents – WebSocket),
               components/ (Layout, MediaHeader, InteractiveSearchDialog, Discover{Card,Row,Dialog}, common),
               pages/ (Dashboard, Library, Movie/Series detail, AddMedia, Discover, Calendar, Wanted, Activity,
               Import, Renamer, System, Login) + pages/settings/ (General, Quality, Integration,
               Database, User); server state via React Query
tests/         unit/, integration/, e2e/ (Playwright)
docs/          this file; roadmap/ (prioritised feature implementation plans)
```

## 3. Database schema

All v1 tables are preserved (additive migration only):

| Table | Purpose | Notable columns |
|---|---|---|
| `movies` | Movie library | ids (tmdb unique, imdb idx), file info, `quality`, cast/crew JSON, release dates, `production_status` (TMDB stage, for the library badge), `metadata_source`, `last_metadata_refresh`, `last_search_at` |
| `series`, `seasons`, `episodes` | TV library | tvdb/tvmaze/imdb ids, `monitor_new_seasons`; episodes indexed by `(series_id, season_number, episode_number)`, `air_date` plus `air_date_utc` (exact air time when a provider knows it, for the release gate) |
| `quality_profiles` | Ranking | `items` (allowed qualities, best first), `cutoff`, codec preferences/rejections, size limits |
| `indexers`, `download_clients` | Integrations | **encrypted** `api_key`/`password`; health, failure counters, circuit-breaker `disabled_until`, remote path mapping |
| `download_queue` | Grab → import lifecycle | client item id, **encrypted** download URL (contains indexer keys), status machine, retry count, client queue position (Activity mirrors the client's order) |
| `history` | Per-media timeline | event type, quality, JSON data |
| `blocklist` | Never-again releases | |
| `jobs`, `scheduled_tasks` | Persistent background work | status, attempts, progress, result; `last_run` per task |
| `users`, `audit_log` | Security | argon2 hash, `token_version`, lockout; append-only audit |
| `notification_channels` | Notifications | plain settings JSON + **encrypted** secret JSON |
| `scan_state`, `library_issues` | Scanner | folder mtimes; typed issues (see `domain/issues.py`) with fingerprint, severity, JSON evidence and an open/resolved/dismissed lifecycle, reconciled by `IssueRecorder` so dismissals persist |
| `discover_seen`, `discover_exclusions` | Discover | first time each TMDB id appeared per section (unique `(media_type, section, tmdb_id)`, drives "new" badges; pruned after a year); "not interested" titles (unique `(media_type, tmdb_id)`, shaped like the planned `import_list_exclusions` so the two can merge) |

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
* **Discover** (`domain/discover.py`) defines the sections (movies: announced, in theaters,
  coming to streaming & video; TV: new shows, new seasons) as date windows and rules – e.g. a
  "new season" is a season ≥ 2 whose season air date is in the window, so binge drops count.
  Providers fetch candidates; these rules decide membership. `DiscoverItem` carries the title plus
  `reason`/`reason_date` and streaming services (`WatchProvider`).
* **Release gate** (`domain/availability.py`): nothing is searched for or queued before it is out,
  because pre-release grabs are CAMs, telesyncs, fakes and malware. A movie is out on its digital or
  physical release date, never its theatrical or festival date. One with no home-release date on
  record counts as back catalogue once its first release is `release_gate_catalog_days` old
  (default 365, 0 = off). An episode is out at `air_date_utc`. With only a date, it is out once that
  date is over everywhere (36 h after it began in UTC). TBA is never out. Every `Availability` names
  its reference date. Automatic search, the missing sweep and RSS hold unreleased targets before
  touching an indexer. The decision engine rejects releases covering anything unreleased (a season
  pack needs the whole season aired), so interactive search shows the reason and a user can still
  override. A grab's history/audit entry records the date that cleared it. `MovieRepository.released`
  / `EpisodeRepository.aired` are the SQL forms (wanted lists, counts), kept in step by a parity test;
  `released` never evaluates to NULL, so its negation selects exactly the movies still held. The UI
  uses the same gate: a movie without a file is *Missing* only once it is out; otherwise
  `movie_release_state` says why not (in theaters, a TMDB production stage, coming soon, announced,
  canceled) and the library's *Upcoming* filter lists those.
  TMDB/TVDB only give episode dates, so a TMDB-first series refresh borrows TVmaze's `airstamp`s when
  it has recent or upcoming episodes (matched on season, episode *and* date).

## 5. Plugin architecture

Contracts in `plugins/base.py`: `MetadataProvider`, `IndexerPlugin`, `DownloadClientPlugin`,
`NotificationPlugin`. Each declares a `key` and `fields` (used by the UI to render forms).
`MetadataProvider.discover()` is optional (`supports_discover`); only TMDB implements it, reading
TMDB's list endpoints (`/discover/*`, `/movie/now_playing`) plus one detail call per title for
regional release dates and JustWatch streaming services.
`registry.discover()` loads, in order: built-in packages, `data/plugins/*.py` (drop-in), and the
`maniac_media.plugins` entry-point group (pip-installable). A failing plugin is recorded and
skipped; the core never imports a concrete plugin.

Built in: TMDB, TVDB v4, TVmaze, IMDb · Torznab (subclass of Newznab, same module), Newznab, RSS, Demo · qBittorrent, Transmission,
Deluge, SABnzbd, NZBGet, Simulator · Email, Discord, Telegram, Webhook (HMAC-signed).

**Decision – classes + registry over setuptools-only plugins.** Drop-in files make
experimentation trivial for self-hosters, entry points serve packaged plugins; both converge on
the same registry.

## 6. Service layer

| Service | Responsibility |
|---|---|
| `media_service` | add/edit/delete, monitoring presets, add season/episode, prune stale seasons (removes file-less episodes/seasons no longer in provider metadata; dry-run supported), recycle on delete |
| `metadata_service` | provider chain by priority, merge richer data over keyless data, batch refresh |
| `discover_service` | Discover sections from the first discover-capable provider; in-memory section cache (6 h, warmed by `discover_refresh`, keyed by region/languages), first-seen recording on each fill; per request it drops exclusions and flags titles already in the library (TMDB/IMDb/TVDB ids) |
| `indexer_service` | fan-out search (asyncio.gather), per-indexer token bucket, retry/backoff, circuit breaker, health |
| `decision_engine` | accept/reject + score every release with human-readable reasons |
| `search_service` | interactive search (server-side release cache), automatic search, missing sweep, RSS matching; applies the release gate (§4) before searching |
| `download_service` | grab via protocol-appropriate client, reconcile client state, failure → blocklist + re-search, retry |
| `import_service` | validate → match → duplicate/upgrade/conflict → atomic placement → record; manual import; watch-folder report |
| `renamer_service` | preview, dry-run, bulk rename (files or whole folders) |
| `scanner_service` | full/incremental scans, missing/duplicate/corrupt/upgrade detection |
| `database_service` | migration wizard |
| `notification_service` | event → channel fan-out |
| `quality_service` | quality profile CRUD and defaults |
| `fileops` | filesystem primitives: transfer modes, `FileTransaction`, recycle bin, file validation |
| `audit` | append-only audit-log writer (same transaction as the change) |
| `dashboard_service` | dashboard aggregates |
| `health_service` | health checks (indexers, clients, disk, database) |
| `user_service` | users, roles, lockout, token revocation |

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
* Auth: `GET /auth/status`, `POST /auth/setup` (first run only), `POST /auth/login` → Bearer JWT;
  `GET /auth/me`; `POST /auth/logout` (revokes all tokens); `POST /auth/password`.
* Resources: `/movies` (+ `/{id}/refresh`, `/bulk`), `/series` (+ `/{id}/refresh`, `/{id}/monitoring`,
  `/{id}/seasons`, `/{id}/episodes`, `/{id}/seasons/{n}/monitor`, `/{id}/prune-stale`),
  `/episodes/monitor`, `/artwork/{type}/{id}/{kind}`, `/calendar`, `/lookup`, `/discover/{movie|tv}`
  (+ `/{section}`, `/refresh`), `/discover/exclusions` (+ `DELETE /{media_type}/{tmdb_id}`), `/releases`,
  `/releases/grab`, `/search/automatic`, `/search/missing`, `/queue` (+ `/{id}/retry`, `/{id}/import`,
  `/refresh`), `/history`, `/blocklist`, `/wanted/missing`, `/wanted/cutoff`, `/library/scan`,
  `/library/issues` (+ `/{id}/resolve`), `/library/manual-import` (+ `/scan`, `/match`, `/delete`),
  `/library/download-folder`, `/rename/preview`, `/rename`, `/settings` (+ `/naming/preview`),
  `/qualities`, `/quality-profiles`, `/plugins`, `/indexers`, `/download-clients`, `/notifications`
  (indexers/clients/notifications each have `/test`), `/metadata-providers`, `/database`
  (`/test`, `/migrate`, `/migration`, `/backup`), `/users`, `/dashboard`, `/system/{status,health,
  tasks,jobs,audit,logs}` (tasks: `PUT /{name}`, `POST /{name}/run`; jobs: `POST /{id}/cancel`),
  `/ws`; plus `/metrics` (Prometheus, outside `/api/v1`).
* Compat API (`backend/api/compat/`): `/api/compat/radarr/api/v3/*` and `/api/compat/sonarr/api/v3/*`
  are Radarr/Sonarr v3 facades covering the subset Seerr/Overseerr call (status, profiles, root
  folders, tags, movie/series CRUD + lookup, episodes, search commands, queue). Seerr identifies
  shows by TVDB id; a lookup miss matches by TVmaze/IMDb/TMDB id and backfills `series.tvdb_id`.
  Tags (`tags` table, `movies.tags`/`series.tags` id lists) are stored with Radarr semantics; a newly
  attached requester tag (`<id>-<name>`, `domain/tags.py`) becomes a `requested` history entry and the
  `requested_by` field on movie/series detail. User guide: [SEERR.md](SEERR.md). Auth is an API key via `?apikey=` (the key must allow query
  params); it only translates to the same services as `/api/v1` and is excluded from the OpenAPI
  spec. Seerr's file deletes are honoured only with `seerr_allow_file_deletion`, via the recycle bin.
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
queue. Scheduled tasks (default interval): download monitor (1 min), download-folder scan (5 min),
RSS sync (15 min), health check (15 min), metadata refresh (12 h, self-continuing backlog), library
scan (6 h), Discover refresh (6 h, re-fetches the Discover sections so page loads hit a warm cache),
missing search (24 h, off by default), cleanup/housekeeping (24 h). On-demand job types:
import download, automatic search, rename.

**Decision – DB-backed queue over Redis/Celery.** Zero extra infrastructure for self-hosters,
transactional with the data it touches, and portable across all supported databases.

## 11. Filesystem management

* Atomic placement (`*.maniac-partial` + `os.replace`), cross-volume fallback (copy + fsync +
  rename + delete), long Windows paths (`\\?\`), UNC shares.
* Modes: move, copy, hardlink (default for torrents so seeding continues), symlink.
* `FileTransaction` journals operations; any failure – including the DB commit – replays it backwards.
* Recycle bin instead of deletion; housekeeping purges after N days.
* A move-mode import removes the release folder it emptied, plus any parents that emptied with it. Housekeeping
  also catches stragglers that are untouched for 1 h. Both only use `rmdir`. Neither touches the download folder
  itself, client category/working folders (`movies/`, `incomplete/`, `_UNPACK_*` …), downloads still in flight,
  or a library/recycle bin nested underneath.
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
  Revision `0003` adds `download_queue.client_position` (idempotent: skipped if the column exists).
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

## 18. Next roadmap

The post-v2.1 roadmap now lives in [roadmap/README.md](roadmap/README.md). It has a prioritised,
phased list with a dependency graph and one detailed implementation plan per feature. Every item
previously listed here is covered there: custom formats, anime/XEM and alternate titles, import
lists, ffprobe inspection, API keys, subtitles and media-server refresh. OIDC, Helm and read
replicas are tracked in [roadmap/15-deferred-and-reframed.md](roadmap/15-deferred-and-reframed.md).
