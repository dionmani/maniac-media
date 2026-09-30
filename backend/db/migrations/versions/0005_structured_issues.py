"""Structured library issues: fingerprint, severity, status lifecycle and JSON payload.

Revision ID: 0005_structured_issues
Revises: 0004_media_files
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.domain.issues import default_severity, fingerprint

revision = "0005_structured_issues"
down_revision = "0004_media_files"
branch_labels = None
depends_on = None

BATCH = 1000


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("library_issues")}

    with op.batch_alter_table("library_issues") as batch:
        if "fingerprint" not in existing:
            batch.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        if "severity" not in existing:
            batch.add_column(sa.Column("severity", sa.String(16), nullable=False, server_default="warning"))
        if "status" not in existing:
            batch.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="open"))
        if "data" not in existing:
            batch.add_column(sa.Column("data", sa.JSON, nullable=True))
        if "first_detected_at" not in existing:
            batch.add_column(sa.Column("first_detected_at", sa.DateTime, nullable=True))
        if "last_seen_at" not in existing:
            batch.add_column(sa.Column("last_seen_at", sa.DateTime, nullable=True))
        if "resolved_at" not in existing:
            batch.add_column(sa.Column("resolved_at", sa.DateTime, nullable=True))
        if "resolution" not in existing:
            batch.add_column(sa.Column("resolution", sa.String(32), nullable=True))
        if "episode_id" not in existing:
            batch.add_column(sa.Column("episode_id", sa.Integer, nullable=True))

    indexes = {i["name"] for i in insp.get_indexes("library_issues")}
    if "ix_library_issues_fingerprint" not in indexes:
        op.create_index("ix_library_issues_fingerprint", "library_issues", ["fingerprint"])
    if "ix_library_issues_status" not in indexes:
        op.create_index("ix_library_issues_status", "library_issues", ["status"])
    if "ix_library_issues_status_type" not in indexes:
        op.create_index("ix_library_issues_status_type", "library_issues", ["status", "issue_type"])

    _backfill(bind)


def downgrade() -> None:
    op.drop_index("ix_library_issues_status_type", table_name="library_issues")
    op.drop_index("ix_library_issues_status", table_name="library_issues")
    op.drop_index("ix_library_issues_fingerprint", table_name="library_issues")
    with op.batch_alter_table("library_issues") as batch:
        for col in ("episode_id", "resolution", "resolved_at", "last_seen_at", "first_detected_at", "data", "status", "severity", "fingerprint"):
            batch.drop_column(col)


def _backfill(conn) -> None:
    issues = sa.table(
        "library_issues",
        sa.column("id", sa.Integer),
        sa.column("issue_type", sa.String),
        sa.column("media_type", sa.String),
        sa.column("media_id", sa.Integer),
        sa.column("path", sa.String),
        sa.column("resolved", sa.Boolean),
        sa.column("detected_at", sa.DateTime),
        sa.column("fingerprint", sa.String),
        sa.column("severity", sa.String),
        sa.column("status", sa.String),
        sa.column("first_detected_at", sa.DateTime),
        sa.column("last_seen_at", sa.DateTime),
        sa.column("resolved_at", sa.DateTime),
        sa.column("resolution", sa.String),
    )
    seen_open: set[str] = set()
    last_id = 0
    while True:
        rows = conn.execute(
            sa.select(issues.c.id, issues.c.issue_type, issues.c.media_type, issues.c.media_id, issues.c.path,
                      issues.c.resolved, issues.c.detected_at)
            .where(issues.c.id > last_id, issues.c.fingerprint.is_(None))
            .order_by(issues.c.id)
            .limit(BATCH)
        ).all()
        if not rows:
            break
        for r in rows:
            last_id = r.id
            fp = fingerprint(r.issue_type, r.media_type, r.media_id, r.path)
            status, resolution, resolved_at = "resolved", "auto", r.detected_at
            if not r.resolved:
                status, resolution, resolved_at = "open", None, None
                if fp in seen_open:  # legacy duplicates of the same open issue: keep the oldest, close the rest
                    status, resolution, resolved_at = "resolved", "auto", r.detected_at
                else:
                    seen_open.add(fp)
            conn.execute(
                issues.update().where(issues.c.id == r.id).values(
                    fingerprint=fp,
                    severity=default_severity(r.issue_type),
                    status=status,
                    resolved=status != "open",
                    first_detected_at=r.detected_at,
                    last_seen_at=r.detected_at,
                    resolved_at=resolved_at,
                    resolution=resolution,
                )
            )
