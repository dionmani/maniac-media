"""movies.production_status: TMDB's production stage, for the library's release badge.

Revision ID: 0014_movie_production_status
Revises: 0013_media_file_quality_lock
Create Date: 2026-10-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_movie_production_status"
down_revision = "0013_media_file_quality_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("movies")}
    if "production_status" not in existing:
        # plain ADD COLUMN, not batch mode: a SQLite batch rebuild drops movies, and the
        # ON DELETE CASCADE from media_files would wipe every movie's files with it
        op.add_column("movies", sa.Column("production_status", sa.String(32), nullable=True))
        # only movies without a file show the badge: make them due so the next metadata refresh fills
        # the stage in, instead of waiting out the 7-day cycle
        movies = sa.table("movies", sa.column("has_file", sa.Boolean), sa.column("metadata_source", sa.String),
                          sa.column("last_metadata_refresh", sa.DateTime))
        op.execute(movies.update().where(movies.c.has_file == sa.false(), movies.c.metadata_source == "tmdb")
                   .values(last_metadata_refresh=None))


def downgrade() -> None:
    # Left in place, like 0013: dropping a column on SQLite needs a batch rebuild. A nullable
    # string is ignored by older code.
    pass
