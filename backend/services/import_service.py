"""Import engine: validate → match → decide (duplicate / upgrade / conflict) → place atomically → record.

Each import runs as one unit of work spanning the filesystem *and* the database:
file operations are journalled in a :class:`FileTransaction`; if anything fails – including
the final database commit – the journal is replayed backwards so the library, the download
folder and the database are left exactly as they were.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.observability import IMPORT_FAILURES_TOTAL, IMPORTS_TOTAL
from backend.core.settings import get_settings
from backend.db import models
from backend.db.repositories import MediaFileRepository, QueueRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain.diagnosis import ImportFailureReason, classify_oserror
from backend.domain.inspection import MEDIAINFO_EMPTY, mediainfo_tokens
from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, TemplateError, render
from backend.domain.parser import (
    identity_conflict,
    is_partial,
    is_sample,
    is_video,
    normalize_title,
    parse_episode,
    parse_folder_title,
    parse_movie,
    resolve_series,
    years_conflict,
)
from backend.domain.quality import ParsedQuality, parse_quality
from backend.services import fileops, import_diagnosis, inspection_service, media_file_service
from backend.services.audit import SYSTEM, Actor, audit, history
from backend.services.quality_service import spec_for

log = logging.getLogger("maniac.import")


class ImportError_(RuntimeError):
    def __init__(self, message: str, *, reason: ImportFailureReason = ImportFailureReason.UNKNOWN, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.evidence = evidence or {}


@dataclass
class Candidate:
    path: Path
    size: int
    state: str  # ok | partial | corrupt | not_video | too_small | sample
    reason: str | None
    quality: ParsedQuality
    media_type: str | None = None
    media_id: int | None = None
    title: str | None = None
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    library_state: str | None = None  # new | upgrade | duplicate (only set once matched)
    existing_quality: str | None = None
    existing_size: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "state": self.state,
            "reason": self.reason,
            "quality": self.quality.quality.name,
            "media_type": self.media_type,
            "media_id": self.media_id,
            "title": self.title,
            "season": self.season,
            "episodes": self.episodes,
            "library_state": self.library_state,
            "existing_quality": self.existing_quality,
            "existing_size": self.existing_size,
        }


@dataclass
class DeleteResult:
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failed_paths: dict[str, str] = field(default_factory=dict)  # path -> reason, so the UI can show it per-row
    failed_codes: dict[str, str] = field(default_factory=dict)  # path -> ImportFailureReason value

    @property
    def ok(self) -> bool:
        return bool(self.imported) and not self.failed


# --------------------------------------------------------------------------- discovery
def find_video_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Return (main videos, partial files) under ``root`` (a file or directory)."""
    if root.is_file():
        return ([root] if is_video(root) else []), ([root] if is_partial(root) else [])
    videos, partial = [], []
    for entry in fileops.iter_files(root):
        p = Path(entry.path)
        if is_partial(p) or p.name.endswith(fileops.PARTIAL_SUFFIX):
            partial.append(p)
        elif is_video(p) and not is_sample(p.name):
            videos.append(p)
    return videos, partial


_EXTRAS_DIRS = {"featurettes", "featurette", "extras", "extra", "behind the scenes", "deleted scenes", "interviews", "trailers", "shorts", "scenes"}


def _in_extras_dir(path: Path) -> bool:
    """Bonus material (Featurettes/, Extras/ ...) is never a main movie or episode file."""
    return any(part.lower() in _EXTRAS_DIRS for part in path.parent.parts)


def inspect(path: Path, min_size_mb: int) -> Candidate:
    size = path.stat().st_size if path.exists() else 0
    check = fileops.check_video_file(path, min_size_mb)
    state = "sample" if is_sample(path.name) or _in_extras_dir(path) else check.state
    return Candidate(path, size, state, check.reason, parse_quality(path.name if parse_quality(path.name).quality.weight else path.parent.name))


def match(db: Session, candidate: Candidate, hint_name: str | None = None) -> Candidate:
    """Best-effort library match for a loose file (manual import / watch folder)."""
    name = candidate.path.name
    ep = parse_episode(name) or (parse_episode(hint_name) if hint_name else None)
    if ep and ep.season is not None and ep.episodes:
        series = resolve_series(SeriesRepository(db).index(), ep.series_title, ep.year)
        if series:
            candidate.media_type, candidate.media_id, candidate.title = "tv", series.id, series.title
            candidate.season, candidate.episodes = ep.season, ep.episodes
        _annotate_library_state(db, candidate)
        return candidate
    pm = parse_movie(hint_name or name)
    if not pm.year:
        folder_title, folder_year, _ = parse_folder_title(candidate.path.parent.name)
        pm.title, pm.year = folder_title, folder_year
    key = normalize_title(pm.title)
    options = [m for m in db.scalars(select(models.Movie).where(models.Movie.title.ilike(f"%{pm.title[:3]}%"))) if normalize_title(m.title) == key]
    if pm.year:
        options = [m for m in options if not years_conflict(pm.year, m.year)]  # Dune.2021 is not Dune (1984)
    if options:
        movie = options[0]
        candidate.media_type, candidate.media_id, candidate.title = "movie", movie.id, f"{movie.title} ({movie.year})"
    _annotate_library_state(db, candidate)
    return candidate


def _candidate_formats(
    db: Session,
    title: str,
    size_bytes: int,
    episode_count: int,
    quality: ParsedQuality,
    profile,
) -> tuple[list, int, str]:
    from backend.domain.custom_formats import ReleaseFacts
    from backend.domain.custom_formats import score as score_formats
    from backend.services import quality_service

    matcher = quality_service.get_matcher(db)
    facts = ReleaseFacts.from_title(
        title,
        size_bytes=size_bytes,
        episode_count=max(episode_count, 1),
        quality=quality,
    )
    matches = matcher.match(facts)
    total_score, _ = score_formats(matches, profile.format_scores)
    matched_ids = {m.format_id for m in matches}
    rename_str = " ".join(cf.name for cf in matcher.formats if cf.include_in_rename and cf.id in matched_ids)
    return matches, total_score, rename_str


