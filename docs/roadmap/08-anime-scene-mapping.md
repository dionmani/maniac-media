# 08 – Anime absolute numbering, scene mapping (XEM) & alternate titles

> Phase 3 · Effort L (2–3 weeks) · Risk medium (external API: thexem.info) · Depends on nothing hard · Priority #11

## 1. Summary & user value

Anime releases rarely use `S01E01`. Fansub and scene groups number episodes **absolutely**
(`[SubsPlease] Frieren - 27 (1080p)`), split or merge seasons differently from TVDB, and use
romaji or English titles interchangeably. Other shows have **scene numbering** that differs from
TVDB too: seasons that the scene splits, and specials that the scene numbers as regular episodes.
Without mapping, Maniac can't parse, match, search or import these releases.

This feature adds:

- **absolute-number parsing** for anime;
- a **scene-mapping provider** abstraction with **XEM** (TheXEM.info) as the built-in;
- **alternate titles** for every series and movie (XEM names, TMDB alternative titles, TVDB
  aliases). This also fixes plenty of non-anime mismatches, like foreign titles and "&" vs "and"
  variants;
- anime-aware search, decisions, import and scanning, plus UI to see and override mappings.

## 2. Current state

- `series.series_type` has an `anime` option ([models.py:105](../../backend/db/models.py#L105)),
  editable through the API ([schemas.py:245](../../backend/api/schemas.py#L245)). **Nothing reads
  it** yet.
- `episodes.absolute_number` exists ([models.py:153](../../backend/db/models.py#L153)) and is
  populated from TVDB ([tvdb.py:129](../../backend/plugins/metadata/tvdb.py#L129) →
  [metadata_service.py:281](../../backend/services/metadata_service.py#L281)).
- The parser recognises `S01E01`, `1x01`, daily and season packs only
  ([parser.py:18-26](../../backend/domain/parser.py#L18-L26)). `[Group] Title - 27` returns
  `None`, so the scanner skips it ([scanner_service.py:265-267](../../backend/services/scanner_service.py#L265-L267))
  and the decision engine rejects it with "Could not parse season/episode".
- `Target.aliases` / `SearchCriteria.aliases` exist, but only `original_title` is ever passed
  ([search_service.py:67](../../backend/services/search_service.py#L67),
  [:86](../../backend/services/search_service.py#L86)).

## 3. Scope

### MVP

- Absolute patterns in the parser.
- A `SCENE_MAPPING` plugin kind with XEM.
- `scene_mappings` and `alternate_titles` tables.
- A mapping refresh job.
- Anime-aware decision, import and scanner logic.
- Absolute-number search queries.
- Alternate titles in matching and search.
- A series anime toggle and a mapping tab.
- The `{absolute}` naming token.

### Later

- AniDB/AniList metadata (anime-first providers, better titles, OVA handling).
- Manual mapping overrides UI (MVP: read-only view plus a "refresh mappings" button;
  `scene_mappings.source='manual'` rows are supported in the schema).
- Batch/"complete series" packs with absolute ranges (`01-12`).

### Out

- Anime-specific release profiles (preferred groups and dual-audio). These are handled by custom
  formats (02) with TRaSH's anime formats.

## 4. Design

### 4.1 Domain – parser (`backend/domain/parser.py`)

New patterns are tried **after** the existing SxxEyy patterns fail:

```python
_ABSOLUTE_PATTERNS = [
    # [Group] Title - 27 (1080p) [ABCD1234].mkv ; [Group] Title - 27v2 ; Title - 1001
    re.compile(r"^(?:\[(?P<group>[^\]]+)\][ ._-]*)?(?P<title>.+?)[ ._-]+-[ ._-]+(?P<abs>\d{1,4})(?:v(?P<ver>\d))?(?:[ ._-]*-[ ._-]*(?P<abs2>\d{1,4}))?(?=[ ._\[(]|$)"),
    # Title Episode 27 / Title Ep27 / Title E27 (no season)
    re.compile(r"(?P<title>.+?)[ ._-]+(?:episode|ep|e)[ ._-]?(?P<abs>\d{2,4})(?:v(?P<ver>\d))?\b", re.I),
    # Title.27.1080p (bare number between title and quality) – lowest confidence
    re.compile(r"^(?P<title>.+?)[ ._](?P<abs>\d{2,4})[ ._](?=(?:\d{3,4}p|web|bd|hdtv))", re.I),
]
```

`ParsedEpisode` gains four fields:

- `absolute_episodes: list[int]`;
- `version: int` (the `v2` marker);
- `numbering: "standard" | "absolute" | "daily"`;
- `crc: str | None` (the `[ABCD1234]` suffix).

Guards against false positives:

- Numbers that look like years (1900–2099) are skipped *unless* the series is known to have more
  than 1,900 episodes. A resolver caller can pass `max_absolute`.
- Resolutions (`1080`, `720`) and codecs (`264`, `265`) are skipped.
- The bare-number pattern is only returned with `confidence="low"`, and only consulted for series
  with `series_type='anime'`.

The parser stays pure and **doesn't decide** whether a number is absolute or a season-less
episode. It returns candidates, and the mapping resolver decides with series context. Existing
release names that parse with SxxEyy are unaffected.

`ParsedEpisode` also gets `release_group` from the `[Group]` prefix when present. Anime groups put
it at the front, which the existing suffix-based `release_group()` misses.

### 4.2 Domain – scene mapping resolver (`backend/domain/scene_mapping.py`, new, pure)

```python
@dataclass(frozen=True)
class EpisodeRef:
    season: int
    episode: int
    absolute: int | None

@dataclass(frozen=True)
class MappingTable:
    scene_to_tvdb: dict[tuple[int, int], EpisodeRef]        # (scene_season, scene_episode) → canonical
    scene_abs_to_tvdb: dict[int, EpisodeRef]
    tvdb_abs_to_tvdb: dict[int, EpisodeRef]                  # from episodes.absolute_number

def resolve(parsed: ParsedEpisode, table: MappingTable, *, anime: bool, use_scene: bool) -> list[EpisodeRef] | None
    """Order:
       1. SxxEyy + use_scene → scene_to_tvdb lookup, fall back to identity
       2. absolute (anime) → scene_abs_to_tvdb, then tvdb_abs_to_tvdb
       3. SxxEyy (anime, no mapping) → identity
       Returns None when nothing resolves (caller reports 'unparseable_episode' / 01)."""
```

### 4.3 Plugin contract – `PluginKind.SCENE_MAPPING`

```python
class SceneMappingProvider(Plugin):
    kind = PluginKind.SCENE_MAPPING
    async def mappings(self, tvdb_id: int) -> list[SceneMappingRow]: ...
    async def alternate_titles(self) -> dict[int, list[AltTitle]]: ...   # bulk: tvdb_id → titles (+ scene season)
```

**Built-in XEM** (`backend/plugins/scene_mapping/xem.py`):

- `GET https://thexem.info/map/all?id={tvdb}&origin=tvdb` → a list of
  `{scene:{season,episode,absolute}, tvdb:{season,episode,absolute}}`.
- `GET https://thexem.info/map/havemap?origin=tvdb` → the list of tvdb ids that *have* mappings,
  used to skip lookups for unmapped series.
- `GET https://thexem.info/map/allNames?origin=tvdb&seasonNumbers=1` → names per series, with
  optional season-specific titles (e.g. "Attack on Titan Final Season" = S4).
- Rate limited (the shared token bucket, ≤ 1 req/s) and cached. XEM is community-run and
  sometimes down, so failure keeps the last mappings.

### 4.4 Alternate titles from metadata providers

Extend `MediaMetadata` with `alternate_titles: list[AltTitle]` (title, country/language, type).

- **TMDB**: `/movie/{id}/alternative_titles` and `/tv/{id}/alternative_titles` (use
  `append_to_response` in the existing detail call to avoid extra requests).
- **TVDB v4**: `aliases` on the series record.
- **TVmaze**: `/shows/{id}/akas`.

`metadata_service` merges and deduplicates by `normalize_title`, and stores the result during
refresh.

### 4.5 Data model – migration `NNNN_scene_mapping`

`scene_mappings` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `series_id` | FK series.id, index | |
| `source` | String(16) | `xem` \| `manual` |
| `scene_season`, `scene_episode`, `scene_absolute` | Integer (nullable) | |
| `tvdb_season`, `tvdb_episode`, `tvdb_absolute` | Integer (nullable) | |
| `updated_at` | DateTime | |

Indexes are `(series_id, scene_season, scene_episode)` and `(series_id, scene_absolute)`. Manual
rows win over XEM rows for the same scene key.

`alternate_titles` table (for **both** media types):

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `media_type` | String(16) | |
| `movie_id` / `series_id` | FK nullable, index | |
| `title` | String(512) | |
| `normalized` | String(512), index | `normalize_title(title)`; used for lookups by the scanner/import matcher |
| `source` | String(16) | tmdb \| tvdb \| tvmaze \| xem \| manual |
| `language` | String(8) nullable | |
| `scene_season` | Integer nullable | XEM season-specific names |

`series` gains `use_scene_numbering` Boolean (default: true when `series_type='anime'` and XEM
has a map), and `last_mapping_refresh` DateTime.

### 4.6 Services

- **`scene_mapping_service.py`**:
  - `refresh(series_id)` pulls XEM when the series has a tvdb id and appears in `havemap`, then
    replaces the non-manual rows.
  - `refresh_names()` runs in bulk daily and upserts `alternate_titles` with `source='xem'`.
  - `table_for(db, series_id) -> MappingTable` is cached per series and invalidated on refresh.
- **Aliases everywhere**: `search_service.build_target` fills `Target.aliases` and
  `SearchCriteria.aliases` from `alternate_titles` (capped at 10, distinct by normalised title).
  **This helps every series and movie, not just anime.** `import_service.match` and the scanner's
  `by_title` maps also index alternate titles, so a folder named with a foreign title matches.
- **Decision engine** (`evaluate` for TV):
  1. Parse.
  2. For an anime series, or with `use_scene_numbering`, run `scene_mapping.resolve(parsed,
     table)` → the canonical episodes.
  3. Compare the canonical episodes with the target (instead of the raw parsed numbers).
  4. Carry `scene_label` ("Abs 27 → S02E03") into the `Decision` for the UI.
  5. Series-title matching checks aliases, including season-specific XEM names, which then also
     *imply* the season.
- **Indexer search**: for anime targets, `SearchCriteria` gains `absolute: int | None`, and
  Newznab/Torznab plugins add a query variant `q="{title} {absolute:02d}"` (with `tvsearch` when
  there's a tvdbid) alongside the standard `season`/`ep` params. The union of results is
  de-duplicated by guid. The per-indexer rate limiter already bounds the extra requests.
- **Import**: `import_queue_item` / `_import_one` resolve (season, episodes) through the resolver
  when the file name is absolute. The queue item keeps its grab-time mapping (`episode_numbers`)
  as a hint.
- **Scanner**: when `parse_episode` returns absolute numbering for an anime series, resolve via
  `tvdb_abs_to_tvdb` (library files are usually TVDB-absolute after renaming).

### 4.7 Jobs

- `scene_mapping_refresh`: every 24 h, for anime series and series with `use_scene_numbering`.
  It's also triggered when a series is switched to anime.
- The XEM names refresh is part of the same job (bulk, one request).

### 4.8 Naming

Add `{absolute}` (supports `{absolute:03d}`) and `{version}` (e.g. `v2`) to `EPISODE_TOKENS`. The
default suggested anime format is
`{Title} - S{season:02d}E{episode:02d} - {absolute:03d} - {EpisodeTitle} [{Quality}]`, which
Plex and Jellyfin parse fine and keeps the absolute number visible.

### 4.9 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| PUT | `/series/{id}` | MEDIA_WRITE | `SeriesUpdate` already accepts `series_type` ([schemas.py:245](../../backend/api/schemas.py#L245)); add `use_scene_numbering` |
| GET | `/series/{id}/scene-mappings` | MEDIA_READ | rows + source + last refresh |
| POST | `/series/{id}/scene-mappings/refresh` | MEDIA_WRITE | |
| PUT | `/series/{id}/scene-mappings` *(later)* | MEDIA_WRITE | manual rows |
| GET | `/series/{id}` / `/movies/{id}` | MEDIA_READ | gain `alternate_titles[]` |
| GET | `/series/{id}/episodes` | MEDIA_READ | episodes gain `absolute_number`, `scene: {season, episode, absolute} \| null` |
| GET | `/releases` | SEARCH | releases gain `scene_label` |

### 4.10 Frontend

- **Series detail** ([SeriesDetailPage.tsx](../../frontend/src/pages/SeriesDetailPage.tsx)):
  - A series type selector (Standard / Daily / Anime) and a "Use scene numbering" toggle.
  - Episode rows show an `Abs` column for anime, plus a scene badge when the mapping differs
    ("Scene S02E01").
  - A **Mappings** tab with a table (scene ↔ TVDB), its source and a refresh button.
  - An "Also known as" list.
- **Interactive search**: shows the scene label (e.g. "27 → S02E03") so users trust the match.
- **Import page**: shows both scene and canonical numbers when resolving candidates.
- **Add series**: suggests `anime` when the genres include Animation and the origin country is JP.

## 5. Implementation milestones

1. **Alternate titles** (metadata providers, table, aliases in search, match and scanner). This
   ships value for all libraries on its own.
2. **Parser absolute patterns** with a corpus of about 150 real anime release names and
   false-positive guards.
3. **Scene mapping plugin kind + XEM + tables + refresh job.**
4. **Resolver + decision engine + import + scanner integration.**
5. **Absolute search queries** in Newznab/Torznab.
6. **UI + naming tokens.**

## 6. Testing

- **Unit:**
  - Parser corpus: SubsPlease, Erai-raws and HorribleSubs styles, `v2`, ranges `01-12`, a title
    with a number ("86 - 05", "Mob Psycho 100 - 03"), a year-looking episode.
  - Resolver matrix (scene season split, absolute → TVDB, missing mapping fallback).
  - Alias de-duplication.
- **Integration** (XEM mocked with `httpx.MockTransport` and recorded payloads):
  - Anime series with an absolute release → the demo indexer result is approved for the right
    episode.
  - Import of `[Grp] Title - 27.mkv` → placed as S02E03.
  - Scan of absolute-named files → episodes marked with a file.
  - A foreign-title folder matches through an alternate title.
- **Regression:** every existing parser test passes unchanged.

## 7. Risks & open questions

- **Bare-number ambiguity** ("Title 2 - 05" could be season 2 or a sequel's title). Mitigated by
  per-series context, alias matching including season-specific names, and low confidence on the
  bare-number pattern.
- **XEM availability**: cached, with failures degrading to TVDB absolute numbering.
- **Open:** add AniList/AniDB as metadata providers for anime-first titles? They'd be valuable
  for OVAs and specials. Proposed as a follow-up plugin, since the metadata plugin contract
  already supports it.

## 8. Acceptance criteria

- `[Group] Title - 27 (1080p).mkv` for an anime series is parsed, matched, searched, imported and
  scanned to the correct TVDB episode.
- Scene-numbered releases for mapped series are accepted for the canonical episode.
- Series and movies match releases and folders by any alternate title from TMDB, TVDB, TVmaze or
  XEM.
- Standard series behave exactly as before.
