"""series.country: the origin country that tells same-titled shows apart ("Ghosts.US" vs "Ghosts.UK").

Revision ID: 0016_series_country
Revises: 0015_purge_orphaned_media_refs
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.domain.parser import title_key

revision = "0016_series_country"
down_revision = "0015_purge_orphaned_media_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("series")}
    if "country" not in existing:
        # plain ADD COLUMN, not batch mode: a SQLite batch rebuild drops series, and the
        # ON DELETE CASCADE from media_files would wipe every episode's files with it
        op.add_column("series", sa.Column("country", sa.String(2), nullable=True))
    # shows that share a title with another library show need their country first: make them due so the next
    # metadata refresh fills it in; the rest pick it up on the normal cycle
    series = sa.table("series", sa.column("id", sa.Integer), sa.column("title", sa.String),
                      sa.column("last_metadata_refresh", sa.DateTime))
    groups: dict[str, list[int]] = {}
    for row in op.get_bind().execute(sa.select(series.c.id, series.c.title)):
        groups.setdefault(title_key(row.title or ""), []).append(row.id)
    twins = [i for ids in groups.values() if len(ids) > 1 for i in ids]
    for start in range(0, len(twins), 500):
        op.execute(series.update().where(series.c.id.in_(twins[start:start + 500])).values(last_metadata_refresh=None))


def downgrade() -> None:
    # Left in place, like 0014: dropping a column on SQLite needs a batch rebuild. A nullable
    # string is ignored by older code.
    pass