def _annotate_library_state(db: Session, candidate: Candidate) -> None:
    """Set library_state/existing_quality/existing_size once a media match is known.

    Read-only mirror of the duplicate/upgrade comparison `_check_existing` performs at import
    time, run eagerly during scan so the UI can show it before the user clicks Import.
    """
    from backend.services.quality_service import get_matcher
    from backend.services.search_service import compute_file_format_score

    if candidate.state != "ok" or not candidate.media_id:
        return
    matcher = get_matcher(db)
    if candidate.media_type == "movie":
        movie = db.get(models.Movie, candidate.media_id)
        if movie is None or not movie.has_file:
            candidate.library_state = "new"
            return
        profile = spec_for(db, movie.quality_profile_id)
        current_q = parse_quality(movie.quality or movie.file_path or "")
        current_fs, _ = compute_file_format_score(db, "movie", movie.id, movie.file_path, profile, matcher)
        _, candidate_fs, _ = _candidate_formats(db, candidate.path.name, candidate.size, 1, candidate.quality, profile)
        candidate.library_state = (
            "upgrade"
            if profile.is_upgrade(current_q, candidate.quality, current_fs=current_fs, candidate_fs=candidate_fs)
            else "duplicate"
        )
        candidate.existing_quality = movie.quality
        candidate.existing_size = movie.file_size_bytes
        return
    if candidate.media_type == "tv" and candidate.season is not None and candidate.episodes:
        episodes = list(db.scalars(select(models.Episode).where(
            models.Episode.series_id == candidate.media_id, models.Episode.season_number == candidate.season,
            models.Episode.episode_number.in_(candidate.episodes))))
        if not episodes or not all(e.has_file for e in episodes):
            candidate.library_state = "new"
            return
        series = db.get(models.Series, candidate.media_id)
        profile = spec_for(db, series.quality_profile_id if series else None)
        worst = min(episodes, key=lambda e: parse_quality(e.quality or e.file_path or "").quality.weight)
        current_q = parse_quality(worst.quality or worst.file_path or "")
        current_fs, _ = compute_file_format_score(
            db, "tv", candidate.media_id, worst.file_path, profile, matcher, episode_id=worst.id
        )
        _, candidate_fs, _ = _candidate_formats(
            db, candidate.path.name, candidate.size, len(candidate.episodes), candidate.quality, profile
        )
        candidate.library_state = (
            "upgrade"
            if profile.is_upgrade(current_q, candidate.quality, current_fs=current_fs, candidate_fs=candidate_fs)
            else "duplicate"
        )
        candidate.existing_quality = worst.quality
        candidate.existing_size = worst.file_size_bytes


# --------------------------------------------------------------------------- destination
def movie_destination(
    movie: models.Movie, src: Path, quality: ParsedQuality, original: str, custom_formats_token: str = "",
    mediainfo: dict[str, str] | None = None,
) -> Path:
    settings = get_settings()
    root = Path(movie.root_folder or settings.media_root_movies)
    folder = Path(movie.path) if movie.path else root / render(settings.movie_folder_format, {"title": movie.title, "year": movie.year}, MOVIE_TOKENS)
    if not settings.rename_on_import:
        return folder / src.name
    values = {
        "title": movie.title, "year": movie.year, "quality": quality.quality.name, "resolution": quality.quality.resolution.value,
        "source": quality.quality.source.value, "codec": quality.codec.value if quality.codec.value != "unknown" else "",
        "imdbid": movie.imdb_id, "tmdbid": movie.tmdb_id, "releasegroup": parse_movie(original).release_group, "originalname": Path(original).stem,
        "customformats": custom_formats_token,
        **(mediainfo or MEDIAINFO_EMPTY),  # measured facts when the file was probed before import
    }
    return folder / (render(settings.movie_format, values, MOVIE_TOKENS) + src.suffix.lower())


def episode_destination(
    series: models.Series, episodes: list[models.Episode], src: Path, quality: ParsedQuality, original: str,
    custom_formats_token: str = "", mediainfo: dict[str, str] | None = None,
) -> Path:
    settings = get_settings()
    root = Path(series.root_folder or settings.media_root_tv)
    folder = Path(series.path) if series.path else root / render(settings.series_folder_format, {"title": series.title, "year": series.year}, EPISODE_TOKENS)
    first = episodes[0]
    if not settings.rename_on_import:
        return folder / f"Season {first.season_number:02d}" / src.name
    title = " + ".join(e.title for e in episodes) if len(episodes) > 1 else first.title
    values = {
        "title": series.title, "year": series.year, "season": first.season_number, "episode": first.episode_number,
        "episodetitle": title, "airdate": first.air_date, "quality": quality.quality.name, "resolution": quality.quality.resolution.value,
        "source": quality.quality.source.value, "codec": quality.codec.value if quality.codec.value != "unknown" else "",
        "tvdbid": series.tvdb_id, "imdbid": series.imdb_id, "releasegroup": (parse_episode(original) or parse_movie(original)).release_group,
        "originalname": Path(original).stem, "customformats": custom_formats_token,
        **(mediainfo or MEDIAINFO_EMPTY),
    }
    rel = render(settings.tv_format, values, EPISODE_TOKENS)
    if len(episodes) > 1:
        last = episodes[-1].episode_number
        rel = rel.replace(f"E{first.episode_number:02d}", f"E{first.episode_number:02d}-E{last:02d}", 1)
    return folder / (rel + src.suffix.lower())


