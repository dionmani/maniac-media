# 06 – Edition-aware movie & episode tracking

> Phase 3 · Effort L (2–3 weeks) · Risk low · Depends on 02 (edition spec), F1 (per-file edition) · Priority #10

## 1. Summary & user value

Radarr treats "Blade Runner: The Final Cut", the Director's Cut and the theatrical cut as the
same movie with one file slot. Collectors either give up or maintain separate root folders by
hand. Plex, Jellyfin and Emby all understand multiple editions: Plex reads `{edition-Director's
Cut}` in filenames, and Jellyfin/Emby group `Movie - Director's Cut.mkv` style versions. So the
media-server side is already solved. Only the acquisition side is missing.

This feature has two modes:

- **Single mode (default):** a movie has one file slot, and editions are a *preference*. For
  example, "prefer IMAX, avoid Theatrical" is expressed through a custom-format edition spec (02).
- **Multi mode (opt-in per movie):** the user picks the editions they want (e.g. Theatrical +
  Director's Cut). Each wanted edition becomes its **own slot**: searched, grabbed, imported,
  upgraded and named independently, and placed side by side in the movie folder.

For TV, editions are detected and labelled (extended episodes, uncut) but aren't separate slots
in this iteration. Specials, OVAs and recaps are handled by anime and scene mapping (08).

## 2. Current state

- Parser: no edition detection. `parse_movie`
  ([parser.py:94](../../backend/domain/parser.py#L94)) cuts the title at the year.
- Naming: an `{edition}` token is **declared** in `MOVIE_TOKENS`
  ([naming.py:27-39](../../backend/domain/naming.py#L27-L39)) but never populated.
  `movie_destination` doesn't pass it
  ([import_service.py:180-184](../../backend/services/import_service.py#L180-L184)).
- A movie has exactly one `file_path`. On an upgrade, `_import_one` recycles the existing file
  ([import_service.py:223-227](../../backend/services/import_service.py#L223-L227)), so importing
  a second edition would delete the first.

## 3. Scope

### MVP

- Edition parsing (canonical set, aliases and Plex syntax).
- `media_files.edition` (F1).
- Edition custom-format spec (02).
- `{edition}` / `{Edition Tags}` naming.
- Multi-edition mode for movies (wanted editions, per-edition search/import/upgrade, UI).

### Later

- TV multi-edition slots.
- Edition-aware duplicate grouping defaults (05 uses edition in its group key once multi mode
  exists; that's already designed).
- Edition data from TMDB release types (theatrical vs extended runtime).

### Out

- Sports alternate broadcasts and documentary regional cuts (see [15](15-deferred-and-reframed.md)).

## 4. Design

### 4.1 Domain – `backend/domain/editions.py` (new, pure)

```python
CANONICAL = {
    "theatrical": "Theatrical",
    "directors_cut": "Director's Cut",
    "extended": "Extended",
    "unrated": "Unrated",
    "uncut": "Uncut",
    "imax": "IMAX",
    "imax_enhanced": "IMAX Enhanced",
    "remastered": "Remastered",
    "anniversary": "Anniversary Edition",
    "criterion": "Criterion",
    "special_edition": "Special Edition",
    "final_cut": "Final Cut",
    "ultimate": "Ultimate Edition",
    "open_matte": "Open Matte",
    "redux": "Redux",
    "black_and_white": "Black & White",   # "Chrome", "Noir" editions
}
ALIASES: list[tuple[re.Pattern, str]]   # "DC", "Directors.Cut", "Dir.Cut" → directors_cut;
                                         # "Extended.Cut", "Extended.Edition", "EE" → extended;
                                         # "IMAX.Enhanced" before "IMAX"; "4K.Remaster(ed)" → remastered;
                                         # "25th.Anniversary" → anniversary …

def parse_edition(name: str) -> Edition | None
    """1) Plex syntax {edition-XYZ} (verbatim label, canonicalised if known)
       2) alias patterns in the region between the year and the quality tokens
          ("Movie.2019.Directors.Cut.1080p…"), so titles like "Extended Family (2019)" don't match
       3) None"""

@dataclass(frozen=True)
class Edition:
    key: str        # canonical key or "custom:<slug>"
    label: str      # display / naming label

def plex_suffix(edition: Edition) -> str   # "{edition-Director's Cut}"
```

Restricting the alias search to the region after the year is essential. Otherwise titles
containing "Extended", "Final Cut" or "Redux" would be misparsed. The parser already locates the
year ([parser.py:97-106](../../backend/domain/parser.py#L97-L106)), so pass the slice after it.

`ParsedMovie` gains an `edition: Edition | None` field. `parse_episode` also records the edition
(labelling only).

### 4.2 Custom-format integration (02)

Add `SpecKind.EDITION`, whose value is a canonical key or a regex. With it, single mode needs no
special logic: "prefer IMAX +50, penalise Theatrical −20" are just format scores. TRaSH's "IMAX",
"IMAX Enhanced", "Criterion" and "Special Edition" formats import directly.

### 4.3 Data model – migration `NNNN_editions`

- `movies.edition_mode` String(8) default `single`.
- `movie_edition_wants` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `movie_id` | FK movies.id, index | |
| `edition_key` | String(64) | canonical or `custom:<slug>` |
| `label` | String(128) | |
| `monitored` | Boolean default true | |
| `quality_profile_id` | Integer nullable | defaults to the movie's profile |
| unique | (`movie_id`, `edition_key`) | |

- `media_files.edition` (from F1) holds the edition key. It's set on import and scan.
- `history.data.edition` records it on grab and import events.

In multi mode, the legacy `movies.has_file` means "at least one wanted edition has a file", and
`movies.file_path` mirrors the **first wanted edition's** primary file. That keeps existing
readers sensible.

### 4.4 Services

- **Search** (`search_service.build_target`): in multi mode, build **one target per monitored
  edition that lacks a file or is below cutoff**. `Target` gains `edition: str | None`. The
  indexer query is unchanged (indexers don't search by edition), but the release title is
  filtered on the client side.
- **Decision engine**:
  - In multi mode, `evaluate` rejects a release whose parsed edition ≠ the target edition, with
    the reason "Edition mismatch (Extended vs Director's Cut)". A release with **no** edition
    token matches the `theatrical` target, since that's the scene convention.
  - In single mode, editions only affect format scores (02).
- **Download**: the queue row gains an `edition` column (String(64), nullable) so the import knows
  which slot it fills.
- **Import** (`_import_one`):
  - The existing-file lookup becomes "the primary file **for this edition**" (via F1
    `media_files` filtered by edition), so importing a Director's Cut never recycles the
    Theatrical file.
  - The destination filename uses the naming format with `{edition}`. The default movie format
    becomes `{Title} ({Year}) {edition-{Edition}} [{Quality}]` when multi mode is used anywhere.
    The renderer omits the braces segment when the edition is empty (see §4.5).
  - Upgrade logic is per edition.
- **Scanner**: sets `media_files.edition` from `parse_edition(filename)`. In multi mode, each
  distinct edition file is primary for its own slot. Files for editions that aren't wanted are
  non-primary and surface as duplicates (05).
- **Wanted**: missing and cutoff views list per-edition rows for multi-mode movies, e.g. "Blade
  Runner (1982) – Final Cut".

### 4.5 Naming

- Populate `edition` in `movie_destination` values (the label).
- Add an **optional-segment syntax** to the allow-list renderer
  ([naming.py:95](../../backend/domain/naming.py#L95)): `{edition-{Edition}}` renders to
  `{edition-Director's Cut}` when the edition is set and to nothing otherwise, collapsing doubled
  spaces. This is a narrowly defined construct (a literal prefix, one token, a literal suffix)
  and keeps the renderer allow-list based.
- Add the renamer preview to the edition tests: existing multi-edition files get Plex-compatible
  names on bulk rename.

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/movies/{id}` | MEDIA_READ | adds `edition_mode`, `editions: [{key,label,monitored,has_file,file_id,quality,cutoff_met}]` |
| PUT | `/movies/{id}/editions` | MEDIA_WRITE | `{edition_mode, wanted: [{key|label, monitored, quality_profile_id?}]}` |
| GET | `/editions` | MEDIA_READ | canonical list with labels (for pickers) |
| POST | `/search/automatic` | SEARCH | accepts optional `edition` |
| GET | `/releases?movie_id=&edition=` | SEARCH | filters and annotates by edition |

### 4.7 Frontend

- **Movie detail**:
  - an "Editions" section with a Single/Multi toggle;
  - in multi mode: edition chips (Theatrical, Director's Cut, Extended…) with a
    file/missing/upgrade status and per-edition search buttons;
  - an "Add edition" picker (canonical list plus a custom label);
  - in single mode: shows the detected edition of the current file.
- **Interactive search**: an edition column and a filter, where releases for editions other than
  the target are greyed out with the reason.
- **Library list**: an "Editions: 2/3" badge for multi-mode movies.
- **Settings → Naming**: the `{edition-{Edition}}` example in the token help, plus a Plex/Jellyfin
  note.

## 5. Implementation milestones

1. `parse_edition` with an extensive corpus (including false-positive titles: "Extended Family",
   "The Final Cut (2004)", "Apocalypse Now Redux" where Redux *is* the edition, "Unrated" in
   group names).
2. `media_files.edition` population on import and scan, plus the `{edition}` naming token and
   optional segment.
3. Custom-format `EDITION` spec (single-mode preferences).
4. Multi mode: migration, wants CRUD, per-edition targets, decision rejection, import slotting,
   wanted views.
5. UI.

## 6. Testing

- **Unit:**
  - `parse_edition` corpus, including Plex syntax round-trip.
  - The optional-segment renderer.
  - Decision rejection matrix (target edition × release edition × no-edition release).
- **Integration:**
  - A multi-mode movie wanting Theatrical + Director's Cut: grab and import a DC release, then a
    theatrical release → two files side by side, and neither is recycled.
  - A DC upgrade recycles only the old DC file.
  - Scanning a folder with both editions → two primary files, and no duplicate issue.
  - Single mode: an IMAX format score changes the ranking.
- **E2E:** toggle multi mode and add an edition, and the chips render with their status.

## 7. Risks & open questions

- **Edition false positives** in titles. Mitigated by the region-restricted parsing and the
  corpus tests.
- **"No edition = Theatrical"** is a convention, not a guarantee. It's documented in the UI, and
  users can mark a file's edition manually (`PUT /media-files/{id}` with an `edition` field).
- **Open:** should TMDB alternative runtimes be used to *verify* an edition (e.g. an extended cut
  is 20+ minutes longer)? Possible with 03's duration and noted for later.

## 8. Acceptance criteria

- Edition is detected from release and file names, including Plex `{edition-…}` syntax, without
  misparsing titles.
- In single mode, users can prefer or avoid editions via custom formats.
- In multi mode, each wanted edition is searched, imported, upgraded and named independently, and
  importing one edition never removes another.
- Files are named in a way Plex and Jellyfin recognise as editions of the same movie.
