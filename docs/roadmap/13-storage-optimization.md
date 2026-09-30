# 13 – Smart storage optimization (report, strip tracks, transcode)

> Phase 5 · Effort XL (4+ weeks, three stages) · Risk **high** (rewrites media) · Depends on F1, 03 (hard), F2, 12 (invoker / long-running lane) · Priority #16

## 1. Summary & user value

Large libraries (the dashboard screenshot showed 22.5 TB) waste a lot of space:

- REMUX-sized files for content nobody rewatches;
- H.264 where HEVC or AV1 would halve the size;
- eight audio tracks when the household only speaks one language;
- twenty subtitle streams;
- uncompressed extras.

People use **Tdarr** or **Unmanic**, which run separate flows that fight with Radarr/Sonarr (for
example, a transcoded file no longer matches what the Arr expects). Building this into Maniac
means the library database, the quality profile, custom formats, the recycle bin and the media
server refresh all stay consistent.

It ships in three stages, in order of risk:

| Stage | What | Risk |
|---|---|---|
| **A. Report** | Storage policies evaluated against inspection data. "Savings potential" per policy, title and folder. A storage heatmap. No file changes. | None |
| **B. Strip** | Remove unwanted audio and subtitle tracks by **remuxing** (`-c copy`). Fast, lossless for what's kept. | Medium |
| **C. Transcode** | Re-encode video to a target codec and quality (HEVC/AV1, CRF), with optional hardware acceleration. Slow, lossy, CPU/GPU heavy. | High |

## 2. Current state

