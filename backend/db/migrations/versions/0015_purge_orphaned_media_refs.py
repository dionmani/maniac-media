"""Clean up rows left pointing at deleted movies / series / episodes.

Deleting a library entry used to leave its issues, blocklist, history and downloads behind, keyed by a
bare media_id; SQLite then hands that id to the next item added. Deletes now detach them
(media_service._purge_media_refs); this clears what earlier deletes left. Data only, no schema change.

Revision ID: 0015_purge_orphaned_media_refs
Revises: 0014_movie_production_status
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_purge_orphaned_media_refs"
down_revision = "0014_movie_production_status"
branch_labels = None
depends_on = None

# download_service.TRACKED_STATUSES plus completed-but-not-imported
PENDING_STATUSES = ("queued", "downloading", "paused", "warning", "completed")


def _orphaned(table: sa.TableClause, movies: sa.TableClause, series: sa.TableClause) -> sa.ColumnElement[bool]:
    return sa.or_(
        sa.and_(table.c.media_type == "movie", ~sa.exists().where(movies.c.id == table.c.media_id)),
        sa.and_(table.c.media_type == "tv", ~sa.exists().where(series.c.id == table.c.media_id)),
    )


def upgrade() -> None:
    movies = sa.table("movies", sa.column("id", sa.Integer))
    series = sa.table("series", sa.column("id", sa.Integer))
    episodes = sa.table("episodes", sa.column("id", sa.Integer))

    def ref(name: str, *extra: sa.ColumnClause) -> sa.TableClause:
        return sa.table(name, sa.column("media_type", sa.String), sa.column("media_id", sa.Integer), *extra)

    issues = ref("library_issues", sa.column("episode_id", sa.Integer))
    blocklist = ref("blocklist")
    history = ref("history")
    queue = ref("download_queue", sa.column("status", sa.String), sa.column("error_message", sa.Text),
                sa.column("failure_reason", sa.String), sa.column("failure_signature", sa.String))

    op.execute(issues.delete().where(issues.c.media_id.is_not(None), _orphaned(issues, movies, series)))
    op.execute(issues.delete().where(issues.c.media_type == "tv", issues.c.episode_id.is_not(None),
                                     ~sa.exists().where(episodes.c.id == issues.c.episode_id)))
    op.execute(blocklist.delete().where(_orphaned(blocklist, movies, series)))
    op.execute(history.update().where(history.c.media_id.is_not(None), _orphaned(history, movies, series)).values(media_id=None))
    # failure_analysis stays empty; the queue shows the error message and reason without one
    op.execute(queue.update().where(queue.c.status.in_(PENDING_STATUSES), _orphaned(queue, movies, series)).values(
        status="import_failed", error_message="The library item this download was for was deleted",
        failure_reason="media_deleted", failure_signature="media_deleted"))


def downgrade() -> None:
    pass  # nothing to restore: the removed rows pointed at entries that no longer exist
