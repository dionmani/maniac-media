"""Application settings.

Non-secret configuration lives in ``data/config.json`` and is validated by a Pydantic
model. Anything secret (database URL, API keys, signing keys) lives in the encrypted
secret store instead – ``config.json`` never contains credentials.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.core import paths
from backend.core.secrets import SECRET_DB_URL, SecretError, cipher, secret_store

log = logging.getLogger("maniac.settings")

DbType = Literal["sqlite", "postgresql", "mysql", "mariadb", "mssql"]


class MetadataProviderSettings(BaseModel):
    enabled: bool = True
    priority: int = 100


class AppSettings(BaseModel):
    # --- database (connection URL itself is secret, only the engine name lives here)
    db_type: DbType = "sqlite"

    # --- library layout
    # Env vars only seed the defaults on first run (e.g. inside Docker); config.json wins afterwards.
    media_root_movies: str = Field(default_factory=lambda: os.environ.get("MANIAC_MOVIES_DIR", str(paths.BASE_DIR / "media" / "Movies")))
    media_root_tv: str = Field(default_factory=lambda: os.environ.get("MANIAC_TV_DIR", str(paths.BASE_DIR / "media" / "TV Shows")))
    download_folder: str = Field(default_factory=lambda: os.environ.get("MANIAC_DOWNLOADS_DIR", str(paths.BASE_DIR / "downloads")))
    recycle_bin: str = str(paths.RECYCLE_DIR)

    # --- naming templates (see backend/domain/naming.py for tokens)
    movie_folder_format: str = "{Title} ({Year})"
    movie_format: str = "{Title} ({Year}) [{Quality}]"
    series_folder_format: str = "{Title}"
    tv_format: str = "Season {season:02d}/{Title} - S{season:02d}E{episode:02d} - {EpisodeTitle} [{Quality}]"
    rename_on_import: bool = True

    # --- import engine
    auto_import: bool = True
    import_mode: Literal["move", "copy", "hardlink", "symlink"] = "move"
    min_import_size_mb: int = 0
    watch_download_folder: bool = True
    import_untracked_downloads: bool = False  # also import files dropped in the folder by hand
    recycle_bin_days: int = 30
    follow_symlinks: bool = False
    auto_fix_imports: bool = True  # apply safe (non-settings-changing) import failure fixes automatically
    extra_file_extensions: list[str] = Field(default_factory=lambda: [".srt", ".ass", ".sub", ".idx", ".nfo"])

    # --- automation
    rss_sync_interval_minutes: int = 15
    search_on_add: bool = False
    auto_retry_failed_downloads: bool = True
    max_download_retries: int = 2

    # --- metadata
    metadata_language: str = "en"
    metadata_providers: dict[str, MetadataProviderSettings] = Field(
        default_factory=lambda: {
            "tmdb": MetadataProviderSettings(priority=10),
            "tvdb": MetadataProviderSettings(priority=20),
            "tvmaze": MetadataProviderSettings(priority=30),
            "imdb": MetadataProviderSettings(priority=40),
        }
    )
    metadata_refresh_batch_size: int = 150

    # --- security / API
    access_token_ttl_minutes: int = 720
    rate_limit_per_minute: int = 900
    login_rate_limit_per_minute: int = 10
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    metrics_require_auth: bool = False

    # --- inspection
    ffprobe_path: str | None = None
    inspection_enabled: bool = True
    inspection_concurrency: int = 2
    preferred_audio_languages: list[str] = Field(default_factory=list)
    runtime_tolerance_episode_pct: int = 25
    runtime_tolerance_movie_pct: int = 15
    inspect_before_import: bool = False

    # --- housekeeping
    job_retention_days: int = 7
    history_retention_days: int = 365
    audit_retention_days: int = 730

    model_config = {"extra": "ignore"}


class SettingsManager:
    """Thread-safe cached access to :class:`AppSettings`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._cache: AppSettings | None = None

    def get(self) -> AppSettings:
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            return self._cache.model_copy(deep=True)

    def update(self, **changes) -> AppSettings:
        with self._lock:
            current = self.get().model_dump()
            current.update({k: v for k, v in changes.items() if v is not None})
            new = AppSettings.model_validate(current)
            self._write(new)
            self._cache = new
            return new.model_copy(deep=True)

    def save(self, settings: AppSettings) -> None:
        with self._lock:
            self._write(settings)
            self._cache = settings

    # -- persistence -------------------------------------------------------
    def _load(self) -> AppSettings:
        if not self.path.exists():
            settings = AppSettings()
            self._write(settings)
            return settings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("config.json unreadable (%s); using defaults", exc)
            return AppSettings()

        migrated = self._migrate_legacy(raw)
        settings = AppSettings.model_validate(raw)
        if migrated:
            self._write(settings)
        return settings

    @staticmethod
    def _migrate_legacy(raw: dict) -> bool:
        """Move v1's encrypted connection string out of config.json into the secret store."""
        changed = False
        legacy = raw.pop("db_connection_encrypted", None)
        if legacy:
            try:
                url = cipher.decrypt(legacy)
                if not secret_store.has(SECRET_DB_URL):
                    secret_store.set(SECRET_DB_URL, url)
                log.info("Migrated database connection string into encrypted_config.db")
            except SecretError as exc:
                log.error("Could not migrate legacy database connection string: %s", exc)
            changed = True
        if "db_connection" in raw:  # never persist plaintext connection strings
            raw.pop("db_connection")
            changed = True
        return changed

    def _write(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.model_dump(), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


settings_manager = SettingsManager(paths.CONFIG_FILE)


def get_settings() -> AppSettings:
    return settings_manager.get()
