"""download_clients.tv_category: send TV grabs to their own client category ("tv") apart from movies ("movies").

Revision ID: 0017_download_client_tv_category
Revises: 0016_series_country
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_download_client_tv_category"
down_revision = "0016_series_country"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("download_clients")}
    if "tv_category" not in existing:
        # left empty: TV keeps using ``category`` until the user sets one, so existing clients behave as before
        op.add_column("download_clients", sa.Column("tv_category", sa.String(128), nullable=True))


def downgrade() -> None:
    # Left in place, like 0014: dropping a column on SQLite needs a batch rebuild. A nullable
    # string is ignored by older code.
    pass
