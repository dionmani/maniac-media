"""API keys for integrations.

Revision ID: 0006_api_keys
Revises: 0005_structured_issues
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_api_keys"
down_revision = "0005_structured_issues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = insp.get_table_names()

    if "api_keys" not in tables:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("prefix", sa.String(length=16), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("permissions", sa.JSON(), nullable=True),
            sa.Column("allow_query_param", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_ip", sa.String(length=64), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
