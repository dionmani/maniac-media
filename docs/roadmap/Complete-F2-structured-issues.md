# F2 – Structured library issues

> Phase 1 · Effort S (3–4 dev-days) · Risk low · Unlocks 01, 03, 04, 05

## 1. Summary & user value

Library issues (missing, duplicate, corrupt, upgrade-available and unmatched files) are currently
a free-text list that gets rebuilt on every scan. Anything the user dismisses comes back six hours
later. Issues can't carry machine-readable evidence (e.g. "claimed 1080p, actual 720p", or the
list of duplicate candidates), and they have no stable identity, so there's no history.

This foundation gives each issue a **type from a registry**, **structured data**, a **severity**,
a **stable fingerprint** and a real **open / resolved / dismissed** lifecycle. Several features
depend on it:

- 03 (inspection) and 05 (duplicates) need issues that carry structured payloads and typed
  resolve actions.
- 04 (health score) needs stable `detected_at` / `resolved_at` values for trends.
- 01 (diagnosis) reuses the severity and action vocabulary.

## 2. Current state

- Model: `LibraryIssue` ([models.py:387](../../backend/db/models.py#L387)) has `issue_type`,
  `media_type`, `media_id`, `path`, `details` (Text), `detected_at`, and `resolved` (bool).
- The scanner bulk-resolves every open issue for the media type at the start of each scan, then
  re-inserts whatever it finds
  ([scanner_service.py:87-88](../../backend/services/scanner_service.py#L87-L88),
  [scanner_service.py:197](../../backend/services/scanner_service.py#L197)). As a result:
  - dismissals don't stick;
  - `detected_at` resets on every scan;
  - there are always duplicate rows (resolved copies of the same issue), which housekeeping
    deletes after 30 days ([tasks.py:122](../../backend/workers/tasks.py#L122)).
- TV upgrade opportunities are counted but never recorded as issues
  ([scanner_service.py:235-236](../../backend/services/scanner_service.py#L235-L236)).
- API: `GET /library/issues`, and `POST /library/issues/{id}/resolve`, which only dismisses
  ([library.py:34-55](../../backend/api/v1/library.py#L34-L55)).
- The dashboard counts open issues by type ([dashboard_service.py:56](../../backend/services/dashboard_service.py#L56)).

## 3. Scope

### MVP

- Issue-type registry in the domain layer.
- New columns: `data`, `severity`, `fingerprint`, `status`, `first_detected_at`,
  `last_seen_at`, `resolved_at`, `resolution`.
- Scanner upsert with fingerprints, and resolve-if-not-seen.
- Typed resolve actions (MVP actions: `dismiss`, `reopen`, `rescan`, `search`).
- TV upgrade issues recorded.

### Later (owned by other features)

- `quality_mismatch`, `runtime_mismatch`, `missing_audio`, `language_missing` (03).
- Rich duplicate payload plus the `keep_*` actions (05).
- `storage_optimization_candidate` (13).
- `missing_subtitles` (10).

### Out

- Per-user issue assignment and comments.

## 4. Design

### 4.1 Domain – `backend/domain/issues.py` (new)

```python
class IssueType(StrEnum):
    MISSING_FILE = "missing_file"
    DUPLICATE_FILE = "duplicate_file"
    UPGRADE_AVAILABLE = "upgrade_available"
    CORRUPT_FILE = "corrupt_file"
    UNMATCHED_FOLDER = "unmatched_folder"
    # reserved for later features (registered here so the UI can render them generically)
    QUALITY_MISMATCH = "quality_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    MISSING_AUDIO = "missing_audio"
    LANGUAGE_MISSING = "language_missing"
    MISSING_SUBTITLES = "missing_subtitles"
    STORAGE_CANDIDATE = "storage_optimization_candidate"

class Severity(StrEnum):
    INFO = "info"; WARNING = "warning"; ERROR = "error"

@dataclass(frozen=True)
class IssueTypeSpec:
    type: IssueType
    label: str                    # "Missing file"
    severity: Severity            # default
    actions: tuple[str, ...]      # allowed resolve actions besides dismiss/reopen
    health_component: str | None  # which 04 component it feeds: integrity | quality | duplicates | completeness

REGISTRY: dict[IssueType, IssueTypeSpec] = {...}

def fingerprint(issue_type: str, media_type: str | None, media_id: int | None,
                path: str | None, discriminator: str | None = None) -> str:
    """sha256 over the normalised tuple. `discriminator` separates e.g. S01E02 vs S01E03 duplicates
    of the same series. Paths are normcased and case-folded, so Windows and Linux agree."""
```

Default registry:

| Type | Severity | Actions | Health component |
|---|---|---|---|
| missing_file | error | `rescan`, `search`, `unmonitor` | integrity |
| corrupt_file | error | `rescan`, `search`, `delete_file` | integrity |
| duplicate_file | warning | `keep_best`, `keep_specific` (05) | duplicates |
| upgrade_available | info | `search` | quality |
| unmatched_folder | warning | `match` (open Import page) | completeness |
| quality_mismatch | warning | `relabel`, `search` | integrity |
| runtime_mismatch | warning | `rematch`, `search` | integrity |
| missing_audio | error | `search`, `delete_file` | integrity |
| language_missing | info | `search` | quality |

### 4.2 Data model – migration `NNNN_structured_issues`

Add these columns to `library_issues`:

| Column | Type | Notes |
|---|---|---|
| `fingerprint` | String(64), index | Unique among *open* rows is enforced in code (partial unique indexes aren't portable across engines) |
| `severity` | String(16) default `warning` | |
| `status` | String(16) default `open`, index | `open` \| `resolved` \| `dismissed` |
| `data` | JSON | Structured payload (evidence, candidates, numbers) |
| `first_detected_at` | DateTime | Keeps the existing `detected_at` as a compat alias, set equal on insert |
| `last_seen_at` | DateTime | Updated each time a scan confirms the issue |
| `resolved_at` | DateTime | |
| `resolution` | String(32) | Action that closed it: `auto` (no longer detected), `dismissed`, `keep_best`, … |
| `episode_id` | Integer, nullable | Lets TV issues target a single episode |

Keep the `resolved` bool in sync (`resolved = status != 'open'`) so existing readers and tests
keep working. Backfill: `status = CASE resolved WHEN true THEN 'resolved' ELSE 'open' END`,
`fingerprint` computed in Python inside the migration (batched, 1,000 rows), and
`first_detected_at = last_seen_at = detected_at`.

Add a composite index on `(status, issue_type)` for the dashboard and the issues list.

### 4.3 Services

**New: `backend/services/issue_service.py`**

```python
class IssueRecorder:
    """Collects issues during one detection pass and reconciles them against open rows."""
    def __init__(self, db, scope: IssueScope)   # scope = media_type (+ optional media_id / issue types)
    def report(self, issue_type, media_type, media_id, path, details, *, data=None, episode_id=None,
               discriminator=None, severity=None) -> None
    def finish(self) -> RecorderStats           # upsert + resolve unseen; returns new/still_open/auto_resolved
```

`finish()` does the following:

1. Loads the open **and dismissed** rows for the scope into a `fingerprint → row` dict. This is a
   single query, in line with the scanner's "pre-load everything" style.
2. For each reported issue:
   - an existing *open* row gets `last_seen_at`, `details` and `data` refreshed;
   - an existing *dismissed* row gets `last_seen_at` refreshed and **stays dismissed** (unless its
     `data["signature"]` changed materially, e.g. a new duplicate candidate appeared, in which
     case it's reopened);
   - otherwise a new row is inserted.
3. Every open row in scope that wasn't reported is set to `status='resolved'`,
   `resolution='auto'`, `resolved_at=now`.

Other service changes:

- **Scanner** ([scanner_service.py](../../backend/services/scanner_service.py)): replace the
  bulk `update(...resolved=True)` and the `_issue()` helper with an `IssueRecorder` per scan
  scope. **Incremental scans only reconcile the folders they actually visited.** The recorder is
  told which `media_id`s were scanned (`recorder.touch(media_id)`), and unseen issues are only
  auto-resolved for touched media. The whole-library missing-file pass touches every media id.
  Without this, an incremental scan would auto-resolve issues for skipped folders.
- Record `upgrade_available` for TV episodes (currently only counted), with
  `discriminator="S01E02"` and `episode_id`.
- **Housekeeping** ([tasks.py:122](../../backend/workers/tasks.py#L122)): delete issues with
  `status='resolved'` older than 30 days. Keep dismissed ones, since they carry the user's intent,
  unless their media no longer exists.

### 4.4 API

| Method | Path | Change |
|---|---|---|
| GET | `/library/issues` | Adds filters `status` (default `open`), `severity`, `media_id`. Rows gain `severity`, `status`, `data`, `first_detected_at`, `last_seen_at`, `label`, `actions` (from the registry). |
| GET | `/library/issues/summary` | **New.** `{by_type: {type: {open, dismissed, severity}}, by_severity: {...}}`, used by the dashboard and 04. |
| GET | `/library/issue-types` | **New.** The registry as JSON so the UI renders labels, actions and severities without hard-coding them. |
| POST | `/library/issues/{id}/resolve` | Body becomes optional `{action: "dismiss" \| "reopen" \| "rescan" \| "search" \| …, params: {}}`. No body means `dismiss` (backward compatible). Unknown or disallowed action → 422. |
| POST | `/library/issues/bulk` | **New.** `{ids: [...], action}`, capped at 500. |

Action dispatch lives in `issue_service.resolve(db, issue, action, params, actor)`:

- `search` → enqueue `automatic_search`;
- `rescan` → enqueue `library_scan` scoped to the media;
- `unmonitor` → `media_service`;
- `delete_file` → recycle via `FileTransaction`.

Every action is audited (`audit(db, "media", "issue.<action>", …)`).

Permissions: `MEDIA_READ` for reads, `MEDIA_WRITE` for resolve, `MEDIA_DELETE` for `delete_file`.

### 4.5 Frontend

- **Issues view:** today this is a section of [WantedPage.tsx](../../frontend/src/pages/WantedPage.tsx).
  Promote it to a dedicated `pages/IssuesPage.tsx` with its own route (`/issues`) in `App.tsx`
  and a nav entry, since 03, 05 and 13 all add issue types and detail panels. The new page has:
  - status segmented control (Open / Dismissed / Resolved);
  - severity and type filters;
  - rows show the registry label, a severity pill and "first seen / last seen";
  - an action menu driven by the `actions` field;
  - multi-select bulk dismiss/search.
- Expandable row detail renders `data` with per-type components, falling back to a key/value
  table for types that don't have one yet. 03 and 05 plug their own detail components in here.
- Add the `LibraryIssue` and `IssueTypeSpec` types to `api/types.ts`.

### 4.6 Events & metrics

- `EventType.ISSUES_CHANGED = "library.issues_changed"`, published by `finish()` with counts, so
  the dashboard and issues page refresh live.
- Gauge `maniac_library_issues_open{type,severity}`, updated after each `finish()`.

## 5. Implementation milestones

1. **Domain + migration**: `domain/issues.py`, fingerprint, `NNNN_structured_issues` with
   backfill, and model columns. Unit tests for fingerprint normalisation (Windows vs POSIX paths,
   case).
2. **IssueRecorder + scanner switch**: replace the bulk resolve, add incremental-safe
   reconciliation, and record TV upgrades. Integration tests: dismiss persists across a rescan;
   an issue auto-resolves once fixed; an incremental scan doesn't resolve issues for untouched
   folders.
3. **API**: filters, summary, issue-types, typed resolve and bulk.
4. **UI**: status tabs, registry-driven actions, detail renderer.

## 6. Testing

- **Unit:** fingerprint stability; registry completeness (every `IssueType` has a spec);
  recorder reconciliation logic against an in-memory list.
- **Integration** (`tests/integration/test_pipeline.py` style):
  1. Create a duplicate movie folder → scan → one open issue.
  2. Dismiss it → rescan → still one row, still dismissed.
  3. Remove the extra file → rescan → resolved with `resolution='auto'`.
  4. Add a third file → dismissed issue reopens (signature changed).
- Migration test: a v2.1 DB with open and resolved issues upgrades with the correct status values.
- **E2E:** issues page renders, and dismissing moves an issue to the Dismissed tab.

## 7. Risks & open questions

- **Incremental scans:** getting "touched media" right is the key correctness risk. It's covered
  by the integration test above.
- **Signature for reopening dismissed issues:** keep it simple (a sorted list of paths for
  duplicates, and the claimed/actual pair for mismatches). Feature docs define it per type.
- **Open question:** should dismissals expire (e.g. after 180 days)? Default is no.

## 8. Acceptance criteria

- A dismissed issue doesn't reappear after scans unless its underlying evidence changes.
- `first_detected_at` for an ongoing issue stays the same across scans.
- The issues list can be filtered by status, severity and type, and actions come from the server
  registry.
- Existing API clients that call `resolve` with no body still work.
