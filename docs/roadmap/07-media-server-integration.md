# 07 – Plex / Jellyfin / Emby integration & watch-driven automation

> Phase 3 · Effort XL (3–4 weeks, three independently shippable stages) · Risk medium (stage C deletes – recycle bin, dry-run, confirmation) · Depends on F3 (inbound webhooks), F1 (file-level actions), 05 safety patterns · Priority #9

## 1. Summary & user value

Radarr and Sonarr push metadata *to* media servers, and that's all. The library never reacts to
what people actually watch. This feature closes the loop in three stages:

| Stage | Value | Risk |
|---|---|---|
| **A. Library refresh** | New imports, renames and deletes appear in Plex, Jellyfin or Emby within seconds, with a partial (path) refresh instead of a full library scan. | None |
| **B. Watch history** | Maniac knows what each user watched and when: "Recently watched" and "Stale series" widgets, and watched/unwatched state on detail pages. | None (read-only) |
| **C. Watch-driven rules** | Delete episodes N days after they're watched, pause series nobody watches, hold the next season until the current one is watched, only upgrade unwatched content. | Deletes: guarded |

Stage A replaces ARCHITECTURE §18 item 7. Stages B and C are the brainstorm's
"library feels alive" feature. The "don't download new seasons if I haven't watched the last one"
and "pause shows you're behind on" calendar ideas are rules in stage C.

## 2. Current state