def _enqueue_inspection_after_commit(db: Session, media_file_id: int) -> None:
    """Queue ffprobe inspection once the import commits.

    ``job_queue.enqueue`` opens its own session; calling it mid-import would contend with this
    session's write lock (SQLite: "database is locked") and could queue a job for a rolled-back file.
    """
    from sqlalchemy import event

    from backend.workers.queue import job_queue

    def _fire(_session: Session) -> None:
        try:
            job_queue.enqueue("inspect_file", {"media_file_id": media_file_id}, dedupe_key=f"file:{media_file_id}")
        except Exception:  # inspection is best-effort; never fail a committed import
            log.warning("could not queue inspection for media file %s", media_file_id, exc_info=True)

    event.listen(db, "after_commit", _fire, once=True)


def _reject_if_truncated(pre: inspection_service.PreImportCheck | None, expected_min: int | None, src: Path,
                         size: int, *, is_movie: bool) -> None:
    """Pre-import gate for truncation, which needs the target's expected runtime (known only here)."""
    if pre is None or pre.inspection is None:
        return
    finding = inspection_service.truncation_finding(pre.inspection, expected_min, size, is_movie=is_movie)
    if finding is not None:
        raise ImportError_(f"Pre-import check: {finding.message}", reason=ImportFailureReason.CORRUPT_FILE,
                           evidence={"file": src.name, "check": "truncation", **finding.data})


def _pre_import_rejection(src: Path) -> tuple[inspection_service.PreImportCheck | None, str | None, dict]:
    """Run the optional pre-import probe; returns (check, rejection message, evidence)."""
    pre = inspection_service.pre_import_check(src)
    if pre is not None and pre.rejection:
        return pre, f"Pre-import check: {pre.rejection}", pre.evidence
    return pre, None, {}


# --------------------------------------------------------------------------- core import
def _import_one(
    db: Session, tx: fileops.FileTransaction, src: Path, quality: ParsedQuality, media_type: str, media_id: int,
    season: int | None, episode_numbers: list[int], mode: str, original_name: str, queue_id: int | None,
    force: bool = False, pre: inspection_service.PreImportCheck | None = None,
) -> str:
    from backend.services.quality_service import get_matcher
    from backend.services.search_service import compute_file_format_score

    settings = get_settings()
    recycle_root = Path(settings.recycle_bin)
    src_size = src.stat().st_size if src.exists() else 0
    matcher = get_matcher(db)

    if media_type == "movie":
        movie = db.get(models.Movie, media_id)
        if movie is None:
            raise ImportError_("Movie no longer exists in the library", reason=ImportFailureReason.MEDIA_DELETED, evidence={"media_id": media_id})
        profile = spec_for(db, movie.quality_profile_id)
        existing = Path(movie.file_path) if movie.has_file and movie.file_path else None
        current_q = parse_quality(movie.quality or movie.file_path or "") if existing else None
        current_fs, _ = (
            compute_file_format_score(db, "movie", movie.id, movie.file_path, profile, matcher)
            if existing
            else (None, None)
        )
        _reject_if_truncated(pre, movie.runtime_minutes, src, src_size, is_movie=True)
        scored_cf, candidate_fs, rename_cf = _candidate_formats(db, original_name or src.name, src_size, 1, quality, profile)
        upgrade = _check_existing(existing, src, current_q, quality, profile, force, current_fs=current_fs, candidate_fs=candidate_fs)
        existing_mf = MediaFileRepository(db).by_path(existing) if existing else None
        dest = movie_destination(movie, src, quality, original_name, custom_formats_token=rename_cf,
                                 mediainfo=mediainfo_tokens(pre.inspection) if pre else None)
        _clear_destination(tx, dest, existing, recycle_root)
        if upgrade and existing_mf:
            media_file_service.detach_file(db, existing_mf, reason="upgraded")
        used = tx.place(src, dest, mode)
        _move_sidecars(tx, src, dest, mode)
        mf = media_file_service.attach_movie_file(
            db, movie, dest, quality, scene_name=original_name, source="import", make_primary=True
        )
        db.flush()
        _enqueue_inspection_after_commit(db, mf.id)

        event = "upgraded" if upgrade else "imported"
        history(db, "movie", movie.id, movie.title, event, quality=quality.quality.name, download_id=queue_id,
                details=f"{used} → {dest}",
                data={
                    "source": str(src),
                    "previous_quality": current_q.quality.name if current_q else None,
                    "formats": [m.name for m in scored_cf],
                    "format_score": candidate_fs,
                    **({"forced": True} if force else {}),
                })
        audit(db, "import", f"movie.{event}", entity_type="movie", entity_id=movie.id, details={"file": str(dest), "mode": used})
        return f"{movie.title} ({movie.year})"

    series = db.get(models.Series, media_id)
    if series is None:
        raise ImportError_("Series no longer exists in the library", reason=ImportFailureReason.MEDIA_DELETED, evidence={"media_id": media_id})
    if season is None or not episode_numbers:
        raise ImportError_(f"Could not determine season/episode for {src.name}", reason=ImportFailureReason.UNPARSEABLE_EPISODE,
                           evidence={"file": src.name})
    episodes = list(db.scalars(select(models.Episode).where(
        models.Episode.series_id == series.id, models.Episode.season_number == season,
        models.Episode.episode_number.in_(episode_numbers)).order_by(models.Episode.episode_number)))
    missing = set(episode_numbers) - {e.episode_number for e in episodes}
    if missing:
        season_row = db.scalar(select(models.Season).where(models.Season.series_id == series.id, models.Season.season_number == season))
        if season_row is None:
            season_row = models.Season(series_id=series.id, season_number=season, monitored=True)
            db.add(season_row)
            db.flush()
        for n in sorted(missing):
            ep = models.Episode(series_id=series.id, season_id=season_row.id, season_number=season, episode_number=n,
                                title=f"Episode {n}", monitored=True, has_file=False, status="missing")
            db.add(ep)
            episodes.append(ep)
        db.flush()
        episodes.sort(key=lambda e: e.episode_number)
    _reject_if_truncated(pre, inspection_service.expected_runtime_minutes([e.runtime_minutes for e in episodes], series.runtime_minutes),
                         src, src_size, is_movie=False)
    profile = spec_for(db, series.quality_profile_id)
    existing_paths = {Path(e.file_path) for e in episodes if e.has_file and e.file_path}
    existing = next(iter(existing_paths)) if len(existing_paths) == 1 else None
    current_q = None
    current_fs = None
    if all(e.has_file for e in episodes):
        worst_ep = min(episodes, key=lambda e: parse_quality(e.quality or e.file_path or "").quality.weight)
        current_q = parse_quality(worst_ep.quality or worst_ep.file_path or "")
        current_fs, _ = compute_file_format_score(
            db, "tv", series.id, worst_ep.file_path, profile, matcher, episode_id=worst_ep.id
        )
    scored_cf, candidate_fs, rename_cf = _candidate_formats(
        db, original_name or src.name, src_size, len(episode_numbers), quality, profile
    )
    upgrade = (
        _check_existing(existing, src, current_q, quality, profile, force, current_fs=current_fs, candidate_fs=candidate_fs)
        if (current_q or force)
        else False
    )
    dest = episode_destination(series, episodes, src, quality, original_name, custom_formats_token=rename_cf,
                               mediainfo=mediainfo_tokens(pre.inspection) if pre else None)
    for old in existing_paths:
        if old != dest and old.exists():
            tx.recycle(old, recycle_root)
            old_mf = MediaFileRepository(db).by_path(old)
            if old_mf:
                media_file_service.detach_file(db, old_mf, reason="upgraded")
    _clear_destination(tx, dest, None, recycle_root)
    used = tx.place(src, dest, mode)
    _move_sidecars(tx, src, dest, mode)
    mf = media_file_service.attach_episode_file(
        db, series, episodes, dest, quality, scene_name=original_name, source="import", make_primary=True
    )
    db.flush()
    _enqueue_inspection_after_commit(db, mf.id)

    label = f"{series.title} S{season:02d}E{episode_numbers[0]:02d}" + (f"-E{episode_numbers[-1]:02d}" if len(episode_numbers) > 1 else "")
    event = "upgraded" if upgrade else "imported"
    history(db, "tv", series.id, label, event, quality=quality.quality.name, download_id=queue_id, details=f"{used} → {dest}",
            data={
                "source": str(src),
                "formats": [m.name for m in scored_cf],
                "format_score": candidate_fs,
                **({"forced": True} if force else {}),
            })
    audit(db, "import", f"episode.{event}", entity_type="series", entity_id=series.id, details={"file": str(dest), "episodes": episode_numbers})
    return label


