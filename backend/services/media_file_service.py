"""Single write path for all media file creation, modification, and removal.

Always synchronizes legacy columns on Movie and Episode in the same database session.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import models
from backend.db.base import utcnow
from backend.domain.parser import normalise_path_key, release_group
from backend.domain.quality import ParsedQuality, parse_quality

log = logging.getLogger("maniac.media_files")


def _compute_relative_path(path_str: str, root_folder: str | None, fallback_folder: str | None) -> str | None:
    if root_folder:
        try:
            rel = os.path.relpath(path_str, root_folder).replace("\\", "/")
            if not rel.startswith("../"):
                return rel
        except ValueError:
            pass
    if fallback_folder:
        try:
            rel = os.path.relpath(path_str, fallback_folder).replace("\\", "/")
            if not rel.startswith("../"):
                return rel
        except ValueError:
            pass
    return Path(path_str).name


def _insert_or_get(db: Session, mf: models.MediaFile) -> models.MediaFile:
    """Insert ``mf``; if a concurrent session (scanner/watcher) already inserted the same
    path_hash, discard ours and return the winning row so the caller can update it."""
    try:
        with db.begin_nested():
            db.add(mf)
            db.flush()
        return mf
    except IntegrityError:
        existing = db.scalar(select(models.MediaFile).where(models.MediaFile.path_hash == mf.path_hash))
        if existing is None:
            raise
        return existing


def attach_movie_file(
    db: Session,
    movie: models.Movie,
    path: Path | str,
    quality: ParsedQuality | None,
    *,
    scene_name: str | None = None,
    source: str = "scan",
    make_primary: bool = True,
    edition: str | None = None,
) -> models.MediaFile:
    """Attach or update a video file for a movie, updating legacy columns."""
    path_str = str(path)
    norm_key = normalise_path_key(path_str)
    h = hashlib.sha256(norm_key.encode()).hexdigest()

    size_bytes = 0
    mtime = None
    try:
        st = os.stat(path_str)
        size_bytes = st.st_size
        mtime = st.st_mtime
    except OSError:
        pass

    q = quality if quality is not None else parse_quality(Path(path_str).name)
    quality_name = q.quality.name if q and q.quality and q.quality.weight else (movie.quality or "Unknown")
    codec_name = q.codec.value if q and q.codec and q.codec.value != "unknown" else None
    revision = 2 if (q and (q.proper or q.repack)) else 1
    rel_grp = release_group(Path(path_str).name)
    rel_path = _compute_relative_path(path_str, movie.root_folder, movie.path)

    mf = db.scalar(select(models.MediaFile).where(models.MediaFile.path_hash == h))
    if mf is None:
        mf = models.MediaFile(
            media_type="movie",
            movie_id=movie.id,
            series_id=None,
            path=path_str,
            path_hash=h,
            relative_path=rel_path,
            size_bytes=size_bytes,
            mtime=mtime,
            quality=quality_name,
            codec=codec_name,
            revision=revision,
            release_group=rel_grp,
            scene_name=scene_name or Path(path_str).name,
            edition=edition,
            source=source,
            is_primary=make_primary,
            added_at=utcnow(),
            updated_at=utcnow(),
        )
        winner = _insert_or_get(db, mf)
        if winner is not mf:
            mf = winner
            mf.movie_id = movie.id
            mf.series_id = None
            mf.path = path_str
            mf.size_bytes = size_bytes
            mf.mtime = mtime
            mf.updated_at = utcnow()
    else:
        mf.movie_id = movie.id
        if mf.size_bytes != size_bytes or mf.mtime != mtime:
            mf.inspected_at = None
            mf.inspection_error = None
            
        mf.path = path_str
        mf.relative_path = rel_path
        mf.size_bytes = size_bytes
        mf.mtime = mtime
        if quality_name and quality_name != "Unknown":
            mf.quality = quality_name
        if codec_name:
            mf.codec = codec_name
        if rel_grp:
            mf.release_group = rel_grp
        if scene_name:
            mf.scene_name = scene_name
        if edition:
            mf.edition = edition
        mf.updated_at = utcnow()

    if make_primary:
        db.execute(
            update(models.MediaFile)
            .where(models.MediaFile.movie_id == movie.id, models.MediaFile.id != mf.id)
            .values(is_primary=False)
        )
        mf.is_primary = True
    else:
        # If this is the only file for the movie, it must be primary
        other_primary = db.scalar(
            select(models.MediaFile.id).where(
                models.MediaFile.movie_id == movie.id,
                models.MediaFile.is_primary.is_(True),
                models.MediaFile.id != mf.id,
            )
        )
        if not other_primary:
            mf.is_primary = True

    db.flush()
    sync_legacy(db, movie)
    return mf


def attach_episode_file(
    db: Session,
    series: models.Series,
    episodes: Sequence[models.Episode],
    path: Path | str,
    quality: ParsedQuality | None,
    *,
    scene_name: str | None = None,
    source: str = "scan",
    make_primary: bool = True,
) -> models.MediaFile:
    """Attach or update a video file for one or more episodes, updating legacy columns."""
    path_str = str(path)
    norm_key = normalise_path_key(path_str)
    h = hashlib.sha256(norm_key.encode()).hexdigest()

    size_bytes = 0
    mtime = None
    try:
        st = os.stat(path_str)
        size_bytes = st.st_size
        mtime = st.st_mtime
    except OSError:
        pass

    q = quality if quality is not None else parse_quality(Path(path_str).name)
    quality_name = q.quality.name if q and q.quality and q.quality.weight else "Unknown"
    codec_name = q.codec.value if q and q.codec and q.codec.value != "unknown" else None
    revision = 2 if (q and (q.proper or q.repack)) else 1
    rel_grp = release_group(Path(path_str).name)
    rel_path = _compute_relative_path(path_str, series.root_folder, series.path)

    mf = db.scalar(select(models.MediaFile).where(models.MediaFile.path_hash == h))
    if mf is None:
        mf = models.MediaFile(
            media_type="tv",
            movie_id=None,
            series_id=series.id,
            path=path_str,
            path_hash=h,
            relative_path=rel_path,
            size_bytes=size_bytes,
            mtime=mtime,
            quality=quality_name,
            codec=codec_name,
            revision=revision,
            release_group=rel_grp,
            scene_name=scene_name or Path(path_str).name,
            source=source,
            is_primary=make_primary,
            added_at=utcnow(),
            updated_at=utcnow(),
        )
        winner = _insert_or_get(db, mf)
        if winner is not mf:
            mf = winner
            mf.series_id = series.id
            mf.path = path_str
            mf.size_bytes = size_bytes
            mf.mtime = mtime
            mf.updated_at = utcnow()
    else:
        mf.series_id = series.id
        if mf.size_bytes != size_bytes or mf.mtime != mtime:
            mf.inspected_at = None
            mf.inspection_error = None
            
        mf.path = path_str
        mf.relative_path = rel_path
        mf.size_bytes = size_bytes
        mf.mtime = mtime
        if quality_name and quality_name != "Unknown":
            mf.quality = quality_name
        if codec_name:
            mf.codec = codec_name
        if rel_grp:
            mf.release_group = rel_grp
        if scene_name:
            mf.scene_name = scene_name
        mf.updated_at = utcnow()

    # Link episodes
    for ep in episodes:
        link = db.scalar(
            select(models.EpisodeFile).where(
                models.EpisodeFile.media_file_id == mf.id,
                models.EpisodeFile.episode_id == ep.id,
            )
        )
        if link is None:
            db.add(models.EpisodeFile(media_file_id=mf.id, episode_id=ep.id))
    db.flush()

    if make_primary:
        subq = (
            select(models.EpisodeFile.media_file_id)
            .where(
                models.EpisodeFile.episode_id.in_([e.id for e in episodes]),
                models.EpisodeFile.media_file_id != mf.id,
            )
            .scalar_subquery()
        )
        db.execute(update(models.MediaFile).where(models.MediaFile.id.in_(subq)).values(is_primary=False))
        mf.is_primary = True

    db.flush()

    for ep in episodes:
        sync_legacy(db, ep)

    if not series.path and path_str:
        parent = Path(path_str).parent
        series.path = str(parent.parent if parent.name.lower().startswith(("season", "specials", "s0", "s1")) else parent)

    return mf


def move_file(db: Session, media_file: models.MediaFile, new_path: Path | str) -> None:
    """Update file path, hash, relative path and legacy columns upon rename or move."""
    path_str = str(new_path)
    media_file.path = path_str
    media_file.path_hash = hashlib.sha256(normalise_path_key(path_str).encode()).hexdigest()

    if media_file.media_type == "movie" and media_file.movie:
        media_file.relative_path = _compute_relative_path(path_str, media_file.movie.root_folder, media_file.movie.path)
    elif media_file.media_type == "tv" and media_file.series:
        media_file.relative_path = _compute_relative_path(path_str, media_file.series.root_folder, media_file.series.path)

    try:
        st = os.stat(path_str)
        media_file.size_bytes = st.st_size
        media_file.mtime = st.st_mtime
    except OSError:
        pass

    media_file.updated_at = utcnow()
    db.flush()

    if media_file.media_type == "movie" and media_file.movie:
        sync_legacy(db, media_file.movie)
    elif media_file.media_type == "tv":
        for ep in media_file.episodes:
            sync_legacy(db, ep)


def detach_file(db: Session, media_file: models.MediaFile, *, reason: str) -> None:
    """Detach/delete a media file record and recompute legacy columns on the owners."""
    log.info("Detaching file %s (reason: %s)", media_file.path, reason)
    movie = media_file.movie if media_file.media_type == "movie" else None
    episodes = list(media_file.episodes) if media_file.media_type == "tv" else []
    was_primary = media_file.is_primary
    movie_id = media_file.movie_id

    db.delete(media_file)
    db.flush()

    if movie:
        if was_primary:
            next_mf = db.scalar(
                select(models.MediaFile)
                .where(models.MediaFile.movie_id == movie_id)
                .order_by(models.MediaFile.id.desc())
            )
            if next_mf:
                next_mf.is_primary = True
                db.flush()
        sync_legacy(db, movie)

    if episodes:
        for ep in episodes:
            if was_primary:
                next_mf = db.scalar(
                    select(models.MediaFile)
                    .join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id)
                    .where(models.EpisodeFile.episode_id == ep.id)
                    .order_by(models.MediaFile.id.desc())
                )
                if next_mf:
                    next_mf.is_primary = True
                    db.flush()
            sync_legacy(db, ep)


def set_primary(db: Session, media_file: models.MediaFile) -> None:
    """Mark a media file as primary for its owner(s), updating legacy columns."""
    if media_file.media_type == "movie" and media_file.movie_id:
        db.execute(
            update(models.MediaFile)
            .where(models.MediaFile.movie_id == media_file.movie_id, models.MediaFile.id != media_file.id)
            .values(is_primary=False)
        )
        media_file.is_primary = True
        db.flush()
        if media_file.movie:
            sync_legacy(db, media_file.movie)
    elif media_file.media_type == "tv":
        media_file.is_primary = True
        for ep in media_file.episodes:
            subq = (
                select(models.EpisodeFile.media_file_id)
                .where(
                    models.EpisodeFile.episode_id == ep.id,
                    models.EpisodeFile.media_file_id != media_file.id,
                )
                .scalar_subquery()
            )
            db.execute(update(models.MediaFile).where(models.MediaFile.id.in_(subq)).values(is_primary=False))
            sync_legacy(db, ep)
        db.flush()


def sync_legacy(db: Session, owner: models.Movie | models.Series | models.Episode) -> None:
    """Recompute legacy columns from primary MediaFile for Movie or Episode."""
    if isinstance(owner, models.Movie):
        primary = db.scalar(
            select(models.MediaFile)
            .where(models.MediaFile.movie_id == owner.id, models.MediaFile.is_primary.is_(True))
            .order_by(models.MediaFile.id.desc())
        )
        if primary is None:
            primary = db.scalar(
                select(models.MediaFile)
                .where(models.MediaFile.movie_id == owner.id)
                .order_by(models.MediaFile.id.desc())
            )
            if primary:
                primary.is_primary = True
                db.flush()

        if primary:
            owner.has_file = True
            owner.file_path = primary.path
            owner.file_size_bytes = primary.size_bytes
            owner.quality = primary.quality or owner.quality
            if primary.path:
                owner.path = str(Path(primary.path).parent)
        else:
            owner.has_file = False
            owner.file_path = None
            owner.file_size_bytes = 0

    elif isinstance(owner, models.Episode):
        primary = db.scalar(
            select(models.MediaFile)
            .join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id)
            .where(models.EpisodeFile.episode_id == owner.id, models.MediaFile.is_primary.is_(True))
            .order_by(models.MediaFile.id.desc())
        )
        if primary is None:
            primary = db.scalar(
                select(models.MediaFile)
                .join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id)
                .where(models.EpisodeFile.episode_id == owner.id)
                .order_by(models.MediaFile.id.desc())
            )
            if primary:
                primary.is_primary = True
                db.flush()

        if primary:
            owner.has_file = True
            owner.file_path = primary.path
            owner.file_size_bytes = primary.size_bytes
            owner.quality = primary.quality or owner.quality
            owner.status = "downloaded"
        else:
            owner.has_file = False
            owner.file_path = None
            owner.file_size_bytes = 0
            if owner.status == "downloaded":
                owner.status = "missing"

    elif isinstance(owner, models.Series):
        for ep in owner.episodes:
            sync_legacy(db, ep)


def check_consistency(db: Session) -> dict[str, int]:
    """Check and repair any drift between legacy columns and primary media_files."""
    mismatches = 0

    for movie in db.scalars(select(models.Movie)):
        primary = db.scalar(
            select(models.MediaFile)
            .where(models.MediaFile.movie_id == movie.id, models.MediaFile.is_primary.is_(True))
        )
        drift = False
        if primary is None and movie.has_file:
            drift = True
        elif primary is not None:
            if not movie.has_file or movie.file_path != primary.path or (movie.file_size_bytes or 0) != primary.size_bytes:
                drift = True
        if drift:
            log.warning("Consistency mismatch on movie %s (%s); repairing legacy columns", movie.title, movie.id)
            sync_legacy(db, movie)
            mismatches += 1

    for ep in db.scalars(select(models.Episode)):
        primary = db.scalar(
            select(models.MediaFile)
            .join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id)
            .where(models.EpisodeFile.episode_id == ep.id, models.MediaFile.is_primary.is_(True))
        )
        drift = False
        if primary is None and ep.has_file:
            drift = True
        elif primary is not None:
            if not ep.has_file or ep.file_path != primary.path or (ep.file_size_bytes or 0) != primary.size_bytes:
                drift = True
        if drift:
            log.warning("Consistency mismatch on episode %s (%s); repairing legacy columns", ep.title, ep.id)
            sync_legacy(db, ep)
            mismatches += 1

    if mismatches:
        db.commit()
    return {"repaired": mismatches}
