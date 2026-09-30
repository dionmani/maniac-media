# 14 – Hybrid cloud & tiered storage

> Phase 6 · Effort XL (staged; A ≈ 1.5 weeks, B ≈ 1.5 weeks, C ≈ 2+ weeks) · Risk **high** (moves media; touches every path operation) · Depends on F1, 07 (media-server refresh after moves), 13 (long lane, verification patterns) · Priority #17

## 1. Summary & user value

Libraries increasingly span a fast local disk, a NAS, and cloud storage (Backblaze B2, S3, Google
Drive or OneDrive, usually through **rclone mount**). Radarr and Sonarr assume one local
filesystem per root. They handle cloud mounts badly (slow scans, API bans from over-reading, no
hardlinks), and they have no concept of moving old content to cheaper storage.

### Decision D1 – "native cloud playback" is not achievable at this layer

Plex, Jellyfin and Emby can only play files they can reach through a **filesystem path**. A
library that lives "natively" in S3, with no mount, is invisible to them. So this feature treats
cloud storage as:

- a **mounted root folder** (rclone, or a NAS share backed by the cloud) that Maniac understands
  and handles carefully (stages A and B);
- optionally, a **native archive backend** for content that doesn't need to be playable, such as
  cold backups (stage C).

The brainstorm's "partial local caching" is exactly what `rclone mount --vfs-cache-mode full`
already does. We document recommended rclone settings rather than reimplementing a VFS cache.

## 2. Current state