def _check_existing(
    existing: Path | None, src: Path, current_q, new_q, profile, force: bool = False,
    *, current_fs: int | None = None, candidate_fs: int = 0,
) -> bool:
    """Duplicate / upgrade / conflict resolution. Returns True if this is an upgrade.

    ``force`` (from the FORCE_IMPORT fix) skips the allowed/upgrade checks but never the
    identical-file duplicate check, which would make no sense to override.
    """
    if existing is None or not existing.exists():
        if not force and not profile.is_allowed(new_q.quality):
            raise ImportError_(f"{new_q.quality.name} is not allowed by profile '{profile.name}'", reason=ImportFailureReason.QUALITY_NOT_ALLOWED,
                               evidence={"quality": new_q.quality.name, "profile": profile.name, "allowed": list(profile.allowed)})
        return False
    try:
        if existing.samefile(src):
            raise ImportError_("Duplicate: file is already the library file", reason=ImportFailureReason.DUPLICATE,
                               evidence={"existing": str(existing)})
    except OSError:
        pass
    if not force and existing.stat().st_size == src.stat().st_size and existing.name == src.name:
        raise ImportError_("Duplicate: identical file already in library", reason=ImportFailureReason.DUPLICATE,
                           evidence={"existing": str(existing), "size": existing.stat().st_size})
    if not force and not profile.is_upgrade(current_q, new_q, current_fs=current_fs, candidate_fs=candidate_fs):
        raise ImportError_(f"Not an upgrade: existing {current_q.quality.name if current_q else 'file'} is equal or better",
                           reason=ImportFailureReason.NOT_AN_UPGRADE,
                           evidence={"existing_quality": current_q.quality.name if current_q else None, "new_quality": new_q.quality.name})
    return True


def _clear_destination(tx: fileops.FileTransaction, dest: Path, existing: Path | None, recycle_root: Path) -> None:
    if existing and existing.exists() and existing != dest:
        tx.recycle(existing, recycle_root)
    if dest.exists():
        tx.recycle(dest, recycle_root)


def _move_sidecars(tx: fileops.FileTransaction, src: Path, dest: Path, mode: str) -> None:
    extras = {e.lower() for e in get_settings().extra_file_extensions}
    try:
        siblings = list(src.parent.iterdir())
    except OSError:
        return
    for sib in siblings:
        if sib.is_file() and sib.suffix.lower() in extras and sib.name.lower().startswith(src.stem.lower()):
            suffix = sib.name[len(src.stem):]
            target = dest.with_name(dest.stem + suffix)
            if not target.exists():
                try:
                    tx.place(sib, target, "copy" if mode in ("hardlink", "symlink") else mode)
                except OSError as exc:
                    log.info("Sidecar %s not imported: %s", sib.name, exc)


