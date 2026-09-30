# 11 – PWA & mobile experience

> Phase 4 · Effort M (≈1.5 weeks) · Risk low · Depends on F3 (optional, for native clients later) · Priority #14

## 1. Summary & user value

The brainstorm asks for "a real mobile app, not a web UI wrapped in a mobile browser", with push
notifications, quick search, queue control, a library health overview and storage usage.

A native iOS/Android app is a separate product: two codebases (or React Native), store review,
signing and release pipelines. Most of what users actually want can be delivered by making the
existing React SPA an **installable Progressive Web App**:

- a home-screen icon, full-screen standalone window, and a splash screen;
- **Web Push notifications**, which work on Android and on iOS 16.4+ for installed PWAs;
- a mobile-first layout for the screens people use on a phone (Dashboard, Activity, Search/Add,
  Calendar, Wanted, Health);
- share-target support: share an IMDb or TMDB link from the phone → Maniac adds it.

A native app stays possible later. F3's API keys and the OpenAPI spec (`/api/openapi.json`) make
generated clients straightforward.

## 2. Current state

- The React SPA is built by Vite into `frontend/dist` and served by the backend.
- Auth: a JWT in **sessionStorage** ([auth.ts:26-33](../../frontend/src/store/auth.ts#L26-L33)),
  deliberately, so tokens die with the tab. An installed PWA would therefore log out every time
  it's closed.
- Notifications: email, Discord, Telegram and webhook plugins
  ([channels.py](../../backend/plugins/notifications/channels.py)).
- Security headers include `X-Frame-Options: DENY` and `no-store` on the API (ARCHITECTURE §9).

## 3. Scope

### MVP

- Web app manifest and icons.
- A service worker (app shell only).
- Install prompt.
- Responsive audit and fixes for the key pages.
- A **Web Push** notification plugin, with per-device subscription management.
- An optional **"Remember this device"** login using a refresh token.

### Later

- Share target (`share_target` in the manifest → `/add?url=`).
- Offline read-only cache of the dashboard (stale-while-revalidate).
- Background sync for queued actions.
- Native wrapper (Capacitor) if app-store presence becomes a goal.

### Out

- A fully native app in this iteration.

## 4. Design

### 4.1 Manifest & service worker

- Use `vite-plugin-pwa` (Workbox) as a dev dependency. It generates `manifest.webmanifest` and
  `sw.js`.
- Manifest: `name` "Maniac Media", `short_name` "Maniac", `display` standalone, `start_url` "/",
  theme and background colours from the theme tokens, icons at 192, 512 and maskable 512.
- **Caching strategy:**
  - precache the hashed build assets (JS, CSS, fonts, icons);
  - navigation requests are **network-first** with the cached `index.html` as the offline
    fallback;
  - `/api/*` is **never cached** by the service worker, which respects the existing `no-store`
    header and avoids stale or authorised data sitting on the device;
  - artwork (`/api/v1/artwork/...`) may be cache-first with a 7-day expiration and 500 entries,
    since it's public-ish images already served by library id.
- The service worker is updated with a `prompt` strategy: a "New version available – reload"
  toast.
- Backend: serve `sw.js` with `Cache-Control: no-cache` and `Service-Worker-Allowed: /`. There's no
  Content-Security-Policy today. If one is added later, it must include `worker-src 'self'`.

### 4.2 "Remember this device" (refresh tokens)

sessionStorage-only JWTs are right for shared browsers but frustrating for an installed app. So
this adds opt-in persistence.

**Model.** `device_sessions` table:

- `id`, `user_id` (FK), `name` (user agent summary, editable);
- `refresh_hash` String(64) unique: sha256 of 256 random bits, the same approach as F3;
- `created_at`, `last_used_at`, `expires_at` (default 90 days, sliding);
- `revoked_at`;
- `token_version_at_issue`.

**Flow:**

1. The login form gets a "Remember this device" checkbox, off by default.
2. When it's checked, the server returns a refresh token in an **HttpOnly, Secure, SameSite=Strict
   cookie** scoped to `/api/v1/auth/refresh`, so JavaScript never sees it and XSS can't
   exfiltrate it.
3. The access token stays short-lived and in memory or sessionStorage as today.
4. `POST /auth/refresh` rotates the refresh token on every use, with reuse detection: presenting
   an old token revokes the whole session.

**Revocation.** Password change, logout-all and a `token_version` bump all revoke device sessions.
UserSettings lists devices with a "Sign out" option per device.

### 4.3 Web Push – `webpush` notification plugin

- **VAPID key pair** is generated on first use and stored in the secret store (the same mechanism
  as the JWT signing key). The public key is exposed at `GET /notifications/webpush/public-key`.
- **Subscriptions.** `push_subscriptions` table:
  - `id`, `user_id`, `endpoint` (String(1024)) with an `endpoint_hash` unique;
  - `p256dh` and `auth` (EncryptedString);
  - `device_name`, `events` JSON (subscribed event types), `created_at`, `last_success_at`,
    `failure_count`.
- **Plugin** (`plugins/notifications/webpush.py`): uses `pywebpush` (a new dependency, pure
  Python + cryptography). `send(message)` fans out to every subscription whose `events` include
  the event, **filtered by permission**: a `read_only` user doesn't get import-failure details if
  they can't see the queue. A 404/410 response deletes the subscription.
- Payload: `{title, body, url, tag, severity}`, at most 4 KB. The `url` deep-links into the SPA
  (e.g. `/activity?item=123`). No secrets or file paths go in the payload.
- **Service worker handlers:** `push` shows the notification, and `notificationclick` focuses or
  opens the SPA at `url`.
- **UI:** Settings → Notifications → "This device" card with Enable push (permission prompt), an
  event picker and a "Send test" button.

The existing notification fan-out ([notification_service.py](../../backend/services/notification_service.py))
treats `webpush` as one more channel type, except that its recipients are subscriptions rather
than one configured destination. `notification_channels` gets a single system-managed `webpush`
row, and per-user event filters live on the subscriptions.

### 4.4 Mobile layout pass

These pages get a mobile audit (at 360 px and 414 px widths):

| Page | Mobile adaptations |
|---|---|
| Dashboard | single-column cards; health widget (04) at the top; sparkline instead of a full chart |
| Activity | queue rows become cards with swipe actions (pause, remove, retry); failures tab (01) with fix buttons |
| Add / Search | full-screen search, large poster results, one-tap add with the default profile |
| Calendar | agenda list view by default on narrow screens |
| Wanted | cards with a search button |
| Movie/Series detail | tabs collapse into accordions; action bar pinned at the bottom |

Navigation: a bottom navigation bar on narrow screens (Dashboard, Activity, Search, Calendar,
More) instead of the side drawer. Touch targets are at least 44 px.

### 4.5 API summary

| Method | Path | Perm | Notes |
|---|---|---|---|
| POST | `/auth/login` | public | gains `remember_device: bool` |
| POST | `/auth/refresh` | refresh cookie | rotates; 10/min rate limit |
| GET/DELETE | `/auth/devices`, `/auth/devices/{id}` | self | |
| GET | `/notifications/webpush/public-key` | authenticated | |
| POST/PUT/DELETE | `/notifications/webpush/subscriptions` | authenticated (own) | |
| POST | `/notifications/webpush/test` | authenticated | to this subscription |

## 5. Implementation milestones

1. Manifest, service worker, icons, update toast, and backend headers for `sw.js`.
2. Mobile layout pass (bottom nav, Activity cards, Add flow, Calendar agenda).
3. Web Push: VAPID, subscriptions, the plugin, service worker handlers and the settings card.
4. Remember-device refresh tokens with rotation and a device list.
5. *(Later)* Share target, offline dashboard snapshot.

## 6. Testing

- **Unit:** refresh-token rotation and reuse detection; the push payload builder (size cap,
  permission filtering).
- **Integration:**
  - login with remember → refresh works → the old refresh token is rejected;
  - password change revokes device sessions;
  - a push subscription CRUD round trip;
  - a failed push endpoint (410) is removed (`pywebpush` mocked).
- **E2E (Playwright):**
  - the manifest is served and the service worker registers;
  - at a mobile viewport (`devices['Pixel 7']`), Dashboard and Activity render with no
    horizontal scroll and the bottom nav is visible.
- **Manual:** install on Android Chrome and iOS Safari 16.4+, and receive a test push.

## 7. Risks & open questions

- **iOS Web Push limits** (installed PWAs only, occasional reliability issues). This is
  documented, and Telegram/Discord remain alternatives.
- **Refresh tokens widen the attack surface.** Mitigated: opt-in, HttpOnly cookie, rotation with
  reuse detection, revocable, sliding expiry.
- **HTTPS required** for service workers and push, except on `localhost`. Most self-hosters use a
  reverse proxy. The docs cover this, and the UI hides the install and push options on insecure
  origins with an explanation.
- **Open:** is a native app still wanted after the PWA? Decide after usage feedback. F3 plus
  OpenAPI keep that path open.

## 8. Acceptance criteria

- Maniac can be installed on Android and iOS home screens and opens standalone.
- Users can enable push per device and choose events. A test push arrives, and tapping it opens
  the relevant page.
- Dashboard, Activity, Add, Calendar and Wanted are usable one-handed at 360 px width.
- With "Remember this device", the installed app stays signed in across restarts, and it can be
  revoked from another device.
- API responses are never cached by the service worker.
