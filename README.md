# Maniac Media

Unified movie **and** TV automation – Radarr and Sonarr in one application, switchable between
Movies and TV Shows mode, sharing one core: metadata, indexers, download clients, import engine,
renamer, scanner, quality profiles, users and an encrypted, migratable database.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, and every decision behind it.

## Quick start (Windows / Linux / macOS)

```bash
pip install -r requirements.txt          # Python 3.12+
cd frontend && npm ci && npm run build && cd ..
python run.py                            # http://127.0.0.1:8000
```

On first launch the database (`data/app.db`) is created, migrated and seeded. Open the UI and
create the administrator account. Then:

1. **Settings → Media management** – movie/TV root folders and the download watch folder.
2. **Settings → Indexers** – add Torznab/Newznab endpoints (Prowlarr, Jackett, NZBGeek…) or RSS.
3. **Settings → Download clients** – qBittorrent, Transmission, Deluge, SABnzbd or NZBGet.
4. **Settings → Metadata** – optional TMDB/TVDB keys (TVmaze and IMDb work without keys).
5. **Library → Scan folder** to adopt existing media, or **Add New** to search providers.

Want to try the pipeline without real accounts? Add the **Demo** indexer and the **Simulator**
download client (they write placeholder files into your download folder – use test folders).

### Upgrading from v1

Just start v2. The existing `maniac_media.db` is backed up to `data/backups/`, adopted and migrated
in place; the master key moves from `data/master.key` into the OS credential store; every movie,
series and episode is kept. Metadata enrichment then runs in the background.

## Docker / Unraid

Pushing to `main` (or a `v*.*.*` tag) runs `.github/workflows/publish.yml`, which tests the code and
publishes a multi-arch image to `ghcr.io/dionmani/maniac-media` (`:latest`, `:<version>`, `:sha-…`).

**Unraid (Docker tab):** copy `unraid/maniac-media.xml` to
`/boot/config/plugins/dockerMan/templates-user/my-maniac-media.xml`, then *Add Container → maniac-media*. Defaults: `/mnt/user/appdata/maniac-media → /config`,
`/mnt/user/data → /data`, `PUID=99`, `PGID=100`.

**Compose:**

```bash
cp .env.example .env   # set MANIAC_MASTER_KEY (openssl rand -base64 32)
docker compose up -d                       # SQLite
docker compose --profile postgres up -d    # + PostgreSQL (then Settings → Database → migrate)
```

Keep downloads and media on one share (`/data/downloads`, `/data/media`) and give your download client
the same `/data` mapping – imports then hardlink instantly and no remote path mapping is needed.

## Development

```bash
pip install -r requirements-dev.txt
pytest                                    # unit + integration
pytest --cov=backend
pip install playwright && python -m playwright install chromium
pytest tests/e2e -m e2e -o addopts=""     # browser tests (needs frontend/dist)
cd frontend && npm run dev                # Vite on :3000, proxies /api to :8000
```

| Variable | Purpose |
|---|---|
| `MANIAC_DATA_DIR` | data directory (default `./data`, `/config` in Docker) |
| `MANIAC_MASTER_KEY` | master encryption key (required where no OS keyring exists) |
| `MANIAC_HOST` / `MANIAC_PORT` | bind address (default `127.0.0.1:8000`) |
| `MANIAC_LOG_FORMAT` | `console` or `json` |
| `MANIAC_LOG_LEVEL` | `INFO`, `DEBUG`, … |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | enable OpenTelemetry tracing |

Optional database drivers: `pip install -r requirements-optional.txt`.

API docs: `/api/docs` · Metrics: `/metrics` · Health: `/api/health`
