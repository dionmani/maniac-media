"""v2 platform schema: metadata, quality ranking, integrations, users, audit, jobs, scanner state.

Purely additive – no existing column is dropped or rewritten – so it is safe to run on a
populated v1 library. Plaintext credentials left by v1 are encrypted in place.

Revision ID: 0002_v2_platform
Revises: 0001_baseline
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_v2_platform"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

NEW_COLUMNS: dict[str, list[sa.Column]] = {
    "movies": [
        sa.Column("original_title", sa.String(512)),
        sa.Column("sort_title", sa.String(512)),
        sa.Column("tagline", sa.String(1024)),
        sa.Column("certification", sa.String(32)),
        sa.Column("release_date", sa.String(10)),
        sa.Column("digital_release_date", sa.String(10)),
        sa.Column("physical_release_date", sa.String(10)),
        sa.Column("rating", sa.Float),
        sa.Column("votes", sa.Integer),
        sa.Column("cast_members", sa.JSON),
        sa.Column("crew", sa.JSON),
        sa.Column("quality", sa.String(64)),
        sa.Column("minimum_availability", sa.String(32)),
        sa.Column("metadata_source", sa.String(32)),
        sa.Column("last_metadata_refresh", sa.DateTime),
        sa.Column("last_search_at", sa.DateTime),
    ],
    "series": [
        sa.Column("imdb_id", sa.String(16)),
        sa.Column("tvmaze_id", sa.Integer),
        sa.Column("original_title", sa.String(512)),
        sa.Column("sort_title", sa.String(512)),
        sa.Column("first_aired", sa.String(10)),
        sa.Column("runtime_minutes", sa.Integer),
        sa.Column("certification", sa.String(32)),
        sa.Column("rating", sa.Float),
        sa.Column("votes", sa.Integer),
        sa.Column("cast_members", sa.JSON),
        sa.Column("series_type", sa.String(16)),
        sa.Column("monitor_new_seasons", sa.Boolean),
        sa.Column("metadata_source", sa.String(32)),
        sa.Column("last_metadata_refresh", sa.DateTime),
        sa.Column("last_search_at", sa.DateTime),
    ],
    "episodes": [
        sa.Column("file_size_bytes", sa.BigInteger),
        sa.Column("runtime_minutes", sa.Integer),
        sa.Column("tvmaze_id", sa.Integer),
        sa.Column("tvdb_id", sa.Integer),
        sa.Column("absolute_number", sa.Integer),
        sa.Column("last_search_at", sa.DateTime),
    ],
    "quality_profiles": [
        sa.Column("upgrade_allowed", sa.Boolean),
        sa.Column("preferred_codecs", sa.JSON),
        sa.Column("rejected_codecs", sa.JSON),
        sa.Column("min_size_mb", sa.Integer),
        sa.Column("max_size_mb", sa.Integer),
        sa.Column("prefer_proper", sa.Boolean),
    ],
    "indexers": [
        sa.Column("implementation", sa.String(32)),
        sa.Column("api_path", sa.String(128)),
        sa.Column("movie_categories", sa.JSON),
        sa.Column("tv_categories", sa.JSON),
        sa.Column("enable_rss", sa.Boolean),
        sa.Column("enable_search", sa.Boolean),
        sa.Column("rate_limit_per_minute", sa.Integer),
        sa.Column("minimum_seeders", sa.Integer),
        sa.Column("health_status", sa.String(16)),
        sa.Column("health_message", sa.Text),
        sa.Column("last_health_check", sa.DateTime),
        sa.Column("failure_count", sa.Integer),
        sa.Column("disabled_until", sa.DateTime),
        sa.Column("last_rss_sync", sa.DateTime),
    ],
    "download_clients": [
        sa.Column("url_base", sa.String(256)),
        sa.Column("api_key", sa.Text),
        sa.Column("priority", sa.Integer),
        sa.Column("remove_completed", sa.Boolean),
        sa.Column("remote_path", sa.String(1024)),
        sa.Column("local_path", sa.String(1024)),
        sa.Column("health_status", sa.String(16)),
        sa.Column("health_message", sa.Text),
        sa.Column("last_health_check", sa.DateTime),
    ],
    "download_queue": [
        sa.Column("download_client_id", sa.Integer),
        sa.Column("client_item_id", sa.String(128)),
        sa.Column("download_url", sa.Text),
        sa.Column("guid", sa.String(512)),
        sa.Column("protocol", sa.String(16)),
        sa.Column("indexer_id", sa.Integer),
        sa.Column("episode_numbers", sa.JSON),
        sa.Column("eta_seconds", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.Integer),
        sa.Column("updated_at", sa.DateTime),
    ],
    "history": [
        sa.Column("download_id", sa.Integer),
        sa.Column("data", sa.JSON),
    ],
}

NEW_INDEXES = [
    ("ix_movies_has_file", "movies", ["has_file"]),
    ("ix_movies_imdb_id", "movies", ["imdb_id"]),
    ("ix_movies_sort_title", "movies", ["sort_title"]),
    ("ix_series_imdb_id", "series", ["imdb_id"]),
    ("ix_series_tvmaze_id", "series", ["tvmaze_id"]),
    ("ix_series_sort_title", "series", ["sort_title"]),
    ("ix_seasons_series_id", "seasons", ["series_id"]),
    ("ix_episodes_series_id", "episodes", ["series_id"]),
    ("ix_episodes_season_id", "episodes", ["season_id"]),
    ("ix_episodes_has_file", "episodes", ["has_file"]),
    ("ix_episodes_series_season_episode", "episodes", ["series_id", "season_number", "episode_number"]),
    ("ix_download_queue_status", "download_queue", ["status"]),
    ("ix_download_queue_client_item_id", "download_queue", ["client_item_id"]),
    ("ix_history_event_type", "history", ["event_type"]),
    ("ix_history_timestamp", "history", ["timestamp"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, columns in NEW_COLUMNS.items():
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column._copy())  # copy: Column objects bind to one Table

    for name, table, cols in NEW_INDEXES:
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, cols)

    op.create_table(
        "blocklist",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("media_id", sa.Integer, nullable=False),
        sa.Column("release_title", sa.String(1024), nullable=False),
        sa.Column("indexer", sa.String(128)),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_blocklist_release_title", "blocklist", ["release_title"])

    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("implementation", sa.String(32), nullable=False),
        sa.Column("settings", sa.JSON),
        sa.Column("secret_settings", sa.Text),
        sa.Column("events", sa.JSON),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("token_version", sa.Integer, nullable=False),
        sa.Column("failed_logins", sa.Integer, nullable=False),
        sa.Column("locked_until", sa.DateTime),
        sa.Column("last_login", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("user_id", sa.Integer),
        sa.Column("username", sa.String(64)),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32)),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("details", sa.JSON),
    )
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])
    op.create_index("ix_audit_log_category", "audit_log", ["category"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("trigger", sa.String(16)),
        sa.Column("run_after", sa.DateTime),
        sa.Column("progress", sa.Float),
        sa.Column("message", sa.Text),
        sa.Column("result", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("started_at", sa.DateTime),
        sa.Column("finished_at", sa.DateTime),
    )
    op.create_index("ix_jobs_name", "jobs", ["name"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "scheduled_tasks",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("last_run", sa.DateTime),
        sa.Column("last_duration_ms", sa.Integer),
        sa.Column("last_status", sa.String(16)),
    )

    op.create_table(
        "scan_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("path_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("mtime", sa.Float, nullable=False),
        sa.Column("last_scanned", sa.DateTime, nullable=False),
    )

    op.create_table(
        "library_issues",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("issue_type", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(16)),
        sa.Column("media_id", sa.Integer),
        sa.Column("path", sa.String(1024)),
        sa.Column("details", sa.Text),
        sa.Column("detected_at", sa.DateTime, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=False),
    )
    op.create_index("ix_library_issues_issue_type", "library_issues", ["issue_type"])
    op.create_index("ix_library_issues_resolved", "library_issues", ["resolved"])

    _encrypt_plaintext_credentials(bind)


def _encrypt_plaintext_credentials(bind) -> None:
    """v1 stored indexer API keys and client passwords as plaintext. Encrypt them in place."""
    from backend.core.secrets import Cipher, cipher

    targets = [("indexers", "api_key"), ("download_clients", "password")]
    for table, column in targets:
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
        for row_id, value in rows:
            if value and not Cipher.is_encrypted(value):
                token = cipher.encrypt(value, purpose=f"{table}.{column}")
                bind.execute(sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :i"), {"v": token, "i": row_id})


def downgrade() -> None:
    for table in ("library_issues", "scan_state", "scheduled_tasks", "jobs", "audit_log", "users", "notification_channels", "blocklist"):
        op.drop_table(table)
    for name, table, _ in NEW_INDEXES:
        op.drop_index(name, table_name=table)
    for table, columns in NEW_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.drop_column(column.name)
