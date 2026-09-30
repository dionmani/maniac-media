# 10 – Subtitle provider plugins & subtitle lifecycle

> Phase 4 · Effort L (≈3 weeks) · Risk medium (provider quotas / ToS) · Depends on F1 (media_files), 03 (audio/subtitle stream languages) · Priority #13

## 1. Summary & user value

Subtitles are a daily need for many households: non-native-language content, hearing-impaired
viewers, forced subtitles for foreign dialogue. Today people run **Bazarr** alongside
Radarr/Sonarr. Native subtitle management would add:

- policy-driven **search and download** from subtitle providers after import and on a schedule;
- language rules such as "English always; Dutch too; only fetch English when the audio isn't
  English";
- **upgrades**: replace a low-scoring subtitle with a better match;
- awareness of **embedded** subtitle tracks (via 03), so we don't download what's already there;
- management per file: list, search manually, pick, delete.

## 2. Current state

- Sidecars are moved on import when their extension is in `extra_file_extensions` (default
  `.srt .ass .sub .idx .nfo`) and their name starts with the video stem
  (`_move_sidecars`, [import_service.py:314](../../backend/services/import_service.py#L314)). The
  language suffix is kept (`Movie.en.srt` → `NewName.en.srt`), but nothing is recorded.
- The renamer moves sidecars too (check `renamer_service` when implementing; it must keep doing
  so).
- There's no subtitle data model and no provider integration.

## 3. Scope

### MVP

- `PluginKind.SUBTITLE_PROVIDER`.
- Built-in **OpenSubtitles.com** (REST API v1, API key + user login, download quota) and
  **Podnapisi** (no key).
- A global subtitle policy with per-series and per-movie overrides.
- Scoring.
- `subtitle_files` tracking for sidecars and embedded tracks.
- Post-import search.
- A scheduled missing-subtitle sweep with backoff.
- Upgrades.
- Manual search and download UI.
- A `missing_subtitles` issue.

### Later

- More providers (Addic7ed, Subscene successors, Gestdown for TV, Jimaku for anime), each as a
  plugin.
- **Subtitle sync correction** with `ffsubsync`/`alass` (a heavy optional dependency). It would
  be an optional plugin behind a feature flag.
- Extracting embedded text subtitles to sidecars (`ffmpeg -map 0:s:N`).
- Translation.

### Out

- Hosting or uploading subtitles.

## 4. Design

### 4.1 Domain – `backend/domain/subtitles.py` (new, pure)

```python
@dataclass(frozen=True)
class SubtitlePolicy:
    languages: tuple[str, ...]                 # ISO 639-1 codes in priority order, e.g. ("en", "nl")
    hearing_impaired: str = "prefer_not"       # prefer | prefer_not | only | any
    include_forced: bool = True                # also fetch forced subs for each language when available
    only_if_audio_not_in: tuple[str, ...] = () # skip language X if the audio already includes any of these
    count_embedded: bool = True                # an embedded track in language X satisfies X
    min_score: int = 60                        # 0..100 normalised
    upgrade_until_score: int = 90
    upgrade_window_days: int = 14              # keep looking for better subs this long after import

@dataclass(frozen=True)
class SubtitleQuery:
    media_type: str
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: int | None
    season: int | None
    episode: int | None
    languages: tuple[str, ...]
    hearing_impaired: str
    release_name: str | None       # media_files.scene_name — strongest signal
    file_hash: str | None          # OpenSubtitles moviehash
    file_size: int | None

@dataclass(frozen=True)
class SubtitleCandidate:
    provider: str
    provider_id: str
    language: str
    forced: bool
    hearing_impaired: bool
    release_names: tuple[str, ...]
    hash_match: bool
    downloads: int | None
    rating: float | None
    format: str                    # srt | ass | ...

def wanted_languages(policy, audio_langs: list[str], existing: list[ExistingSub]) -> list[WantedSub]
def score(candidate, query) -> int      # 0..100
def opensubtitles_hash(read_chunk: Callable[[int, int], bytes], size: int) -> str   # pure given a reader
def sidecar_name(video_stem: str, language: str, forced: bool, hi: bool, ext: str) -> str   # "X.en.forced.srt", "X.en.hi.srt"
def parse_sidecar_name(filename: str, video_stem: str) -> tuple[str | None, bool, bool] | None
```

**Scoring** (normalised to 100):

| Signal | Points |
|---|---:|
| Hash match | 50 |
| Release group match (release names vs `scene_name`) | 15 |
| Source match (WEB-DL vs BluRay: timing differs) | 10 |
| Resolution match | 5 |
| Title + year (movie) or season + episode (TV) match | 15 |
| HI preference satisfied | 5 |

Weighting release group and source matters because those determine timing. Downloads and rating
only break ties.

`only_if_audio_not_in`: with policy `languages=("en",)` and `only_if_audio_not_in=("en",)`, an
English-audio file gets no English subtitles, while a Japanese-audio anime does. The audio
languages come from `media_files.audio_languages` (03). Without inspection data, we fall back to
the metadata original language (TMDB `original_language`, stored at refresh).

### 4.2 Plugin contract

```python
class SubtitleProviderPlugin(Plugin):
    kind = PluginKind.SUBTITLE_PROVIDER
    languages: ClassVar[frozenset[str]] = frozenset()     # supported, empty = all
    supports_hash: ClassVar[bool] = False
    async def search(self, query: SubtitleQuery) -> list[SubtitleCandidate]: ...
    async def download(self, candidate: SubtitleCandidate) -> bytes: ...   # raw file content (≤ 2 MB)
    async def quota(self) -> ProviderQuota | None: ...                     # remaining downloads / reset time
```

**OpenSubtitles.com**:

- API key header, plus `POST /login` for a JWT, which is cached.
- Search: `GET /subtitles?imdb_id=&season_number=&episode_number=&languages=&moviehash=`.
- Download: `POST /download` returns a link, which is then fetched.
- Honour `remaining` downloads, and back off until `reset_time` when it hits zero.

### 4.3 Data model – migration `NNNN_subtitles`

`subtitle_providers` has the same shape as `notification_channels`: `id`, `name`,
`implementation`, `settings` JSON, `secret_settings` EncryptedString, `enabled`, `priority`,
health columns, `quota_remaining`, and `quota_reset_at`.

`subtitle_files` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `media_file_id` | FK media_files.id, index | |
| `language` | String(8) | ISO 639-1 (normalised from 639-2 via a static table) |
| `forced` | Boolean | |
| `hearing_impaired` | Boolean | |
| `embedded` | Boolean | true = a stream inside the container (from 03); path is null |
| `stream_index` | Integer nullable | for embedded tracks |
| `path` | String(1024) nullable | sidecar path |
| `format` | String(8) | srt, ass, sup, … |
| `provider` / `provider_id` | String nullable | null for pre-existing or embedded |
| `score` | Integer nullable | |
| `added_at` | DateTime | |

`subtitle_search_state` (backoff, one row per media_file + language):

| Column | Type |
|---|---|
| `media_file_id`, `language`, `forced` (unique together) | |
| `last_searched_at`, `attempts`, `next_search_at`, `last_result` | |

Policy storage: a global policy in settings (`subtitle_policy`), plus a nullable
`movies.subtitle_policy` / `series.subtitle_policy` JSON override (null = inherit).

### 4.4 Services – `subtitle_service.py`

- `sync_existing(media_file)` records sidecars found next to the video (`parse_sidecar_name`) and
  embedded tracks from 03's `mediainfo`. It's called after import, after rename, and by the
  scanner.
- `wanted(media_file)` combines the policy, audio languages and existing subtitles into a list of
  wanted `(language, forced)` entries.
- `search(media_file, wanted)` fans out to enabled providers in priority order
  (`asyncio.gather` with per-provider timeouts), scores and filters by `min_score`, and returns
  ranked candidates.
- `download_and_place(media_file, candidate)`:
  1. Fetch the bytes, capped at 2 MB, and validate: decodable text for srt/ass (detect the
     encoding with `charset-normalizer` or stdlib fallbacks; normalise to UTF-8), and a plausible
     cue count.
  2. Write to `<video>.maniac-partial` using the `FileTransaction` placement.
  3. When upgrading, the existing sidecar goes to the recycle bin.
  4. Record `subtitle_files` and history event `subtitle_downloaded`.
- `process(media_file)` is the automatic path: sync existing → wanted → search → download the best
  per wanted entry → update the search state. An entry that isn't found backs off
  (1 h → 6 h → 24 h → 72 h, then weekly for `upgrade_window_days`, then monthly).
- `upgrade(media_file)`: within `upgrade_window_days`, subtitles below `upgrade_until_score` are
  re-searched, and replaced when a candidate beats them by at least 10 points.

Related changes elsewhere:

- The renamer, when it renames a video, renames its tracked sidecars and updates
  `subtitle_files.path`. `_move_sidecars` already handles import.
- Make sure `.srt/.ass/.sub/.idx/.sup` stay in `extra_file_extensions`.

### 4.5 Jobs

| Job | Trigger | Notes |
|---|---|---|
| `subtitle_search` | event after `import_download` / `inspect_file` completes (prefer after inspection so audio languages are known; fallback delay 10 min) | per media_file |
| `subtitle_sweep` | every 6 h | files whose `next_search_at ≤ now`, batch 100, quota-aware |
| `subtitle_upgrade` | every 24 h | within upgrade window |

### 4.6 Issues (F2)

`missing_subtitles` (severity info): a wanted language is still missing after 3 attempts. It
auto-resolves when the subtitle is found or the policy changes.

### 4.7 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET/POST/PUT/DELETE | `/subtitle-providers` | SETTINGS_* | secrets write-only |
| POST | `/subtitle-providers/{id}/test` | SETTINGS_WRITE | login + quota |
| GET/PUT | `/settings` (`subtitle_policy`) | SETTINGS_* | |
| PUT | `/movies/{id}` / `/series/{id}` | MEDIA_WRITE | `subtitle_policy` override (add to `MovieUpdate` / `SeriesUpdate`) |
| GET | `/media-files/{id}/subtitles` | MEDIA_READ | existing + wanted/missing |
| POST | `/media-files/{id}/subtitles/search` | MEDIA_WRITE | `{language, forced?}` → candidates with scores (cached tokens like interactive release search) |
| POST | `/media-files/{id}/subtitles/download` | MEDIA_WRITE | `{token}` |
| DELETE | `/subtitles/{id}` | MEDIA_DELETE | sidecar → recycle bin; embedded tracks can't be deleted (422) |
| GET | `/wanted/subtitles` | MEDIA_READ | files missing wanted subtitles |

### 4.8 Frontend

- **Settings → Subtitles**:
  - providers (cards with quota display);
  - the policy editor: a language picker (ordered), HI preference, forced, "only when audio isn't
    in…", minimum and upgrade scores.
- **Movie detail / episode expand**:
  - a "Subtitles" list with language flags, forced/HI/embedded badges, provider and score;
  - actions: Search (a dialog with ranked candidates showing scores and matched signals),
    Download, Delete;
  - wanted-but-missing languages shown as dashed chips.
- **Series detail**: a per-series policy override and bulk "Search missing subtitles for season".
- **Wanted → Subtitles tab.**

### 4.9 Events, metrics, security

- `EventType.SUBTITLE_DOWNLOADED`, and notification support.
- Metric `maniac_subtitle_downloads_total{provider,result}`, plus a quota gauge per provider.
- Downloaded content is treated as untrusted text: size cap, encoding normalisation, extension
  from an allow-list (`srt`, `ass`, `ssa`, `sub`, `vtt`), never executed or parsed as HTML. Names
  come from `sidecar_name`, so there's no provider-controlled path component.

## 5. Implementation milestones

1. Domain (policy, wanted, scoring, hash, sidecar names) with tests.
2. The `subtitle_files` model and `sync_existing` (sidecars and embedded from 03). **This alone
   gives visibility**: the UI lists existing subtitles.
3. Plugin kind, OpenSubtitles and Podnapisi plugins, provider CRUD and test.
4. Search, download, place and the manual UI.
5. Automatic post-import search, the sweep with backoff, upgrades and the issue type.
6. Renamer integration, and policy overrides.
7. *(Later)* Sync correction plugin, embedded extraction, more providers.

## 6. Testing

- **Unit:**
  - `opensubtitles_hash` against the published reference values.
  - Sidecar name round-trip (`X.en.srt`, `X.eng.forced.srt`, `X.en.sdh.srt`, `X.pt-BR.srt`).
  - Wanted-language logic including `only_if_audio_not_in` and embedded tracks.
  - Score ordering.
- **Integration** (providers mocked):
  - Import → search → a sidecar is placed and recorded.
  - A second run is a no-op.
  - Quota exhausted → backoff until reset.
  - Upgrade replaces the sidecar and recycles the old one.
  - Renaming a video renames its sidecars.
  - An English-audio movie gets no English subtitles under the "only if audio not in en" policy.
- **E2E:** the subtitles card renders, and the manual search dialog lists candidates.

## 7. Risks & open questions

- **Provider ToS and quotas**: OpenSubtitles free accounts get 20 downloads a day. Backoff and
  prioritisation (recent imports first) are essential, and the quota is shown in the UI.
- **Encoding hell** (legacy cp1252 or Windows-1256 subtitles): normalise to UTF-8 with detection
  and keep the original bytes in the recycle bin if that fails.
- **Open:** extract embedded text subtitles to sidecars for players that ignore embedded tracks?
  Deferred.

## 8. Acceptance criteria

- Existing sidecar and embedded subtitles are visible per file.
- After an import, wanted languages are fetched automatically within quota, following the policy
  (including the audio-language rule).
- Users can manually search, pick and delete subtitles.
- Low-scoring subtitles are upgraded within the upgrade window.
- Missing wanted subtitles are visible in Wanted and as issues.