def _mode_for(protocol: str | None) -> str:
    mode = get_settings().import_mode
    # Moving a torrent's payload breaks seeding: hardlink instead (falls back to copy across volumes).
    if protocol == "torrent" and mode == "move":
        return "hardlink"
    return mode


# Per-video failures recorded as plain strings in ``result.failed`` (not raised, so several
# videos in one download can each be reported) map onto a reason for diagnosis purposes.
_CANDIDATE_STATE_REASONS: dict[str, ImportFailureReason] = {
    "corrupt": ImportFailureReason.CORRUPT_FILE,
    "too_small": ImportFailureReason.TOO_SMALL,
    "sample": ImportFailureReason.SAMPLE_ONLY,
    "not_video": ImportFailureReason.UNSUPPORTED_FORMAT,
    "missing": ImportFailureReason.PATH_NOT_FOUND,
}


def import_queue_item(queue_id: int, force: bool = False) -> ImportResult:
    result = ImportResult()
    allowed_statuses = ["completed", "import_failed"] + (["importing"] if force else [])
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            raise ImportError_("Queue item not found")
        # Atomic claim: the WHERE guard and the write happen in one statement, so a concurrent
        # caller (e.g. the worker pool racing a manual retry) that loses the race sees rowcount
        # 0 and skips instead of both proceeding to import the same file.
        claim = db.execute(
            update(models.DownloadQueueItem)
            .where(models.DownloadQueueItem.id == queue_id, models.DownloadQueueItem.status.in_(allowed_statuses))
            .values(status="importing")
        )
        if claim.rowcount == 0:
            result.skipped.append(f"Queue item is {q.status}")
            return result
        info = (q.download_path, q.media_type, q.media_id, q.season_num, q.episode_numbers or ([q.ep_num] if q.ep_num else []), q.protocol, q.release_title)
    bus.publish(EventType.QUEUE_CHANGED)
    try:
        return _import_claimed(queue_id, force, *info)
    except Exception as exc:
        # Nothing else moves a row out of "importing" (a re-run can't claim it), so whatever escaped,
        # release the claim rather than leave the row showing as importing for good.
        _release_claim(queue_id, f"Unexpected error: {exc}")
        raise


def _import_recorded(db: Session, q: models.DownloadQueueItem) -> bool:
    """Whether any of this download's files reached the library – an interrupted import may have
    committed its file work before it could mark the row imported."""
    stmt = select(models.History.id).where(models.History.download_id == q.id,
                                           models.History.event_type.in_(("imported", "upgraded")))
    if q.added_at is not None:  # SQLite can reuse a deleted row's id; ignore an earlier owner's history
        stmt = stmt.where(models.History.timestamp >= q.added_at)
    return db.scalar(stmt.limit(1)) is not None


def _release_claim(queue_id: int, error: str) -> None:
    try:
        with session_scope() as db:
            q = db.get(models.DownloadQueueItem, queue_id)
            if q is None or q.status != "importing":
                return
            if _import_recorded(db, q):
                q.status = "imported"
            else:
                q.status, q.error_message = "import_failed", error
                q.failure_reason = q.failure_signature = ImportFailureReason.UNKNOWN.value
    except Exception:
        log.exception("Could not release the import claim on queue item %s", queue_id)
        return
    bus.publish(EventType.QUEUE_CHANGED)


def recover_interrupted_imports() -> int:
    """Settle queue items a restart left in "importing": the re-queued job can't claim them again.

    Runs at startup before the workers, so no import is genuinely in flight. Items whose files made it
    into the library are marked imported; the rest go back to completed and are imported again.
    """
    from backend.workers.queue import job_queue

    retry: list[int] = []
    with session_scope() as db:
        stuck = list(db.scalars(select(models.DownloadQueueItem).where(models.DownloadQueueItem.status == "importing")))
        for q in stuck:
            if _import_recorded(db, q):
                q.status = "imported"
            else:
                q.status = "completed"
                retry.append(q.id)
    for qid in retry:
        job_queue.enqueue("import_download", {"queue_id": qid}, trigger="event", dedupe_key=str(qid))
    if stuck:
        log.warning("Recovered %d import(s) interrupted by a restart (%d re-queued)", len(stuck), len(retry))
    return len(stuck)


