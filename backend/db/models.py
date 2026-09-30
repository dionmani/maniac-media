"""Persistence model.

Tables that existed in v1 keep their names and columns so an upgrade is an additive
migration, never a destructive one. Sizes are ``BigInteger`` (files > 2 GiB overflow a
32-bit INTEGER on PostgreSQL/MSSQL).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, EncryptedString, utcnow


# --------------------------------------------------------------------------- media
class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    year: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32), default="released")  # announced | in_cinemas | released
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    quality_profile_id: Mapped[int | None] = mapped_column(Integer, default=1)
    root_folder: Mapped[str | None] = mapped_column(String(1024))
    path: Mapped[str | None] = mapped_column(String(1024))
    file_path: Mapped[str | None] = mapped_column(String(1024))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=0)
    has_file: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    imdb_id: Mapped[str | None] = mapped_column(String(16), index=True)
    overview: Mapped[str | None] = mapped_column(Text)
    poster_path: Mapped[str | None] = mapped_column(String(1024))
    backdrop_path: Mapped[str | None] = mapped_column(String(1024))
    genres: Mapped[str | None] = mapped_column(String(512))
    runtime_minutes: Mapped[int | None] = mapped_column(Integer, default=0)
    studio: Mapped[str | None] = mapped_column(String(256))
    added_date: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # v2
    original_title: Mapped[str | None] = mapped_column(String(512))
    sort_title: Mapped[str | None] = mapped_column(String(512), index=True)
    tagline: Mapped[str | None] = mapped_column(String(1024))
    certification: Mapped[str | None] = mapped_column(String(32))
    release_date: Mapped[str | None] = mapped_column(String(10))
    digital_release_date: Mapped[str | None] = mapped_column(String(10))
    physical_release_date: Mapped[str | None] = mapped_column(String(10))
    rating: Mapped[float | None] = mapped_column(Float)
    votes: Mapped[int | None] = mapped_column(Integer)
    cast_members: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    crew: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    quality: Mapped[str | None] = mapped_column(String(64))
    minimum_availability: Mapped[str | None] = mapped_column(String(32), default="released")
    metadata_source: Mapped[str | None] = mapped_column(String(32))
    last_metadata_refresh: Mapped[datetime | None] = mapped_column(DateTime)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime)


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    year: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32), default="continuing")
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    quality_profile_id: Mapped[int | None] = mapped_column(Integer, default=1)
    root_folder: Mapped[str | None] = mapped_column(String(1024))
    path: Mapped[str | None] = mapped_column(String(1024))
    tvdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    poster_path: Mapped[str | None] = mapped_column(String(1024))
    backdrop_path: Mapped[str | None] = mapped_column(String(1024))
    network: Mapped[str | None] = mapped_column(String(256))
    genres: Mapped[str | None] = mapped_column(String(512))
    total_seasons: Mapped[int | None] = mapped_column(Integer, default=1)
    added_date: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # v2
    imdb_id: Mapped[str | None] = mapped_column(String(16), index=True)
    tvmaze_id: Mapped[int | None] = mapped_column(Integer, index=True)
    original_title: Mapped[str | None] = mapped_column(String(512))
    sort_title: Mapped[str | None] = mapped_column(String(512), index=True)
    first_aired: Mapped[str | None] = mapped_column(String(10))
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    certification: Mapped[str | None] = mapped_column(String(32))
    rating: Mapped[float | None] = mapped_column(Float)
    votes: Mapped[int | None] = mapped_column(Integer)
    cast_members: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    series_type: Mapped[str | None] = mapped_column(String(16), default="standard")  # standard | daily | anime
    monitor_new_seasons: Mapped[bool | None] = mapped_column(Boolean, default=True)
    metadata_source: Mapped[str | None] = mapped_column(String(32))
    last_metadata_refresh: Mapped[datetime | None] = mapped_column(DateTime)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime)

    seasons: Mapped[list["Season"]] = relationship(
        back_populates="series", cascade="all, delete-orphan", order_by="Season.season_number"
    )
    episodes: Mapped[list["Episode"]] = relationship(back_populates="series", cascade="all, delete-orphan")


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)
    season_number: Mapped[int] = mapped_column(Integer)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)

    series: Mapped[Series] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="season", cascade="all, delete-orphan", order_by="Episode.episode_number"
    )


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (Index("ix_episodes_series_season_episode", "series_id", "season_number", "episode_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    season_number: Mapped[int] = mapped_column(Integer)
    episode_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    overview: Mapped[str | None] = mapped_column(Text)
    air_date: Mapped[str | None] = mapped_column(String(10))
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    has_file: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    quality: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32), default="missing")  # missing | downloading | downloaded
    # v2
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    tvmaze_id: Mapped[int | None] = mapped_column(Integer)
    tvdb_id: Mapped[int | None] = mapped_column(Integer)
    absolute_number: Mapped[int | None] = mapped_column(Integer)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime)

    series: Mapped[Series] = relationship(back_populates="episodes")
    season: Mapped[Season] = relationship(back_populates="episodes")


# --------------------------------------------------------------------------- quality
class QualityProfile(Base):
    __tablename__ = "quality_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    cutoff: Mapped[str | None] = mapped_column(String(64))
    items: Mapped[list[str] | None] = mapped_column(JSON, default=list)  # allowed qualities, best first
    # v2
    upgrade_allowed: Mapped[bool | None] = mapped_column(Boolean, default=True)
    preferred_codecs: Mapped[list[str] | None] = mapped_column(JSON, default=list)  # best first
    rejected_codecs: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    min_size_mb: Mapped[int | None] = mapped_column(Integer, default=0)
    max_size_mb: Mapped[int | None] = mapped_column(Integer, default=0)
    prefer_proper: Mapped[bool | None] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- integrations
class Indexer(Base):
    __tablename__ = "indexers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    protocol: Mapped[str | None] = mapped_column(String(16), default="torrent")  # torrent | usenet
    url: Mapped[str] = mapped_column(String(1024))
    api_key: Mapped[str | None] = mapped_column(EncryptedString("indexers.api_key"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int | None] = mapped_column(Integer, default=25)
    # v2
    implementation: Mapped[str | None] = mapped_column(String(32), default="torznab")
    api_path: Mapped[str | None] = mapped_column(String(128), default="/api")
    movie_categories: Mapped[list[int] | None] = mapped_column(JSON)
    tv_categories: Mapped[list[int] | None] = mapped_column(JSON)
    enable_rss: Mapped[bool | None] = mapped_column(Boolean, default=True)
    enable_search: Mapped[bool | None] = mapped_column(Boolean, default=True)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, default=30)
    minimum_seeders: Mapped[int | None] = mapped_column(Integer, default=1)
    health_status: Mapped[str | None] = mapped_column(String(16), default="unknown")  # ok | warning | error | unknown
    health_message: Mapped[str | None] = mapped_column(Text)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime)
    failure_count: Mapped[int | None] = mapped_column(Integer, default=0)
    disabled_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_rss_sync: Mapped[datetime | None] = mapped_column(DateTime)


class DownloadClient(Base):
    __tablename__ = "download_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    client_type: Mapped[str] = mapped_column(String(32), default="simulator")
    host: Mapped[str | None] = mapped_column(String(256), default="localhost")
    port: Mapped[int | None] = mapped_column(Integer, default=8080)
    username: Mapped[str | None] = mapped_column(String(256))
    password: Mapped[str | None] = mapped_column(EncryptedString("download_clients.password"))
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(128), default="maniac-media")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # v2
    url_base: Mapped[str | None] = mapped_column(String(256))
    api_key: Mapped[str | None] = mapped_column(EncryptedString("download_clients.api_key"))
    priority: Mapped[int | None] = mapped_column(Integer, default=1)
    remove_completed: Mapped[bool | None] = mapped_column(Boolean, default=False)
    remote_path: Mapped[str | None] = mapped_column(String(1024))  # remote path mapping: client's view
    local_path: Mapped[str | None] = mapped_column(String(1024))  # ...and ours
    health_status: Mapped[str | None] = mapped_column(String(16), default="unknown")
    health_message: Mapped[str | None] = mapped_column(Text)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime)


class DownloadQueueItem(Base):
    __tablename__ = "download_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(16))  # movie | tv
    media_id: Mapped[int] = mapped_column(Integer)
    season_num: Mapped[int | None] = mapped_column(Integer)
    ep_num: Mapped[int | None] = mapped_column(Integer)
    indexer_name: Mapped[str | None] = mapped_column(String(128))
    release_title: Mapped[str] = mapped_column(String(1024))
    quality: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=0)
    download_speed_bps: Mapped[int | None] = mapped_column(BigInteger, default=0)
    progress: Mapped[float | None] = mapped_column(Float, default=0.0)
    # queued | downloading | paused | completed | importing | imported | failed | import_failed | warning
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    download_path: Mapped[str | None] = mapped_column(String(1024))
    added_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # v2
    download_client_id: Mapped[int | None] = mapped_column(Integer)
    client_item_id: Mapped[str | None] = mapped_column(String(128), index=True)
    download_url: Mapped[str | None] = mapped_column(EncryptedString("download_queue.download_url"))
    guid: Mapped[str | None] = mapped_column(String(512))
    protocol: Mapped[str | None] = mapped_column(String(16))
    indexer_id: Mapped[int | None] = mapped_column(Integer)
    episode_numbers: Mapped[list[int] | None] = mapped_column(JSON)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    client_position: Mapped[int | None] = mapped_column(Integer)  # order in the client's queue, 0 = next up
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int | None] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class History(Base):
    __tablename__ = "history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16))
    media_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(1024))
    # added | grabbed | downloaded | imported | renamed | deleted | download_failed | import_failed | upgraded
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    quality: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, index=True)
    # v2
    download_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Blocklist(Base):
    __tablename__ = "blocklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16))
    media_id: Mapped[int] = mapped_column(Integer)
    release_title: Mapped[str] = mapped_column(String(1024), index=True)
    indexer: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    implementation: Mapped[str] = mapped_column(String(32))  # email | discord | telegram | webhook
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    secret_settings: Mapped[str | None] = mapped_column(EncryptedString("notification_channels.secret_settings"))
    events: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- users & audit
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="read_only")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=1)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), index=True)  # user|media|import|rename|delete|system|security|settings
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | warning | error | critical
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- jobs
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|completed|failed|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=5)  # lower runs first
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    trigger: Mapped[str | None] = mapped_column(String(16), default="manual")  # manual | scheduled | event | startup
    run_after: Mapped[datetime | None] = mapped_column(DateTime)
    progress: Mapped[float | None] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer)
    last_status: Mapped[str | None] = mapped_column(String(16))


# --------------------------------------------------------------------------- scanner
class ScanState(Base):
    """Folder modification times, so incremental scans skip unchanged folders."""

    __tablename__ = "scan_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path_hash: Mapped[str] = mapped_column(String(64), unique=True)
    path: Mapped[str] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(16))
    mtime: Mapped[float] = mapped_column(Float)
    last_scanned: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryIssue(Base):
    __tablename__ = "library_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # missing_file | duplicate_file | upgrade_available | corrupt_file | unmatched_folder
    issue_type: Mapped[str] = mapped_column(String(32), index=True)
    media_type: Mapped[str | None] = mapped_column(String(16))
    media_id: Mapped[int | None] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(String(1024))
    details: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
