"""API contracts (Pydantic v2). Secrets are write-only: responses expose ``has_*`` flags instead."""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ORM = ConfigDict(from_attributes=True)


def public_art(media_type: str, media_id: int, path: str | None, kind: str = "poster") -> str | None:
    """Local artwork (e.g. folder.jpg found by the scanner) is exposed through the artwork endpoint."""
    if not path or path.startswith(("http://", "https://", "/api/")):
        return path
    return f"/api/v1/artwork/{media_type}/{media_id}/{kind}"


class _ArtMixin(BaseModel):
    """Rewrites local artwork paths into URLs the browser can load."""

    art_type: ClassVar[str] = "movie"

    @model_validator(mode="after")
    def _rewrite_art(self):
        media_id = getattr(self, "id", None)
        if media_id is not None:
            for field, kind in (("poster_path", "poster"), ("backdrop_path", "backdrop")):
                if hasattr(self, field):
                    object.__setattr__(self, field, public_art(self.art_type, media_id, getattr(self, field), kind))
        return self


class Message(BaseModel):
    status: str = "success"
    message: str


# ------------------------------------------------------------------ auth
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


class SetupRequest(LoginRequest):
    pass


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ORM
    id: int
    username: str
    role: str
    is_active: bool
    last_login: datetime | None = None
    created_at: datetime | None = None
    permissions: list[str] = []


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: SecretStr
    role: Literal["admin", "power_user", "read_only"] = "read_only"


class UserUpdate(BaseModel):
    role: Literal["admin", "power_user", "read_only"] | None = None
    is_active: bool | None = None
    password: SecretStr | None = None


class PasswordChange(BaseModel):
    current_password: SecretStr
    new_password: SecretStr


# ------------------------------------------------------------------ api keys
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    permissions: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(None, ge=1, le=3650)
    allow_query_param: bool = False


class ApiKeyOut(BaseModel):
    model_config = ORM
    id: int
    name: str
    prefix: str
    user_id: int
    permissions: list[str] = []
    allow_query_param: bool = False
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    revoked_at: datetime | None = None


class ApiKeyCreatedResponse(ApiKeyOut):
    api_key: str = ""



class ApiKeyPreset(BaseModel):
    name: str
    description: str
    permissions: list[str]
    allow_query_param: bool



# ------------------------------------------------------------------ media
class CastOut(BaseModel):
    name: str
    character: str | None = None
    image: str | None = None


class MediaFileOut(BaseModel):
    model_config = ORM
    id: int
    media_type: str
    movie_id: int | None = None
    series_id: int | None = None
    path: str
    size_bytes: int
    quality: str | None = None
    codec: str | None = None
    release_group: str | None = None
    scene_name: str | None = None
    edition: str | None = None
    is_primary: bool
    added_at: datetime | None = None
    inspected_at: datetime | None = None
    inspection_error: str | None = None
    container: str | None = None
    duration_seconds: float | None = None
    overall_bitrate: int | None = None
    video_codec: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_resolution: str | None = None
    video_bit_depth: int | None = None
    hdr_format: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_languages: list[str] | None = None
    subtitle_languages: list[str] | None = None


class EpisodeLinkOut(BaseModel):
    model_config = ORM
    id: int
    series_id: int
    season_number: int
    episode_number: int
    title: str


class MovieLinkOut(BaseModel):
    model_config = ORM
    id: int
    title: str
    year: int


class SeriesLinkOut(BaseModel):
    model_config = ORM
    id: int
    title: str
    year: int


class MediaFileDetailOut(MediaFileOut):
    movie: MovieLinkOut | None = None
    series: SeriesLinkOut | None = None
    episodes: list[EpisodeLinkOut] = []
    mediainfo: dict[str, Any] | None = None


class MovieOut(_ArtMixin):
    model_config = ORM
    id: int
    title: str
    year: int
    status: str | None = None
    monitored: bool
    quality_profile_id: int | None = None
    root_folder: str | None = None
    path: str | None = None
    file_path: str | None = None
    file_size_bytes: int | None = 0
    has_file: bool
    quality: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    overview: str | None = None
    tagline: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    genres: str | None = None
    runtime_minutes: int | None = None
    studio: str | None = None
    certification: str | None = None
    release_date: str | None = None
    digital_release_date: str | None = None
    physical_release_date: str | None = None
    rating: float | None = None
    votes: int | None = None
    original_title: str | None = None
    cast_members: list[CastOut] | None = None
    crew: list[dict[str, Any]] | None = None
    metadata_source: str | None = None
    last_metadata_refresh: datetime | None = None
    last_search_at: datetime | None = None
    added_date: datetime | None = None
    files: list[MediaFileOut] = []