def _import_claimed(queue_id: int, force: bool, download_path, media_type, media_id, season, episodes, protocol,
                    release_title) -> ImportResult:
    settings = get_settings()
    result = ImportResult()
    failure = None
    fail_reason = ImportFailureReason.UNKNOWN
    fail_evidence: dict = {}
    try:
        if not download_path or not Path(download_path).exists():
            raise ImportError_(f"Download path not found: {download_path}", reason=ImportFailureReason.PATH_NOT_FOUND,
                               evidence={"download_path": download_path})
        videos, partial = find_video_files(Path(download_path))
        if partial and not videos:
            with session_scope() as db:
                db.get(models.DownloadQueueItem, queue_id).status = "completed"
            result.skipped.append("Download still has partial files; will retry")
            return result
        if not videos:
            raise ImportError_("No video files found in download", reason=ImportFailureReason.NO_VIDEO_FILES,
                               evidence={"download_path": download_path})
        mode = _mode_for(protocol)
        series = None
        if media_type == "tv":
            with session_scope() as db:
                row = db.get(models.Series, media_id)
                series = SimpleNamespace(title=row.title, year=row.year, country=row.country) if row is not None else None
            # last line of defence against a same-titled show: Dark.Matter.2024.* never lands in Dark Matter (2015),
            # Ghosts.US never in the British Ghosts (a country the series doesn't know yet is no proof either way)
            release_ep = parse_episode(release_title)
            conflict = release_ep and series and identity_conflict(release_ep.series_title, release_ep.year, series, unknown_country=False)
            if conflict:
                raise ImportError_(f"Release is for a different show than {series.title}: {conflict}",
                                   reason=ImportFailureReason.NOT_MATCHED,
                                   evidence={"release_title": release_title, "conflict": conflict})
        for video in videos:
            candidate = inspect(video, settings.min_import_size_mb)
            if candidate.state != "ok":
                result.failed.append(f"{video.name}: {candidate.reason or candidate.state}")
                fail_reason = _CANDIDATE_STATE_REASONS.get(candidate.state, ImportFailureReason.NO_VIDEO_FILES)
                fail_evidence = {"file": video.name, "state": candidate.state, "detail": candidate.reason}
                continue
            pre, rejection, evidence = _pre_import_rejection(video)
            if rejection:
                result.failed.append(f"{video.name}: {rejection}")
                fail_reason, fail_evidence = ImportFailureReason.CORRUPT_FILE, evidence
                continue
            file_season, file_eps = season, episodes
            if media_type == "tv":
                parsed = parse_episode(video.name) or parse_episode(release_title)
                conflict = parsed and series and identity_conflict(parsed.series_title, parsed.year, series, unknown_country=False)
                if conflict:
                    result.failed.append(f"{video.name}: file is for a different show ({conflict})")
                    fail_reason, fail_evidence = ImportFailureReason.NOT_MATCHED, {"file": video.name, "conflict": conflict}
                    continue
                if parsed and parsed.episodes:
                    file_season, file_eps = parsed.season if parsed.season is not None else season, parsed.episodes
                elif len(videos) > 1 or not episodes:
                    result.failed.append(f"{video.name}: cannot determine episode")
                    fail_reason, fail_evidence = ImportFailureReason.UNPARSEABLE_EPISODE, {"file": video.name, "release_title": release_title}
                    continue
            quality = candidate.quality if candidate.quality.quality.weight else parse_quality(release_title)
            label = _run_import(video, quality, media_type, media_id, file_season, list(file_eps or []), mode, release_title, queue_id, force, pre)
            result.imported.append(label)
    except ImportError_ as exc:
        failure = str(exc)
        fail_reason, fail_evidence = exc.reason, exc.evidence
    except Exception as exc:
        log.exception("Import of queue item %s crashed", queue_id)
        failure = f"Unexpected error: {exc}"
        fail_reason, fail_evidence = ImportFailureReason.UNKNOWN, {"exception": type(exc).__name__}

    if failure or (result.failed and not result.imported):
        failure = failure or "; ".join(result.failed)
    analysis = None
    if failure:
        with session_scope() as db:
            q = db.get(models.DownloadQueueItem, queue_id)
            if q is not None:
                analysis = import_diagnosis.diagnose(db, q, fail_reason, fail_evidence, failure)
    retry_count_before = 0
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is not None:
            if failure:
                q.status = "import_failed"
                q.error_message = failure
                q.failure_reason = analysis.reason.value if analysis else fail_reason.value
                q.failure_signature = analysis.signature if analysis else fail_reason.value
                q.failure_analysis = analysis.to_dict() if analysis else None
                retry_count_before = q.retry_count or 0
                history(db, q.media_type, q.media_id, q.release_title, "import_failed", quality=q.quality, download_id=q.id, details=failure,
                        data={"analysis": analysis.to_dict()} if analysis else None)
            else:
                q.status = "imported"
                q.error_message = "; ".join(result.failed) or None
                q.failure_reason = None
                q.failure_signature = None
                q.failure_analysis = None
    if result.imported and _mode_for(protocol) == "move":
        src = Path(download_path)
        _remove_emptied_folder(src if src.is_dir() else src.parent)
    if failure:
        IMPORTS_TOTAL.labels(media_type, "failed").inc()
        IMPORT_FAILURES_TOTAL.labels(analysis.reason.value if analysis else fail_reason.value).inc()
        bus.publish(EventType.IMPORT_FAILED, queue_id=queue_id, title=release_title, reason=(analysis.summary if analysis else failure),
                    media_type=media_type, media_id=media_id, reason_code=(analysis.reason.value if analysis else fail_reason.value),
                    fix_available=bool(analysis and analysis.fixes))
    else:
        IMPORTS_TOTAL.labels(media_type, "imported").inc()
        bus.publish(EventType.IMPORT_COMPLETED, queue_id=queue_id, title=release_title, items=result.imported, media_type=media_type, media_id=media_id)
    bus.publish(EventType.QUEUE_CHANGED)

    # Auto-fix strategy: only fixes the probe marked safe_auto run without a click, and only up to
    # a small retry cap per item – anything that changes settings or overrides a quality decision
    # always needs a click (handled through the API, never here).
    if failure and analysis and settings.auto_fix_imports and retry_count_before < 3:
        auto_fix = next((f for f in analysis.fixes if f.safe_auto), None)
        if auto_fix is not None:
            with session_scope() as db:
                q = db.get(models.DownloadQueueItem, queue_id)
                if q is not None:
                    q.retry_count = retry_count_before + 1
            try:
                asyncio.run(import_diagnosis.apply_fix(queue_id, auto_fix.kind, auto_fix.params, SYSTEM, bulk=False))
            except Exception:
                log.exception("Auto-fix %s failed for queue item %s", auto_fix.kind, queue_id)

    if failure:
        result.failed = [failure]
    return result


