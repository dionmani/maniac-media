# 03 – Media inspection via ffprobe

> **Status: Complete (2026-10-16) – milestones 1–7 done; auto-relabel (opt-in) remains a later idea** · Phase 2 · Effort M (1.5–2 weeks) · Risk low · Depends on F1 (media_files), F2 (issue types) · Unlocks 04, 05, 10, 13 · Priority #5

## 1. Summary & user value

Today everything Maniac knows about a file comes from its **name**. A file called
`Movie.2019.1080p.BluRay.x264.mkv` might really be a 720p upscale. It might have no English
audio, or be an episode from the wrong season, or be truncated. Radarr/Sonarr users currently
need Tdarr, MediaInfo scripts or manual checks to find out.

Running **ffprobe** on each library file gives the real stream facts:

- container, duration and bitrate;
- video codec, resolution, bit depth, HDR format and frame rate;
- audio tracks (codec, channels, language, commentary);
- subtitle tracks (language, forced, SDH);
- chapters.

Comparing those facts with the filename and the metadata catches several problems:

- **Mislabelled quality**: the name says 1080p, the stream is 720p.
- **Wrong episode or movie**: the duration differs from the metadata runtime by more than a
  tolerance. This is the practical, deterministic core of the brainstorm's "AI metadata
  correction".
- **Missing audio**, or no audio in a preferred language.
- **Corrupt or truncated files**: ffprobe fails, or the reported duration is much shorter than
  expected.

Other features build on this data: the health score (04), duplicate comparison (05), subtitle
rules like "only when audio isn't English" (10), and storage savings estimates (13).

## 2. Current state