- No media-server integration exists. Notification channels (email, Discord, Telegram, webhook)
  react to `EventType`s ([events.py:20](../../backend/core/events.py#L20)) via
  [notification_service.py](../../backend/services/notification_service.py).
- Monitoring flags exist at the series, season and episode level (`monitored`,
  `monitor_new_seasons`).
- Remote path mapping exists for download clients (`map_remote_path`,
  [_common.py:109](../../backend/plugins/download_clients/_common.py#L109)). The same concept is
  needed for media servers (e.g. Plex in Docker sees `/media/Movies`, while Maniac sees
  `M:\Movies`).

## 3. Scope

### MVP (stage A + B)

- A media-server plugin kind with Plex, Jellyfin and Emby built-ins.
- Path-scoped refresh on import, rename and delete.
- A watch-history sync job.
- Matching watch events to library items.
- Watched state in the UI.
- Dashboard widgets.

### Stage C

- An automation rules engine with four rule types.
- A preview/pending-actions queue.
- Confirmation mode.
- Keep tags.

### Later

- Inbound webhooks (Plex `media.scrobble`, Jellyfin Webhook plugin) for real-time updates, using
  F3 keys.
- Trakt as a watch-history source (shares the import-list OAuth from 09).
- Per-user rules.

### Out

- Two-way metadata editing in the media server.
- Managing media-server users.

## 4. Design

### 4.1 Plugin contract – `PluginKind.MEDIA_SERVER`

Add the kind to [base.py:17](../../backend/plugins/base.py#L17) and
`BUILTIN_PACKAGES["media_server"] = "backend.plugins.media_servers"`
([registry.py:33](../../backend/plugins/registry.py#L33)).

```python
class MediaServerPlugin(Plugin):
    kind = PluginKind.MEDIA_SERVER
    supports_watch_history: ClassVar[bool] = True
    supports_partial_refresh: ClassVar[bool] = True

    async def libraries(self) -> list[ServerLibrary]: ...            # id, name, type, locations[]
    async def refresh(self, paths: list[str]) -> None: ...           # server-side paths (already mapped)
    async def refresh_all(self, library_ids: list[str] | None = None) -> None: ...
    async def users(self) -> list[ServerUser]: ...                     # id, name
    async def watch_history(self, since: datetime | None) -> AsyncIterator[PlaybackEvent]: ...
    async def item_state(self, refs: list[ServerItemRef]) -> list[WatchState]: ...  # optional bulk check
```

Domain DTOs go in `domain/types.py`:

```python
@dataclass
class PlaybackEvent:
    server_event_id: str       # stable id for dedupe (Plex historyKey, Jellyfin/Emby UserData hash)
    user_id: str
    user_name: str
    media_type: str            # movie | episode
    guids: dict[str, str]      # {"tmdb": "603", "tvdb": "81189", "imdb": "tt0133093"}
    series_guids: dict[str, str]
    season: int | None
    episode: int | None
    file_path: str | None      # server-side path, for fallback matching
    played_at: datetime
    progress: float            # 0..1
    completed: bool            # progress ≥ 0.9 or server "played" flag
```

**Built-ins** (`backend/plugins/media_servers/`):

- **Plex** (`plex.py`):
  - fields: url, token (secret);
  - `GET /library/sections`;
  - refresh: `GET /library/sections/{id}/refresh?path=<urlencoded>`;
  - history: `GET /status/sessions/history/all?sort=viewedAt:desc&viewedAt>=<ts>` (paged);
  - GUIDs from `Guid` children (`tmdb://`, `tvdb://`, `imdb://`);
  - users via `/accounts`.
- **Jellyfin** (`jellyfin.py`) and **Emby** (`emby.py`) share `_mediabrowser.py`, since the APIs
  are near-identical (header `X-Emby-Token` vs `Authorization: MediaBrowser Token=`):
  - refresh: `POST /Library/Media/Updated` with `{Updates:[{Path, UpdateType}]}`;
  - history: per user,
    `GET /Users/{id}/Items?Filters=IsPlayed&Recursive=true&Fields=ProviderIds,Path,UserData&SortBy=DatePlayed&SortOrder=Descending`
    (incremental by `DatePlayed ≥ since`);
  - `ProviderIds` gives Tmdb, Tvdb and Imdb.

Tokens are stored encrypted. Add Plex token patterns (`X-Plex-Token=`) to `RedactingFilter`.

### 4.2 Data model – migration `NNNN_media_servers`

`media_servers` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(128) | |
| `implementation` | String(32) | plex \| jellyfin \| emby |
| `settings` | JSON | url, selected library ids, user filter |
| `secret_settings` | `EncryptedString("media_servers.secret_settings")` | token |
| `path_mappings` | JSON | `[{server: "/media/Movies", local: "M:\\Movies"}]` (multiple allowed) |
| `refresh_on_import` / `refresh_on_rename` / `refresh_on_delete` | Boolean | |
| `sync_watch_history` | Boolean | |
| `last_history_sync` | DateTime | cursor |
| `enabled`, `health_status`, `health_message`, `last_health_check` | | same pattern as indexers |

`playback_history` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `server_id` | FK media_servers.id, index | |
| `server_event_id` | String(128) | unique with server_id |
| `server_user_id` / `user_name` | String(64) / String(128) | |
| `media_type` | String(16) | movie \| tv |
| `movie_id` | FK, nullable, index | |
| `series_id` | FK, nullable, index | |
| `episode_id` | FK, nullable, index | |
| `played_at` | DateTime, index | |
| `progress` | Float | |
| `completed` | Boolean | |

The `(series_id, played_at)` index serves the "last watched per series" query.

`watch_state` (a materialised summary, updated by sync so rules and UI stay cheap):

| Column | Type |
|---|---|
| `media_type`, `movie_id`/`episode_id` (unique) | |
| `watched_by` | JSON (list of user names who completed) |
| `first_watched_at`, `last_watched_at` | DateTime |
| `play_count` | Integer |

`automation_rules` (stage C):

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(128) | |
| `rule_type` | String(32) | `delete_after_watched` \| `pause_if_unwatched` \| `hold_next_season` \| `upgrade_unwatched_only` |
| `scope` | String(16) | `global` \| `series` \| `movie` \| `tag` |
| `scope_ids` | JSON | series or movie ids when scoped |
| `params` | JSON | per rule type (below) |
| `users` | JSON | which server users count; `[]` = any user |
| `user_mode` | String(8) | `any` \| `all` (all selected users must have watched) |
| `mode` | String(16) | `preview` \| `confirm` \| `auto` (default `confirm`) |
| `enabled` | Boolean | |
| `last_run`, `last_result` | DateTime / JSON | |

`automation_actions` (the pending and executed action log):

| Column | Type |
|---|---|
| `id`, `rule_id` FK, `action` (`delete_file`/`unmonitor_series`/`unmonitor_season`/`skip_upgrade`) | |
| `media_type`, `movie_id`/`series_id`/`episode_id`, `media_file_id` | |
| `reason` (Text), `bytes` BigInteger | |
| `status` (`pending`/`approved`/`executed`/`rejected`/`expired`/`failed`), `created_at`, `decided_by`, `executed_at` | |

Also add `movies.tags` and `series.tags` (JSON list) if they don't already exist, to support the
`keep` protection tag and tag-scoped rules.

### 4.3 Services

**`media_server_service.py`**
- `build(row)` resolves the plugin via the registry and applies decrypted settings.
- `map_to_server(local_path, mappings)` and `map_to_local(server_path, mappings)` are pure helpers
  in `domain/paths.py`, shared with the download-client mapping logic (refactor
  `map_remote_path` to use them).
- **Stage A (refresh):** an event subscriber registers on `IMPORT_COMPLETED`, `RENAME_COMPLETED`
  and `MEDIA_DELETED`. It collects affected folder paths (the movie folder or season folder),
  **debounces for 10 s and coalesces**, so a season pack import produces one refresh per folder.
  Then it enqueues a `media_server_refresh` job with the mapped paths. If partial refresh isn't
  supported or the path isn't in any server library location, it falls back to a library refresh.
- **Stage B (sync):** `sync_history(server_id)` pages through `watch_history(since=cursor −
  overlap 1h)`. It upserts `playback_history` on `(server_id, server_event_id)` and matches
  items:
  1. by GUID: tmdb → movie, tvdb/tmdb series + season/episode → episode, imdb as a fallback;
  2. by path: `map_to_local(file_path)` → `media_files.path_hash` (F1).

  Unmatched events are kept with null FKs and are re-matched on the next sync (capped at 30 days).
  Finally it updates `watch_state` for the touched items and advances the cursor.

**`automation_service.py`** (stage C) has a pure rule evaluation core in `domain/automation.py`:

```python
def evaluate_delete_after_watched(items: list[WatchedFile], params, now) -> list[ProposedAction]
def evaluate_pause_if_unwatched(series: list[SeriesWatchSummary], params, now) -> list[ProposedAction]
def evaluate_hold_next_season(series: list[SeriesWatchSummary], params) -> list[ProposedAction]
def should_skip_upgrade(watch_state, params) -> bool
```

| Rule | Params | Behaviour |
|---|---|---|
| `delete_after_watched` | `days` (default 30), `keep_latest_n_episodes` (default 0), `min_file_age_days` (7), `exclude_tags` (["keep"]), `movies: bool` | Episode or movie files watched (per `user_mode`) at least `days` ago → propose `delete_file` (recycle). Episodes are then unmonitored so they aren't re-downloaded. |
| `pause_if_unwatched` | `days` (default 60), `only_continuing: true` | A series with monitored future episodes but no completed play by the selected users in `days` → propose `unmonitor_series` (sets `monitor_new_seasons=false` and unmonitors unaired episodes). It's reversible, since the user re-monitors. |
| `hold_next_season` | `watched_pct` (default 0.8) | While less than `watched_pct` of the latest season with files is watched, new seasons are added **unmonitored**. Hook: the metadata refresh when new seasons appear ([media_service](../../backend/services/media_service.py) monitor-new-seasons logic) consults this rule. Once the threshold is reached, the next season is monitored automatically (and searched if `search_on_add`). |
| `upgrade_unwatched_only` | `consider_in_progress_watched: bool` | `decision_engine` / `search_service.build_target`: if the item is watched, `has_file` counts as cutoff met, so there's no upgrade. It's evaluated at search time, not as an action. |

`run_rules()`:

1. Evaluates each enabled rule.
2. Writes `automation_actions` with `status=pending`, deduplicated against existing pending
   actions for the same target.
3. `mode=auto` executes immediately, within caps.
4. `mode=confirm` leaves the actions pending for approval (expiring after 14 days).
5. `mode=preview` records them for display only.

Execution goes through `media_file_service` / `FileTransaction` → recycle bin, then `audit()`,
then history event `deleted` (`data.rule_id`), and finally a media-server refresh (stage A).

**Hard safety limits** apply regardless of mode:

- at most 50 files or 200 GB per run;
- never delete a file younger than `min_file_age_days`;
- never act on titles tagged `keep`;
- never act when the latest history sync is more than 24 h old (stale data);
- `auto` mode requires the rule to have run in preview or confirm mode at least once, as a
  "you've seen what this does" gate.

### 4.4 Jobs

| Job | Schedule | Notes |
|---|---|---|
| `media_server_refresh` | event | dedupe per server; payload is the path list |
| `watch_history_sync` | 30 min (enabled when a server has `sync_watch_history`) | self-continuing when paging a large initial history |
| `automation_rules` | 24 h (disabled until a rule exists) | also runnable on demand |
| `automation_expire` | part of `cleanup` | expire stale pending actions |

### 4.5 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET/POST/PUT/DELETE | `/media-servers` | SETTINGS_* | secrets write-only (`has_token`) |
| POST | `/media-servers/test` / `/{id}/test` | SETTINGS_WRITE | connection + version + libraries |
| GET | `/media-servers/{id}/libraries` | SETTINGS_READ | for the library/path-mapping picker, with a suggested mapping (reuses 01's `infer_path_mapping`) |
| POST | `/media-servers/{id}/sync` | SYSTEM_ADMIN | run history sync now |
| GET | `/watch-history` | MEDIA_READ | paginated, filters `movie_id`, `series_id`, `user`, `since` |
| GET | `/movies/{id}`, `/series/{id}/episodes` | MEDIA_READ | gain `watch: {watched, watched_by[], last_watched_at, play_count}` |
| GET/POST/PUT/DELETE | `/automation/rules` | new `AUTOMATION_MANAGE` (admin + power_user) | |
| POST | `/automation/rules/{id}/preview` | AUTOMATION_MANAGE | dry run → proposed actions, bytes |
| GET | `/automation/actions?status=pending` | MEDIA_READ | |
| POST | `/automation/actions/decide` | AUTOMATION_MANAGE + MEDIA_DELETE for deletes | `{ids, decision: approve \| reject}` |
| POST | `/webhooks/plex`, `/webhooks/jellyfin` *(later)* | API key with `webhook` preset, `?apikey=` allowed | triggers a targeted sync for that item |

### 4.6 Frontend

- **Settings → Connections → Media servers**: add/edit dialog (type, URL, token), test,
  library picker, path mappings with a suggestion, and refresh/sync toggles.
- **Detail pages**: a watched badge (eye icon) with who and when. Episode lists get a watched
  column. Series show "Last watched 43 days ago".
- **Dashboard**:
  - *Recently watched*: the last 10 across users.
  - *Stale series*: monitored, continuing series with no plays in N days (a candidate list for
    `pause_if_unwatched`).
  - *Watch-driven savings*: bytes deleted by rules in the last 30 days, plus pending bytes.
- **Automation page** (`/automation`, new):
  - a rules list with an enable toggle and mode;
  - the rule editor (type, scope, params, users, mode) with a **"Preview"** button that shows the
    proposed actions table before saving;
  - a **Pending actions** tab (approve or reject individually or in bulk, showing reasons and
    sizes) and a **History** tab.
- **Series and movie detail**: an "Automation" card showing which rules apply, a toggle for the
  `keep` tag, and per-series rule overrides.

### 4.7 Events, notifications, metrics

- New `EventType`s: `MEDIA_SERVER_REFRESHED`, `WATCH_HISTORY_SYNCED`,
  `AUTOMATION_ACTIONS_PENDING` (so notification channels can say "5 actions await approval"),
  `AUTOMATION_EXECUTED`.
- Metrics:
  - `maniac_media_server_requests_total{server,op,result}`;
  - `maniac_playback_events_total{server}`;
  - `maniac_automation_actions_total{rule_type,status}`.
- Health checks: media server unreachable; history sync stale by more than 2 h while enabled.

### 4.8 Security

- Tokens are encrypted, write-only, and redacted from logs.
- Webhook endpoints accept `?apikey=` only for keys with `allow_query_param` (F3). The payload is
  treated as a *hint*: it triggers an authenticated pull from the server rather than being
  trusted directly, so a forged webhook can at most cause a sync.
- The new permission `automation:manage` keeps rule creation away from `read_only` users. Deletes
  additionally require `media:delete`.

## 5. Implementation milestones

**Stage A (≈1 week)**

1. Plugin kind, Plex and Jellyfin/Emby plugins with `test`, `libraries` and `refresh`.
2. `media_servers` table, CRUD, UI and path mapping helpers (refactor the download-client
   mapping onto them).
3. Debounced refresh subscriber and job.

**Stage B (≈1 week)**

4. `watch_history` implementations and `playback_history` / `watch_state` tables.
5. Matching (GUID then path), sync job and cursor.
6. Watched state in the APIs and UI, plus dashboard widgets.

**Stage C (≈1.5–2 weeks)**

7. Domain rule evaluators with an exhaustive unit test matrix.
8. Rules and actions tables, service, safety limits, preview and confirm.
9. `hold_next_season` hook in new-season monitoring, and the `upgrade_unwatched_only` hook in the
   search target.
10. Automation UI (rules, preview, pending, history), plus notifications.
11. *(Later)* Webhooks via F3.

## 6. Testing

- **Unit:**
  - Path mapping both ways (Windows ↔ POSIX, multiple mappings, longest prefix wins).
  - GUID parsing for Plex (`plex://movie/…` vs `tmdb://`).
  - Rule evaluators, including edge cases: `user_mode=all` with one user missing; the keep tag;
    min file age; the keep-latest-N episodes; `hold_next_season` thresholds.
  - Safety caps.
- **Integration:** a fake media-server plugin (like `download_clients/simulator.py`) registered
  from tests, returning scripted history.
  - Import → one debounced refresh with the mapped path.
  - A sync matches by GUID and by path.
  - `delete_after_watched` in confirm mode → a pending action → approve → the file is in the
    recycle bin, the episode is unmonitored, and a refresh is triggered.
  - A stale sync blocks execution.
  - `upgrade_unwatched_only` stops an upgrade grab.
- Recorded HTTP fixtures (`httpx.MockTransport`) for real Plex and Jellyfin response shapes.
- **E2E:** the media-server settings form, and the automation preview table renders.

## 7. Risks & open questions

- **Deleting something the user wanted to keep.** That's the main risk. Mitigated by
  confirm-mode default, the preview gate before auto, the keep tag, caps, recycle bin, a
  stale-data guard and audit.
- **Plex GUID agent differences** (legacy `com.plexapp.agents.*` vs new `plex://`). New-agent
  items expose external GUIDs through the `Guid` array; legacy agents embed the ID in the agent
  string. Both parsers are needed.
- **History volume** for large households: incremental cursors and the materialised
  `watch_state` keep queries cheap. Retention for `playback_history` defaults to 2 years.
- **Open:** is "watched" at a 90 % threshold right for all servers? It's configurable per server,
  with a default of 0.9 or the server's own played flag.

## 8. Acceptance criteria

- **Stage A:** an imported episode appears in Plex or Jellyfin within 30 s without a full library
  scan.
- **Stage B:** detail pages show accurate watched state per user. The dashboard shows recently
  watched and stale series.
- **Stage C:** rules can be previewed before enabling. In confirm mode, nothing is deleted
  without approval. Executed deletions are in the recycle bin, audited, and reflected in the
  media server.
- A new season of a series whose previous season isn't 80 % watched is added unmonitored when
  `hold_next_season` applies, and gets monitored automatically once the threshold is reached.
