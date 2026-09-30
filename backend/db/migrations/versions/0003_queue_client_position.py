"""Track each download's position in its client's queue so the Activity view can mirror it.

Revision ID: 0003_queue_client_position
Revises: 0002_v2_platform
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_queue_client_position"
down_revision = "0002_v2_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("download_queue")}
    if "client_position" not in existing:
        op.add_column("download_queue", sa.Column("client_position", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("download_queue") as batch:
        batch.drop_column("client_position")