class MovieSummary(_ArtMixin):
    model_config = ORM
    id: int
    title: str
    sort_title: str | None = None
    year: int
    monitored: bool
    has_file: bool
    quality: str | None = None
    poster_path: str | None = None
    studio: str | None = None
    status: str | None = None
    rating: float | None = None
    runtime_minutes: int | None = None
    file_size_bytes: int | None = 0
    genres: str | None = None
    certification: str | None = None
    added_date: datetime | None = None


class MovieUpdate(BaseModel):
    monitored: bool | None = None
    quality_profile_id: int | None = None
    root_folder: str | None = None
    minimum_availability: Literal["announced", "in_cinemas", "released"] | None = None
    title: str | None = Field(None, min_length=1, max_length=512)
    year: int | None = Field(None, ge=1870, le=2100)


class AddMediaRequest(BaseModel):
    """Add from a metadata search result (IDs are re-resolved server-side)."""

    title: str
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = Field(None, pattern=r"^tt\d{5,10}$")
    tvdb_id: int | None = None
    tvmaze_id: int | None = None
    quality_profile_id: int = 1
    root_folder: str | None = None
    monitored: bool = True
    monitor: Literal["all", "future", "missing", "none", "latest", "first"] = "all"
    search_on_add: bool = False


class EpisodeOut(BaseModel):
    model_config = ORM
    id: int
    series_id: int
    season_number: int
    episode_number: int
    title: str
    overview: str | None = None
    air_date: str | None = None
    monitored: bool
    has_file: bool
    file_path: str | None = None
    file_size_bytes: int | None = None
    quality: str | None = None
    status: str | None = None
    runtime_minutes: int | None = None
    files: list[MediaFileOut] = []


class SeasonOut(BaseModel):
    model_config = ORM
    id: int
    season_number: int
    monitored: bool
    episodes: list[EpisodeOut] = []


class SeriesSummary(_ArtMixin):
    model_config = ORM
    art_type: ClassVar[str] = "tv"
    id: int
    title: str
    sort_title: str | None = None
    year: int
    status: str | None = None
    monitored: bool
    network: str | None = None
    poster_path: str | None = None
    total_seasons: int | None = None
    rating: float | None = None
    genres: str | None = None
    episode_count: int = 0
    episode_file_count: int = 0
    aired_episode_count: int = 0
    missing_count: int = 0
    size_on_disk: int = 0
    added_date: datetime | None = None


class SeriesOut(SeriesSummary):
    quality_profile_id: int | None = None
    root_folder: str | None = None
    path: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    tvmaze_id: int | None = None
    imdb_id: str | None = None
    overview: str | None = None
    backdrop_path: str | None = None
    first_aired: str | None = None
    runtime_minutes: int | None = None
    certification: str | None = None
    votes: int | None = None
    cast_members: list[CastOut] | None = None
    series_type: str | None = None
    monitor_new_seasons: bool | None = None
    metadata_source: str | None = None
    last_metadata_refresh: datetime | None = None
    seasons: list[SeasonOut] = []


class SeriesUpdate(BaseModel):
    monitored: bool | None = None
    quality_profile_id: int | None = None
    root_folder: str | None = None
    series_type: Literal["standard", "daily", "anime"] | None = None
    monitor_new_seasons: bool | None = None
    title: str | None = Field(None, min_length=1, max_length=512)


class BulkEditRequest(BaseModel):
    """Mass edit from the library poster/table view; only the fields that are sent are changed."""
    ids: list[int] = Field(min_length=1, max_length=10000)
    monitored: bool | None = None
    quality_profile_id: int | None = None
    monitor_new_seasons: bool | None = None  # series only


class MonitorRequest(BaseModel):
    monitored: bool


class EpisodeMonitorRequest(BaseModel):
    episode_ids: list[int] = Field(min_length=1, max_length=5000)
    monitored: bool


class AddSeasonRequest(BaseModel):
    season_number: int = Field(ge=0, le=500)
    episode_count: int = Field(0, ge=0, le=500)


class StaleSeason(BaseModel):
    season_number: int
    episodes: int
    removed: bool  # True when the whole season goes, False when only some of its episodes do


class PruneStaleResult(BaseModel):
    provider: str
    episodes: int
    kept_with_files: int
    seasons: list[StaleSeason]
    applied: bool


class LinkRequest(BaseModel):
    """"Link to…": point a library entry at the title an IMDb/TMDB/TVDB id (or URL) identifies."""
    external_id: str = Field(min_length=1, max_length=300)
    dry_run: bool = True  # preview the match; nothing is changed
    rename: bool = True  # after linking, rename the folder + files to the naming formats


