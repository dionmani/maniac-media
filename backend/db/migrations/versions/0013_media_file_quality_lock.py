"""media_files.quality_locked: a relabelled quality survives rescans.

Revision ID: 0013_media_file_quality_lock
Revises: 0012_inspection_issue_types
Create Date: 2026-10-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_media_file_quality_lock"
down_revision = "0012_inspection_issue_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("media_files")}
    if "quality_locked" not in existing:
        # plain ADD COLUMN, not batch mode: a SQLite batch rebuild drops media_files, and the
        # ON DELETE CASCADE from episode_files would wipe every episode link with it
        op.add_column("media_files", sa.Column("quality_locked", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    # Left in place: dropping a column on SQLite needs a batch rebuild, which would cascade-delete
    # episode_files (see above). An unused boolean with a server default is harmless to older code.
    pass