- Storage totals by media type, and disk usage per root, in `dashboard_service.stats`
  ([dashboard_service.py:41-48](../../backend/services/dashboard_service.py#L41-L48),
  [:69](../../backend/services/dashboard_service.py#L69)).
- There's no per-file stream data until 03, and no ffmpeg in Docker until 03 (ffprobe only).
- Job queue: 4 asyncio workers ([queue.py:167-168](../../backend/workers/queue.py#L167-L168)).
  Exclusive-by-name. **A multi-hour transcode job would occupy a worker slot the whole time.**
- `FileTransaction` provides atomic placement and rollback. The recycle bin provides reversibility.

## 3. Scope

### MVP (stage A + B)

- Storage policies.
- Evaluation and savings estimates.
- Candidate issues.
- Heatmap and savings dashboard.
- Track stripping by remux with verification.
- A long-running job lane.
- Processing windows.

### Stage C

- Video transcoding (software: libx265, libsvtav1).
- Verification.
- Hardware acceleration (NVENC, QSV, VAAPI) as a later milestone.

### Later

- Compressing or deleting extras.
- Audio normalisation (loudnorm). This is lossy for audio, and opt-in.

### Out

- Distributed transcoding across nodes (the Tdarr worker model).
- Live or streaming transcode (that's the media server's job).

## 4. Design

### 4.1 Domain – `backend/domain/storage_policy.py` (new, pure)

```python
@dataclass(frozen=True)
class StoragePolicy:
    id: int
    name: str
    scope: PolicyScope                 # media_type, root folder(s), quality profile ids, tags, min_age_days
    keep_audio_languages: tuple[str, ...] = ()   # empty = keep all
    keep_original_language_audio: bool = True    # always keep the metadata original language
    keep_commentary: bool = False
    keep_subtitle_languages: tuple[str, ...] = ()
    keep_forced_subtitles: bool = True
    video: VideoTarget | None = None             # stage C
    min_savings_pct: int = 15                    # skip if estimated savings below
    only_watched: bool = False                   # with 07: only optimise content already watched
    skip_hdr: bool = True                        # stage C: never re-encode HDR/DV by default
    skip_remux_sources: bool = False             # stage C: protect Remux quality files

@dataclass(frozen=True)
class VideoTarget:
    codec: str                    # hevc | av1
    crf: int                      # e.g. 22 for hevc, 30 for av1
    preset: str                   # "medium" / svt preset "6"
    max_height: int | None        # e.g. 1080 → downscale 2160 content (opt-in)
    only_if_codec_in: tuple[str, ...] = ("h264", "mpeg2video", "vc1", "mpeg4")
    max_bitrate_kbps: dict[str, int] | None = None   # per resolution class: re-encode only above

@dataclass(frozen=True)
class PlannedAction:
    kind: str                     # strip_tracks | transcode
    media_file_id: int
    remove_audio: tuple[int, ...] # stream indexes
    remove_subs: tuple[int, ...]
    video: VideoTarget | None
    estimated_bytes_after: int
    reasons: tuple[str, ...]

def plan(file: FileFacts, policy: StoragePolicy) -> PlannedAction | None
def estimate_strip_savings(file: FileFacts, remove_audio, remove_subs) -> int      # stream bitrate × duration
def estimate_transcode_size(file: FileFacts, target: VideoTarget) -> int          # lookup table by codec/res/crf, conservative
def ffmpeg_args_strip(src, dst, keep_streams) -> list[str]
def ffmpeg_args_transcode(src, dst, keep_streams, target, hw: str | None) -> list[str]
def verify(before: MediaInspection, after: MediaInspection, action: PlannedAction) -> list[str]   # problems
```

**Safety rules built into `plan()`**, which are never overridable per file:

- Never remove the **last** audio track, or all audio in the original language.
- Never remove the default audio track unless a kept track can become the default.
- Never re-encode HDR/DV content when `skip_hdr` is set (the default), since tone mapping and
  metadata loss are too easy to get wrong.
- Never touch **hardlinked** files (`nlink > 1`, i.e. still seeding). The new file would double the
  used space, and it breaks torrent seeding.
- Skip files currently being imported, renamed or queued for an upgrade.

`verify()` checks that:

- the output duration is within ±1 % (or ±2 s);
- the video stream is present;
- every kept audio and subtitle stream is present with the same language and codec (strip);
- the codec matches the target (transcode);
- the output size is at least 1 MB and smaller than the input;
- ffprobe parses the output.

### 4.2 Data model – migration `NNNN_storage_optimization`

`storage_policies`: `id`, `name`, `enabled`, `priority`, `scope` JSON, `rules` JSON
(StoragePolicy fields), `mode` (`report` \| `confirm` \| `auto`), `created_at`, `updated_at`.

`storage_actions` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `policy_id` | FK | |
| `media_file_id` | FK media_files.id, index | |
| `kind` | String(16) | strip_tracks \| transcode |
| `plan` | JSON | the `PlannedAction` |
| `status` | String(16), index | `planned` \| `approved` \| `queued` \| `running` \| `verifying` \| `done` \| `failed` \| `skipped` \| `reverted` |
| `bytes_before` / `bytes_estimated` / `bytes_after` | BigInteger | |
| `progress` | Float | |
| `started_at` / `finished_at` | DateTime | |
| `error` | Text | |
| `recycled_path` | String(1024) | where the original went (for revert) |

Settings added:

- `storage_processing_window` (e.g. `"01:00-07:00"`, empty = always);
- `storage_max_concurrent` (1);
- `ffmpeg_path`;
- `storage_temp_dir` (default: alongside the file, `.maniac-partial` in the same folder so the
  final rename is atomic);
- `storage_hwaccel` (none \| nvenc \| qsv \| vaapi);
- `storage_daily_limit_gb` (safety cap).

### 4.3 Long-running job lane

Extend the job system ([queue.py](../../backend/workers/queue.py)):

- `job_queue.register(..., lane="long")` on a job definition.
- `WorkerPool` gains a separate lane pool of size `storage_max_concurrent` (default 1). `claim()`
  takes a `lane` argument, so long jobs never occupy the four standard workers, and standard jobs
  never wait behind a transcode.
- Cooperative cancellation: `JobContext.cancelled` is polled by the handler. On cancel, the ffmpeg
  process is terminated and the partial output removed.
- Processing windows: the handler checks the window before starting each file. Outside the window
  it re-enqueues itself with `run_after` = the next window start. A running file is allowed to
  finish (or, optionally, is killed and resumed later from scratch).

### 4.4 Services – `storage_service.py`

- `evaluate(policy_id | all)` → for each file in scope with inspection data (03), `plan()`. It
  upserts `storage_actions` with `status=planned` (or reports them via the F2 issue
  `storage_optimization_candidate` with savings in `data`).
- `summary()` → savings potential totals by policy, root folder, media type and the top 50 titles.
- `approve(ids)`, or `mode=auto` → `queued` → enqueue `storage_process` jobs (lane long), within
  the daily GB cap.
- `process(action_id)`:
  1. Re-inspect the source (it might have changed since planning). Re-plan, and skip if no longer
     applicable.
  2. Check free space: at least the estimated output + 10 %, on the target volume.
  3. Run ffmpeg via `create_subprocess_exec`: `-hide_banner -nostdin -y -progress pipe:1`, and
     `-protocol_whitelist file` with the `file:` prefix for input, as in 03. Parse progress
     (`out_time_ms / duration`) into `JobContext.progress`. Timeout = duration × a factor
     (e.g. 3× realtime for software HEVC).
  4. Write the output to `<final>.maniac-partial`.
  5. ffprobe the output, then run `verify(before, after)`. On failure, delete the partial and mark
     `failed` with reasons.
  6. `FileTransaction`: recycle the original, then place the new file at the original path (same
     name, same extension or `.mkv` if the container changes; the renamer handles naming).
  7. Update `media_files` (size, mtime, codec) and re-run inspection. Record history event
     `optimized` (`data`: before/after bytes, action) and `audit()`.
  8. Media-server refresh (07).
  9. `revert(action_id)` restores the original from the recycle bin while it's still retained,
     via `FileTransaction`.
- Quality bookkeeping: a transcoded file keeps its **quality** (Bluray-1080p stays Bluray-1080p;
  the source didn't change), but its **codec** changes. A custom-format score is recomputed from
  inspection (not the scene name) and flagged `optimized=true` so upgrade logic doesn't
  immediately "upgrade back" to an x264 release (`media_files.optimized` Boolean). Search treats
  `optimized` files as cutoff-met unless the user explicitly searches.

### 4.5 Jobs

| Job | Lane | Schedule |
|---|---|---|
| `storage_evaluate` | standard | 24 h + after `media_inspection` backlog completes |
| `storage_process` | long | event (approved actions), respects window |

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET/POST/PUT/DELETE | `/storage/policies` | SETTINGS_* | validation: HDR, audio safety |
| POST | `/storage/policies/{id}/evaluate` | LIBRARY_SCAN | returns planned count and savings |
| GET | `/storage/summary` | MEDIA_READ | savings potential, by-root, by-type, top titles |
| GET | `/storage/heatmap` | MEDIA_READ | size by root/series/year and growth per week (from `history` imports' sizes; `history.data.size_bytes` added at import) |
| GET | `/storage/actions` | MEDIA_READ | filter status/policy |
| POST | `/storage/actions/approve` / `/reject` / `/cancel` / `/revert` | new `STORAGE_MANAGE` (admin) | |

### 4.7 Frontend

- **Settings → Storage policies**: the policy editor has these sections:
  - scope;
  - audio: language multi-select, keep original language, keep commentary;
  - subtitles;
  - video (stage C, collapsed and marked "Advanced – re-encodes video"): codec, CRF slider with
    guidance text, preset, max height, skip HDR, skip Remux;
  - min savings;
  - mode (Report / Confirm / Auto);
  - the processing window.

  The **Preview** button shows the planned actions and total savings before the policy is saved.
- **Storage page** (`/storage`, new):
  - a heatmap (a treemap by root → series/movie, sized by bytes and coloured by savings potential
    or growth);
  - a growth chart (bytes added per week);
  - a savings-potential card per policy;
  - an actions queue with progress bars, approve/cancel/revert, and before/after sizes.

  Charts follow a single visual system (the dataviz conventions).
- **Dashboard**: a "Savings potential: X TB" card, and "Saved so far: Y TB".
- **Movie detail**: the Files card shows an "Optimized" badge and the history of actions.

### 4.8 Events, metrics

- Events: `STORAGE_ACTION_COMPLETED`, `STORAGE_ACTION_FAILED`.
- Metrics:
  - `maniac_storage_bytes_saved_total{kind}`;
  - `maniac_storage_actions_total{kind,status}`;
  - `maniac_storage_savings_potential_bytes{policy}`.

### 4.9 Docker

Stage B/C needs full **ffmpeg** in the image (replacing the static ffprobe from 03), or a
separate `-full` image tag to keep the default image slim. **Recommendation:** ship `ffmpeg` in
the default image (about 80 MB is acceptable next to media sizes). Hardware acceleration needs
device mappings (`/dev/dri` for VAAPI/QSV, the NVIDIA container runtime for NVENC), documented in
`docker-compose.yml` comments.

## 5. Implementation milestones

**Stage A – report (≈1 week)**

1. Domain `plan()` / estimates / safety rules (strip only) plus policy CRUD and the evaluate job.
2. Summary and heatmap APIs, the Storage page, and the dashboard card. Candidate issues.

**Stage B – strip (≈1.5 weeks)**

3. Long-running lane in the worker pool, cancellation and processing windows.
4. `process()` for strip_tracks: ffmpeg remux, verify, FileTransaction swap, revert.
5. Approve/confirm flows, caps, audit, notifications and media-server refresh.

**Stage C – transcode (≈2 weeks)**

6. Transcode args (x265, SVT-AV1), size estimation table, and transcode verification.
7. The `optimized` flag in upgrade logic and search.
8. *(Later)* Hardware acceleration presets, and detection of available encoders
   (`ffmpeg -encoders`).

## 6. Testing

- **Unit:**
  - The `plan()` safety matrix (last audio, original language, HDR skip, hardlinked, commentary).
  - Savings estimates are monotonic and conservative.
  - ffmpeg argv builders (stream maps, no shell, whitelist).
  - The `verify()` matrix.
- **Integration** (ffmpeg **real** where available, marked `ffmpeg` and skipped otherwise): tiny
  test files are generated with `ffmpeg -f lavfi` (a 5 s colour bar video plus two audio tracks
  tagged eng/jpn plus a subtitle).
  - Strip jpn audio → verify passes → the original is in the recycle bin → `media_files` is
    updated.
  - Corrupt output (simulated by truncating the partial) → verify fails, the original is
    untouched.
  - Cancel mid-run → the partial is removed.
  - Revert restores the original.
  - The window defers processing.
- **Lane test:** a long job running doesn't block `download_monitor` jobs.
- **E2E:** the policy preview renders the planned savings table.

## 7. Risks & open questions

- **Quality loss, or a broken file, replacing the original.** Mitigated by the verification gate,
  the recycle bin (with retention to cover a revert window, recommended ≥ 14 days for optimised
  originals via a separate `storage_recycle_days`), confirm mode by default, the daily cap, and
  HDR skipped by default.
- **Double storage during processing:** the output is written alongside the original, and there's
  a free-space check.
- **CPU saturation** on NAS hardware: concurrency 1, processing windows, and ffmpeg `-threads`
  configurable.
- **Seeding breakage:** hardlinked files are skipped.
- **Open:** keep a "never optimise" tag per title (reuse the `keep` tag from 07)? Recommended:
  yes.

## 8. Acceptance criteria

- **Stage A:** users see accurate savings potential per policy and folder without any file being
  modified.
- **Stage B:** unwanted tracks are removed losslessly. Each result is verified before replacing
  the original, the original can be restored while it's in the recycle bin, and the library,
  media server and history reflect the change.
- **Stage C:** video re-encoding respects HDR and Remux protections and the processing windows,
  and never runs without approval unless the policy is explicitly set to auto.
- Long-running optimisation never delays download monitoring or imports.
