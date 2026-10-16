"""Custom formats table and per-profile format scoring columns.

Revision ID: 0011_custom_formats
Revises: 0010_episode_air_time
Create Date: 2026-10-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_custom_formats"
down_revision = "0010_episode_air_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "custom_formats" not in tables:
        op.create_table(
            "custom_formats",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("specifications", sa.JSON(), nullable=True),
            sa.Column("include_in_rename", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("name", name="uq_custom_formats_name"),
        )

    existing_qp = {c["name"] for c in insp.get_columns("quality_profiles")}
    with op.batch_alter_table("quality_profiles") as batch:
        if "format_scores" not in existing_qp:
            batch.add_column(sa.Column("format_scores", sa.JSON(), nullable=True))
        if "min_format_score" not in existing_qp:
            batch.add_column(sa.Column("min_format_score", sa.Integer(), nullable=True, server_default="0"))
        if "upgrade_until_format_score" not in existing_qp:
            batch.add_column(sa.Column("upgrade_until_format_score", sa.Integer(), nullable=True, server_default="10000"))
        if "min_format_upgrade_delta" not in existing_qp:
            batch.add_column(sa.Column("min_format_upgrade_delta", sa.Integer(), nullable=True, server_default="1"))

    if "media_files" in tables:
        existing_mf = {c["name"] for c in insp.get_columns("media_files")}
        if "custom_formats" not in existing_mf or "custom_format_score" not in existing_mf:
            with op.batch_alter_table("media_files") as batch:
                if "custom_formats" not in existing_mf:
                    batch.add_column(sa.Column("custom_formats", sa.JSON(), nullable=True))
                if "custom_format_score" not in existing_mf:
                    batch.add_column(sa.Column("custom_format_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("quality_profiles") as batch:
        batch.drop_column("min_format_upgrade_delta")
        batch.drop_column("upgrade_until_format_score")
        batch.drop_column("min_format_score")
        batch.drop_column("format_scores")
    op.drop_table("custom_formats")
