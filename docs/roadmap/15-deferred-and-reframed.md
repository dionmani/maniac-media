# 15 – Deferred & reframed ideas

This covers brainstorm ideas that aren't scheduled as standalone features. They're either:

- reframed into something more concrete and already covered by another doc;
- small enough to be add-ons to another feature; or
- deliberately deferred, with the reason.

Nothing from the brainstorm has been dropped silently. See the mapping table in the
[README](README.md#6-brainstorm--roadmap-mapping).

---

## 1. "AI-powered metadata correction" → reframed

**What was proposed:** detect mislabelled releases, fix wrong season or episode numbers, identify
wrong posters, rename based on actual content, recognise foreign titles, and match by video
fingerprint.

**Why it's reframed.** Most of this is solvable with deterministic signals we'll already have,
and they're predictable, explainable and free to run:

| Sub-problem | Covered by |
|---|---|
| Mislabelled quality (says 1080p, is 720p) | [03](03-media-inspection-ffprobe.md) `quality_mismatch` |
| Wrong episode imported | [03](03-media-inspection-ffprobe.md) `runtime_mismatch` (duration vs episode runtime) |
| Wrong season/episode numbers from scene releases | [08](08-anime-scene-mapping.md) scene mapping (XEM) |
| Foreign or alternative titles not matching | [08](08-anime-scene-mapping.md) alternate titles (TMDB, TVDB, TVmaze, XEM), used by search, import and scanner |
| Import failures from naming or matching | [01](01-import-failure-diagnosis.md) diagnosis |

**What's left, and deferred:**

- **Wrong posters.** Small: add a per-title artwork override (pick from the provider's image list
  or upload one). It's a candidate add-on to the metadata service, S effort.
- **Unmatched folders.** The scanner creates titles from folder names without a provider match
  (`metadata_source` null). Add a fuzzy "suggested match" step to the metadata refresh: search
  the providers by the parsed title and year, rank by normalised similarity, auto-link above a
  high threshold, and otherwise raise an `unmatched_folder` issue (F2) with the top 3
  suggestions and one-click linking. It's deterministic, M effort, and a good follow-up to 08.
- **Optional LLM assist, opt-in and off by default.** For the few items left unresolved after the
  fuzzy step, an optional plugin could send **only the filename and folder name** (no paths
  beyond the leaf, no user data) to an LLM API and ask for a structured title/year/season/episode
  guess, which is then verified against the metadata providers before it's offered as a
  suggestion. It would be a `MetadataAssist` plugin kind so it stays out of core. Deferred until
  the deterministic pipeline is in place and there's evidence of residual need.
- **Video fingerprinting** (matching content by perceptual hash or audio fingerprint): there's no
  public reference database for TV episodes or movies, so it would need a crowd-sourced dataset.
  Out of scope.

## 2. "Smart airing calendar" → add-ons

The existing Calendar ([CalendarPage.tsx](../../frontend/src/pages/CalendarPage.tsx)) gets these
small enhancements. They can be picked up at any time:

| Idea | Implementation | Depends on |
|---|---|---|
| Premiere / finale / mid-season tags | Derived at render time: E01 is *Season Premiere*, S01E01 is *Series Premiere*, and the last episode of a season in metadata is *Season Finale* (*Series Finale* if the series status is ended). A long gap after an episode (more than 21 days before the next air date) is *Mid-season Finale*. Pure function in `domain/`. | – |
| Holiday specials | S00 episodes with air dates in late December get a *Special* tag (a heuristic). | – |
| "Returning series" notification | During metadata refresh, a new season appearing for a series whose previous season ended more than 60 days ago raises `EventType.SERIES_RETURNING`. Notification channels can subscribe. | – |
| Auto-download rules per show | Already covered by monitoring presets and `monitor_new_seasons`. The "hold next season until watched" variant is in [07](07-media-server-integration.md) stage C. | 07 |
| Auto-pause shows you're behind on | [07](07-media-server-integration.md) `pause_if_unwatched`. | 07 |
| iCal feed | `GET /calendar.ics?apikey=…` (RFC 5545, all-day events, 30 days back / 90 days forward, UID per episode) for Google, Apple or Outlook calendars. | [F3](F3-api-keys.md) |

## 3. "Unified dashboard showing everything" → assembled incrementally

This doesn't need a separate project. It's the sum of the dashboard widgets that each feature
contributes:

| Widget | From |
|---|---|
| Health score + top actions | [04](04-library-health-score.md) |
| Failures by root cause | [01](01-import-failure-diagnosis.md) |
| Recently watched / stale series / watch-driven savings | [07](07-media-server-integration.md) |
| Savings potential / storage heatmap | [13](13-storage-optimization.md) |
| Duplicate reclaimable space | [05](05-duplicate-intelligence.md) |
| Collections coverage | [09](09-import-lists-collections.md) |
| Roots by tier | [14](14-hybrid-cloud-storage.md) |

Once three or more of these exist, do a **dashboard layout pass**: user-arrangeable cards with a
persisted layout per user (a `user_preferences` JSON column), and hideable widgets. That's M
effort, scheduled after phase 3.

## 4. Sports & documentaries → out of scope (for now)

- **Sports** don't fit the series/season/episode model: events, rounds, leagues, replays. The
  existing *daily* series type (air-date based) covers simple cases like a weekly show. Proper
  support needs a sports-specific metadata provider (e.g. TheSportsDB) and naming model. That's
  roughly a separate media type, and it isn't justified yet.
- **Documentary regional cuts** can use editions ([06](06-edition-tracking.md)) with custom
  labels (e.g. "UK Version", "US Version").

## 5. From ARCHITECTURE §18 – not in the brainstorm but still pending

| Item | Status |
|---|---|
| OIDC single sign-on | Deferred. Add an `oidc` login provider (Authentik, Authelia, Keycloak) mapping groups to roles. It sits naturally after [F3](F3-api-keys.md), M effort. Many self-hosters already put a forward-auth proxy in front, so demand should be validated first. |
| Kubernetes Helm chart | Deferred. Community-contributable. The Docker image already runs as non-root with a read-only root filesystem, so it's Helm-ready. |
| Horizontally scaled read replicas for the API tier | Deferred indefinitely. Single-process by design (ARCHITECTURE §1). The DB-backed job queue and in-process event bus would need a leader election and a shared bus (Postgres LISTEN/NOTIFY) first. There's no demand at self-hosted scale. |
| Radarr/Sonarr-compatible API shim (for Overseerr/Jellyseerr) | Deferred. It would let request managers talk to Maniac natively via `/api/v3/movie`, `/api/v3/series` and friends. It's a large, moving surface. Reconsider after [F3](F3-api-keys.md) if users ask for Overseerr integration specifically. |
| Native mobile app | Deferred behind the PWA ([11](11-pwa-mobile.md)). |
| Subtitle auto-sync correction | Deferred, as an optional plugin in [10](10-subtitles.md). |
