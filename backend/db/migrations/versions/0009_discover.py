"""Discover: first-seen tracking ("new" badges) and "not interested" exclusions.

Revision ID: 0009_discover
Revises: 0008_tags
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_discover"
down_revision = "0008_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "discover_seen" not in tables:
        op.create_table(
            "discover_seen",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("section", sa.String(length=32), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("media_type", "section", "tmdb_id", name="uq_discover_seen_media_section_tmdb"),
        )
        op.create_index("ix_discover_seen_first_seen_at", "discover_seen", ["first_seen_at"])

    if "discover_exclusions" not in tables:
        op.create_table(
            "discover_exclusions",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("media_type", "tmdb_id", name="uq_discover_exclusions_media_tmdb"),
        )


def downgrade() -> None:
    op.drop_table("discover_exclusions")
    op.drop_index("ix_discover_seen_first_seen_at", table_name="discover_seen")
    op.drop_table("discover_seen")
