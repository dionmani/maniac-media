# 12 – Plugin ecosystem hardening & extension points

> Phase 5 · Effort M (≈1.5 weeks) · Risk low · Depends on 07/08/09/10 (which add plugin kinds) · Unlocks 13 (transcoder/storage plugins), 14 (storage backends) · Priority #15

## 1. Summary & user value

Maniac already has a real plugin system: contracts, a registry, drop-in files and entry points.
After phases 3 and 4 it will have eight plugin kinds instead of four. Before third parties start
building on it, the system needs:

- **a versioned plugin API**, so a plugin written today fails clearly (instead of crashing
  strangely) when a contract changes;
- **runtime health and isolation of failures**: per-call timeouts, error tracking and a visible
  last error per plugin;
- **event hook plugins**: "run my code after import", the equivalent of Radarr's Custom Script
  connection, plus renamer and import hooks;
- a **Plugins page** that shows kind, version, source, capabilities and health;
- **author documentation and a contract test kit**.

This doc also sets out an honest **trust model**. In-process Python plugins can't be sandboxed
meaningfully, so we don't pretend to.

## 2. Current state

- Contracts are in [base.py](../../backend/plugins/base.py): `Plugin` (key, name, description,
  fields, builtin, `test()`, `describe()`), plus `MetadataProvider`, `IndexerPlugin`,
  `DownloadClientPlugin` and `NotificationPlugin`.
- Discovery is in [registry.py](../../backend/plugins/registry.py): built-ins → `data/plugins/*.py`
  → the `maniac_media.plugins` entry points. Failures are recorded in `_load_errors`, and key
  collisions log a warning and override.
- `GET /plugins` returns `catalogue()`: plugins by kind, load errors, and the plugin dir.
- Plugin calls are made directly by services (e.g. `indexer_service` has its own circuit breaker
  and health; download clients have health columns). There's no uniform timeout or health layer.

## 3. Scope

### MVP

- A plugin API version and metadata.
- Compatibility checks.
- The `PluginInvoker` wrapper (timeout, metrics, last error).
- The `EventHookPlugin` kind with a built-in "Run command" hook.
- Safe mode.
- The Plugins page.
- `docs/PLUGINS.md`.
- A contract test kit.

### Later

- Plugin settings stored generically (a `plugin_configs` table for kinds without their own table).
- An in-app plugin index or install from a URL. This is deliberately not planned yet because of
  its security implications.
- Subprocess-isolated plugins over JSON-RPC, for untrusted code (see the trust model).

### Out

- A marketplace, signing or a review process.

## 4. Design

### 4.1 Versioning & metadata

In `plugins/base.py`:

```python
PLUGIN_API_VERSION = (1, 0)       # major, minor — bump major on breaking contract changes

class Plugin(ABC):
    ...
    version: ClassVar[str] = "0.0.0"                 # plugin's own version
    api_version: ClassVar[tuple[int, int]] = (1, 0)  # contract version it was written against
    author: ClassVar[str | None] = None
    homepage: ClassVar[str | None] = None
    capabilities: ClassVar[frozenset[str]] = frozenset()   # e.g. {"rss", "search", "partial_refresh"}
    call_timeout: ClassVar[float] = 30.0
```

In `registry.register`, reject when `cls.api_version[0] != PLUGIN_API_VERSION[0]` and record a
load error ("written for plugin API 2.x, this Maniac provides 1.x"). A higher **minor** version
than ours logs a warning (it may use features we lack). Built-ins always match.

Existing `ClassVar` capability flags (`supports_rss`, `supports_partial_refresh`, …) are mirrored
into `capabilities` by `describe()` for uniform display. The flags stay for code readability.

### 4.2 Runtime wrapper – `backend/plugins/invoker.py`

```python
class PluginInvoker:
    """Uniform call path: timeout, metrics, error capture, health state. Services call plugins through this."""
    async def call(self, plugin: Plugin, method: str, *args, timeout: float | None = None, **kwargs)
```

It does the following:

- `asyncio.wait_for(getattr(plugin, method)(*args, **kwargs), timeout or plugin.call_timeout)`;
- records `maniac_plugin_calls_total{kind,key,method,outcome}` and
  `maniac_plugin_call_seconds{kind,key,method}`;
- keeps in-memory health per `(kind, key, instance_id)`: last success, last error (message, type,
  timestamp), consecutive failures. This is exposed through `/plugins/health`;
- converts unexpected exceptions into `PluginError`, which carries the original type name. The
  traceback is logged at debug level only (tracebacks may contain secrets from URLs; the
  `RedactingFilter` still applies).

Adoption is incremental. New plugin kinds (07–10) use the invoker from day one, and the existing
services (`indexer_service`, `download_service`, `metadata_service`,
`notification_service`) migrate one at a time. `indexer_service` keeps its circuit breaker and
calls the invoker inside it.

### 4.3 Event hooks – `PluginKind.EVENT_HOOK`

```python
class EventHookPlugin(Plugin):
    kind = PluginKind.EVENT_HOOK
    events: ClassVar[frozenset[str]] = frozenset()   # EventType values it wants; empty = configurable
    async def handle(self, event: Event) -> None: ...
```

- Configured instances (`event_hooks` table: id, name, implementation, settings,
  secret_settings, events, enabled) subscribe through the existing bus
  ([events.py](../../backend/core/events.py)).
- Hooks run **through the job queue** (an `event_hook` job type, max_attempts 3), never inline on
  the bus, so a slow hook can't block imports and runs survive a restart.
