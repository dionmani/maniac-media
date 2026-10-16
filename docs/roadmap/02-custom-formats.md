# 02 – Custom formats & advanced release scoring (Complete)

> **Status: Complete (2026-10-16)** · Phase 1 · Effort L (2–3 weeks) · Risk low · Depends on nothing (stores matches on F1 when available) · Priority #3

## 1. Summary & user value

Quality (resolution × source) is too coarse to pick the release people actually want. Two
`Bluray-2160p` releases can differ in HDR flavour (DV vs HDR10+), audio (TrueHD Atmos vs AAC
stereo), edition (IMAX vs theatrical), group reputation, streaming service, language, or whether
they're re-encoded "micro" releases. Radarr and Sonarr v4 solve this with **custom formats**:
user-defined matchers, each scored per quality profile, where the scores decide ranking,
minimum acceptability and upgrades. The community-maintained **TRaSH Guides** publish hundreds of
ready-made formats as JSON.

Custom formats give users:

- fine-grained preferences without code;
- a "why was this chosen" breakdown in interactive search;
- one-click import of TRaSH formats;
- automatic upgrades from a worse format to a better one within the same quality.

## 2. Current state

- `parse_quality` detects resolution, source, codec, PROPER/REPACK and a single `hdr` boolean
  ([quality.py:126](../../backend/domain/quality.py#L126)). `release_group()` exists in
  [parser.py:60](../../backend/domain/parser.py#L60).
- `QualityProfileSpec.score()` = `rank*1000 + codec_score*10 + revision`
  ([quality.py:209](../../backend/domain/quality.py#L209)).
- `decision_engine.evaluate` packs the final score as `profile.score*1000 + seeders + usenet bonus
  − age` ([decision_engine.py:121-123](../../backend/services/decision_engine.py#L121-L123)). The
  packing is fragile: 500 seeders plus the usenet bonus can overflow into the next rank bucket.
- `is_upgrade` looks only at quality rank, codec and revision
  ([quality.py:216](../../backend/domain/quality.py#L216)).
- A release's name is lost after import (renamed), so a format can't be re-evaluated for the
  existing file. F1 adds `media_files.scene_name` for this. Until then, `history.data` of the
  `imported` event carries the source name.

## 3. Scope

### MVP

- Custom format CRUD with these specification types: `release_title`, `release_group`, `source`,
  `resolution`, `codec`, `quality_modifier` (remux / proper / repack), `size`, `indexer`,
  `protocol`.
- Per-profile scores, `min_format_score`, `upgrade_until_format_score`.
- Integration into ranking, rejection and upgrades.
- "Why" breakdown.
- A tester endpoint.
- Radarr/Sonarr JSON import and export (TRaSH-compatible).

### Later

- `language` spec (needs parsed language, and 03 for the actual audio).
- `edition` spec (06).
- `indexer_flag` (freeleech, internal) once Torznab attributes are parsed.
- `{CustomFormats}` naming token.
- TRaSH sync (periodic pull of a curated set).

### Out

- Arbitrary Python expressions.
- Per-format global scores (see decision D1).

## 4. Design

### Decision D1 – scores belong to the quality profile, not the format

The brainstorm proposed a `score` column on `custom_formats`. That doesn't work when profiles
disagree: `x265` is *good* in a 4K profile and *bad* in a "compatible 1080p for old TVs" profile.
Radarr moved to per-profile scores for this reason, and TRaSH guides are written against that
model. A format is only a *matcher*. The profile maps `format_id → score`.

### Decision D2 – rank with a tuple, not packed integers

`Decision.sort_key` becomes a tuple, and Python compares tuples lexicographically:

```
(approved, quality_rank, format_score, codec_score, revision, protocol_pref, min(seeders,500), -age_days)
```

This removes the overflow issue and makes the priority order explicit. `Decision.score` stays as
a display integer for backwards compatibility with the API.

### 4.1 Domain – `backend/domain/custom_formats.py` (new, pure)

```python
class SpecKind(StrEnum):
    RELEASE_TITLE = "release_title"     # regex on full release name
    RELEASE_GROUP = "release_group"     # regex on parsed group
    SOURCE = "source"                   # Source enum value
    RESOLUTION = "resolution"           # Resolution enum value
    CODEC = "codec"
    QUALITY_MODIFIER = "quality_modifier"  # remux | proper | repack | hdr
    SIZE = "size"                       # min_gb / max_gb (per episode for TV)
    INDEXER = "indexer"                 # indexer id
    PROTOCOL = "protocol"
    # later: LANGUAGE, EDITION, INDEXER_FLAG

@dataclass(frozen=True)
class Specification:
    name: str
    kind: SpecKind
    value: str | dict           # regex string, enum value or {min,max}
    negate: bool = False
    required: bool = False

@dataclass(frozen=True)
class CustomFormat:
    id: int
    name: str
    specifications: tuple[Specification, ...]
    include_in_rename: bool = False

@dataclass(frozen=True)
class ReleaseFacts:             # what the matcher sees; built from ReleaseInfo or a MediaFile
    title: str
    group: str | None
    quality: ParsedQuality
    size_bytes: int
    episode_count: int
    indexer_id: int | None
    protocol: str | None

@dataclass(frozen=True)
class FormatMatch:
    format_id: int
    name: str
    matched_specs: tuple[str, ...]

class FormatMatcher:
    def __init__(self, formats: Iterable[CustomFormat]) -> None     # precompiles regexes once
    def match(self, facts: ReleaseFacts) -> list[FormatMatch]

def score(matches: list[FormatMatch], format_scores: dict[int, int]) -> tuple[int, list[tuple[str, int]]]
    """total score + breakdown [(format name, score)], used for the 'why' panel."""

def validate_regex(pattern: str) -> str | None
    """None if OK, else the error message. Rejects >500 chars, compile errors, and obviously
    catastrophic patterns (nested unbounded quantifiers like (a+)+, (.*)*)."""
```

**Matching semantics** (Radarr-compatible, so imported TRaSH formats behave identically):

- Specifications are grouped by `kind`.
- A format matches when **every required spec passes** and, **for each kind that has non-required
  specs, at least one of them passes**.
- `negate` inverts a single spec's result.

Regexes are case-insensitive and compiled once per matcher, and the matcher is cached per formats
version (§4.3).

Extend `ParsedQuality` in [quality.py](../../backend/domain/quality.py) so common specs
don't need regexes. Detection stays pure and additive:

- `hdr_format: str | None` (DV, HDR10+, HDR10, HLG);
- `audio: str | None` (TrueHD Atmos, DTS-HD MA, DTS-X, DD+ Atmos, DD+, DD, AAC, FLAC, Opus);
- `audio_channels: str | None` (7.1, 5.1, 2.0);
- `streaming_service: str | None` (AMZN, NF, DSNP, ATVP, HMAX…).

### 4.2 Profile spec changes (`QualityProfileSpec`)

```python
format_scores: dict[int, int] = field(default_factory=dict)
min_format_score: int = 0                 # reject releases scoring below
upgrade_until_format_score: int = 10000   # stop format upgrades once reached
min_format_upgrade_delta: int = 1         # avoid churn on tiny improvements

def is_upgrade(self, current: ParsedQuality | None, candidate: ParsedQuality,
               current_fs: int | None = None, candidate_fs: int = 0) -> bool:
    # existing quality-based logic first (unchanged semantics);
    # NEW: same quality rank and current_fs is known →
    #      upgrade iff candidate_fs >= current_fs + min_format_upgrade_delta
    #      and current_fs < upgrade_until_format_score
```

Two points about cutoffs:

- When the quality cutoff is met, format-score upgrades are **still allowed** until
  `upgrade_until_format_score`. That matches Radarr and is the main reason people use custom
  formats.
- If `current_fs is None` (legacy file, no scene name), format upgrades are skipped. We never
  churn on unknown data.

### 4.3 Data model – migration `NNNN_custom_formats`

`custom_formats` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(128) unique | |
| `specifications` | JSON | list of `{name, kind, value, negate, required}` |
| `include_in_rename` | Boolean | |
| `source` | String(32) | `user` \| `trash:<id>` (provenance for sync) |
| `created_at`, `updated_at` | DateTime | |

Columns added to `quality_profiles`:

- `format_scores` JSON (`{"<format_id>": score}`, string keys for JSON portability);
- `min_format_score` Integer default 0;
- `upgrade_until_format_score` Integer default 10000;
- `min_format_upgrade_delta` Integer default 1.

Columns added to `media_files` (if F1 has landed; otherwise add them in F1's migration):
`custom_formats` JSON (list of ids), `custom_format_score` Integer.

We don't add a `release_matches` table: grab-time matches are stored in `history.data`
(`{"formats": [...], "format_score": n}`), which is enough for analysis, avoids a high-volume
table, and is purged by the existing history retention.

**Deleting a format** removes its id from every profile's `format_scores` in the same transaction.

### 4.4 Services

- **`quality_service`**:
  - `load_formats(db) -> list[CustomFormat]`, plus a module-level cache
    `(_formats_version, FormatMatcher)`. The version is bumped on every CRUD write and published
    as `EventType.SETTINGS_CHANGED` so other workers invalidate. It's a single process today;
    this keeps things correct if there's ever more than one.
  - `to_spec` reads the new profile columns.
  - Validation checks regexes with `validate_regex`, and that `format_scores` keys reference
    existing formats.
- **`decision_engine.evaluate`**:
  - builds `ReleaseFacts` from `release` and the parsed quality;
  - `matches = matcher.match(facts)`, then `fs, breakdown = score(matches, profile.format_scores)`;
  - rejects when `fs < profile.min_format_score` with reason
    `"Custom format score {fs} below minimum {min}"`;
  - passes the upgrade check `current_fs = target.current_format_score`;
  - `Decision` gains `custom_formats`, `format_score` and `score_breakdown`, and `sort_key` per
    D2;
  - `rank()` sorts by `sort_key`.
- **`search_service.build_target`**: computes `target.current_format_score` for the existing file
  by running the matcher on `media_files.scene_name` (F1), or on `history.data.source` of the
  last `imported` event as a fallback. For TV, it takes the **minimum** across target episodes,
  the same approach as the worst-quality logic already in
  [search_service.py:81-83](../../backend/services/search_service.py#L81-L83).
- **`download_service.grab`**: stores `formats` and `format_score` in the `grabbed` history event.
- **`import_service`**: on import, computes matches from the release title (queue) or file name
  and stores them on `media_files`. `_check_existing` / `_annotate_library_state` pass format
  scores into `is_upgrade`.
- **`scanner_service`**: files found by scanning get matches computed from their file name, with
  `source='scan'`. Their score is informational, and they're treated as `current_fs=None` for
  upgrades unless the name looks like a scene name (contains a group). Otherwise renamed files
  would be "upgraded" forever.
- **Wanted → Cutoff unmet** (`/wanted/cutoff`): also includes items whose format score is below
  `upgrade_until_format_score` when their profile has format scores configured.

### 4.5 Import / export (TRaSH compatibility)

`custom_format_io.py` in services:

- Import accepts Radarr/Sonarr JSON (a single format or an array) in this shape:
  `{name, includeCustomFormatWhenRenaming, specifications: [{name, implementation, negate,
  required, fields: {value}}]}`.
- The implementation mapping is `ReleaseTitleSpecification → release_title`,
  `ReleaseGroupSpecification → release_group`, `SourceSpecification → source` (Radarr numeric
  source enum → our `Source`), `ResolutionSpecification → resolution`,
  `QualityModifierSpecification → quality_modifier`, `SizeSpecification → size`, and
  `IndexerFlagSpecification`, `LanguageSpecification` and `EditionSpecification` → *kept but
  disabled* with a warning until supported.
- The response lists `imported`, `updated` (same name) and `warnings` per unsupported spec.
- Export produces the same shape, so formats can be shared back.
- Optional `trash_ids` field: if present, stored in `source` for future sync.

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/custom-formats` | SETTINGS_READ | list |
| POST | `/custom-formats` | SETTINGS_WRITE | 422 with per-spec errors |
| PUT | `/custom-formats/{id}` | SETTINGS_WRITE | |
| DELETE | `/custom-formats/{id}` | SETTINGS_WRITE | also strips from profiles |
| POST | `/custom-formats/test` | SETTINGS_READ | `{title, size_bytes?, indexer_id?, profile_id?}` → parsed facts, matches, per-spec pass/fail, total score |
| POST | `/custom-formats/import` | SETTINGS_WRITE | Radarr/TRaSH JSON body or `{url}` (fetched via `core/http.py`, 1 MB cap, JSON only) |
| GET | `/custom-formats/export?ids=` | SETTINGS_READ | |
| GET/PUT | `/quality-profiles/{id}` | existing | gains `format_scores`, `min_format_score`, `upgrade_until_format_score`, `min_format_upgrade_delta` |
| GET | `/releases` | existing | each release gains `custom_formats: [{id,name,score}]`, `format_score`, `score_breakdown: [{label, value}]` covering quality rank, formats, codec, proper, seeders, age |

### 4.7 Frontend

- **Settings → Quality** ([QualitySettings.tsx](../../frontend/src/pages/settings/QualitySettings.tsx)),
  new "Custom formats" section:
  - format list with spec count and which profiles score it;
  - editor dialog: name, include-in-rename, and a spec rows table (kind select, value input that
    adapts to kind, negate and required toggles), with live regex validation from the server
    `test` endpoint (debounced);
  - "Import JSON" (paste or URL) and "Export";
  - a **tester** panel: paste a release title and see which formats match and why.
- **Profile editor**: a "Custom format scores" table (format, score input, quick presets
  −10000 / 0 / +100), plus min score, upgrade-until and delta.
- **Interactive search** ([InteractiveSearchDialog.tsx](../../frontend/src/components/InteractiveSearchDialog.tsx)):
  - format badges per release (green for a positive score, red for negative, tooltip with the
    score);
  - a sortable format-score column;
  - an expandable "Why this rank" row showing `score_breakdown` and rejections;
  - the top approved release is marked "Would be grabbed automatically".
- **History**: the `grabbed` detail shows formats and score.

### 4.8 Events & metrics

- `EventType.SETTINGS_CHANGED` (new, generic) with `{section: "custom_formats"}`.
- No new metrics (decision volume is already visible through indexer metrics).

### 4.9 Security & performance

- **ReDoS**: patterns are capped at 500 chars, compile-checked, and screened by a nested-quantifier
  heuristic. Only `SETTINGS_WRITE` can create them. Matching runs against release titles, which
  are ≤ 1024 chars, and each call is timed in tests. If there's evidence of pathological patterns
  from imports, adopt the `regex` package with `timeout=` behind an optional dependency (listed as
  an open question).
- **Performance**: a search returns ~100–1,000 releases × ~50 formats × ~3 specs. With
  precompiled regexes that's well under 50 ms. There's a benchmark test with 1,000 titles × 100
  TRaSH formats < 200 ms.

## 5. Implementation milestones

1. [x] **Domain**: `custom_formats.py`, the extended `ParsedQuality` detection (HDR format, audio,
   channels, streaming service), and tests against a corpus of ~200 real release names checked in
   under `tests/fixtures/release_names.txt`.
2. [x] **Persistence + CRUD + tester + validation.** (`0011_custom_formats` migration, `custom_formats` table, `quality_profiles` & `media_files` columns)
3. [x] **Decision engine**: `sort_key`, min score rejection, breakdown, format-aware `is_upgrade`.
   Existing decision tests pass unchanged when no formats exist.
4. [x] **Search/grab/import wiring**: `current_format_score`, history data, stored matches.
5. [x] **Import/export (TRaSH).** (`backend/services/custom_format_io.py`)
6. [x] **UI**: formats editor, profile scores, search badges and "why" panel.
7. [x] **Cutoff-unmet** integration and the `{CustomFormats}` naming token.

## 6. Testing

- **Unit** (`tests/unit/test_custom_formats.py`):
  - [x] spec semantics (required, negate, per-kind OR);
  - [x] all spec kinds;
  - [x] TRaSH JSON round-trip for a sample set (x265 HD, DV HDR10, TrueHD Atmos, LQ groups,
    AMZN/NF);
  - [x] `is_upgrade` matrix (quality better / same with higher format score / same with score at
    upgrade-until / cutoff met with format upgrade / unknown current score);
  - [x] `sort_key` ordering (quality beats format score; format score beats seeders);
  - [x] `validate_regex` rejects `(a+)+$`;
  - [x] benchmark (<200 ms for 1,000 titles × 100 formats).
- **Integration** (`tests/integration/test_custom_formats_flow.py`):
  - [x] A profile scoring `LQ` groups at −10000 and min 0 → an interactive search via the demo
    indexer shows the LQ release rejected with the reason.
  - [x] Automatic search grabs the highest format score within the same quality.
  - [x] With an existing file matching "x264" (score 0) and a new release "x265" (score 100), the
    grab happens as a format upgrade. When the file is at upgrade-until, it doesn't.
- **E2E:** create a format via the UI, score it in a profile, and see the badge in interactive
  search.

## 7. Risks & open questions

- **Semantic drift from Radarr** could make imported TRaSH formats behave differently. Mitigated
  by using Radarr's matching semantics and by the round-trip tests.
- **Upgrade loops** (two releases alternately "better"). Prevented by `min_format_upgrade_delta`,
  plus blocklisting and history checks. Also add a guard: never grab a release whose title equals
  the current file's `scene_name`.
- **Open:** should we ship a small curated default set (LQ groups, x265 for HD, DV/HDR10+,
  Atmos)? Recommendation: yes, as *importable presets*, not auto-applied.
- **Open:** do we need the `regex` package with timeouts? Revisit after TRaSH import testing.

## 8. Acceptance criteria

- [x] Users can create formats, import TRaSH JSON, and assign per-profile scores.
- [x] Interactive search shows matched formats and a per-release ranking explanation.
- [x] Automatic search picks the highest-quality release first, then the highest format score. It
  rejects releases below the minimum score.
- [x] Existing files are upgraded to a higher format score within the same quality until
  upgrade-until is reached, and never loop.
- [x] With no custom formats defined, behaviour is identical to today (regression suite green).