- Validity checking is header-magic only: `fileops.check_video_file`
  ([fileops.py:106](../../backend/services/fileops.py#L106)) verifies EBML/ftyp/RIFF bytes.
- Quality is always parsed from names: `parse_quality` in the scanner
  ([scanner_service.py:175](../../backend/services/scanner_service.py#L175)) and in import
  (`inspect()`, [import_service.py:104](../../backend/services/import_service.py#L104)).
- Runtime metadata exists: `movies.runtime_minutes`, `episodes.runtime_minutes`, and
  `series.runtime_minutes` as a fallback.
- The Docker image doesn't ship ffmpeg ([Dockerfile](../../Dockerfile)).

## 3. Scope

### MVP

- ffprobe discovery and health check.
- A pure parser of the ffprobe JSON.
- Inspection stored on `media_files`.
- Post-import inspection job.
- Backlog inspection task.
- Mismatch detection recorded as issues (`quality_mismatch`, `runtime_mismatch`,
  `missing_audio`, `language_missing`, `truncated_file`, `invalid_media`).
- A "Technical info" panel.
- Naming tokens for MediaInfo.

### Later

- Pre-import ffprobe validation, so a corrupt download is rejected before it's placed (toggle).
- Optional quick decode test (`ffmpeg -v error -i f -t 30 -f null -`) for deeper corruption
  checks.
- Using actual resolution to *correct* the stored quality automatically (opt-in).

### Out

- Full decode integrity scans. They're too expensive for large libraries and would be a separate
  manual tool.

## 4. Design

### 4.1 Domain – `backend/domain/inspection.py` (new, pure)

```python
@dataclass(frozen=True)
class VideoStream:
    codec: str            # hevc | h264 | av1 | mpeg2video | vc1 …
    width: int
    height: int
    bit_depth: int | None
    frame_rate: float | None
    bitrate: int | None
    hdr_format: str | None  # "DV", "DV HDR10", "HDR10+", "HDR10", "HLG", None
    profile: str | None

@dataclass(frozen=True)
class AudioStream:
    codec: str            # truehd | eac3 | ac3 | dts | aac | flac | opus …
    profile: str | None   # "DTS-HD MA", "Atmos" (from profile/title)
    channels: int
    layout: str | None    # "7.1", "5.1(side)", "stereo"
    language: str | None  # ISO 639-2 as tagged; "und" → None
    title: str | None
    default: bool
    commentary: bool      # disposition.comment or title matches /commentary/i

@dataclass(frozen=True)
class SubtitleStream:
    codec: str            # subrip | ass | hdmv_pgs_subtitle | dvd_subtitle
    language: str | None
    forced: bool
    hearing_impaired: bool
    default: bool

@dataclass(frozen=True)
class MediaInspection:
    container: str
    duration_seconds: float | None
    overall_bitrate: int | None
    video: VideoStream | None
    audio: tuple[AudioStream, ...]
    subtitles: tuple[SubtitleStream, ...]
    chapters: int

def parse_ffprobe(payload: dict) -> MediaInspection          # raises InspectionParseError
def resolution_class(width: int, height: int) -> Resolution  # crop tolerant (see below)
def compare(claimed: ParsedQuality, actual: MediaInspection, *, expected_runtime_min: int | None,
            preferred_audio_languages: list[str]) -> list[Finding]

@dataclass(frozen=True)
class Finding:
    kind: str       # quality_mismatch | runtime_mismatch | missing_audio | language_missing | truncated_file | invalid_media
    severity: str
    message: str
    data: dict
```

**Resolution classification is by width first.** Scope and flat crops keep the width but reduce
the height: 1920×800 is still 1080p, and 3840×1600 is 2160p.

| Rule | Class |
|---|---|
| `width ≥ 3200` or `height ≥ 1800` | 2160p |
| `width ≥ 1700` or `height ≥ 900` | 1080p |
| `width ≥ 1200` or `height ≥ 600` | 720p |
| otherwise | SD |

**HDR detection:**

- `color_transfer == smpte2084` → HDR10. If there's HDR10+ side data (`SMPTE2094-40`), it's
  HDR10+.
- `color_transfer == arib-std-b67` → HLG.
- DOVI configuration record side data, or `codec_tag` `dvhe`/`dvh1` → DV. Combined with HDR10 →
  "DV HDR10".

**Comparison rules:**

- **quality_mismatch**: the actual resolution class is *lower* than the claimed one (e.g. claimed
  1080p, actual 720p). Upscale detection is out of scope, since you can't tell from metadata.
  Higher-than-claimed only earns an `info` finding.
- **runtime_mismatch**: expected runtime is known and
  `|duration − expected| / expected > tolerance`. The tolerance is 25 % for episodes (recaps,
  intros and runtime metadata are noisy) and 15 % for movies (theatrical vs extended cuts are
  legitimately longer, so only *shorter* counts toward a mismatch unless it's > +40 %). Episodes
  shorter than 5 minutes are skipped.
- **truncated_file**: duration < 50 % of expected *and* file size < 50 % of a low-end bitrate floor
  for the *measured* resolution × expected runtime (SD 0.5, 720p 1, 1080p 2, 2160p 6 Mbps). That's
  evidence of an incomplete file. A short file at a normal size is reported as `runtime_mismatch`
  instead, since it's more likely the wrong episode or noisy runtime metadata. With no size known,
  truncation is never reported.
- **invalid_media**: no video stream, or ffprobe reports invalid data.
- These are deliberately **not** `corrupt_file`: that type belongs to the scanner's header-magic
  check, and each detector auto-resolves only the types it owns. Sharing a type let every library
  scan close ffprobe's findings.
- **missing_audio**: no audio streams at all.
- **language_missing**: preferred audio languages are configured and none of the tagged audio
  languages match. Untagged (`und`) audio **doesn't** count as missing, only "unknown".

### 4.2 Data model – migration `NNNN_media_inspection`

Scalar columns on `media_files` hold everything we filter or aggregate on. The full payload goes
in JSON.

| Column | Type | Notes |
|---|---|---|
| `inspected_at` | DateTime, index | null = never inspected |
| `inspected_size` / `inspected_mtime` | BigInteger / Float | skip re-inspection if unchanged |
| `inspection_error` | String(512) | last error (ffprobe failed/timeout) |
| `container` | String(16) | |
| `duration_seconds` | Float | |
| `overall_bitrate` | BigInteger | |
| `video_codec` | String(16), index | |
| `video_width` / `video_height` | Integer | |
| `video_resolution` | String(8), index | actual class: SD/720p/1080p/2160p |
| `video_bit_depth` | Integer | |
| `hdr_format` | String(16), index | |
| `audio_codec` | String(32) | primary (default or first) track |
| `audio_channels` | Integer | primary track |
| `audio_languages` | JSON | list |
| `subtitle_languages` | JSON | list |
| `mediainfo` | JSON | full `MediaInspection` as a dict |

### 4.3 Services – `backend/services/inspection_service.py` (new)

```python
def ffprobe_path() -> str | None      # settings.ffprobe_path → shutil.which("ffprobe") → None
async def probe(path: Path, timeout: float = 30) -> dict
async def inspect_file(media_file_id: int, *, force: bool = False) -> InspectionOutcome
async def inspect_backlog(limit: int, progress) -> dict
def evaluate_findings(db, media_file, inspection) -> list[Finding]   # uses domain.compare + settings
```

**Process execution** (security-relevant):

- Run `asyncio.create_subprocess_exec` with an argument list and never a shell:
  `[ffprobe, "-v", "error", "-hide_banner", "-print_format", "json", "-show_format",
  "-show_streams", "-show_chapters", "-protocol_whitelist", "file", "-i", f"file:{abs_path}"]`.
- The `file:` prefix plus `-protocol_whitelist file` stop crafted filenames (`concat:…`,
  `http://…`, `subfile,,…`) from making ffprobe read other files or reach the network. There's a
  unit test for this.
- On Windows, pass `fileops.long_path(path)`.
- `asyncio.wait_for(timeout)`, with the process killed on timeout. Stdout is capped at 4 MB.
- `asyncio.Semaphore(settings.inspection_concurrency)`, default 2, so NAS disks don't thrash.

**Remote/cloud mounts:** skip or throttle when the owning root is flagged remote (14). ffprobe
reads headers, which on rclone mounts triggers downloads.

**Flow for `inspect_file`:**

1. Load the `media_file`. If the size and mtime match the `inspected_*` values and `force` is
   false, return `skipped`.
2. Probe → `parse_ffprobe` → update the columns.
3. `evaluate_findings` → report through the F2 `IssueRecorder` scoped to this file (so fixed
   issues auto-resolve on re-inspection).
4. Probe failure: set `inspection_error`, and report `invalid_media` if ffprobe reports invalid
   data (as opposed to a timeout or missing binary).
5. Publish `EventType.MEDIA_INSPECTED`.

**Settings added:**

- `ffprobe_path: str | None`;
- `inspection_enabled: bool = True` (auto-disabled with a health warning if no binary);
- `inspection_concurrency: int = 2`;
- `preferred_audio_languages: list[str] = []`;
- `runtime_tolerance_episode_pct = 25`;
- `runtime_tolerance_movie_pct = 15`;
- `inspect_before_import: bool = False` (a later milestone).

### 4.4 Jobs

- `inspect_file` (event, priority 6, non-exclusive, max_attempts 2): enqueued by the import
  service after a successful import, with `dedupe_key=f"file:{id}"`. **The import doesn't wait**
  for inspection, so the import transaction stays fast.
- `media_inspection` (scheduled every 6 h, enabled): works through uninspected files and files
  whose size or mtime changed, 200 per batch (`inspection_service._needs_inspection`; files whose
  last probe failed wait until they change). It re-enqueues itself with
  `dedupe_key="backlog"` while more are pending, the same pattern as `metadata_refresh`
  ([tasks.py:57-67](../../backend/workers/tasks.py#L57-L67)).
- The scanner marks `media_files` whose mtime or size changed (`inspected_at=None`), so the
  backlog picks them up.

### 4.5 Health check

In [health_service.py](../../backend/services/health_service.py): if inspection is enabled and
ffprobe isn't found, return a `warning` with the message "ffprobe not found – install ffmpeg or
set its path in Settings → General". Also report the ffprobe version.

### 4.6 Docker

Add ffprobe to the runtime stage. `apt-get install -y --no-install-recommends ffmpeg` adds about
80 MB. Alternatively, copy a static `ffprobe` binary from `mwader/static-ffmpeg` in a build stage,
which adds about 30 MB and only includes ffprobe. **Recommendation:** use the static ffprobe now,
and switch to full ffmpeg when 13 needs encoding. Multi-arch builds must fetch the matching arch.

### 4.7 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/media-files/{id}` | MEDIA_READ | includes `mediainfo` and scalar fields |
| POST | `/media-files/{id}/inspect` | MEDIA_WRITE | force re-inspect; returns job id |
| POST | `/library/inspect` | LIBRARY_SCAN | `{scope: movies\|tv\|all, only_uninspected: true}` → enqueue backlog |
| GET | `/movies/{id}`, `/series/{id}/episodes` | MEDIA_READ | the file summary gains `video_resolution`, `hdr_format`, `audio_codec`, `audio_channels`, `audio_languages` |
| GET | `/library/issues?issue_type=quality_mismatch` | existing | new types flow through F2 |
| GET | `/system/status` | SYSTEM_READ | `ffprobe: {found, path, version}` |

### 4.8 Frontend

- **Movie detail / episode row expand**: a "Technical info" card with:
  - Video: codec, resolution (actual vs claimed; a warning badge on mismatch), HDR badge, bit
    depth, bitrate;
  - Audio: a table of tracks (language flag, codec + profile, channels, commentary tag);
  - Subtitles: language chips with forced and SDH markers;
  - Duration vs expected runtime;
  - "Re-inspect" button and last inspected time.
- **Library list**: optional columns and filters for actual resolution, HDR, video codec and
  audio.
- **Issues**: F2 detail renderers for `quality_mismatch` (claimed vs actual) and
  `runtime_mismatch` (duration vs expected, with a "Re-match / Manual import" action).
- **Settings → General**: ffprobe path, concurrency, preferred audio languages, runtime
  tolerances.

### 4.9 Naming tokens

Add these to `MOVIE_TOKENS` and `EPISODE_TOKENS`
([naming.py:27](../../backend/domain/naming.py#L27)): `{mediainfo_videocodec}`,
`{mediainfo_audiocodec}`, `{mediainfo_audiochannels}`, `{mediainfo_hdr}`,
`{mediainfo_resolution}`.

Rendering at import time means ffprobe has to have run **before** placement, which requires
`inspect_before_import`, or a rename after inspection. For the MVP these tokens are only valid in
the Renamer (post-inspection). Validation warns if they're used in the import format while
`inspect_before_import` is off.

## 5. Implementation milestones

1. [x] **Domain parser + comparison**, with ffprobe JSON fixtures: HEVC DV+HDR10 4K, H.264 1080p
   scope (1920×800), 720p mislabelled as 1080p, multi-audio with commentary, forced and SDH subs,
   no audio, truncated.
2. [x] **Service + safe subprocess + health check + settings**, with the protocol-whitelist test.
3. [x] **Migration + persistence + post-import job + backlog task.**
4. [x] **Findings → issues** (F2 recorder), plus the auto-resolve on re-inspect path.
5. [x] **API + UI panels + library filters.** Movies and TV share one filter implementation
   (`repositories.media_file_filter`); a series matches when any of its episode files matches, and
   its row lists the distinct measured resolutions/HDR/codecs across its inspected files.
6. [x] **Docker ffprobe** (multi-arch) and docs in the README.
7. [x] `inspect_before_import`, MediaInfo naming tokens in import formats, and the quick
   decode test. Implementation notes:
   - **Pre-import check** (`inspection_service.pre_import_check`, off by default): probes each
     download before placement and rejects it (left in the download folder, reason
     `corrupt_file`) on positive evidence only – ffprobe "invalid data", no video stream, the
     truncation rule against the *target's* expected runtime, or a failed decode test. If the probe
     can't run (timeout, missing binary, odd output) the import proceeds unchecked.
   - **MediaInfo tokens** in import formats are filled from that probe; with the check off they
     stay empty there (the Renamer still fills them post-inspection).
   - **Decode test** uses `ffprobe -count_frames` over three 10 s windows (start, middle, just
     before the end) instead of `ffmpeg -t 30`: the image ships only ffprobe, and the end window is
     what catches truncation – a cut-off MKV keeps its full duration in the header, so neither the
     header probe nor a first-30 s decode notices. It runs on imports and single-file inspections
     (post-import job, Re-inspect), never the library backlog (seconds per file → hours). Findings
     carry `source: decode` + size/mtime and survive later header-only passes until the content
     changes. Sampling can miss small damaged patches between windows; full decodes stay out of scope.

## 6. Testing

- **Unit:**
  - `parse_ffprobe` on all fixtures;
  - `resolution_class` table (1920×800 → 1080p, 1280×536 → 720p, 3840×1608 → 2160p,
    720×576 → SD);
  - HDR detection matrix;
  - `compare` rules and tolerance edges;
  - argv builder always includes `file:` and the whitelist.
- **Integration:** mock `probe()` with fixtures via monkeypatch, so CI doesn't need ffprobe.
  - Import a file whose name says 1080p but whose fixture says 1280×720 → a `quality_mismatch`
    issue appears.
  - Re-inspect with a corrected fixture → the issue auto-resolves.
  - An unchanged mtime skips re-inspection.
- **Optional real-binary test**, marked `ffprobe` and skipped when the binary is absent. It
  generates a 2-second test file with `ffmpeg -f lavfi` if available.

## 7. Risks & open questions

- **Runtime metadata noise** (TVmaze/TVDB runtimes are per series, not per episode). Mitigated by
  generous tolerances, `warning` severity only, and never acting automatically.
- **I/O load on large libraries** (e.g. 20k files × ~0.2 s each ≈ 1 h). Mitigated by the
  backlog batching, concurrency 2, and skip-unchanged. The first run is visible as a job with
  progress.
- **Untagged audio languages** are common in older rips. They're treated as unknown and never
  counted as missing.
- **Open:** should a detected mismatch automatically change the stored `quality` to the actual
  resolution? Recommendation: offer it as a per-issue "Relabel" action first, and add an opt-in
  auto setting later.

## 8. Acceptance criteria

- With ffprobe present, every newly imported file is inspected within minutes, and the whole
  library is inspected in the background.
- The movie and episode detail pages show real video, audio and subtitle facts.
- Mislabelled resolution, large runtime deviation, missing audio and missing preferred-language
  audio each show up as distinct issues. They auto-resolve when fixed.
- With ffprobe absent, the app works as before and the System page shows a clear warning.
- No filename can make ffprobe read a non-local resource.
