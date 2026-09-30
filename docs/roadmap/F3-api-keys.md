# F3 – API keys for integrations

> Phase 3 · Effort S (≈3 dev-days) · Risk low · Unlocks 07 (inbound webhooks), 11 (native/remote clients), iCal feed, Overseerr / Home Assistant / Homepage widgets

## 1. Summary & user value

Everything outside the browser currently needs a user's password so it can get a JWT that
expires after 12 hours. Overseerr, Home Assistant, dashboard widgets (Homepage, Homarr),
Plex/Jellyfin webhooks, calendar apps (iCal) and a future native app all need a **long-lived,
revocable, least-privilege credential**. Radarr and Sonarr use an `X-Api-Key` header, and most
tools in the ecosystem already know how to send one.

## 2. Current state

- Auth is Bearer JWT only: `current_user()` ([deps.py:59](../../backend/api/deps.py#L59)) decodes
  the token, checks `token_version`, and loads the user. `Requires(permission)`
  ([deps.py:85](../../backend/api/deps.py#L85)) enforces RBAC.
- Roles map to fixed permission sets ([security.py:45](../../backend/core/security.py#L45)).
- The rate limiter is per client, and there's a stricter one on auth endpoints.
- ARCHITECTURE §18 item 5 already plans for this.

## 3. Scope

### MVP

- Named API keys owned by a user, with a permission subset no larger than the owner's role.
- Optional expiry.
- `X-Api-Key` header auth.
- A `?apikey=` query parameter accepted **only** on routes that opt in (webhooks, `calendar.ics`).
- Key management UI.
- Audit.

### Later

- Per-key rate limits.
- IP allow-lists.
- OIDC SSO (see [15](15-deferred-and-reframed.md)).

### Out

- OAuth2 authorization-server features (third-party app consent screens).

## 4. Design

### 4.1 Domain / core

In `core/security.py`:

```python
API_KEY_PREFIX = "mm_"
def generate_api_key() -> tuple[str, str, str]:
    """returns (plaintext 'mm_<43 url-safe chars>', sha256 hex digest, display prefix 'mm_ab12cd')"""
def hash_api_key(plaintext: str) -> str  # sha256; high-entropy secrets don't need argon2
```

Keys are 256-bit random values, so a fast hash is fine and lookups stay O(1) by digest. That's
the same reasoning GitHub uses for its tokens.

The `mm_` prefix lets `RedactingFilter` scrub keys from logs. Add the pattern
`mm_[A-Za-z0-9_-]{20,}` and also scrub the `apikey=` query parameter, which is already covered.

### 4.2 Data model – migration `NNNN_api_keys`

`api_keys` table:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(128) | e.g. "Overseerr", "Home Assistant" |
| `key_hash` | String(64), unique | sha256 |
| `prefix` | String(16) | shown in UI to identify the key |
| `user_id` | FK users.id, index | acts as this user |
| `permissions` | JSON | list of `Permission` values; must be ⊆ owner's role at use time |
| `allow_query_param` | Boolean default false | needed for webhook/iCal URLs |
| `created_at`, `expires_at` (nullable), `last_used_at`, `last_used_ip` | DateTime/String | |
| `revoked_at` | DateTime nullable | |

`last_used_at` is written at most once a minute per key (an in-memory throttle), so every request
doesn't turn into a write.

### 4.3 Auth resolution (`api/deps.py`)

`current_user()` becomes an ordered resolver:

1. `Authorization: Bearer <jwt>` → existing path.
2. `X-Api-Key: mm_…` → look up by hash and reject if revoked or expired. Load the owner and
   reject if the owner is inactive or locked. The effective permissions are
   `set(key.permissions) & ROLE_PERMISSIONS[owner.role]`, recomputed on every request, so
   demoting the owner narrows the key immediately.
3. `?apikey=` → only if the route declares `api_key_query=True` **and** the key has
   `allow_query_param`.

`CurrentUser` gains `auth_method: "jwt" | "api_key"` and `api_key_id`. `Requires()` checks the
effective permission set.

Some actions stay **JWT-only** whatever permissions a key has: user management, API key
management, password change, database migration, and security settings. Keys can't mint keys.
This is implemented with a `Requires(..., interactive=True)` flag.

Failed key auth counts toward the per-IP auth rate limiter (10/min), which slows brute force.
It's hopeless against 256-bit keys anyway, but it keeps the logs quiet.

### 4.4 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/api-keys` | self (own keys); `USERS_MANAGE` sees all | never returns plaintext |
| POST | `/api-keys` | self, **JWT only** | body `{name, permissions[], expires_in_days?, allow_query_param}`; returns plaintext **once** |
| POST | `/api-keys/{id}/revoke` | owner or `USERS_MANAGE`, JWT only | |
| DELETE | `/api-keys/{id}` | same | hard delete after revoke |
| GET | `/api-keys/presets` | self | permission presets: *Read-only dashboard*, *Request manager (Overseerr)*, *Webhook receiver*, *Calendar feed* |

Every create, revoke and delete is audited in the `security` category. A request made with a key
that fails a permission check is audited too, just as JWT denials are today.

Password changes and logout already bump `token_version`. **They don't revoke API keys**, since
integrations shouldn't break when a user logs out. The Users page gets a "Revoke all keys"
button, and deactivating a user makes all their keys fail.

### 4.5 Frontend

`pages/settings/UserSettings.tsx` gets an "API keys" section:

- a table of name, prefix, permissions summary, created, last used and expiry;
- a create dialog with a preset picker and checkboxes;
- a one-time reveal dialog with a copy button and a "this won't be shown again" warning.

### 4.6 Consumers enabled by this

- **07**: `POST /webhooks/plex`, `POST /webhooks/jellyfin` (`api_key_query=True`).
- **15**: `GET /calendar.ics?apikey=…` (read-only calendar feed).
- **External tools**: Overseerr/Jellyseerr (these expect the Radarr/Sonarr API shape; a thin
  compatibility shim is a separate, later decision, see [15](15-deferred-and-reframed.md)).

## 5. Implementation milestones

1. Model, migration, and the `generate/hash` helpers with unit tests.
2. Resolver in `deps.py` plus the JWT-only guard. Integration tests cover each resolver branch.
3. CRUD API, audit and presets.
4. UI section.

## 6. Testing

- **Unit:** key format and prefix; hash determinism; redaction of `mm_…` in log lines.
- **Integration:**
  - A key with `media:read` can read `/movies` but gets 403 on `/movies/bulk`.
  - Demoting the owner to `read_only` removes the key's write permissions immediately.
  - A revoked key gets 401. An expired key gets 401.
  - `?apikey=` is rejected on normal routes and accepted on an opted-in test route.
  - A key can't create keys (403).
  - Plaintext is never present in list responses.

## 7. Risks & open questions

- **Keys in URLs** (`?apikey=`) leak into proxy logs and browser history. That's why it's opt-in
  per key and per route, and documented as such.
- **Open:** do we want Radarr-compatible `X-Api-Key` routes (e.g. `/api/v3/…`) so Overseerr can
  talk to Maniac natively? That's a large compatibility surface, deferred and tracked in
  [15](15-deferred-and-reframed.md).

## 8. Acceptance criteria

- A user can create a scoped key, see the plaintext once, and use it with `X-Api-Key` from curl.
- A key's effective permissions never exceed its owner's current role.
- Revocation takes effect on the next request.
- No plaintext keys appear in logs, audit entries or API responses after creation.
