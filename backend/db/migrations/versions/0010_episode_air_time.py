"""Exact episode air time (UTC) for the release gate.

Revision ID: 0010_episode_air_time
Revises: 0009_discover
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_episode_air_time"
down_revision = "0009_discover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("episodes")}
    if "air_date_utc" not in existing:
        op.add_column("episodes", sa.Column("air_date_utc", sa.DateTime))


def downgrade() -> None:
    with op.batch_alter_table("episodes") as batch:
        batch.drop_column("air_date_utc")
