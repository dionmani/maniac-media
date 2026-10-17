# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Maniac Media is a unified movie + TV automation platform (Radarr and Sonarr combined into one app,
switchable between Movies and TV mode) — Python/FastAPI backend, React SPA frontend. It's a
self-hosted "modular monolith": one process, sharing metadata/indexer/download-client/import/rename/
scanner logic across both media types, backed by an encrypted, engine-portable database
(SQLite/PostgreSQL/MySQL/MariaDB/MSSQL).

**Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making non-trivial changes.** It is the
authoritative design record — every architectural decision, the full DB schema, the plugin system,
security model, job system, filesystem semantics, and API spec are documented there with the
reasoning behind them. This file only summarizes what's needed to navigate and run the code; don't
duplicate ARCHITECTURE.md's content from memory — go re-read the relevant section there.

## Commands

### Backend

```bash
pip install -r requirements-dev.txt        # includes requirements.txt + pytest/ruff
python run.py                              # dev server, http://127.0.0.1:8000 (MANIAC_RELOAD=1 for autoreload)

pytest                                     # unit + integration (e2e + benchmark excluded by default via pyproject addopts)
pytest tests/unit/test_domain.py           # single file
pytest tests/unit/test_domain.py -k parse  # single test by name
pytest --cov=backend                       # with coverage (CI gate: --cov-fail-under=65)
pytest -m benchmark                        # opt-in timing benchmarks (excluded by default, not run in CI)

ruff check backend tests                   # lint (CI only checks --select E9,F63,F7,F82)

pip install playwright && python -m playwright install chromium
pytest tests/e2e -m e2e -o addopts=""      # browser e2e tests; needs frontend/dist built first
```

Test isolation: `tests/conftest.py` sets `MANIAC_DATA_DIR` to a fresh temp dir and a fixed
`MANIAC_MASTER_KEY` *before* `backend` is imported (paths/secrets resolve at import time), so tests
never touch a real `./data`. Fixtures are session-scoped and build on each other: `library` →
`database` (runs `bootstrap_database()`) → `client` (FastAPI `TestClient`) → `admin_headers` (does
first-run `/auth/setup`, falls back to `/auth/login` if already set up).

### Frontend (`frontend/`)

```bash
npm ci
npm run dev         # Vite dev server on :3000, proxies /api to :8000
npm run build        # tsc --noEmit + vite build -> frontend/dist (backend serves this in prod)
npm run typecheck    # tsc --noEmit only
```

### Docker / release

Push to `main` or a `v*.*.*` tag runs `.github/workflows/ci.yml`: backend tests (Linux+Windows ×
Python 3.12/3.13), a Postgres migration-wizard job, frontend build, Playwright e2e against the built
SPA, then a multi-arch image published to `ghcr.io/dionmani/maniac-media`.

```bash
docker compose up -d                       # SQLite (requires .env with MANIAC_MASTER_KEY)
docker compose --profile postgres up -d    # + Postgres, then migrate via Settings → Database in the UI
```

## Architecture (quick orientation — see ARCHITECTURE.md §1–2 for the full picture)

Strict layering, dependencies only point downward:

```
backend/api/v1/*      → thin FastAPI routers: validation (Pydantic v2), auth/RBAC, pagination
backend/services/*    → one module per use-case family; orchestrates domain + plugins + db
backend/domain/*      → pure logic, no I/O: quality.py, parser.py, naming.py, types.py
backend/plugins/*     → provider-specific code behind contracts in plugins/base.py
backend/workers/*     → APScheduler → jobs table → asyncio worker pool; watchdog folder watcher
backend/core/*        → settings, secrets (AES-GCM + OS keyring), security (argon2/JWT/RBAC),
                         logging (redaction), events bus, rate limiting, HTTP retry, observability
backend/db/*          → SQLAlchemy models, sessions (engine hot-swap), repositories, Alembic migrations
frontend/src/         → api/ (client+types), store/ (Zustand), hooks/ (WebSocket live events),
                         components/, pages/
```

Key rules to preserve when editing:

- **`domain` has zero app imports; `plugins` import only `domain` + `plugins.base`.** This is what
  keeps provider specifics (TMDB shape, qBittorrent API, etc.) out of business logic. Never import
  a concrete plugin from `services` or `domain` — go through `plugins/registry.py`.
- **Sync DB sessions, async everywhere else.** DB-only endpoints are plain `def` (run in
  threadpool); anything touching the network (indexers, download clients, metadata providers,
  notifications) is `async` via `httpx`; heavy filesystem work goes through `asyncio.to_thread`.
  Don't block the event loop with sync I/O in an `async def` handler.
- **Repositories, not raw queries, from services.** `db/repositories.py`'s `Repository.paginate()`
  whitelists sortable columns (`sortable` dict) — never interpolate a client-supplied sort key into
  SQL. Add new intention-revealing query methods there rather than building ad-hoc queries in a
  service.
- **Plugins are discovered, not imported.** `plugins/registry.discover()` loads built-ins, then
  `data/plugins/*.py` drop-ins, then the `maniac_media.plugins` entry-point group. A new built-in
  plugin subclasses the relevant contract in `plugins/base.py` and declares `key` + `fields`; it
  doesn't need registration elsewhere.
- **Secrets are encrypted at the column level** (`EncryptedString` in `db/base.py`) for indexer/
  download-client credentials, download queue URLs, and notification channel secrets. Never log a
  raw secret — the `RedactingFilter` (core/logging.py) scrubs known patterns, but new secret fields
  should reuse `EncryptedString` rather than relying on redaction alone.
- **Background work goes through the jobs table**, not ad-hoc `asyncio.create_task`. See
  ARCHITECTURE.md §10 — the DB-backed queue is deliberate (survives restart, no Redis/Celery
  dependency for self-hosters).
- **Filesystem writes use `FileTransaction`** (atomic placement, journal + rollback on failure,
  cross-volume fallback). Don't `shutil.move`/`os.rename` media files directly in service code.

## Testing conventions

- `tests/unit/` — pure domain + security + infrastructure logic, no DB/network.
- `tests/integration/` — full API via FastAPI `TestClient` against a real (temp) SQLite DB;
  `test_databases.py` also runs against Postgres/MySQL/MSSQL when `MANIAC_TEST_POSTGRES_URL` etc.
  are set (CI provides a Postgres service for this).
- `tests/e2e/` — Playwright against the built frontend; marked `e2e` and excluded by the default
  pytest `addopts` (`-m 'not e2e and not benchmark'` in `pyproject.toml`), so `pytest` alone never
  runs them.
- Wall-clock performance assertions are marked `benchmark` and are likewise opt-in
  (`pytest -m benchmark`): shared CI runners are too slow and noisy for fixed time budgets.