def _run_import(src: Path, quality, media_type, media_id, season, episodes, mode, original, queue_id, force: bool = False,
                pre: inspection_service.PreImportCheck | None = None) -> str:
    tx = fileops.FileTransaction()
    try:
        with session_scope() as db:  # commit happens on exit; failure below rolls files back too
            label = _import_one(db, tx, src, quality, media_type, media_id, season, episodes, mode, original, queue_id, force, pre)
        return label
    except TemplateError as exc:
        tx.rollback()
        raise ImportError_(f"Naming template error: {exc}", reason=ImportFailureReason.NAMING_TEMPLATE_ERROR, evidence={"error": str(exc)}) from exc
    except ImportError_:
        tx.rollback()
        raise
    except OSError as exc:
        tx.rollback()
        reason = classify_oserror(exc.errno, getattr(exc, "winerror", None))
        raise ImportError_(f"Filesystem error: {exc.strerror or exc}", reason=reason,
                           evidence={"errno": exc.errno, "path": str(exc.filename or src)}) from exc
    except Exception:
        tx.rollback()
        raise


# --------------------------------------------------------------------------- manual import
def manual_scan(folder: str) -> list[dict]:
    root = Path(folder)
    if not root.exists():
        raise ImportError_(f"Folder not found: {folder}")
    settings = get_settings()
    videos, partial = find_video_files(root)
    out = []
    with session_scope() as db:
        for v in videos[:1000]:
            c = inspect(v, settings.min_import_size_mb)
            match(db, c, hint_name=v.parent.name if v.parent != root else None)
            out.append(c.to_dict())
    for p in partial[:200]:
        out.append(Candidate(p, p.stat().st_size if p.exists() else 0, "partial", "Download in progress", parse_quality(p.name)).to_dict())
    return out


def _import_quality(src: Path, label: str | None):
    """Quality used at import time. The UI only sends the label ("Bluray-1080p"), which drops PROPER/REPACK;
    re-parse the file name first so the import judges the same quality the scan showed (otherwise a REPACK
    is listed as an "upgrade" and then rejected as "not an upgrade")."""
    from_name = parse_quality(src.name)
    if not label or from_name.quality.name.lower() == label.lower():
        return from_name
    return parse_quality(label)   # the quality came from the folder name (file name has none)


def manual_import(items: list[dict], mode: str | None, actor: Actor, force: bool = False) -> ImportResult:
    result = ImportResult()
    mode = mode or get_settings().import_mode
    for item in items:
        src = Path(item["path"])
        check = fileops.check_video_file(src, 0)
        if not check.ok:
            result.failed.append(f"{src.name}: {check.reason}")
            result.failed_paths[item["path"]] = check.reason or "Invalid file"
            continue
        pre, rejection, _ = _pre_import_rejection(src)
        if rejection:
            result.failed.append(f"{src.name}: {rejection}")
            result.failed_paths[item["path"]] = rejection
            result.failed_codes[item["path"]] = str(ImportFailureReason.CORRUPT_FILE)
            continue
        try:
            label = _run_import(src, _import_quality(src, item.get("quality")), item["media_type"], int(item["media_id"]),
                                item.get("season"), list(item.get("episodes") or []), mode, src.name, None, force, pre=pre)
            result.imported.append(label)
            IMPORTS_TOTAL.labels(item["media_type"], "imported").inc()
            if mode == "move":
                _remove_emptied_folder(src.parent)
        except Exception as exc:
            result.failed.append(f"{src.name}: {exc}")
            result.failed_paths[item["path"]] = str(exc)
            if isinstance(exc, ImportError_):
                result.failed_codes[item["path"]] = str(exc.reason)
            IMPORTS_TOTAL.labels(item.get("media_type") or "unknown", "failed").inc()
    with session_scope() as db:
        audit(db, "import", "manual_import", actor=actor, details={"imported": len(result.imported), "failed": result.failed[:20]},
              success=not result.failed)
    if result.imported:
        bus.publish(EventType.IMPORT_COMPLETED, title=f"Manual import ({len(result.imported)} files)", items=result.imported)
    return result


def annotate_match(path: str, media_type: str, media_id: int, season: int | None, episodes: list[int]) -> dict:
    """Re-run the duplicate/upgrade comparison for a file against a manually-chosen match.

    `manual_scan` only annotates `library_state` when it can guess the match itself; once the
    user picks a target by hand in the UI, this recomputes the same comparison against that
    explicit choice so upgrade/duplicate/equal-quality shows up before Import is clicked.
    """
    src = Path(path)
    if not src.exists():
        raise ImportError_(f"File not found: {path}")
    settings = get_settings()
    candidate = inspect(src, settings.min_import_size_mb)
    if candidate.state != "ok":
        return candidate.to_dict()
    candidate.media_type, candidate.media_id = media_type, media_id
    with session_scope() as db:
        if media_type == "movie":
            movie = db.get(models.Movie, media_id)
            if movie is None:
                raise ImportError_("Movie not found")
            candidate.title = f"{movie.title} ({movie.year})"
        else:
            series = db.get(models.Series, media_id)
            if series is None:
                raise ImportError_("Series not found")
            candidate.title = series.title
            candidate.season, candidate.episodes = season, episodes or []
        _annotate_library_state(db, candidate)
    return candidate.to_dict()


def delete_candidates(paths: list[str], actor: Actor) -> DeleteResult:
    """Permanently remove stray files from the download folder (corrupt files, rejected
    duplicates). These never entered the library, so there is nothing to recycle – unlike
    replacing a library file, which still goes through ``fileops.recycle``.
    """
    result = DeleteResult()
    root = Path(get_settings().download_folder).resolve()
    for raw in paths:
        p = Path(raw)
        try:
            resolved = p.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            result.failed.append(f"{raw}: outside the download folder")
            continue
        if not resolved.exists():
            result.failed.append(f"{raw}: not found")
            continue
        try:
            os.remove(fileops.long_path(resolved))
            fileops.remove_empty_dirs(resolved.parent, root)
            result.deleted.append(str(resolved))
        except OSError as exc:
            result.failed.append(f"{raw}: {exc.strerror or exc}")
    with session_scope() as db:
        audit(db, "import", "delete_candidate", actor=actor, details={"deleted": result.deleted, "failed": result.failed},
              success=not result.failed)
    return result