- **Built-in "Run command" hook:**
  - Settings: an executable path, argument template and timeout.
  - It executes with `asyncio.create_subprocess_exec` (**no shell**). The event payload is passed
    as environment variables (`MANIAC_EVENT=import.completed`, `MANIAC_MOVIE_ID=…`,
    `MANIAC_FILE_PATH=…`), the Arr-compatible pattern, plus JSON on stdin.
  - Creating one requires `SYSTEM_ADMIN`, since it's arbitrary command execution by design. It's
    audited, and the executable must exist and be absolute.
- The existing webhook notification plugin already covers HTTP. Event hooks are for local
  automation.

**Import and rename extension points** (synchronous, pure-domain hooks):

- `NamingTokenProvider` (a sub-kind): contributes extra tokens to the renderer (e.g.
  `{mediainfo_*}` from 03 could be implemented this way).
- `ReleaseFilter`: extra accept/reject rules in the decision engine, returning rejection strings.

Both run in-process with a strict time budget (they're pure and must not do I/O). They're
declared in `plugins/base.py` so the core never imports them concretely.

### 4.4 Safe mode

`MANIAC_SAFE_MODE=1` (an env var, matching the existing `MANIAC_*` configuration; `run.py` takes no CLI
arguments today) skips `data/plugins` and entry-point
discovery, so users can recover when a third-party plugin breaks startup. The System page shows a
"Safe mode – external plugins disabled" banner.

### 4.5 Trust model (documented in PLUGINS.md and the UI)

- **Plugins are trusted code.** A drop-in or pip-installed plugin runs in the Maniac process with
  full access to the database session factory, the secret store and the filesystem. Installing a
  plugin is equivalent to installing any Python package.
- What we provide is **fault isolation** (timeouts, exceptions contained, load failures skipped,
  safe mode). It isn't **security isolation**.
- Real sandboxing would need a subprocess with JSON-RPC over stdio, a restricted environment and
  no DB access. It's noted as a later option for untrusted plugins, and the contracts
  (DTO-in/DTO-out) are already serialisable, which keeps it feasible.

### 4.6 API

| Method | Path | Perm | Notes |
|---|---|---|---|
| GET | `/plugins` | SETTINGS_READ | adds `version`, `api_version`, `author`, `homepage`, `capabilities`, `source` (`builtin` \| `dropin` \| `entrypoint`), `module`, `compatible` |
| GET | `/plugins/health` | SYSTEM_READ | per configured instance: last success/error, consecutive failures, p95 latency |
| POST | `/plugins/reload` | SYSTEM_ADMIN | re-runs discovery (`discover(force=True)`) for drop-ins. New drop-ins appear without a restart; changed modules still need a restart, which is documented |
| GET/POST/PUT/DELETE | `/event-hooks` | SYSTEM_ADMIN | |
| POST | `/event-hooks/{id}/test` | SYSTEM_ADMIN | fires a synthetic `test` event |

### 4.7 Frontend

- **System → Plugins** (a new tab in [SystemPage.tsx](../../frontend/src/pages/SystemPage.tsx)):
  - a table grouped by kind with name, version, source badge, capabilities chips and a health dot;
  - a load-errors panel (existing data, better presented);
  - "Reload drop-ins";
  - links to the configuration page for each kind (Indexers, Clients, Media servers, …).
- **Settings → Connections → Event hooks**: the list and editor, with a warning banner explaining
  command execution.

### 4.8 Author documentation & test kit

- `docs/PLUGINS.md` (new): contracts per kind with DTO references, field specs, a minimal example
  per kind, packaging via `pyproject` entry points, versioning rules, the trust model, and
  debugging (`/plugins`, safe mode).
- `backend/plugins/testing.py`: a pytest helper, `assert_plugin_contract(cls)`. It checks
  key/name/fields validity, that `describe()` is JSON-serialisable, that async methods are
  coroutines, and that the API version is compatible. It also provides fake DTO factories for
  plugin authors' own tests. Built-in plugins run it in CI.
- A template repo layout, described in PLUGINS.md.

## 5. Implementation milestones

1. Versioning metadata, the compatibility check in `register`, and extended `describe()`.
2. `PluginInvoker` plus metrics. Adopt it in the new kinds, then migrate `notification_service`
   and `metadata_service`.
3. Safe mode.
4. `EventHookPlugin`, the `event_hooks` table, the job type, and the built-in Run command hook.
5. The Plugins page, health endpoint and reload.
6. PLUGINS.md and the contract test kit, run in CI for all built-ins.
7. *(Later)* `NamingTokenProvider` and `ReleaseFilter` extension points.

## 6. Testing

- **Unit:**
  - An incompatible major version is rejected with the error recorded.
  - Invoker timeout → `PluginError` and health updated.
  - Metrics labels.
  - Run command argv building (no shell; env vars; path must be absolute).
- **Integration:**
  - A drop-in plugin fixture in a temp `data/plugins` appears in `/plugins` with
    `source=dropin`, and a broken one appears in errors.
  - Safe mode hides both.
  - An event hook receives `import.completed` via the job queue after a pipeline import.
- **Contract kit** runs against every built-in plugin.

## 7. Risks & open questions

- **Wrapper overhead** is negligible (microseconds) compared with network calls.
- **Run command is RCE by design.** It's admin-only, audited, has no shell, and is documented.
- **Open:** do we want to publish a separate `maniac-media-plugin-sdk` package (contracts + DTOs
  only) so plugin authors don't depend on the whole app? Useful once third-party plugins exist.
  It's deferred.

## 8. Acceptance criteria

- Every plugin reports a version and API version. Incompatible plugins are skipped with a clear
  error.
- A hanging plugin call times out without blocking the worker pool, and its error is visible on
  the Plugins page.
- Users can configure a "Run command" hook that fires after imports with the event data in
  environment variables.
- `MANIAC_SAFE_MODE=1` starts Maniac with only built-in plugins.
- PLUGINS.md lets a developer write, test and package a new indexer plugin without reading core
  code.
