# 01 – Context-aware import failure diagnosis & auto-fix

> Phase 1 · Effort M (≈1.5 weeks) · Risk low · Depends on F2 (soft: shares severity/action vocabulary) · Priority #2

## 1. Summary & user value

The dashboard shows "failed imports in the last 7 days". When there are dozens of them, the user
has to open each queue row, read a one-line error, and guess the cause. Radarr and Sonarr are no
better ("Import failed, path does not exist or is not accessible by Sonarr").

This feature turns every import failure into a **diagnosis** with four parts:

- a machine-readable **reason**;
- the **evidence** gathered by probing the system;
- **suggested fixes**, each a one-click action where it's safe;
- an **aggregate view** ("71 of 93 failures are one remote path mapping problem. Fix it once.").

The biggest win is that failures usually cluster. A single wrong path mapping or permission
problem causes most of them, so fixing the root cause once clears many rows.

## 2. Current state

- Import is `import_service.import_queue_item`
  ([import_service.py:339](../../backend/services/import_service.py#L339)). Failures are raised as
  `ImportError_(str)` ([import_service.py:33](../../backend/services/import_service.py#L33)) or
  caught as generic exceptions ("Unexpected error: …"). They end up in:
  - `download_queue.status='import_failed'` and `error_message` (free text)
    ([import_service.py:394-397](../../backend/services/import_service.py#L394-L397));
  - `history` event `import_failed` with `details` = the same text;
  - `EventType.IMPORT_FAILED` with `reason` = the same text;
  - `IMPORTS_TOTAL{media_type,result}` ([observability.py:29](../../backend/core/observability.py#L29)),
    which has no reason label.
- Failure sites and their messages today (these become reason codes):

| Site | Message | Reason code |
|---|---|---|
| [import_service.py:356-357](../../backend/services/import_service.py#L356-L357) | "Download path not found: …" | `path_not_found` / `remote_path_mapping` (needs probing) |
| [import_service.py:359-363](../../backend/services/import_service.py#L359-L363) | partial files → skipped, retried | `still_downloading` |
| [import_service.py:364-365](../../backend/services/import_service.py#L364-L365) | "No video files found in download" | `no_video_files` (sub-cases: archive not extracted, only samples, disc image) |
| `inspect()` states | corrupt / too_small / sample / not_video | `corrupt_file`, `too_small`, `sample_only`, `unsupported_format` |
| [import_service.py:377-379](../../backend/services/import_service.py#L377-L379) | "cannot determine episode" | `unparseable_episode` |
| [import_service.py:221](../../backend/services/import_service.py#L221), [:243](../../backend/services/import_service.py#L243) | "… no longer exists in the library" | `media_deleted` |
| [import_service.py:292-303](../../backend/services/import_service.py#L292-L303) `_check_existing` | not allowed / duplicate / not an upgrade | `quality_not_allowed`, `duplicate`, `not_an_upgrade` |
| [import_service.py:419-421](../../backend/services/import_service.py#L419-L421) | template error | `naming_template_error` |
| `fileops.transfer` OSErrors | EACCES / ENOSPC / EXDEV / ENAMETOOLONG / EEXIST | `permission_denied`, `disk_full`, `cross_device_failed`, `path_too_long`, `destination_exists` |

- Remote path mapping already exists per download client (`remote_path` / `local_path`,
  [models.py:223-224](../../backend/db/models.py#L223-L224)) and is applied by
  `map_remote_path` ([_common.py:109](../../backend/plugins/download_clients/_common.py#L109)).
  The most common real-world failure is a missing or wrong mapping, for example when a
  Docker-based client reports `/downloads/complete/X` but Maniac sees `D:\Downloads\complete\X`.
- Retry endpoints: `POST /queue/{id}/import` re-runs the import
  ([activity.py:87](../../backend/api/v1/activity.py#L87)). `POST /queue/{id}/retry` re-sends the
  *download* ([activity.py:78](../../backend/api/v1/activity.py#L78)).

## 3. Scope

### MVP

- Structured reasons.
- A probe pipeline.
- Analysis stored on the queue item and in history.
- A fix catalogue with safe one-click actions.
- An aggregate failure summary.
- UI in Activity and on the Dashboard.
- A reason label on metrics.

### Later

- Watch-folder (untracked) failure diagnosis (`scan_download_folder`) using the same pipeline.
- Automatic archive extraction (`.rar`), which is a feature in its own right.
- Manual-import failure diagnosis (`manual_import`).

### Out

- Automatically changing settings without a click. Every settings-changing fix is user-confirmed.

## 4. Design

### 4.1 Domain – `backend/domain/diagnosis.py` (new, pure)

```python
class ImportFailureReason(StrEnum):
    PATH_NOT_FOUND = "path_not_found"
    REMOTE_PATH_MAPPING = "remote_path_mapping"
    PERMISSION_DENIED = "permission_denied"
    DISK_FULL = "disk_full"
    CROSS_DEVICE_FAILED = "cross_device_failed"
    PATH_TOO_LONG = "path_too_long"
    DESTINATION_EXISTS = "destination_exists"
    STILL_DOWNLOADING = "still_downloading"
    NO_VIDEO_FILES = "no_video_files"
    ARCHIVE_NOT_EXTRACTED = "archive_not_extracted"
    SAMPLE_ONLY = "sample_only"
    CORRUPT_FILE = "corrupt_file"
    TOO_SMALL = "too_small"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNPARSEABLE_EPISODE = "unparseable_episode"
    NOT_MATCHED = "not_matched"
    MEDIA_DELETED = "media_deleted"
    QUALITY_NOT_ALLOWED = "quality_not_allowed"
    NOT_AN_UPGRADE = "not_an_upgrade"
    DUPLICATE = "duplicate"
    NAMING_TEMPLATE_ERROR = "naming_template_error"
    UNKNOWN = "unknown"

class FixKind(StrEnum):
    RETRY = "retry"                               # plain re-run (after the user fixed something outside)
    RETRY_WHEN_STABLE = "retry_when_stable"       # schedule re-run in N minutes
    APPLY_PATH_MAPPING = "apply_path_mapping"     # set client remote/local mapping, then retry all affected
    CREATE_FOLDER = "create_folder"               # create missing destination parent
    FORCE_IMPORT = "force_import"                 # override not_an_upgrade / quality_not_allowed for this item
    MANUAL_IMPORT = "manual_import"               # deep-link to Import page with folder prefilled
    BLOCKLIST_AND_SEARCH = "blocklist_and_search" # release is bad (corrupt/sample-only/no video)
    REMOVE = "remove"                             # drop from queue (+ optionally client data)
    OPEN_SETTINGS = "open_settings"               # link to relevant settings page (naming, root folders)

@dataclass(frozen=True)
class FixSuggestion:
    kind: FixKind
    label: str                     # "Map /downloads → D:\Downloads on client 'qBittorrent'"
    params: dict[str, Any]
    safe_auto: bool                # may run without a click when strategy=auto_fix
    changes_settings: bool
    bulk_applicable: bool          # one click fixes every failure with the same signature

@dataclass(frozen=True)
class ImportFailureAnalysis:
    reason: ImportFailureReason
    summary: str                   # one human sentence
    evidence: dict[str, Any]       # probe results (paths, errno, free bytes…), never secrets
    fixes: tuple[FixSuggestion, ...]
    signature: str                 # groups identical root causes, e.g. "remote_path_mapping:client=2:/downloads"
    confidence: float              # 0..1, how sure the probe pipeline is
    analysed_at: str

def infer_path_mapping(reported: str, candidates_found: list[str]) -> tuple[str, str] | None:
    """Longest common *suffix* between the client-reported path and a path that exists locally →
    (remote_prefix, local_prefix). Pure string logic; tested with Windows/POSIX/UNC combos."""

def classify_oserror(errno_: int | None, winerror: int | None) -> ImportFailureReason
```

`classify_oserror` maps `EACCES/EPERM` → permission, `ENOSPC` → disk full, `EXDEV` → cross
device, `ENAMETOOLONG` or `winerror 206` → path too long, `EEXIST` → destination exists.

### 4.2 Structured exceptions

`ImportError_` gains optional keyword arguments. Existing `raise ImportError_("…")` calls keep
working and become `reason=UNKNOWN` until migrated.

```python
class ImportError_(RuntimeError):
    def __init__(self, message: str, *, reason: ImportFailureReason = ImportFailureReason.UNKNOWN,
                 evidence: dict | None = None) -> None
```

Every raise site listed in §2 is updated to pass its reason. `_run_import` catches `OSError`
from `FileTransaction` and re-raises as `ImportError_(reason=classify_oserror(...),
evidence={"errno":…, "path": exc.filename})`, so filesystem failures stop being "Unexpected
error".

### 4.3 Probe pipeline – `backend/services/import_diagnosis.py` (new)

`diagnose(queue_item, error: ImportError_) -> ImportFailureAnalysis` runs in the import thread
right after the failure. It's cheap (a few stat calls, no network) and each probe has a time cap.

| Reason from raise site | Probes | Output |
|---|---|---|
| `path_not_found` | 1. Is `download_path` absolute and under the client's `remote_path`? 2. Does the mapped path exist? 3. Search `settings.download_folder` (depth ≤ 3) for a directory or file with the same basename. 4. If found, `infer_path_mapping` works out the prefix pair. | Upgrades to `remote_path_mapping` with fix `APPLY_PATH_MAPPING{client_id, remote, local}` (bulk-applicable: signature groups all items from that client). If nothing is found: `PATH_NOT_FOUND` + `REMOVE` / `MANUAL_IMPORT`. |
| `no_video_files` | List the folder: any `.rar/.r00/.zip/.7z` → `ARCHIVE_NOT_EXTRACTED`; only samples → `SAMPLE_ONLY`; `.iso/BDMV/VIDEO_TS` → `UNSUPPORTED_FORMAT` (disc image) | `BLOCKLIST_AND_SEARCH`; for archives, a hint to enable extraction in the client (SABnzbd/NZBGet do it natively; qBittorrent needs a script) |
| `permission_denied` | `os.access(src, R_OK)`; `os.access(dest_parent, W_OK)`; owner uid/gid vs process uid (POSIX); container hint when `/.dockerenv` exists (PUID/PGID) | Evidence shows *which* side is the problem. Fix `RETRY` after the user corrects it. No automatic chmod, ever. |
| `disk_full` | `shutil.disk_usage(dest)` vs file size | Evidence: free vs needed. Fix `RETRY`. Links to 13/14 later. |
| `cross_device_failed` | `fileops.same_volume` | Suggest `copy` import mode, or explain the hardlink fallback |
| `destination_exists` | Is the destination a library file for a *different* title? | `MANUAL_IMPORT` |
| `still_downloading` | Partial extensions present; mtime moving | `RETRY_WHEN_STABLE` (safe_auto) |
| `corrupt_file`, `too_small`, `sample_only` | Header check result (existing); with 03, an ffprobe error | `BLOCKLIST_AND_SEARCH` (auto only if `auto_retry_failed_downloads` is on) |
| `unparseable_episode`, `not_matched` | Parse attempts for file, folder and release name, shown as evidence | `MANUAL_IMPORT` (prefilled with the folder) |
| `not_an_upgrade`, `duplicate`, `quality_not_allowed` | Existing vs candidate quality (and format score after 02) | `FORCE_IMPORT` (explicit click; recycles the existing file as usual) and `REMOVE` |
| `naming_template_error` | Render the template with sample values | `OPEN_SETTINGS` → naming |
| `media_deleted` | – | `REMOVE` |
| `unknown` | Exception type and message | `RETRY`; a stack excerpt is logged, not stored |

Evidence never includes URLs or credentials. It only holds paths, which are the user's own
filesystem and already visible in the queue.

### 4.4 Data model – migration `NNNN_import_failure_analysis`

- `download_queue.failure_reason` String(32), index. This is queryable, so it's a real column.
- `download_queue.failure_signature` String(128), index.
- `download_queue.failure_analysis` JSON (the full `ImportFailureAnalysis`).
- `history.data["analysis"]` holds the same JSON on the `import_failed` event (no schema change).

Promoting reason and signature to columns makes the aggregate a plain `GROUP BY` that's portable
across all engines. That follows the convention of not filtering on JSON paths.

### 4.5 Services – fixes & retries

`import_diagnosis.apply_fix(queue_id, fix_kind, params, actor, *, bulk: bool)`:

- `APPLY_PATH_MAPPING`: validates that the local path exists, then updates the
  `DownloadClient.remote_path` and `local_path` (audited in the `settings` category). If
  `bulk=True`, it re-enqueues `import_download` for every `import_failed` item with the same
  `failure_signature`.
- `CREATE_FOLDER`: `mkdir` of the destination parent, but only inside a configured root folder.
  Then retry.
- `FORCE_IMPORT`: re-runs the import with `ImportOptions(force=True)`, which skips the upgrade
  check in `_check_existing` but still recycles the replaced file and records history event
  `imported` with `data.forced=true`.
- `RETRY_WHEN_STABLE`: `job_queue.enqueue("import_download", …, delay_seconds=300,
  dedupe_key=f"{id}")`.
- `BLOCKLIST_AND_SEARCH`: delegates to `download_service.handle_failed(queue_id, reason,
  release_at_fault=True)`.

**Auto-fix strategy.** A new setting, `auto_fix_imports: bool = True`. When it's on, only fixes
with `safe_auto=True` (`RETRY_WHEN_STABLE`, and `BLOCKLIST_AND_SEARCH` when
`auto_retry_failed_downloads` is set) run immediately after diagnosis. A per-item counter
(`retry_count`) caps them at 3. Anything that changes settings or overrides a quality decision
always needs a click.

**Aggregate.** `import_diagnosis.summary(db, days=7)` groups by `failure_reason` and
`failure_signature` across the current queue plus history, and returns the top signatures with
count, example titles, and the best bulk fix.

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/queue` | QUEUE_READ | Items gain `failure_reason`, `failure_analysis` |
| POST | `/queue/{id}/import` | IMPORT | Optional body `{strategy: "default" \| "auto_fix" \| "force", fix?: {kind, params}}`. Empty body keeps today's behaviour. |
| POST | `/queue/{id}/diagnose` | QUEUE_READ | Re-run the probes, e.g. after the user fixed permissions. Returns the analysis. |
| POST | `/queue/fixes` | IMPORT (+ SETTINGS_WRITE when `changes_settings`) | `{signature, kind, params}` → bulk apply to all items with that signature |
| GET | `/activity/failures/summary?days=7` | QUEUE_READ | `{total, by_reason: [{reason, count}], top_signatures: [{signature, reason, count, examples[], fix}]}` |
| GET | `/history?event_type=import_failed` | existing | Rows now include `data.analysis` |

### 4.7 Frontend

- **Activity → Queue** ([ActivityPage.tsx](../../frontend/src/pages/ActivityPage.tsx)):
  `import_failed` rows get a reason chip (icon and colour by category: *Setup* for mapping,
  permission and disk; *Release* for corrupt, sample or no video; *Library* for not-an-upgrade or
  not matched). Expanding a row shows the summary sentence, an evidence table and fix buttons.
  Buttons that change settings open a confirmation dialog showing the exact change (e.g. "Client
  qBittorrent: remote `/downloads` → local `D:\Downloads`").
- **Activity → "Failures" tab (new)**: the summary grouped by signature, with "Fix all N" buttons.
- **Dashboard** ([DashboardPage.tsx](../../frontend/src/pages/DashboardPage.tsx)): the
  existing failed-imports card gains a top-3 reasons breakdown and links to the Failures tab.
- **Import page**: accepts `?folder=` so the `MANUAL_IMPORT` deep link pre-fills it.

### 4.8 Events, notifications, metrics

- `IMPORT_FAILED` payload gains `reason_code`, `summary` and `fix_available`. Notification
  channels include the summary sentence instead of the raw exception.
- New counter `maniac_import_failures_total{reason}`. `IMPORTS_TOTAL` is unchanged.
- The health check ([health_service.py:31](../../backend/services/health_service.py#L31)) gets a
  new check: "N imports failing with remote path mapping on client X". It shows up on the System
  page and fires `HEALTH_ISSUE`.

### 4.9 Security

- `APPLY_PATH_MAPPING` needs `SETTINGS_WRITE`, and its local path must exist and be a directory.
- `CREATE_FOLDER` is restricted to paths under the configured media roots (`Path.resolve()` +
  `relative_to`, the same pattern as `delete_candidates`,
  [import_service.py:509-517](../../backend/services/import_service.py#L509-L517)).
- The folder search in the probes is bounded (depth ≤ 3, ≤ 5,000 entries) so it can't be used
  for filesystem enumeration beyond the download folder.

## 5. Implementation milestones

1. **Domain + structured raise sites**: `diagnosis.py`, `ImportError_` kwargs, and reasons at
   every raise site plus the OSError mapping. Unit tests for `classify_oserror` and
   `infer_path_mapping`.
2. **Probe pipeline + persistence**: migration, `diagnose()`, storage on queue and history, and
   the metric.
3. **Fix actions + API**: `apply_fix`, extended `/queue/{id}/import`, `/diagnose`, `/queue/fixes`,
   and the summary endpoint.
4. **Auto-fix strategy**: the setting, safe-auto execution and retry caps.
5. **UI**: row detail, Failures tab, dashboard card, Import deep link.
6. **Health check + notification payloads.**

## 6. Testing

- **Unit** (`tests/unit/test_diagnosis.py`), with table-driven `infer_path_mapping` cases:
  - `/downloads/complete/X` + `D:\Downloads\complete\X` → (`/downloads`, `D:\Downloads`);
  - `/data/torrents/tv/X` + `/mnt/user/data/torrents/tv/X`;
  - UNC `\\nas\downloads\X`;
  - no common suffix → `None`.

  Plus `classify_oserror` for POSIX errnos and Windows winerrors.
- **Integration** (`tests/integration/test_downloads.py`), using the simulator client:
  - Set a bogus `remote_path` on the client → complete a download → the item fails with
    `remote_path_mapping` and a correct suggested mapping → `POST /queue/fixes` applies it → the
    item imports.
  - Read-only destination (chmod on POSIX; skipped on Windows) → `permission_denied` with
    evidence pointing at the destination.
  - A lower-quality file over an existing one → `not_an_upgrade` → `strategy=force` imports and
    recycles the old file.
  - A folder containing only `.rar` → `archive_not_extracted`.
  - Summary groups 3 failures that share a signature into one row.
- **E2E:** Activity shows the reason chip and the fix dialog, and the Failures tab renders.

## 7. Risks & open questions

- **Wrong mapping inference.** Mitigated: it's never auto-applied, the dialog shows the exact
  change, and the local path must exist.
- **Probe cost on large download folders.** Mitigated by bounded search.
- **Open:** should `FORCE_IMPORT` be available to `power_user`, or admin only? Recommendation:
  `IMPORT` permission, since it's reversible via the recycle bin.

## 8. Acceptance criteria

- Every `import_failed` item has a `failure_reason` other than `unknown` for all failure sites
  listed in §2.
- A misconfigured remote path mapping is detected, the correct mapping is suggested, and one
  click fixes and re-imports every affected item.
- The dashboard shows failures broken down by reason. The Failures tab groups by root cause.
- No fix that changes settings or overrides quality runs without explicit user action.
