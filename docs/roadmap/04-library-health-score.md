# 04 – Library health score & health dashboard

> Phase 2 · Effort M (≈1 week) · Risk low · Depends on F2 (hard), 03 (optional, adds integrity/coverage signals), 01 (optional, pipeline reasons) · Priority #6

## 1. Summary & user value

The dashboard already has the raw numbers: missing items, issue counts, failures in the last 7
days, and storage. What's missing is a single, understandable answer to "is my library in good
shape, and what should I fix first?".

A **0–100 health score** has three parts:

- a **per-component breakdown** (completeness, integrity, quality, duplicates, pipeline
  reliability, inspection coverage, metadata);
- a **30-day trend**;
- **"top 3 actions"**, which link straight to the filtered view that fixes the biggest deficit.

It turns maintenance into something visible and a bit gamified: people enjoy pushing a number to
100.

## 2. Current state

`dashboard_service.stats` ([dashboard_service.py:24](../../backend/services/dashboard_service.py#L24))
computes these in a handful of indexed queries:

- movies and episodes: total, with file, missing (monitored and aired);
- `failed_imports`, and `failed_last_7_days` (from history);
- open issues grouped by type;
- storage bytes and disks.

There's no persistence of past values, so no trend.

## 3. Scope

### MVP

- A pure score calculator.
- Components from existing data plus F2 issues (and 03 when present).
- Daily snapshots with a 30-day trend.
- An API.
- A dashboard widget and a detail page with actions.

### Later

- Per-root-folder and per-media-type sub-scores.
- A score-change notification ("Health dropped below 80").
- Achievements and badges.

### Out

- Cross-user leaderboards.

## 4. Design

### 4.1 Domain – `backend/domain/health_score.py` (new, pure)

```python
@dataclass(frozen=True)
class HealthInputs:
    wanted_total: int            # monitored & aired (movies released + episodes aired)
    wanted_missing: int
    files_total: int
    files_below_cutoff: int      # upgrade_available (+ format-score below upgrade-until, 02)
    integrity_issues: int        # missing_file + corrupt_file + quality_mismatch + runtime_mismatch + missing_audio
    duplicate_files: int         # non-primary files in duplicate groups (05) or duplicate issues
    duplicate_bytes: int
    library_bytes: int
    imports_ok_7d: int
    imports_failed_7d: int
    downloads_failed_7d: int
    files_inspected: int | None  # None when inspection unavailable
    titles_total: int
    titles_without_metadata: int # metadata_source is null or last refresh > 90 days

@dataclass(frozen=True)
class Component:
    key: str          # completeness | integrity | quality | duplicates | reliability | coverage | metadata
    label: str
    weight: float     # effective (after re-normalisation)
    score: float      # 0..100
    bad: int
    total: int
    detail: str       # "412 of 16,210 wanted episodes missing"
    action: Action | None

@dataclass(frozen=True)
class Action:
    label: str        # "Search 412 missing episodes"
    route: str        # frontend route with filters, e.g. "/wanted?tab=missing"
    impact: float     # estimated score points gained if fully fixed

@dataclass(frozen=True)
class HealthScore:
    score: int
    grade: str        # A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F
    components: tuple[Component, ...]
    top_actions: tuple[Action, ...]   # 3 highest impact

def calculate(inputs: HealthInputs, weights: dict[str, float] = DEFAULT_WEIGHTS) -> HealthScore
```

**Default weights:**

| Component | Weight | Bad / total | Curve |
|---|---:|---|---|
| Completeness | 30 | missing / wanted | `100 · (1 − r)^0.7` |
| Integrity | 20 | integrity issues / files | `100 · (1 − min(1, 5r))`. Integrity problems are serious, so 20 % bad already scores 0. |
| Quality | 15 | below cutoff / files | `100 · (1 − r)` |
| Pipeline reliability | 15 | failed / (ok + failed) over 7 days | `100 · (1 − r)`. If there were no imports this week, the component is excluded rather than scored 100. |
| Duplicates | 10 | duplicate bytes / library bytes | `100 · (1 − min(1, 4r))` |
| Inspection coverage | 5 | uninspected / files | `100 · (1 − r)`. Excluded if inspection is unavailable. |
| Metadata | 5 | titles without fresh metadata / titles | `100 · (1 − r)` |

The curves stop tiny ratios in large libraries from reading as perfect or disastrous. They're
also easy to explain in the UI ("each 1 % of files with integrity problems costs 5 points of this
component").

**Re-normalisation:** components with `total == 0`, or that are unavailable, are dropped, and the
remaining weights are scaled back to sum to 100. The score is `round(Σ weightᵢ · scoreᵢ / 100)`.

**Action impact:** `weight · (100 − score) / 100`, the points recovered if the component were
fully fixed. The top 3 by impact become `top_actions`.

Weights can be overridden via the settings key `health_weights`, which is validated so it's
non-negative with at least one positive value.

### 4.2 Data model – migration `NNNN_library_health`

`library_health_snapshots` table:

| Column | Type |
|---|---|
| `id` | Integer PK |
| `computed_at` | DateTime, index |
| `day` | String(10), unique: one row per day, the last computation wins |
| `score` | Integer |
| `components` | JSON (list of component dicts) |
| `inputs` | JSON (raw `HealthInputs`, so we can re-score if weights change) |

Housekeeping deletes snapshots older than 400 days (a year of trend).

### 4.3 Services

`dashboard_service` gains two functions:

```python
def health_inputs(db) -> HealthInputs        # indexed COUNT/SUM queries only, like stats()
def health(db, *, with_trend_days: int = 30) -> dict
def snapshot_health() -> None                # compute + upsert today's row
```

Queries reuse what `stats()` already computes (missing counts, failures). Issue-derived counts
come from `GROUP BY issue_type WHERE status='open'` (F2). Inspection coverage uses
`COUNT(media_files) / COUNT(inspected_at)` (03). `stats()` should call `health_inputs()` so the
two never disagree. The whole query budget stays under the dashboard's 200 ms target
([dashboard_service.py:1](../../backend/services/dashboard_service.py#L1)).

Caching: `health()` memoises for 60 s (keyed on nothing, a single library), and is invalidated on
`SCAN_COMPLETED`, `IMPORT_COMPLETED`, `IMPORT_FAILED` and `ISSUES_CHANGED`.

### 4.4 Jobs

- `health_snapshot`: scheduled every 24 h (enabled). It's also enqueued after `library_scan`
  finishes, with a dedupe key.

### 4.5 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/dashboard` | MEDIA_READ | adds `health: {score, grade, delta_7d, top_actions}` |
| GET | `/dashboard/health` | MEDIA_READ | full `HealthScore` + `trend: [{day, score}]` (30 by default, `?days=` ≤ 365) + per-component trend |
| POST | `/dashboard/health/recompute` | LIBRARY_SCAN | forces a snapshot |

### 4.6 Frontend

- **Dashboard widget** ([DashboardPage.tsx](../../frontend/src/pages/DashboardPage.tsx)):
  - a large score with a letter grade and a ring or gauge;
  - the 7-day delta (▲3 / ▼5);
  - a sparkline of the last 30 days;
  - the three action chips, each linking to its route.
- **Health page** (`/health`, new): component cards, each with score, detail sentence, a mini
  trend and a "Fix" button. There's also a stacked contribution bar showing how many points each
  component costs.
- Accessibility: the score colour is never the only signal, since the grade letter and the
  numbers are always shown. It uses the same palette in light and dark mode.

### 4.7 Events & metrics

- `EventType.HEALTH_SCORE_CHANGED` fires when a snapshot differs from the previous day by at least
  5 points. Notification channels can subscribe.
- Gauge `maniac_library_health_score`, plus `maniac_library_health_component{component}`.

## 5. Implementation milestones

1. Domain calculator with table-driven tests (empty library, perfect library, a single
   component dropped, custom weights).
2. `health_inputs()` queries, then refactor `stats()` to share them.
3. Snapshot table, job and trend API.
4. Dashboard widget and Health page.
5. Event and metrics.

## 6. Testing

- **Unit:**
  - Re-normalisation (no inspection, no imports this week).
  - Curve edges.
  - A grade at every boundary.
  - Top actions ordered by impact.
- **Integration:**
  - Seed a library with 10 movies, 3 missing and 1 corrupt → assert the exact component scores
    and the overall score.
  - Snapshot twice on the same day → one row.
  - Trend returns an ordered series.
- **E2E:** the dashboard widget renders, and clicking an action navigates to a filtered view.

## 7. Risks & open questions

- **Score feels arbitrary.** Mitigated by showing the formula per component on the Health page
  ("how is this calculated?") and allowing weight overrides.
- **Daily snapshot misses intra-day swings.** That's acceptable, since health is a slow signal.
- **Open:** include "storage fragmentation" from the brainstorm? It isn't meaningful for media on
  modern filesystems. We'll substitute "free space headroom" as a later component (disks < 10 %
  free).

## 8. Acceptance criteria

- The dashboard shows a health score, grade, delta and top 3 actions within the existing load
  budget.
- The Health page explains each component with its numbers and links to a view that fixes it.
- 30 days of trend are available after 30 days of running, with one snapshot per day.
- Features that aren't available (no ffprobe, no imports) never reduce the score.
