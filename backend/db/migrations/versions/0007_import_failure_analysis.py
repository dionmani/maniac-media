"""Context-aware import failure diagnosis: structured reason/signature/analysis on the queue.

Revision ID: 0007_import_failure_analysis
Revises: 0006_api_keys
Create Date: 2026-10-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_import_failure_analysis"
down_revision = "0006_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("download_queue")}

    with op.batch_alter_table("download_queue") as batch:
        if "failure_reason" not in existing:
            batch.add_column(sa.Column("failure_reason", sa.String(32), nullable=True))
        if "failure_signature" not in existing:
            batch.add_column(sa.Column("failure_signature", sa.String(128), nullable=True))
        if "failure_analysis" not in existing:
            batch.add_column(sa.Column("failure_analysis", sa.JSON(), nullable=True))

    indexes = {i["name"] for i in insp.get_indexes("download_queue")}
    if "ix_download_queue_failure_reason" not in indexes:
        op.create_index("ix_download_queue_failure_reason", "download_queue", ["failure_reason"])
    if "ix_download_queue_failure_signature" not in indexes:
        op.create_index("ix_download_queue_failure_signature", "download_queue", ["failure_signature"])


def downgrade() -> None:
    op.drop_index("ix_download_queue_failure_signature", table_name="download_queue")
    op.drop_index("ix_download_queue_failure_reason", table_name="download_queue")
    with op.batch_alter_table("download_queue") as batch:
        batch.drop_column("failure_analysis")
        batch.drop_column("failure_signature")
        batch.drop_column("failure_reason")
