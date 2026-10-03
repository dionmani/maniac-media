"""Tags, and tag ids on movies/series (request attribution from Seerr).

Revision ID: 0008_tags
Revises: ae4412697e56
Create Date: 2026-10-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_tags"
down_revision = "ae4412697e56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "tags" not in insp.get_table_names():
        op.create_table(
            "tags",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("label", sa.String(length=256), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("label", name="uq_tags_label"),
        )

    for table in ("movies", "series"):
        if "tags" not in {c["name"] for c in insp.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in ("series", "movies"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("tags")
    op.drop_table("tags")
