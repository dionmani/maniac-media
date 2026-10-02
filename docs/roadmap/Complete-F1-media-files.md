# F1 – `media_files` foundation

> Phase 2 · Effort M-L (≈2 weeks) · Risk medium (data migration) · Unlocks 03, 05, 06, 10, 13, 14

## 1. Summary & user value

Maniac has no concept of a *file*. A movie *is* its file (`movies.file_path`, `quality`,
`file_size_bytes`), and each episode row carries its own copy of the file it lives in. That's fine
for "one movie, one file", but it can't represent any of these:

- **several files for one title**: duplicates (05), editions (06), a 4K and a 1080p copy;
- **facts about a file** that don't belong on the title: ffprobe results (03), custom-format
  matches (02), subtitles (10), storage actions (13), the storage tier (14);
- **multi-episode files** as one entity. Today `S01E01-E02.mkv` is stored twice and its size is
  double-counted unless the query de-dupes by path, as the dashboard has to
  ([dashboard_service.py:42-48](../../backend/services/dashboard_service.py#L42-L48)).

Users won't see anything from this directly. It's pure groundwork, and it's the most important
piece of it.

## 2. Current state

- `Movie`: `path`, `file_path`, `file_size_bytes`, `has_file`, `quality`
  ([models.py:30](../../backend/db/models.py#L30)).
- `Episode`: `file_path`, `file_size_bytes`, `has_file`, `quality`, `status`
  ([models.py:131](../../backend/db/models.py#L131)).
- **Writers** (every place that sets those columns):
  - import: [import_service.py:229-234](../../backend/services/import_service.py#L229-L234) and
    [import_service.py:278-279](../../backend/services/import_service.py#L278-L279);
  - scanner: [scanner_service.py:176-180](../../backend/services/scanner_service.py#L176-L180) and
    [scanner_service.py:295](../../backend/services/scanner_service.py#L295);
  - renamer: `renamer_service.execute`;
  - delete: `media_service.delete_movie` / `delete_series`, and missing-file detection in the
    scanner.
- **Readers**: the decision engine (via `search_service.build_target`), `_annotate_library_state`,
  dashboard, wanted/cutoff, the frontend detail pages, and the renamer preview.

## 3. Scope

### MVP

- `media_files` and `episode_files` tables.
- `media_file_service` as the single write path.
- Dual-write to the legacy columns.
- Backfill migration.
- Read APIs that expose `files[]` on movie and episode detail.

### Later

- Switch individual readers to `media_files` one by one (e.g. the dashboard's storage sum).
- Eventually deprecate the legacy columns. That won't happen before v3, because migrations are
  additive only.

### Out

- Non-video extras (trailers, featurettes) as tracked files. They can be added later as
  `role='extra'`.

## 4. Design

### 4.1 Domain

No new domain module. `ParsedQuality` is still the quality representation. Add one helper to
`domain/parser.py`:

```python
def normalise_path_key(path: str) -> str   # normcase + normpath + casefold, forward slashes
```

It's shared with `domain/issues.fingerprint` (F2) and the `path_hash` computation.

### 4.2 Data model – migration `NNNN_media_files`

**`media_files`**

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `media_type` | String(16) | `movie` \| `tv` |
| `movie_id` | FK movies.id, nullable, index | set when media_type=movie |
| `series_id` | FK series.id, nullable, index | set when media_type=tv |
| `path` | String(1024) | absolute path |
| `path_hash` | String(64), **unique** | sha256(`normalise_path_key(path)`) |
| `relative_path` | String(1024) | relative to the media folder (survives root-folder moves, 14) |
| `size_bytes` | BigInteger | |
| `mtime` | Float | detects in-place changes (triggers re-inspection, 03) |
| `quality` | String(64) | name from `QUALITIES` |
| `codec` | String(16) | from `ParsedQuality.codec` |
| `revision` | Integer default 1 | PROPER/REPACK |
| `release_group` | String(64) | |
| `scene_name` | String(1024) | **original release name** at import. Needed to re-evaluate custom formats (02) after rename. |
| `edition` | String(64), nullable | 06 |
| `custom_formats` | JSON | list of matched format ids (02) |
| `custom_format_score` | Integer, nullable | score under the owner's profile at import time |
| `role` | String(16) default `main` | `main` \| `extra` (reserved) |
| `is_primary` | Boolean default true | the file legacy columns mirror; false for duplicates/other editions |
| `added_at` | DateTime | |
| `updated_at` | DateTime | |
| `source` | String(16) | `import` \| `scan` \| `manual` |

Indexes: `(movie_id, is_primary)` and `(series_id)`. Columns reserved for 03 get a separate
migration there, so this one stays small.

**`episode_files`** (link table, supports multi-episode files)

| Column | Type |
|---|---|
| `id` | Integer PK |
| `media_file_id` | FK media_files.id, index, `ON DELETE CASCADE` handled in ORM (`cascade="all, delete-orphan"`) |
| `episode_id` | FK episodes.id, index |
| unique | (`media_file_id`, `episode_id`) |

ORM: `MediaFile.episodes` (secondary relationship), `Movie.files`, `Episode.files`.

**Backfill** runs in Python inside the migration, in batches of 1,000:

- Each movie with `has_file` gets one `media_files` row (`source='scan'`,
  `scene_name = basename(file_path)` as the best available value).
- Episodes are grouped by `file_path`: one `media_files` row per distinct path, plus
  `episode_files` links for every episode sharing it. Size is taken once.
- Rows whose `path_hash` collides (two movies claiming the same file, which is possible today)
  are logged and linked to the lowest id. The collision is reported as a `duplicate_file` issue
  on the next scan.

### 4.3 Services – `backend/services/media_file_service.py` (new)

This becomes the single place that creates, updates and removes file records. It **always**
updates the legacy columns in the same session.

```python
def attach_movie_file(db, movie, path: Path, quality: ParsedQuality, *, scene_name: str | None,
                      source: str, make_primary: bool = True, edition: str | None = None) -> MediaFile
def attach_episode_file(db, series, episodes: list[Episode], path: Path, quality, *, scene_name, source) -> MediaFile
def move_file(db, media_file, new_path: Path) -> None          # renamer; updates path/hash/relative + legacy
def detach_file(db, media_file, *, reason: str) -> None         # missing/deleted; recomputes legacy has_file
def set_primary(db, media_file) -> None                          # 05/06
def sync_legacy(db, owner) -> None                               # recompute Movie/Episode legacy columns from primary file
```

Legacy mirror rules:

- `Movie.has_file` = a primary file exists. `file_path`, `file_size_bytes` and `quality` come
  from the primary file.
- For an episode, `has_file` = any linked file exists. Legacy columns come from the primary
  linked file.

Call-site changes:

| Caller | Change |
|---|---|
| `import_service._import_one` | Replace the direct column writes with `attach_*_file(...)`, passing `scene_name=original_name`. The replaced file's `media_files` row is removed with `detach_file(reason="upgraded")` after `tx.recycle`. |
| `scanner_service._scan_movie_folder` / `_scan_series_folder` | Upsert by `path_hash`. Extra full-size files are recorded with `is_primary=False` instead of being forgotten (this is what 05 consumes). Missing-file detection calls `detach_file`. |
| `renamer_service.execute` | `move_file` for every rename, inside the same transaction as the filesystem op. |
| `media_service.delete_movie/delete_series` | `detach_file` for recycled files. Cascade on title delete. |

### 4.4 Repositories

Add `MediaFileRepository` in [repositories.py](../../backend/db/repositories.py):

- `by_path(path)`;
- `for_movie(movie_id)`;
- `for_episode(episode_id)`;
- `for_series(series_id)`;
- `total_bytes(media_type)`: a de-duplicated SUM, which replaces the dashboard's sub-select;
- `uninspected(limit)` for 03;
- `sortable = {"size": ..., "added": ..., "quality": ...}`.

### 4.5 API

- `GET /movies/{id}` and `GET /series/{id}/episodes`: add `files: [{id, path, size_bytes,
  quality, codec, release_group, scene_name, edition, is_primary, added_at}]`. Existing fields
  stay.
- `GET /media-files/{id}`: a single file with its links (used by 03/05/10 panels).
- `GET /media-files?movie_id=|series_id=|episode_id=`: paginated.
- Permissions: `MEDIA_READ`.

### 4.6 Frontend

- Add the `MediaFile` type.
- Movie detail: a "Files" card listing every file (primary first), size and quality. A badge
  shows "+N other files" when there are duplicates, linking to 05 later.
- Series episode rows: a tooltip with the file name. Multi-episode files show the linked episodes.

## 5. Implementation milestones

1. **Models, migration and backfill**, plus a migration test on a fixture DB (movies, a
   multi-episode file, and a path collision).
2. **`media_file_service` + import path**: dual-write and upgrade detach. Pipeline integration
   tests stay green.
3. **Scanner + renamer + delete**: upsert by hash, record non-primary extras, detach on missing.
4. **Read APIs + UI "Files" card**; the dashboard uses `total_bytes`.
5. **Consistency checker** (housekeeping job step): legacy columns vs primary file. Mismatches are
   logged and repaired via `sync_legacy`. This is a safety net while the dual-write period lasts.

## 6. Testing

- **Unit:** `normalise_path_key` (Windows drive-letter case, UNC, trailing slashes, `\\?\` long
  paths).
- **Integration:**
  - Import a single movie → one file, legacy columns equal.
  - Upgrade → old row gone, new row primary, recycle bin has the old file.
  - Multi-episode import → one file, two links, dashboard bytes counted once.
  - Rename → path/hash updated.
  - Scan a folder with two full-size files → one primary and one non-primary.
  - Delete a file on disk → scan detaches it and the legacy `has_file` flips.
- **Engines:** `test_databases.py` runs the migration on PostgreSQL/MySQL/MSSQL (the unique
  `path_hash` is fine everywhere).

## 7. Risks & open questions

- **Dual-write drift:** mitigated by the single write path and the consistency checker in
  milestone 5.
- **Backfill time on large libraries** (e.g. 16k episodes): a batched Python backfill is about
  seconds to a minute on SQLite. The migrator already backs up first.
- **Hardlinked copies** (torrent seeding) are one inode in two places. Only the library path is
  recorded, which is correct.
- **Open:** should `relative_path` be relative to the media folder (`movie.path`) or to the root
  folder? Recommendation: the root folder, so 14 can relocate roots by changing one row.

## 8. Acceptance criteria

- Every file-bearing movie and episode has exactly one primary `media_files` row after migration.
- All existing tests pass unchanged, which shows the legacy columns are still correct.
- Multi-episode files are one row, and library size is no longer double-counted.
- Scanner-detected extra full-size files persist as non-primary rows.
