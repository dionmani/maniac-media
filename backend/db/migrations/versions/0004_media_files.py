"""media_files and episode_files tables, with backfill for movies and episodes.

Revision ID: 0004_media_files
Revises: 0003_queue_client_position
Create Date: 2026-09-30
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from backend.domain.parser import normalise_path_key

log = logging.getLogger("maniac.migrations.0004")

revision = "0004_media_files"
down_revision = "0003_queue_client_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "media_files" not in tables:
        op.create_table(
            "media_files",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("movie_id", sa.Integer, sa.ForeignKey("movies.id", ondelete="CASCADE"), nullable=True),
            sa.Column("series_id", sa.Integer, sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=True),
            sa.Column("path", sa.String(1024), nullable=False),
            sa.Column("path_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("relative_path", sa.String(1024), nullable=True),
            sa.Column("size_bytes", sa.BigInteger, nullable=False, server_default="0"),
            sa.Column("mtime", sa.Float, nullable=True),
            sa.Column("quality", sa.String(64), nullable=True),
            sa.Column("codec", sa.String(16), nullable=True),
            sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
            sa.Column("release_group", sa.String(64), nullable=True),
            sa.Column("scene_name", sa.String(1024), nullable=True),
            sa.Column("edition", sa.String(64), nullable=True),
            sa.Column("custom_formats", sa.JSON, nullable=True),
            sa.Column("custom_format_score", sa.Integer, nullable=True),
            sa.Column("role", sa.String(16), nullable=False, server_default="main"),
            sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("added_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="scan"),
        )
        op.create_index("ix_media_files_movie_primary", "media_files", ["movie_id", "is_primary"])
        op.create_index("ix_media_files_series_id", "media_files", ["series_id"])
        op.create_index("ix_media_files_movie_id", "media_files", ["movie_id"])
        op.create_index("ix_media_files_path_hash", "media_files", ["path_hash"])

    if "episode_files" not in tables:
        op.create_table(
            "episode_files",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("media_file_id", sa.Integer, sa.ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_id", sa.Integer, sa.ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False),
            sa.UniqueConstraint("media_file_id", "episode_id", name="uq_episode_files_file_episode"),
        )
        op.create_index("ix_episode_files_media_file_id", "episode_files", ["media_file_id"])
        op.create_index("ix_episode_files_episode_id", "episode_files", ["episode_id"])

    # Run backfill from existing movies and episodes
    _backfill(bind)


def downgrade() -> None:
    op.drop_table("episode_files")
    op.drop_table("media_files")


def _backfill(conn) -> None:
    now = datetime.now(timezone.utc)
    seen_hashes: dict[str, int] = {}

    media_files_table = sa.table(
        "media_files",
        sa.column("id", sa.Integer),
        sa.column("media_type", sa.String),
        sa.column("movie_id", sa.Integer),
        sa.column("series_id", sa.Integer),
        sa.column("path", sa.String),
        sa.column("path_hash", sa.String),
        sa.column("relative_path", sa.String),
        sa.column("size_bytes", sa.BigInteger),
        sa.column("mtime", sa.Float),
        sa.column("quality", sa.String),
        sa.column("codec", sa.String),
        sa.column("revision", sa.Integer),
        sa.column("release_group", sa.String),
        sa.column("scene_name", sa.String),
        sa.column("edition", sa.String),
        sa.column("custom_formats", sa.JSON),
        sa.column("custom_format_score", sa.Integer),
        sa.column("role", sa.String),
        sa.column("is_primary", sa.Boolean),
        sa.column("added_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
        sa.column("source", sa.String),
    )

    episode_files_table = sa.table(
        "episode_files",
        sa.column("id", sa.Integer),
        sa.column("media_file_id", sa.Integer),
        sa.column("episode_id", sa.Integer),
    )

    # 1. Backfill movies
    movie_rows = conn.execute(
        sa.text(
            "SELECT id, file_path, file_size_bytes, quality, added_date, updated_at "
            "FROM movies WHERE has_file = 1 AND file_path IS NOT NULL AND file_path != '' "
            "ORDER BY id ASC"
        )
    ).fetchall()

    for row in movie_rows:
        m_id, f_path, f_size, f_qual, f_added, f_updated = row
        norm_key = normalise_path_key(f_path)
        h = hashlib.sha256(norm_key.encode()).hexdigest()
        if h in seen_hashes:
            log.warning(
                "Duplicate path_hash during movie migration: movie_id=%s file_path=%s collides with media_file_id=%s",
                m_id, f_path, seen_hashes[h]
            )
            continue
        scene = Path(f_path).name
        res = conn.execute(
            media_files_table.insert().values(
                media_type="movie",
                movie_id=m_id,
                series_id=None,
                path=f_path,
                path_hash=h,
                relative_path=None,
                size_bytes=int(f_size or 0),
                mtime=None,
                quality=f_qual,
                codec=None,
                revision=1,
                release_group=None,
                scene_name=scene,
                edition=None,
                custom_formats=None,
                custom_format_score=None,
                role="main",
                is_primary=True,
                added_at=f_added or now,
                updated_at=f_updated or now,
                source="scan",
            )
        )
        mf_id = (
            res.inserted_primary_key[0]
            if res.inserted_primary_key and res.inserted_primary_key[0]
            else conn.execute(sa.text("SELECT id FROM media_files WHERE path_hash = :h"), {"h": h}).scalar()
        )
        if mf_id:
            seen_hashes[h] = mf_id

    # 2. Backfill episodes
    ep_rows = conn.execute(
        sa.text(
            "SELECT id, series_id, file_path, file_size_bytes, quality "
            "FROM episodes WHERE has_file = 1 AND file_path IS NOT NULL AND file_path != '' "
            "ORDER BY series_id ASC, season_number ASC, episode_number ASC, id ASC"
        )
    ).fetchall()

    path_to_eps: dict[str, list[dict]] = {}
    for ep in ep_rows:
        ep_id, s_id, f_path, f_size, f_qual = ep
        norm_key = normalise_path_key(f_path)
        path_to_eps.setdefault(norm_key, []).append(
            {"id": ep_id, "series_id": s_id, "path": f_path, "size": f_size, "quality": f_qual}
        )

    for norm_key, eps in path_to_eps.items():
        first = eps[0]
        h = hashlib.sha256(norm_key.encode()).hexdigest()
        mf_id = seen_hashes.get(h)
        if mf_id is None:
            scene = Path(first["path"]).name
            res = conn.execute(
                media_files_table.insert().values(
                    media_type="tv",
                    movie_id=None,
                    series_id=first["series_id"],
                    path=first["path"],
                    path_hash=h,
                    relative_path=None,
                    size_bytes=int(first["size"] or 0),
                    mtime=None,
                    quality=first["quality"],
                    codec=None,
                    revision=1,
                    release_group=None,
                    scene_name=scene,
                    edition=None,
                    custom_formats=None,
                    custom_format_score=None,
                    role="main",
                    is_primary=True,
                    added_at=now,
                    updated_at=now,
                    source="scan",
                )
            )
            mf_id = (
                res.inserted_primary_key[0]
                if res.inserted_primary_key and res.inserted_primary_key[0]
                else conn.execute(sa.text("SELECT id FROM media_files WHERE path_hash = :h"), {"h": h}).scalar()
            )
            if mf_id:
                seen_hashes[h] = mf_id

        if mf_id:
            for ep in eps:
                conn.execute(
                    episode_files_table.insert().values(
                        media_file_id=mf_id,
                        episode_id=ep["id"],
                    )
                )