class LinkMatch(BaseModel):
    provider: str
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    episode_count: int = 0


class LinkExisting(BaseModel):
    id: int
    title: str
    year: int | None = None


class LinkResult(BaseModel):
    match: LinkMatch
    existing: LinkExisting | None = None  # already in the library – linking would duplicate it
    applied: bool = False
    renamed: int = 0
    rename_errors: list[str] = []


class DeleteFileInfo(BaseModel):
    path: str
    exists: bool
    shared: bool = False  # also linked to episodes outside the delete – never recycled


class DeleteEpisodesResult(BaseModel):
    episodes: int
    files: list[DeleteFileInfo]
    files_recycled: int = 0
    applied: bool


class AddEpisodeRequest(BaseModel):
    season_number: int = Field(ge=0, le=500)
    episode_number: int = Field(ge=0, le=5000)
    title: str = Field("", max_length=512)
    air_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ search / queue
class GrabRequest(BaseModel):
    token: str = Field(min_length=8, max_length=64)
    media_type: Literal["movie", "tv"]
    media_id: int
    season: int | None = None
    episodes: list[int] = []


class AutoSearchRequest(BaseModel):
    media_type: Literal["movie", "tv"]
    media_id: int
    season: int | None = None
    episodes: list[int] = []


class QueueItemOut(BaseModel):
    model_config = ORM
    id: int
    title: str
    media_type: str
    media_id: int
    season_num: int | None = None
    ep_num: int | None = None
    episode_numbers: list[int] | None = None
    indexer_name: str | None = None
    release_title: str
    quality: str | None = None
    size_bytes: int | None = 0
    download_speed_bps: int | None = 0
    eta_seconds: int | None = None
    client_position: int | None = None
    progress: float | None = 0
    status: str
    protocol: str | None = None
    download_path: str | None = None
    error_message: str | None = None
    retry_count: int | None = 0
    download_client_id: int | None = None
    added_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    failure_signature: str | None = None
    failure_analysis: dict[str, Any] | None = None


class HistoryOut(BaseModel):
    model_config = ORM
    id: int
    media_type: str
    media_id: int | None = None
    title: str
    event_type: str
    quality: str | None = None
    details: str | None = None
    timestamp: datetime | None = None
    download_id: int | None = None
    data: dict[str, Any] | None = None


class ImportFixRequest(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class ImportRequest(BaseModel):
    strategy: Literal["default", "auto_fix", "force"] = "default"
    fix: ImportFixRequest | None = None


class QueueFixRequest(BaseModel):
    signature: str = Field(min_length=1, max_length=128)
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class BlocklistOut(BaseModel):
    model_config = ORM
    id: int
    media_type: str
    media_id: int
    release_title: str
    indexer: str | None = None
    reason: str | None = None
    created_at: datetime


# ------------------------------------------------------------------ renamer / import
class RenameItem(BaseModel):
    media_type: Literal["movie", "tv"]
    media_id: int
    title: str | None = None


class RenameRequest(BaseModel):
    items: list[RenameItem] = Field(max_length=20000)
    dry_run: bool = False
    include_folders: bool = False


class ManualImportScanRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=1024)


class ManualImportItem(BaseModel):
    path: str
    media_type: Literal["movie", "tv"]
    media_id: int
    season: int | None = None
    episodes: list[int] = []
    quality: str | None = None


class ManualImportRequest(BaseModel):
    items: list[ManualImportItem] = Field(min_length=1, max_length=2000)
    mode: Literal["move", "copy", "hardlink", "symlink"] | None = None


class ManualImportDeleteRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class ScanRequest(BaseModel):
    scope: Literal["movies", "tv", "all"] = "all"
    incremental: bool = True
    media_id: int | None = None  # restrict the pass to one movie / series (needs a single-type scope)


