# 05 – Deep duplicate intelligence & consolidation

> Phase 2 · Effort M-L (≈2 weeks) · Risk medium (deletes files – recycle bin only) · Depends on F1, F2, 03 (and 02/06 for richer comparison) · Priority #7

## 1. Summary & user value

Libraries built from old drives, mixed-quality imports and indexer switches accumulate duplicates:
a 1080p and a 2160p copy, a REMUX and a re-encode, a dubbed and an original-audio version, or the
same movie in two folders. Radarr and Sonarr only replace a file during an upgrade. They don't
detect existing duplicates, compare them, or help you clean up. Users fall back on scripts,
FileBot or Tdarr.

This feature does four things:

- **Finds** every duplicate group: same title (and episode), same edition.
- **Compares** candidates side by side using real stream data (03), custom format scores (02),
  naming conformity and size.
- **Recommends** which file to keep, with reasons, based on a user-ordered rule policy.
- **Consolidates** in one click or in bulk. Dry-run first. Everything else goes to the recycle bin
  (reversible) and every step is audited.

## 2. Current state

- Movies: the scanner flags "extras" larger than 40 % of the main file inside a movie folder as a
  `duplicate_file` issue with a free-text list
  ([scanner_service.py:148-154](../../backend/services/scanner_service.py#L148-L154),
  [:168-169](../../backend/services/scanner_service.py#L168-L169)). Only the biggest file is
  tracked.
- **Cross-folder duplicates are silently lost.** When a second folder (e.g.
  `Avatar (2009) 2160p/`) matches an existing movie by title or year, `_scan_movie_folder`
  overwrites `movie.path` and `movie.file_path` with the folder scanned last
  ([scanner_service.py:144](../../backend/services/scanner_service.py#L144),
  [:164](../../backend/services/scanner_service.py#L164), [:177](../../backend/services/scanner_service.py#L177)).
  The previous file is forgotten, and scan order decides which one wins.
- TV: overlapping `(season, episode)` in one series folder raises an issue, and the second file is
  ignored ([scanner_service.py:276-279](../../backend/services/scanner_service.py#L276-L279)).
- Import: `_check_existing` handles same-file, identical-size and not-an-upgrade cases
  ([import_service.py:289](../../backend/services/import_service.py#L289)). On an upgrade, the old
  file is recycled, which is correct and remains the path for upgrades.

## 3. Scope

### MVP

- Duplicate grouping from `media_files` (F1).
- Cross-folder detection fix.
- A rule-based recommender with reasons.
- A comparison UI.
- Per-group resolve (keep best / keep specific / keep all).
- Bulk resolve by policy with dry-run.
- An optional quick-hash for identical copies.

### Later

- Auto-resolve on scan (off by default, high-confidence groups only).
- An "archive" action that moves to a configurable archive root instead of the recycle bin (ties
  into 14).
- Merging two movie *records* that point at the same TMDB id.

### Out

- Perceptual/video fingerprint matching of *different* titles.

## 4. Design

### 4.1 What counts as a duplicate

Two or more `media_files` rows form a group when all of these hold:

- they're `role='main'`;
- they belong to the same **movie** (same `movie_id`), or overlap in at least one **episode**
  (`episode_files`);
- they have the **same edition**, when edition tracking (06) is in multi mode. In single mode,
  editions are just attributes, so different editions *are* duplicates, and the recommender can
  prefer one via `keep_edition`.

A group can also be marked **intentional** (`keep_all`), e.g. a 4K HDR copy kept alongside a 1080p
copy for remote streaming. Intentional groups are remembered by F2 dismissal and don't reappear
unless a new file joins.

### 4.2 Domain – `backend/domain/duplicates.py` (new, pure)

```python
@dataclass(frozen=True)
class FileCandidate:
    file_id: int
    path: str
    size_bytes: int
    quality: ParsedQuality
    format_score: int | None          # 02
    edition: str | None               # 06
    video_resolution: str | None      # 03 actual
    hdr_format: str | None
    video_codec: str | None
    audio: tuple[AudioSummary, ...]   # codec/profile/channels/language from 03
    subtitle_languages: tuple[str, ...]
    added_at: datetime | None
    naming_conforms: bool             # path == what the renamer would produce
    hardlinked: bool                  # st_nlink > 1 (seeding) — deleting frees no space
    quick_hash: str | None

class Rule(StrEnum):
    BEST_QUALITY = "best_quality"         # profile rank, then format score
    BEST_VIDEO = "best_video"             # actual resolution > HDR > bit depth > codec efficiency
    BEST_AUDIO = "best_audio"             # AUDIO_RANK[codec/profile] then channels
    PREFERRED_LANGUAGE = "preferred_language"  # has audio in preferred languages
    MORE_SUBTITLES = "more_subtitles"
    PROPER_NAMING = "proper_naming"
    SMALLEST = "smallest"
    LARGEST = "largest"
    NEWEST = "newest"
    OLDEST = "oldest"
    KEEP_EDITION = "keep_edition"          # param: edition name

AUDIO_RANK = ["truehd atmos", "dts-x", "truehd", "dts-hd ma", "flac", "pcm", "dd+ atmos",
              "dts-hd hra", "dts", "dd+", "dd", "opus", "aac", "mp3"]

@dataclass(frozen=True)
class Recommendation:
    keep: int                          # file_id
    remove: tuple[int, ...]
    reasons: tuple[str, ...]           # "Keeps Remux-2160p (DV) over Bluray-1080p", "Frees 38.2 GB"
    decided_by: Rule                   # first rule that separated the candidates
    confidence: str                    # high | medium | low
    reclaimable_bytes: int             # excludes hardlinked candidates

def group_key(...) -> str
def recommend(candidates: list[FileCandidate], policy: list[tuple[Rule, dict]], profile: QualityProfileSpec) -> Recommendation
def identical(a: FileCandidate, b: FileCandidate) -> bool   # same size and quick_hash
```

**Recommendation algorithm:**

1. Identical copies (same size and quick hash) collapse first. Keep the one that conforms to
   naming, or else the one in the title's main folder. Confidence: **high**.
2. Otherwise, apply the policy rules in order. Each rule compares candidates and filters to the
   best tier (ties survive). Stop when one candidate remains.
3. `confidence` is **high** when the deciding rule is `best_quality` with a rank difference of at
   least 2 or identical content, **medium** for other deciding rules, and **low** when the tie was
   broken by `newest`/`oldest` fallbacks.

The default policy is `[best_quality, best_video, best_audio, preferred_language, proper_naming,
largest]`. It's stored in the setting `duplicate_policy`.

### 4.3 Data model

There's no new table: groups are derived from `media_files` (F1). Columns added to `media_files`
by migration `NNNN_duplicates`:

- `quick_hash` String(40), nullable, index. This is sha1 over the size plus the first 4 MB plus
  the last 4 MB. It's computed lazily, only for files in groups of files with equal sizes, and
  skipped on remote mounts.
- `nlink` Integer. The hardlink count is recorded at scan/import time to explain why deleting
  frees no space.

Issues use F2 `duplicate_file` with `data`:

```json
{"group_key": "movie:42", "file_ids": [7, 19], "recommendation": {"keep": 19, "remove": [7],
 "reasons": ["…"], "decided_by": "best_quality", "confidence": "high", "reclaimable_bytes": 41012345678},
 "signature": "7,19"}
```

The `signature` is the sorted file ids, so a dismissed group reopens only if the set of files
changes.

### 4.4 Services

**Scanner fixes:**

- `_scan_movie_folder`: when the matched movie already has a primary file in a **different**
  folder that still exists, don't overwrite `movie.path`. Record the found file as a
  non-primary `media_files` row (F1). Primary is decided by the current recommender (or kept as-is
  if the existing primary still exists), so primary selection no longer depends on scan order.
- All full-size files in a movie folder are recorded, not just the biggest (the existing
  `MULTIPART` exclusion for cd1/cd2 is kept).
- TV: the second file for an episode is recorded as a non-primary linked file instead of being
  skipped.

**New `duplicate_service.py`:**

```python
def find_groups(db, *, media_type=None, media_id=None) -> list[DuplicateGroup]   # one grouped query
def evaluate(db, group) -> Recommendation                                         # builds FileCandidates, runs domain.recommend
async def ensure_quick_hashes(file_ids) -> None                                   # to_thread, bounded I/O
def resolve(db, group_key, action, *, keep_file_id=None, dry_run=False, actor) -> ResolveResult
def resolve_bulk(db, *, policy=None, min_confidence="high", limit=100, dry_run=True, actor) -> BulkResult
```

`resolve` does the following:

- It uses one `FileTransaction` per group. Each removed file goes through `tx.recycle()` and then
  `media_file_service.detach_file(reason="duplicate_resolved")`. The kept file gets
  `media_file_service.set_primary()`, then `sync_legacy()`.
- It deletes sidecars (subtitles, nfo) that belong to removed files **only if** the kept file
  doesn't already have an equivalent. Otherwise it moves them to sit next to the kept file.
- It writes `audit(db, "delete", "duplicate.resolve", details={kept, removed, bytes})` and a
  history event `deleted` per removed file.
- The whole thing is idempotent: resolving an already-resolved group is a no-op.

`resolve_bulk` defaults to **dry-run**, and returns the plan (groups, keep/remove, bytes). The
real run needs `dry_run=false` and applies only groups whose confidence is at least
`min_confidence`. It's capped at `limit` groups per call and runs as a job when there are more
than 20 groups.

**Import integration:** when a manual or untracked import candidate is a `duplicate` (not an
upgrade), the manual-import UI offers three options:

- **Replace**: force import (01) and recycle the old file;
- **Keep both**: import as a non-primary file, which creates a duplicate group for later
  resolution;
- **Skip**.

### 4.5 Jobs

- `duplicate_scan` runs after `library_scan` (and on demand). It rebuilds duplicate issues via
  `IssueRecorder(scope=duplicates)` and computes quick hashes for equal-size candidates.
- `duplicate_resolve_bulk` runs large bulk resolutions with progress.

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/library/duplicates` | MEDIA_READ | paginated groups: `{group_key, media_type, media_id, title, episode_label, candidates:[…file summary + inspection…], recommendation, status}`; filters `media_type`, `confidence`, `min_reclaimable_gb` |
| GET | `/library/duplicates/{group_key}` | MEDIA_READ | full comparison |
| POST | `/library/duplicates/{group_key}/resolve` | MEDIA_DELETE | `{action: keep_best \| keep_specific \| keep_all, file_id?, dry_run?}` |
| POST | `/library/duplicates/resolve-bulk` | MEDIA_DELETE | `{policy?, min_confidence, limit, dry_run}` → plan or job id |
| GET/PUT | `/settings` (`duplicate_policy`) | SETTINGS_* | ordered rule list |
| POST | `/library/issues/{id}/resolve` | existing (F2) | `keep_best` / `keep_specific` actions delegate here |

### 4.7 Frontend

- **Issues → Duplicates** view, or a Library "Duplicates" tab:
  - group cards sorted by reclaimable space;
  - each card is a comparison table with one column per file, and rows for path, size, quality,
    format score, actual resolution and HDR, video codec, audio tracks, subtitle languages,
    edition, naming OK, hardlinked and added;
  - the recommended column is highlighted, with a "Recommended because…" list;
  - actions: **Keep recommended**, **Keep this one** (per column), **Keep all (intentional)**.
- **Bulk bar:** "Resolve all high-confidence groups (N groups, frees X GB)". This always opens the
  dry-run plan first, as a table with checkboxes, and then the user confirms.
- **Settings → Media management:** a drag-to-order duplicate policy list.
- **Movie detail "Files" card** (F1): a "Duplicate" badge linking to the group.

### 4.8 Events & metrics

- `EventType.DUPLICATES_RESOLVED` with `{groups, files, bytes}`, which notification channels can
  use.
- Gauge `maniac_duplicate_reclaimable_bytes`.

### 4.9 Safety

- Removal always goes to the recycle bin (`recycle_bin_days` retention). Never `unlink`.
- A file is **never** removed if it's the only file for any linked episode. For multi-episode
  files, check coverage first: removing `S01E01-E02.mkv` when `S01E01.mkv` exists would lose E02.
  `recommend` only proposes removals whose episode set is covered by the kept file(s), and
  otherwise marks the group as "partial overlap – manual".
- Hardlinked candidates are flagged. Removing them frees nothing, and the UI says so.
- Bulk operations are capped per run and audited.

## 5. Implementation milestones

1. **Scanner fixes**: record all full-size files and cross-folder files as non-primary (depends
   on F1). Regression test for the path-overwrite bug.
2. **Domain recommender** plus the audio-rank table, with tests.
3. **`duplicate_service` grouping + evaluate + quick hash**, with issues via F2.
4. **Resolve (single) + safety checks + audit.**
5. **Bulk with dry-run + job.**
6. **UI**: comparison cards, bulk plan, policy settings, manual-import "keep both".
7. *(Later)* Auto-resolve toggle (high confidence only, capped, notification).

## 6. Testing

- **Unit:**
  - Rule ordering.
  - Ties.
  - Confidence levels.
  - Multi-episode coverage guard.
  - Identical-copy collapse.
  - `hardlinked` excluded from reclaimable bytes.
- **Integration:**
  - Two folders for the same movie → after scanning, both files are recorded and one duplicate
    group exists, regardless of scan order.
  - Resolve `keep_best` → the loser is in the recycle bin, `media_files` has one row, and the
    legacy columns point at the winner.
  - Dry-run bulk changes nothing.
  - A dismissed group stays dismissed across rescans and reopens when a third copy appears.
- **E2E:** comparison card renders, and "Keep recommended" removes the card.

## 7. Risks & open questions

- **Wrong recommendation deletes the "good" copy.** Mitigated by the recycle bin, dry-run,
  confidence gating, explicit reasons, and auto-resolve off by default.
- **Quick-hash I/O on network shares.** Only computed for equal-size candidates, 8 MB per file.
- **Open:** should dubbed and original-language versions be treated as editions (06) or as
  duplicates with a `preferred_language` rule? Recommendation: duplicates with the language rule
  for the MVP, and revisit with 06's multi mode.

## 8. Acceptance criteria

- Every title with more than one main file (same edition) appears as a duplicate group, including
  files in different folders.
- Each group has a recommended keeper with human-readable reasons and reclaimable space.
- Resolving a group sends the others to the recycle bin and leaves the library consistent (DB and
  disk).
- Bulk resolution always offers a dry-run plan and applies only the selected or high-confidence
  groups.
- No resolution ever leaves an episode without a file.