- Exactly two roots: `settings.media_root_movies` and `settings.media_root_tv`
  ([settings.py:37-38](../../backend/core/settings.py#L37-L38)). Movies and series store an
  absolute `root_folder`/`path`.
- Scanner: `os.scandir`, with an incremental mode based on folder mtimes. It's efficient locally,
  but on rclone mounts every stat can be an API call.
- The watcher uses `watchdog` on the download folder only
  ([watcher.py](../../backend/workers/watcher.py)). The library isn't watched.
- `fileops.transfer` has hardlink → copy fallback, cross-volume copy with fsync, and atomic
  rename ([fileops.py:66](../../backend/services/fileops.py#L66)).
- The health check reports disk usage per root
  ([health_service.py:48-58](../../backend/services/health_service.py#L48-L58)).

## 3. Scope

### Stage A – multiple root folders & remote-mount mode

- A `root_folders` table (N per media type), each with `tier` and `is_remote` attributes.
- Remote-aware behaviour: throttled scanning, no hardlinks, ffprobe throttled or skipped,
  free-space thresholds.
- A root folder picker on add/import (with a default per type).

### Stage B – tiering

- Tiering policies: move titles between roots by age, watch state (07) or size.
- Verified cross-root moves.
- Media-server refresh after each move.

### Stage C – archive backends (optional plugin kind)

- `PluginKind.STORAGE_BACKEND` for S3-compatible or B2 **archive copies**: back up or offload
  titles with a manifest, and restore on demand.

### Out

- A VFS cache.
- Streaming from object storage.
- Google Drive and OneDrive native APIs. Use rclone; their API quotas make native integration a
  maintenance burden.

## 4. Design

### 4.1 Stage A – `root_folders`

**Data model** – migration `NNNN_root_folders`:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `path` | String(1024) | |
| `path_hash` | String(64) unique | |
| `media_type` | String(16) | movie \| tv |
| `name` | String(128) | "Local SSD", "NAS", "B2 (rclone)" |
| `tier` | String(8) | `hot` \| `warm` \| `cold` |
| `is_remote` | Boolean | enables remote-mount mode |
| `is_default` | Boolean | one per media_type |
| `min_free_gb` | Integer | refuse imports below |
| `scan_interval_minutes` | Integer nullable | override (remote roots: e.g. 24 h) |
| `enabled` | Boolean | |

Add `movies.root_folder_id` and `series.root_folder_id` (FK, nullable) **alongside** the existing
`root_folder` string (additive). Backfill: create two rows from the current settings, mark them
default, and link every title whose `root_folder` or `path` starts with the root. Titles whose
path lies outside both get a new root row per distinct parent directory, which covers users who
already point titles elsewhere.

The settings `media_root_movies` / `media_root_tv` remain as a **seed** (first-run and Docker
env), and the UI then manages roots.

**Remote-mount mode** (`is_remote=True`):

| Area | Local behaviour | Remote behaviour |
|---|---|---|
| Scanner | full or incremental by mtime | incremental only, sequential, `remote_scan_rps` throttle (default 10 stat/s). One `scandir` per title folder, **no per-file `stat()` beyond what `DirEntry` provides** (rclone serves it from its dir cache). `scan_interval_minutes` default 24 h. |
| Missing-file pass | `os.path.exists` per file | skipped unless the folder was rescanned (avoids thousands of API hits) |
| Header validation (`check_video_file`) | reads 12 bytes | skipped for existing files (reading forces a download) |
| ffprobe (03) | on import + backlog | only on import (the file's local source), never in backlog for remote roots unless `inspect_remote=True` |
| Quick hash (05) | computed | skipped |
| Import transfer | hardlink/move | **copy** (hardlinks are impossible), with verify-after-copy (size compare, optional hash), then source removal per import mode. It's a long operation, so it runs as a long-lane job when > 5 GB. |
| Watcher | n/a | never watch remote roots |
| Health | disk usage | disk usage may be meaningless (rclone reports quota or 1 PB). Show "remote – quota unknown" unless `min_free_gb` is set. Check mount liveness (a sentinel file or `os.listdir` with timeout) so a dead mount doesn't look like "all files missing". |

**Critical safety – a dead mount must never cause mass "missing" issues or deletions.** Before
any missing-file logic, each remote root is probed with a timeout-bounded `listdir`. If the mount
is empty or unreachable while the DB has titles in it, the scan of that root aborts with a health
error. This guard applies to local roots too: an unmounted NAS share looks just like an empty
folder.

**Service changes:**

- A `root_folder_service` for CRUD, `root_for(path)`, `default_root(media_type)`, and
  `is_remote(path)` (a prefix map cached in memory).
- `fileops.transfer` gets `remote_target: bool`, which forces copy + verify.
- The scanner iterates over enabled roots per media type instead of one setting.
- `import_service.movie_destination` / `episode_destination` use the title's `root_folder_id`.
  They check `min_free_gb` on that root, and fail with the 01 reason `disk_full` if it's below.
- The add-media flows accept `root_folder_id`.

**API:**

- `/root-folders` CRUD (SETTINGS_*), each response with `free_bytes`, `total_bytes`, `title_count`
  and `bytes_used`.
- `POST /root-folders/{id}/test`: reachability, writability and remote-detection hints (e.g.
  `fuse.rclone` in `/proc/mounts` on Linux).

**Frontend:**

- Settings → Media management → **Root folders** table.
- A root picker in Add Media and in the movie/series edit dialogs.
- The Dashboard disks panel lists every root with its tier badge, local vs remote, and usage.

### 4.2 Stage B – tiering

**Domain** – `backend/domain/tiering.py`:

```python
@dataclass(frozen=True)
class TieringRule:
    id: int
    media_type: str
    from_root_ids: tuple[int, ...]
    to_root_id: int
    conditions: TieringConditions   # added_older_than_days, last_watched_older_than_days (07),
                                    # never_watched_and_added_older_than_days, min_size_gb,
                                    # series_status in (ended, canceled), exclude_tags ("keep", "hot")
    direction: str                  # demote | promote
    max_gb_per_run: int

def select(titles: list[TitleStorageFacts], rule: TieringRule, now) -> list[MovePlan]
```

A **promote** rule moves content back to hot storage, for example when someone starts watching an
old series again (07 watch event). That's the "partial local caching" idea done at the title
level.

**Data model:** `tiering_rules` (as above, plus `enabled`, `mode` preview/confirm/auto) and
`tier_moves` (title, from/to root, bytes, status, started/finished, error) — the same pattern as
`storage_actions` (13).

**Execution** (a long-lane job, one title at a time):

1. Check free space on the target (+10 %).
2. Copy the whole title folder to the target root under the same relative path, using
   `_copy_atomic` per file, with fsync (existing).
3. **Verify**: every file's size matches, and the optional sampled hash (first/last 4 MB) matches.
4. Inside one `FileTransaction` plus one DB transaction, update `movie.path` / `series.path`,
   `root_folder_id`, every `media_files.path` / `path_hash` (F1 keeps `relative_path`, so this is
   a prefix swap), and the legacy `file_path` columns.
5. Remove the source (recycle bin **not** used for tier moves, since the data exists at the
   destination and has been verified; deleting sources is logged and audited). **Only after the
   DB commit succeeds.**
6. Media-server refresh of both old and new paths (07). The media server must have the target
   root in a library; the UI warns during rule setup if no server library covers it.
7. History event `moved` and `audit()`.

Rollback: failure before step 4 deletes the partial destination. Failure at step 4 rolls back
the DB and deletes the destination copy. The source is untouched until the end.

**Frontend:** Settings → Storage → **Tiering rules**, with a preview (titles, GB) and a moves
queue with progress on the Storage page (13). Titles show a tier badge.

### 4.3 Stage C – archive backends (optional)

```python
class StorageBackendPlugin(Plugin):
    kind = PluginKind.STORAGE_BACKEND
    async def put(self, key: str, local_path: Path, progress) -> ObjectInfo: ...
    async def get(self, key: str, local_path: Path, progress) -> None: ...
    async def head(self, key: str) -> ObjectInfo | None: ...
    async def delete(self, key: str) -> None: ...
    async def list(self, prefix: str) -> AsyncIterator[ObjectInfo]: ...
```

Built-in: **S3-compatible** (AWS, B2 S3 API, Wasabi, MinIO) using multipart upload. Doing this
with httpx and SigV4 avoids a boto3 dependency; otherwise it's an optional extra.

Use cases:

- **Archive** a title: upload its files, record `archive_copies` (media_file_id, backend_id, key,
  etag, uploaded_at), and optionally remove the local copy. The title shows as "Archived – not
  playable", with `has_file` false but not treated as missing (a new `archived` status excluded
  from wanted and missing).
- **Restore** a title: download it to its root, verify, re-link, refresh the media server.
- **Backup-only**: keep both copies. The Library health score (04) could later reward backup
  coverage.

Credentials use `EncryptedString`. Transfers run on the long lane with bandwidth limits and a
schedule window.

## 5. Implementation milestones

**Stage A**

1. The `root_folders` model, migration and backfill, the root service, and a root-aware scanner,
   import and add flows.
2. The dead-mount guard for **all** roots (worth shipping on its own; it's a real safety fix today).
3. Remote-mount mode behaviours plus health checks.
4. UI for roots and the pickers.

**Stage B**

5. Domain tiering selection, rules CRUD with preview.
6. The move executor (copy → verify → DB swap → source delete), plus media-server refresh.
7. Promote-on-watch hook (07), and the tier badges.

**Stage C (optional)**

8. The storage backend kind, the S3-compatible plugin, archive and restore flows, and the
   `archived` status.

## 6. Testing

- **Unit:**
  - Root matching by longest prefix.
  - The tiering selection matrix.
  - Verification logic.
  - The dead-mount detection decision table (empty dir with titles in DB, unreachable, OK).
- **Integration:**
  - Two local temp roots, one flagged remote: import into the remote root copies (no hardlink)
    and verifies.
  - The scan of the remote root makes no per-file stat calls beyond `DirEntry` (monkeypatch
    `os.stat` and count).
  - Emptying a root folder (simulating an unmounted share) → the scan aborts with a health error
    and **no** missing issues.
  - A tier move of a series → paths updated, source removed, `media_files` consistent, and the
    refresh event emitted.
  - A failure injected mid-copy → the source is intact and the destination is cleaned up.
- **S3 plugin:** against MinIO in CI (a docker service), or recorded responses.

## 7. Risks & open questions

- **Data loss during moves.** Mitigated by copy → verify → commit → delete ordering, audit, and
  per-run caps.
- **The rclone mount semantics zoo** (VFS cache modes, dir-cache staleness). We document the
  recommended flags (`--vfs-cache-mode full --dir-cache-time 1000h --poll-interval 15s` for
  Google Drive; `--vfs-cache-mode writes` minimum for writing targets) and test against the
  common setups.
- **Media servers must see new paths**: rule setup validates coverage with 07's library
  locations.
- **Open:** should titles span roots (e.g. seasons 1–5 cold, season 6 hot)? That's not supported:
  a title lives under exactly one root. It keeps the model and the media-server library layout
  sane.

## 8. Acceptance criteria

- Users can define multiple root folders per media type, each with a tier and a remote flag, and
  choose roots when adding media.
- A remote root is scanned without per-file API storms. Imports into it copy and verify. An
  unreachable mount never produces "missing file" issues.
- Tiering rules can be previewed, and they move titles between roots with verification. Paths
  update everywhere, and media servers are refreshed.
- *(Stage C)* Titles can be archived to S3-compatible storage and restored, with the archived
  state clearly shown.
