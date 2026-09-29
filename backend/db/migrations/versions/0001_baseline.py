"""Baseline: the v1 schema.

Existing v1 databases are *stamped* at this revision (their tables already exist);
fresh databases create them here.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "movies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("monitored", sa.Boolean),
        sa.Column("quality_profile_id", sa.Integer),
        sa.Column("root_folder", sa.String(1024)),
        sa.Column("path", sa.String(1024)),
        sa.Column("file_path", sa.String(1024)),
        sa.Column("file_size_bytes", sa.BigInteger),
        sa.Column("has_file", sa.Boolean),
        sa.Column("tmdb_id", sa.Integer, unique=True),
        sa.Column("imdb_id", sa.String(16)),
        sa.Column("overview", sa.Text),
        sa.Column("poster_path", sa.String(1024)),
        sa.Column("backdrop_path", sa.String(1024)),
        sa.Column("genres", sa.String(512)),
        sa.Column("runtime_minutes", sa.Integer),
        sa.Column("studio", sa.String(256)),
        sa.Column("added_date", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_index("ix_movies_title", "movies", ["title"])

    op.create_table(
        "series",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("monitored", sa.Boolean),
        sa.Column("quality_profile_id", sa.Integer),
        sa.Column("root_folder", sa.String(1024)),
        sa.Column("path", sa.String(1024)),
        sa.Column("tvdb_id", sa.Integer, unique=True),
        sa.Column("tmdb_id", sa.Integer),
        sa.Column("overview", sa.Text),
        sa.Column("poster_path", sa.String(1024)),
        sa.Column("backdrop_path", sa.String(1024)),
        sa.Column("network", sa.String(256)),
        sa.Column("genres", sa.String(512)),
        sa.Column("total_seasons", sa.Integer),
        sa.Column("added_date", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_index("ix_series_title", "series", ["title"])

    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("series_id", sa.Integer, sa.ForeignKey("series.id"), nullable=False),
        sa.Column("season_number", sa.Integer, nullable=False),
        sa.Column("monitored", sa.Boolean),
    )

    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("series_id", sa.Integer, sa.ForeignKey("series.id"), nullable=False),
        sa.Column("season_id", sa.Integer, sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("season_number", sa.Integer, nullable=False),
        sa.Column("episode_number", sa.Integer, nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("overview", sa.Text),
        sa.Column("air_date", sa.String(10)),
        sa.Column("monitored", sa.Boolean),
        sa.Column("has_file", sa.Boolean),
        sa.Column("file_path", sa.String(1024)),
        sa.Column("quality", sa.String(64)),
        sa.Column("status", sa.String(32)),
    )

    op.create_table(
        "quality_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("cutoff", sa.String(64)),
        sa.Column("items", sa.JSON),
    )

    op.create_table(
        "indexers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("protocol", sa.String(16)),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("api_key", sa.Text),
        sa.Column("enabled", sa.Boolean),
        sa.Column("priority", sa.Integer),
    )

    op.create_table(
        "download_clients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("client_type", sa.String(32)),
        sa.Column("host", sa.String(256)),
        sa.Column("port", sa.Integer),
        sa.Column("username", sa.String(256)),
        sa.Column("password", sa.Text),
        sa.Column("use_ssl", sa.Boolean),
        sa.Column("category", sa.String(128)),
        sa.Column("enabled", sa.Boolean),
    )

    op.create_table(
        "download_queue",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("media_id", sa.Integer, nullable=False),
        sa.Column("season_num", sa.Integer),
        sa.Column("ep_num", sa.Integer),
        sa.Column("indexer_name", sa.String(128)),
        sa.Column("release_title", sa.String(1024), nullable=False),
        sa.Column("quality", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("download_speed_bps", sa.BigInteger),
        sa.Column("progress", sa.Float),
        sa.Column("status", sa.String(32)),
        sa.Column("download_path", sa.String(1024)),
        sa.Column("added_at", sa.DateTime),
        sa.Column("completed_at", sa.DateTime),
    )

    op.create_table(
        "history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("media_id", sa.Integer),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("quality", sa.String(64)),
        sa.Column("details", sa.Text),
        sa.Column("timestamp", sa.DateTime),
    )


def downgrade() -> None:
    for table in (
        "history",
        "download_queue",
        "download_clients",
        "indexers",
        "quality_profiles",
        "episodes",
        "seasons",
        "series",
        "movies",
    ):
        op.drop_table(table)