class IssueActionRequest(BaseModel):
    """Body for ``POST /library/issues/{id}/resolve``; no body at all means ``dismiss``."""
    action: str = Field("dismiss", max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


class IssueBulkRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: str = Field(max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ settings & integrations
class QualityProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    items: list[str] = Field(min_length=1)
    cutoff: str | None = None
    upgrade_allowed: bool = True
    preferred_codecs: list[str] = []
    rejected_codecs: list[str] = []
    min_size_mb: int = Field(0, ge=0)
    max_size_mb: int = Field(0, ge=0)
    prefer_proper: bool = True


class QualityProfileOut(QualityProfileIn):
    model_config = ORM
    id: int
    items: list[str] = []


class IndexerIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    implementation: str = "torznab"
    url: str = Field("", max_length=1024)
    api_path: str | None = "/api"
    api_key: SecretStr | None = None  # omitted/None = keep existing
    enabled: bool = True
    priority: int = Field(25, ge=1, le=100)
    protocol: Literal["torrent", "usenet"] | None = None
    movie_categories: list[int] | None = None
    tv_categories: list[int] | None = None
    enable_rss: bool = True
    enable_search: bool = True
    rate_limit_per_minute: int = Field(30, ge=1, le=600)
    minimum_seeders: int = Field(1, ge=0)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v.rstrip("/")


class IndexerOut(BaseModel):
    model_config = ORM
    id: int
    name: str
    implementation: str | None = None
    url: str
    api_path: str | None = None
    has_api_key: bool = False
    enabled: bool
    priority: int | None = None
    protocol: str | None = None
    movie_categories: list[int] | None = None
    tv_categories: list[int] | None = None
    enable_rss: bool | None = None
    enable_search: bool | None = None
    rate_limit_per_minute: int | None = None
    minimum_seeders: int | None = None
    health_status: str | None = None
    health_message: str | None = None
    last_health_check: datetime | None = None
    failure_count: int | None = None
    disabled_until: datetime | None = None
    last_rss_sync: datetime | None = None


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    client_type: str
    host: str | None = "localhost"
    port: int | None = Field(None, ge=0, le=65535)
    use_ssl: bool = False
    url_base: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    api_key: SecretStr | None = None
    category: str = "maniac-media"
    enabled: bool = True
    priority: int = Field(1, ge=1, le=50)
    remove_completed: bool = False
    remote_path: str | None = None
    local_path: str | None = None


class ClientOut(BaseModel):
    model_config = ORM
    id: int
    name: str
    client_type: str
    protocol: str | None = None
    host: str | None = None
    port: int | None = None
    use_ssl: bool
    url_base: str | None = None
    username: str | None = None
    has_password: bool = False
    has_api_key: bool = False
    category: str | None = None
    enabled: bool
    priority: int | None = None
    remove_completed: bool | None = None
    remote_path: str | None = None
    local_path: str | None = None
    health_status: str | None = None
    health_message: str | None = None
    last_health_check: datetime | None = None


class NotificationIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    implementation: str
    settings: dict[str, Any] = {}
    events: list[str] = []
    enabled: bool = True


class NotificationOut(BaseModel):
    id: int
    name: str
    implementation: str
    settings: dict[str, Any]
    configured_secrets: list[str]
    events: list[str]
    enabled: bool


class GeneralSettings(BaseModel):
    media_root_movies: str | None = None
    media_root_tv: str | None = None
    download_folder: str | None = None
    recycle_bin: str | None = None
    recycle_bin_days: int | None = Field(None, ge=0, le=3650)
    movie_folder_format: str | None = None
    movie_format: str | None = None
    series_folder_format: str | None = None
    tv_format: str | None = None
    rename_on_import: bool | None = None
    auto_import: bool | None = None
    import_mode: Literal["move", "copy", "hardlink", "symlink"] | None = None
    min_import_size_mb: int | None = Field(None, ge=0)
    watch_download_folder: bool | None = None
    import_untracked_downloads: bool | None = None
    search_on_add: bool | None = None
    auto_retry_failed_downloads: bool | None = None
    max_download_retries: int | None = Field(None, ge=0, le=10)
    metadata_language: str | None = None
    metadata_refresh_batch_size: int | None = Field(None, ge=10, le=5000)
    access_token_ttl_minutes: int | None = Field(None, ge=5, le=60 * 24 * 30)
    rate_limit_per_minute: int | None = Field(None, ge=30, le=100000)
    cors_origins: list[str] | None = None
    metrics_require_auth: bool | None = None
    job_retention_days: int | None = Field(None, ge=1)
    history_retention_days: int | None = Field(None, ge=1)
    audit_retention_days: int | None = Field(None, ge=30)


class MetadataProviderUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=1000)
    api_key: SecretStr | None = None
    pin: SecretStr | None = None
    clear_api_key: bool = False


class NamingPreviewRequest(BaseModel):
    template: str = Field(max_length=512)
    kind: Literal["movie", "episode"]


class DatabaseRequest(BaseModel):
    db_type: Literal["sqlite", "postgresql", "mysql", "mariadb", "mssql"]
    host: str | None = "localhost"
    port: int | None = Field(None, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    database: str | None = "maniac_media"
    sqlite_path: str | None = None
    options: dict[str, str] = {}
    overwrite: bool = False


class TaskUpdate(BaseModel):
    interval_minutes: int | None = Field(None, ge=1, le=60 * 24 * 30)
    enabled: bool | None = None


TokenResponse.model_rebuild()
