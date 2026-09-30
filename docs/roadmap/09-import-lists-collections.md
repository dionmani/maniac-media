# 09 – Import lists & collection tracking

> Phase 4 · Effort L (2–3 weeks) · Risk medium (third-party APIs, OAuth) · Depends on nothing hard (uses metadata_service, media_service) · Priority #12

## 1. Summary & user value

Users already curate what they want to watch elsewhere: a Trakt watchlist, IMDb lists, a Plex
watchlist, TMDB lists, franchise collections like "every MCU movie". Import lists sync those
sources into Maniac. Items are either **auto-added** (monitored, and optionally searched), or
queued for **review**. A **Collections** view shows the coverage of each list ("Nolan: 9 of 12,
2 missing, 1 not released").

## 2. Current state

- Adding media: [media_service.py](../../backend/services/media_service.py) (add with a monitoring
  preset, root folder and profile) via `/lookup` → `/movies` or `/series` POST.
- Metadata lookup: `metadata_service` provider chain (TMDB, TVDB, TVmaze, IMDb).
- The setting `search_on_add` exists.
- There's no concept of external lists or exclusions.

## 3. Scope

### MVP

- `PluginKind.IMPORT_LIST`.
- Built-ins: **Custom JSON/URL** (Radarr's "StevenLu" format; simplest, and used as the test
  fixture), **TMDB list**, **TMDB collection**, **Trakt** (watchlist, personal lists, popular or
  trending with limits) and **Plex watchlist**.
- Sync job, ID resolution, auto/review modes.
- **Exclusions.**
- Removal policy.
- Collections view.

### Later

- **IMDb lists.** There's no official API: use the CSV export (`/list/ls…/export`), which is
  fragile and may need a user cookie. This ships as its own milestone with clear caveats.
- Letterboxd.
- MDBList.
- Trakt recommendations.
- "Keep in sync" bidirectional (mark collected in Trakt).

### Out

- Social features.

## 4. Design

### 4.1 Plugin contract

```python
class ImportListPlugin(Plugin):
    kind = PluginKind.IMPORT_LIST
    media_types: ClassVar[tuple[str, ...]] = ("movie", "tv")
    requires_oauth: ClassVar[bool] = False

    async def fetch(self) -> list[ImportListItem]: ...
    # OAuth-capable plugins:
    async def oauth_start(self) -> OAuthChallenge: ...        # device code flow: user_code, verification_url, interval
    async def oauth_poll(self, challenge) -> OAuthTokens | None: ...
    async def refresh_tokens(self, tokens) -> OAuthTokens: ...
```

In `domain/types.py`:

```python
@dataclass
class ImportListItem:
    media_type: str                 # movie | tv
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    tvmaze_id: int | None = None
    list_position: int | None = None
    source_url: str | None = None
```

**Trakt** uses the **OAuth device code flow**, since Maniac is self-hosted with no public
redirect URL. The user sees "Go to trakt.tv/activate and enter ABCD-1234", and the UI polls. Maniac
needs a client id and secret. Either ship a Maniac-registered app id (the preferred user
experience) or let users paste their own (the fallback). Tokens are stored encrypted and refreshed
automatically.

**Plex watchlist**: `https://discover.provider.plex.tv/library/sections/watchlist/all` with an
`X-Plex-Token`. The GUIDs in the response resolve to tmdb, tvdb or imdb ids. The token can be
shared with the media-server config (07) if it's present.

### 4.2 Data model – migration `NNNN_import_lists`

`import_lists` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(128) | |
| `implementation` | String(32) | |
| `media_type` | String(16) | movie \| tv (a list serves one type; Trakt lists with both → two lists) |
| `settings` | JSON | list id/slug, limits, filters (min rating, years, genres) |
| `secret_settings` | EncryptedString | tokens, API keys |
| `enabled` | Boolean | |
| `add_mode` | String(16) | `auto` \| `review` |
| `root_folder` | String(1024) | (becomes a root_folder_id FK once 14 lands) |
| `quality_profile_id` | Integer | |
| `monitor` | String(16) | movies: `movie` \| `none`; series: `all` \| `future` \| `first_season` \| `none` (reuse media_service presets) |
| `search_on_add` | Boolean | |
| `removal_policy` | String(16) | `keep` (default) \| `unmonitor` \| `review` |
| `sync_interval_minutes` | Integer default 360 | |
| `last_sync`, `last_error`, `last_item_count` | | |

`import_list_items` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `list_id` | FK import_lists.id, index | |
| `external_key` | String(64) | `tmdb:603` / `tvdb:81189` / `imdb:tt…` / `title:<normalized>:<year>` |
| `media_type` | String(16) | |
| `title`, `year` | | |
| `tmdb_id`, `tvdb_id`, `imdb_id` | | resolved ids |
| `status` | String(16), index | `pending` (review) \| `added` \| `in_library` \| `excluded` \| `unresolved` \| `removed_from_list` |
| `movie_id` / `series_id` | FK nullable | |
| `first_seen`, `last_seen` | DateTime | |
| `list_position` | Integer | |
| unique | (`list_id`, `external_key`) | |

`import_list_exclusions` table (global, across all lists):

| Column | Type |
|---|---|
| `id`, `media_type`, `tmdb_id`, `tvdb_id`, `imdb_id`, `title`, `year`, `created_at`, `reason` (`deleted` / `manual`) |

**Exclusions are essential.** When a user deletes a movie that came from a list, the delete
dialog offers "Exclude from import lists" (checked by default when the movie has list
membership). Without this, the next sync re-adds it, which is the most common complaint about
Radarr lists.

### 4.3 Services – `import_list_service.py`

`sync(list_id)`:

1. `items = await plugin.fetch()`, with a hard cap (default 1,000) per list.
2. **Resolve IDs.** Items with a tmdb id (movies) or tvdb id (series) are ready. Otherwise
   `metadata_service` looks them up (imdb → tmdb via TMDB `/find`, or a title and year search),
   and failures become `unresolved`.
3. Upsert `import_list_items` by `external_key`, and set `last_seen`.
4. Classify each item:
   - already in the library → `in_library` (link `movie_id`);
   - matches an exclusion → `excluded`;
   - otherwise `pending`.
5. With `add_mode=auto`, pending items go through `media_service.add_movie/add_series(...)` using
   the list's root folder, profile and monitor settings, becoming `added`. If `search_on_add` is
   set, `automatic_search` jobs are enqueued, capped at 20 per sync so a 500-item list doesn't
   flood the indexers. The rest are picked up by the missing-search sweep.
6. **Removal.** Items not seen in this sync (while the fetch succeeded and returned more than 0
   items, which guards against an API outage looking like an empty list) become
   `removed_from_list`. `removal_policy=unmonitor` unmonitors the title if it has no file and no
   *other* list references it. `review` surfaces it in the UI. `keep` does nothing.
7. Record stats, `last_sync` and `last_error`, then publish `EventType.IMPORT_LIST_SYNCED`.

Other functions:

- `approve(item_ids)` / `reject(item_ids, exclude: bool)` for review mode.
- Collections: `coverage(list_id)` returns total, in library, with file, missing (released) and
  unreleased counts.

TMDB **collections** (franchises) have one more feature: when a movie in the library belongs to a
TMDB collection, the movie detail page offers "Track collection", which creates a `tmdb_collection`
list in one click.

### 4.4 Jobs

- `import_list_sync`: a scheduled dispatcher every 15 min that enqueues per-list syncs whose
  `sync_interval_minutes` has elapsed (`dedupe_key=list:<id>`).
- Trakt token refresh happens lazily before a fetch.

### 4.5 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET/POST/PUT/DELETE | `/import-lists` | SETTINGS_* | secrets write-only |
| POST | `/import-lists/test` | SETTINGS_WRITE | fetches up to 20 items as a preview, without saving |
| POST | `/import-lists/{id}/sync` | SETTINGS_WRITE | job id |
| POST | `/import-lists/oauth/start` / `/oauth/poll` | SETTINGS_WRITE | device flow; tokens saved on the pending list config |
| GET | `/import-lists/{id}/items` | MEDIA_READ | paginated, filter `status` |
| POST | `/import-lists/items/approve` / `/reject` | MEDIA_WRITE | `{ids, exclude?}` |
| GET/POST/DELETE | `/import-list-exclusions` | MEDIA_WRITE | |
| GET | `/wanted/collections` | MEDIA_READ | per-list coverage summary |
| DELETE | `/movies/{id}`, `/series/{id}` | existing | gains `add_exclusion: bool` |

### 4.6 Frontend

- **Settings → Import lists**: cards per list with type-specific forms, the Trakt "Connect" button
  opening the device-code dialog, a "Test" button showing a preview, and sync status.
- **Wanted → Collections tab** ([WantedPage.tsx](../../frontend/src/pages/WantedPage.tsx)): one
  card per list with a progress bar (in library, with file, missing, unreleased) and a poster
  grid of missing items with "Add" buttons in review mode.
- **Wanted → "List review" tab**: pending items across lists, with bulk add or reject (and
  exclude).
- **Delete dialogs**: an "Exclude from import lists" checkbox.
- **Movie detail**: "In lists: Trakt Watchlist, Nolan Collection", plus a "Track collection"
  action.

### 4.7 Events, metrics, security

- Events: `IMPORT_LIST_SYNCED`, and `MEDIA_ADDED` (existing) with `source="import_list"`.
- Metric `maniac_import_list_items{list,status}`.
- OAuth tokens are encrypted and redacted. Custom JSON URLs are fetched with the shared HTTP
  client (5 MB cap, JSON only). Private-network URLs are allowed, since self-hosters often serve
  lists locally; this is documented.

## 5. Implementation milestones

1. Plugin kind, the Custom JSON plugin, tables, and a sync service with auto and review modes.
2. Exclusions, including the delete-dialog integration.
3. TMDB list and TMDB collection plugins, plus "Track collection".
4. Trakt with the device flow.
5. Plex watchlist.
6. Collections UI and the review tab.
7. *(Later)* IMDb list plugin, MDBList, Letterboxd.

## 6. Testing

- **Unit:** external-key building; the removal guard (an empty fetch never triggers removals);
  exclusion matching by any id.
- **Integration** (with `httpx.MockTransport` fixtures for each provider):
  - A JSON list with 3 movies, auto mode → 3 added, and search jobs capped.
  - Deleting one with exclude → the next sync doesn't re-add it.
  - Review mode → pending → approve → added.
  - An item removed from the list with `unmonitor` → unmonitored only if it has no file.
  - The Trakt device flow poll sequence (pending → authorised) and token refresh on 401.
- **E2E:** add a Custom JSON list, sync, and the Collections card shows coverage.

## 7. Risks & open questions

- **Third-party API churn** (Plex discover, IMDb). Mitigated by isolating each in its own plugin,
  health status per list, and never destructive removal by default.
- **Mass-add storms**: search caps per sync, plus a list item cap.
- **Open:** should Maniac register an official Trakt app (client id in the code)? That's
  recommended for user experience, and Trakt allows it for open-source apps. It needs the
  maintainer's account.

## 8. Acceptance criteria

- Users can connect Trakt, TMDB, Plex and custom lists, and see a preview before saving.
- Syncs add new items (auto) or queue them (review), and never re-add excluded titles.
- An API outage never unmonitors or removes anything.
- The Collections view shows per-list coverage and lets the user add missing items.
