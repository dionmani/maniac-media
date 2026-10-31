"""Single write path for all media file creation, modification, and removal.

Always synchronizes legacy columns on Movie and Episode in the same database session.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import LibraryIssueRepository
from backend.domain.issues import FILE_SCOPED_ISSUE_TYPES, Status, fingerprint
from backend.domain.parser import normalise_path_key, release_group
from backend.domain.quality import QUALITIES, ParsedQuality, Resolution, get_quality, parse_quality

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


def locked_quality(parsed: ParsedQuality, media_file: models.MediaFile | None) -> ParsedQuality:
    """``parsed`` (from the release name) with a relabelled file's corrected resolution/source.

    Codec, HDR, audio, PROPER/REPACK etc. still come from the name – a relabel only corrects
    the resolution class, which is what ffprobe measured.
    """
    if media_file is None or not media_file.quality_locked or not media_file.quality:
        return parsed
    return dataclasses.replace(parsed, quality=get_quality(media_file.quality))


def refresh_formats(db: Session, media_file: models.MediaFile, *, profile_id: int | None, episode_count: int) -> None:
    """Re-score a file's custom formats (e.g. after a relabel) from its scene name + effective quality."""
    title = media_file.scene_name or Path(media_file.path).name
    quality = locked_quality(parse_quality(title), media_file)
    media_file.custom_formats, media_file.custom_format_score = _compute_file_formats(
        db, title, media_file.size_bytes or 0, episode_count, quality, profile_id)


def relabel(db: Session, media_file: models.MediaFile, resolution: Resolution) -> str:
    """Set a file's quality from its measured ``resolution`` and lock it, returning the new label.

    The source the name claims is kept; with none (a DVR recording, a bare "Show - S01E01.ts") the
    lowest one at that resolution is taken, HDTV or SDTV, which is what a broadcast capture is.
    """
    current = parse_quality(media_file.quality or "").quality
    matched = next((q for q in QUALITIES if q.resolution == resolution and q.source == current.source), None)
    if matched is None:
        matched = next((q for q in QUALITIES if q.resolution == resolution and q.weight > 0), None)
    label = matched.name if matched else resolution.value
    media_file.quality = label
    media_file.quality_locked = True  # a rescan re-parses the filename; don't let it undo the relabel
    owner = media_file.movie if media_file.media_type == "movie" else media_file.series
    refresh_formats(db, media_file, profile_id=owner.quality_profile_id if owner else None,
                    episode_count=max(1, len(media_file.episodes)))
    if media_file.media_type == "movie" and media_file.movie:
        sync_legacy(db, media_file.movie)
    elif media_file.media_type == "tv":
        for ef in db.scalars(select(models.EpisodeFile).where(models.EpisodeFile.media_file_id == media_file.id)):
            if ef.episode:
                sync_legacy(db, ef.episode)
    return label