# --------------------------------------------------------------------------- watch folder
_CLIENT_WORK_PREFIXES = ("_UNPACK_", "_FAILED_", "_ADMIN_")  # SABnzbd/NZBGet working folders
_CATEGORY_DIRS = {"movies", "tv", "series", "complete", "sonarr", "radarr", "maniac-media"}
# folders a download client keeps around rather than per-release ones: emptied all the time, but
# removing them can upset a client (a blackhole watcher loses its folder) or a shared /data layout
_CLIENT_DIRS = _CATEGORY_DIRS | {"incomplete", "intermediate", "downloading", "watch", "blackhole", "torrents", "usenet"}
EMPTY_FOLDER_MIN_AGE = 3600  # seconds; younger empty folders may be a download that's just starting


def scan_download_folder() -> dict:
    """Detect completed / partial / corrupt downloads in the watch folder.

    Items owned by a queue entry are handled by the queue. Untracked files are only imported
    when ``import_untracked_downloads`` is enabled – otherwise they are reported, never moved.
    """
    settings = get_settings()
    root = Path(settings.download_folder)
    report = {"completed": 0, "partial": 0, "corrupt": 0, "imported": 0, "untracked": []}
    if not root.exists():
        return report
    with session_scope() as db:
        tracked = {
            str(Path(p)).lower()
            for p in db.scalars(select(models.DownloadQueueItem.download_path).where(models.DownloadQueueItem.download_path.is_not(None)))
        }
    try:
        entries = list(root.iterdir())
    except OSError:
        return report
    for entry in entries:
        if str(entry).lower() in tracked:
            continue
        upper = entry.name.upper()
        if upper.startswith(_CLIENT_WORK_PREFIXES):
            report["corrupt" if upper.startswith("_FAILED_") else "partial"] += 1
            continue
        if entry.is_dir() and entry.name.lower() in _CATEGORY_DIRS:
            entries.extend(sub for sub in _safe_iterdir(entry))  # category sub-folders
            continue
        videos, partial = find_video_files(entry)
        if partial:
            report["partial"] += 1
            continue
        for video in videos:
            if not fileops.is_stable(video, 0.5):
                report["partial"] += 1
                continue
            with session_scope() as db:
                c = match(db, inspect(video, settings.min_import_size_mb), hint_name=entry.name if entry.is_dir() else None)
            if c.state == "corrupt":
                report["corrupt"] += 1
                continue
            if c.state != "ok":
                continue
            report["completed"] += 1
            if not settings.import_untracked_downloads or not c.media_id:
                if len(report["untracked"]) < 100:
                    report["untracked"].append(c.to_dict())
                continue
            try:
                _run_import(video, c.quality, c.media_type, c.media_id, c.season, c.episodes, settings.import_mode, video.name, None)
                report["imported"] += 1
                if settings.import_mode == "move":
                    _remove_emptied_folder(video.parent)
            except Exception as exc:
                log.info("Untracked import of %s skipped: %s", video.name, exc)
    return report


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _download_folder_guards() -> tuple[Path, Callable[[Path], bool], Callable[[Path], bool]] | None:
    """The download folder plus ``skip`` / ``keep`` predicates that confine empty-folder removal to
    release folders: never a client's category or working folders, a download still in flight, or a
    media library / recycle bin that happens to live underneath."""
    settings = get_settings()
    root = Path(os.path.abspath(settings.download_folder))
    libraries = {_norm(p) for p in (settings.media_root_movies, settings.media_root_tv, settings.recycle_bin) if p}
    if not root.is_dir() or _norm(root) in libraries:
        return None
    with session_scope() as db:
        protected = libraries | {_norm(p) for p in QueueRepository(db).active_download_paths()}

    def skip(path: Path) -> bool:
        return path.name.upper().startswith(_CLIENT_WORK_PREFIXES) or _norm(path) in protected

    def keep(path: Path) -> bool:
        return path.name.lower() in _CLIENT_DIRS

    return root, skip, keep


def prune_download_folder() -> int:
    """Housekeeping: remove empty release folders left in the download folder (older than an hour)."""
    if (guards := _download_folder_guards()) is None:
        return 0
    root, skip, keep = guards
    return fileops.prune_empty_dirs(root, min_age_seconds=EMPTY_FOLDER_MIN_AGE, skip=skip, keep=keep)


def _remove_emptied_folder(folder: Path) -> None:
    """Right after a move import: remove the release folder the video came from once nothing is left
    in it (empty Subs/-style subfolders included), then any parents that emptied with it."""
    try:
        if (guards := _download_folder_guards()) is None:
            return
        root, skip, keep = guards
        folder = Path(os.path.abspath(folder))
        if Path(_norm(root)) not in Path(_norm(folder)).parents or skip(folder) or keep(folder):
            return  # outside the download folder, or a loose file sitting in the root / a category folder
        fileops.prune_empty_dirs(folder, skip=skip, keep=keep)
        fileops.remove_empty_dirs(folder, root, keep=lambda p: skip(p) or keep(p))
    except Exception:  # tidying up must never fail an import that already succeeded
        log.warning("Could not remove the emptied download folder %s", folder, exc_info=True)


def failed_imports(db: Session) -> int:
    from sqlalchemy import func

    return db.scalar(select(func.count(models.DownloadQueueItem.id)).where(models.DownloadQueueItem.status == "import_failed")) or 0


__all__ = [
    "SYSTEM",
    "annotate_match",
    "delete_candidates",
    "failed_imports",
    "import_queue_item",
    "manual_import",
    "manual_scan",
    "prune_download_folder",
    "recover_interrupted_imports",
    "scan_download_folder",
]
