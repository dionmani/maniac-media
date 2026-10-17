"""Move ffprobe findings off ``corrupt_file`` onto their own issue types.

Inspection used to report truncation / unreadable media as ``corrupt_file``, which the scanner's
header check also owns – so every library scan auto-resolved them. Inspection now reports
``truncated_file`` / ``invalid_media`` instead (and truncation additionally requires a size check).

Rather than retyping old rows (whose truncation verdict may not hold under the new rule), this
closes the open inspection-raised ``corrupt_file`` rows and queues their files for re-inspection;
the backlog re-raises whatever still applies under the new types. Files whose "invalid data"
issue was already wrongly auto-resolved by a scan are re-queued too.

Revision ID: 0012_inspection_issue_types
Revises: 0011_custom_formats
Create Date: 2026-10-16
"""
from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0012_inspection_issue_types"
down_revision = "0011_custom_formats"
branch_labels = None
depends_on = None

_INSPECTION_DETAILS = ("ffprobe reported invalid data", "No video stream found")
_TRUNCATED_PREFIX = "File appears truncated%"


def upgrade() -> None:
    bind = op.get_bind()
    issues = sa.table(
        "library_issues",
        sa.column("id", sa.Integer), sa.column("issue_type", sa.String), sa.column("path", sa.String),
        sa.column("details", sa.Text), sa.column("status", sa.String), sa.column("resolved", sa.Boolean),
        sa.column("resolved_at", sa.DateTime), sa.column("resolution", sa.String),
    )
    files = sa.table(
        "media_files",
        sa.column("path", sa.String), sa.column("inspected_at", sa.DateTime), sa.column("inspection_error", sa.String),
    )
    from_inspection = sa.and_(
        issues.c.issue_type == "corrupt_file",
        sa.or_(issues.c.details.in_(_INSPECTION_DETAILS), issues.c.details.like(_TRUNCATED_PREFIX)),
    )

    paths = [p for p in bind.execute(
        sa.select(issues.c.path).where(from_inspection, issues.c.status == "open", issues.c.path.is_not(None))
    ).scalars()]

    bind.execute(
        issues.update().where(from_inspection, issues.c.status == "open")
        .values(status="resolved", resolved=True, resolved_at=datetime.now(UTC).replace(tzinfo=None), resolution="auto")
    )

    reset = files.update().values(inspected_at=None, inspection_error=None)
    for i in range(0, len(paths), 500):
        bind.execute(reset.where(files.c.path.in_(paths[i:i + 500])))
    bind.execute(reset.where(files.c.inspection_error.like("%Invalid data found when processing input%")))


def downgrade() -> None:
    pass  # data-only; the old rows stay resolved and re-inspection re-reports under the new types