def _compute_file_formats(
    db: Session,
    title: str,
    size_bytes: int,
    episode_count: int,
    quality: ParsedQuality | None,
    profile_id: int | None,
) -> tuple[list[int], int]:
    from backend.domain.custom_formats import ReleaseFacts
    from backend.domain.custom_formats import score as score_formats
    from backend.services import quality_service

    matcher = quality_service.get_matcher(db)
    profile = quality_service.spec_for(db, profile_id)
    facts = ReleaseFacts.from_title(
        title,
        size_bytes=size_bytes,
        episode_count=max(episode_count, 1),
        quality=quality,
    )
    matches = matcher.match(facts)
    total_score, _ = score_formats(matches, profile.format_scores)
    return [m.format_id for m in matches], total_score


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

    effective_scene = scene_name or Path(path_str).name
    q = quality if quality is not None else parse_quality(effective_scene)
    quality_name = q.quality.name if q and q.quality and q.quality.weight else (movie.quality or "Unknown")
    codec_name = q.codec.value if q and q.codec and q.codec.value != "unknown" else None
    revision = 2 if (q and (q.proper or q.repack)) else 1
    rel_grp = release_group(effective_scene)
    rel_path = _compute_relative_path(path_str, movie.root_folder, movie.path)
    cf_ids, cf_score = _compute_file_formats(db, effective_scene, size_bytes, 1, q, movie.quality_profile_id)

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
            scene_name=effective_scene,
            edition=edition,
            source=source,
            is_primary=make_primary,
            custom_formats=cf_ids,
            custom_format_score=cf_score,
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
            mf.custom_formats = cf_ids
            mf.custom_format_score = cf_score
            mf.updated_at = utcnow()
    else:
        mf.movie_id = movie.id
        if mf.size_bytes != size_bytes or mf.mtime != mtime:
            mf.inspected_at = None
            mf.inspection_error = None
            mf.quality_locked = False  # new content: a relabel of the old file no longer applies

        mf.path = path_str
        mf.relative_path = rel_path
        mf.size_bytes = size_bytes
        mf.mtime = mtime
        if quality_name and quality_name != "Unknown" and not mf.quality_locked:
            mf.quality = quality_name
        if codec_name:
            mf.codec = codec_name
        if rel_grp:
            mf.release_group = rel_grp
        if scene_name:
            mf.scene_name = scene_name
        if edition:
            mf.edition = edition
        mf.custom_formats = cf_ids
        mf.custom_format_score = cf_score
        if mf.quality_locked:  # cf_ids was scored with the filename's quality, not the relabelled one
            refresh_formats(db, mf, profile_id=movie.quality_profile_id, episode_count=1)
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

    effective_scene = scene_name or Path(path_str).name
    q = quality if quality is not None else parse_quality(effective_scene)
    quality_name = q.quality.name if q and q.quality and q.quality.weight else "Unknown"
    codec_name = q.codec.value if q and q.codec and q.codec.value != "unknown" else None
    revision = 2 if (q and (q.proper or q.repack)) else 1
    rel_grp = release_group(effective_scene)
    rel_path = _compute_relative_path(path_str, series.root_folder, series.path)
    cf_ids, cf_score = _compute_file_formats(
        db, effective_scene, size_bytes, len(episodes) if episodes else 1, q, series.quality_profile_id
    )

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
            scene_name=effective_scene,
            source=source,
            is_primary=make_primary,
            custom_formats=cf_ids,
            custom_format_score=cf_score,
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
            mf.custom_formats = cf_ids
            mf.custom_format_score = cf_score
            mf.updated_at = utcnow()
    else:
        mf.series_id = series.id
        if mf.size_bytes != size_bytes or mf.mtime != mtime:
            mf.inspected_at = None
            mf.inspection_error = None
            mf.quality_locked = False  # new content: a relabel of the old file no longer applies

        mf.path = path_str
        mf.relative_path = rel_path
        mf.size_bytes = size_bytes
        mf.mtime = mtime
        if quality_name and quality_name != "Unknown" and not mf.quality_locked:
            mf.quality = quality_name
        if codec_name:
            mf.codec = codec_name
        if rel_grp:
            mf.release_group = rel_grp
        if scene_name:
            mf.scene_name = scene_name
        mf.custom_formats = cf_ids
        mf.custom_format_score = cf_score
        if mf.quality_locked:
            refresh_formats(db, mf, profile_id=series.quality_profile_id, episode_count=len(episodes))
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
    _rekey_file_issues(db, media_file, media_file.path, path_str)
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


def _owner_id(media_file: models.MediaFile) -> int | None:
    return media_file.movie_id if media_file.media_type == "movie" else media_file.series_id


def retire_file_issues(db: Session, media_file: models.MediaFile) -> int:
    """Auto-resolve the open file-scoped issues of a file whose record is going away.

    Nothing re-inspects a removed path, so without this its quality/runtime/corrupt issues would
    stay open forever (e.g. the old episode file after an upgrade).
    """
    rows = LibraryIssueRepository(db).for_file(media_file.media_type, _owner_id(media_file), media_file.path,
                                               FILE_SCOPED_ISSUE_TYPES, (Status.OPEN.value,))
    now = utcnow()
    for row in rows:
        row.status, row.resolved, row.resolved_at, row.resolution = Status.RESOLVED.value, True, now, "auto"
    return len(rows)


def _rekey_file_issues(db: Session, media_file: models.MediaFile, old_path: str, new_path: str) -> None:
    """Carry a file's open/dismissed file-scoped issues over to its new path on rename/move.

    A move keeps size and mtime, so the file is never re-inspected; without re-keying, its issues
    would point at a path that no longer exists and never be resolved by a later inspection.
    """
    if not old_path or normalise_path_key(old_path) == normalise_path_key(new_path):
        return
    repo = LibraryIssueRepository(db)
    statuses = (Status.OPEN.value, Status.DISMISSED.value)
    owner = _owner_id(media_file)
    taken = {row.fingerprint for row in repo.for_file(media_file.media_type, owner, new_path, FILE_SCOPED_ISSUE_TYPES, statuses)}
    now = utcnow()
    for row in repo.for_file(media_file.media_type, owner, old_path, FILE_SCOPED_ISSUE_TYPES, statuses):
        fp = fingerprint(row.issue_type, row.media_type, row.media_id, new_path)
        if fp in taken:  # the new path already carries this finding: the old row is redundant
            row.status, row.resolved, row.resolved_at, row.resolution = Status.RESOLVED.value, True, now, "auto"
            continue
        row.path, row.fingerprint = new_path[:1024], fp
        taken.add(fp)


def detach_file(db: Session, media_file: models.MediaFile, *, reason: str) -> None:
    """Detach/delete a media file record and recompute legacy columns on the owners."""
    log.info("Detaching file %s (reason: %s)", media_file.path, reason)
    retire_file_issues(db, media_file)
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
