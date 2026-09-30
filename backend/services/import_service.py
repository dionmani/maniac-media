"""Import engine: validate → match → decide (duplicate / upgrade / conflict) → place atomically → record.

Each import runs as one unit of work spanning the filesystem *and* the database:
file operations are journalled in a :class:`FileTransaction`; if anything fails – including
the final database commit – the journal is replayed backwards so the library, the download
folder and the database are left exactly as they were.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.observability import IMPORTS_TOTAL
from backend.core.settings import get_settings
from backend.db import models
from backend.db.session import session_scope
from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, TemplateError, render
from backend.domain.parser import is_partial, is_sample, is_video, normalize_title, parse_episode, parse_folder_title, parse_movie
from backend.domain.quality import ParsedQuality, parse_quality
from backend.services import fileops
from backend.services.audit import SYSTEM, Actor, audit, history
from backend.services.quality_service import spec_for

log = logging.getLogger("maniac.import")


class ImportError_(RuntimeError):
    pass


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


def inspect(path: Path, min_size_mb: int) -> Candidate:
    size = path.stat().st_size if path.exists() else 0
    check = fileops.check_video_file(path, min_size_mb)
    state = "sample" if is_sample(path.name) else check.state
    return Candidate(path, size, state, check.reason, parse_quality(path.name if parse_quality(path.name).quality.weight else path.parent.name))


def match(db: Session, candidate: Candidate, hint_name: str | None = None) -> Candidate:
    """Best-effort library match for a loose file (manual import / watch folder)."""
    name = candidate.path.name
    ep = parse_episode(name) or (parse_episode(hint_name) if hint_name else None)
    if ep and ep.season is not None and ep.episodes:
        key = normalize_title(ep.series_title)
        series = next((s for s in db.scalars(select(models.Series)) if normalize_title(s.title) == key), None)
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
        options = [m for m in options if not m.year or abs(m.year - pm.year) <= 1] or options
    if options:
        movie = options[0]
        candidate.media_type, candidate.media_id, candidate.title = "movie", movie.id, f"{movie.title} ({movie.year})"
    _annotate_library_state(db, candidate)
    return candidate


def _annotate_library_state(db: Session, candidate: Candidate) -> None:
    """Set library_state/existing_quality/existing_size once a media match is known.

    Read-only mirror of the duplicate/upgrade comparison `_check_existing` performs at import
    time, run eagerly during scan so the UI can show it before the user clicks Import.
    """
    if candidate.state != "ok" or not candidate.media_id:
        return
    if candidate.media_type == "movie":
        movie = db.get(models.Movie, candidate.media_id)
        if movie is None or not movie.has_file:
            candidate.library_state = "new"
            return
        profile = spec_for(db, movie.quality_profile_id)
        current_q = parse_quality(movie.quality or movie.file_path or "")
        candidate.library_state = "upgrade" if profile.is_upgrade(current_q, candidate.quality) else "duplicate"
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
        candidate.library_state = "upgrade" if profile.is_upgrade(current_q, candidate.quality) else "duplicate"
        candidate.existing_quality = worst.quality
        candidate.existing_size = worst.file_size_bytes


# --------------------------------------------------------------------------- destination
def movie_destination(movie: models.Movie, src: Path, quality: ParsedQuality, original: str) -> Path:
    settings = get_settings()
    root = Path(movie.root_folder or settings.media_root_movies)
    folder = Path(movie.path) if movie.path else root / render(settings.movie_folder_format, {"title": movie.title, "year": movie.year}, MOVIE_TOKENS)
    if not settings.rename_on_import:
        return folder / src.name
    values = {
        "title": movie.title, "year": movie.year, "quality": quality.quality.name, "resolution": quality.quality.resolution.value,
        "source": quality.quality.source.value, "codec": quality.codec.value if quality.codec.value != "unknown" else "",
        "imdbid": movie.imdb_id, "tmdbid": movie.tmdb_id, "releasegroup": parse_movie(original).release_group, "originalname": Path(original).stem,
    }
    return folder / (render(settings.movie_format, values, MOVIE_TOKENS) + src.suffix.lower())


def episode_destination(series: models.Series, episodes: list[models.Episode], src: Path, quality: ParsedQuality, original: str) -> Path:
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
        "originalname": Path(original).stem,
    }
    rel = render(settings.tv_format, values, EPISODE_TOKENS)
    if len(episodes) > 1:
        last = episodes[-1].episode_number
        rel = rel.replace(f"E{first.episode_number:02d}", f"E{first.episode_number:02d}-E{last:02d}", 1)
    return folder / (rel + src.suffix.lower())


# --------------------------------------------------------------------------- core import
def _import_one(
    db: Session, tx: fileops.FileTransaction, src: Path, quality: ParsedQuality, media_type: str, media_id: int,
    season: int | None, episode_numbers: list[int], mode: str, original_name: str, queue_id: int | None,
) -> str:
    settings = get_settings()
    recycle_root = Path(settings.recycle_bin)

    if media_type == "movie":
        movie = db.get(models.Movie, media_id)
        if movie is None:
            raise ImportError_("Movie no longer exists in the library")
        profile = spec_for(db, movie.quality_profile_id)
        existing = Path(movie.file_path) if movie.has_file and movie.file_path else None
        current_q = parse_quality(movie.quality or movie.file_path or "") if existing else None
        upgrade = _check_existing(existing, src, current_q, quality, profile)
        dest = movie_destination(movie, src, quality, original_name)
        _clear_destination(tx, dest, existing, recycle_root)
        used = tx.place(src, dest, mode)
        _move_sidecars(tx, src, dest, mode)
        movie.path = str(dest.parent)
        movie.file_path = str(dest)
        movie.file_size_bytes = dest.stat().st_size
        movie.has_file = True
        movie.quality = quality.quality.name
        event = "upgraded" if upgrade else "imported"
        history(db, "movie", movie.id, movie.title, event, quality=quality.quality.name, download_id=queue_id,
                details=f"{used} → {dest}", data={"source": str(src), "previous_quality": current_q.quality.name if current_q else None})
        audit(db, "import", f"movie.{event}", entity_type="movie", entity_id=movie.id, details={"file": str(dest), "mode": used})
        return f"{movie.title} ({movie.year})"

    series = db.get(models.Series, media_id)
    if series is None:
        raise ImportError_("Series no longer exists in the library")
    if season is None or not episode_numbers:
        raise ImportError_(f"Could not determine season/episode for {src.name}")
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
    profile = spec_for(db, series.quality_profile_id)
    existing_paths = {Path(e.file_path) for e in episodes if e.has_file and e.file_path}
    existing = next(iter(existing_paths)) if len(existing_paths) == 1 else None
    current_q = None
    if all(e.has_file for e in episodes):
        current_q = min((parse_quality(e.quality or e.file_path or "") for e in episodes), key=lambda q: q.quality.weight)
    upgrade = _check_existing(existing, src, current_q, quality, profile) if current_q else False
    dest = episode_destination(series, episodes, src, quality, original_name)
    for old in existing_paths:
        if old != dest and old.exists():
            tx.recycle(old, recycle_root)
    _clear_destination(tx, dest, None, recycle_root)
    used = tx.place(src, dest, mode)
    _move_sidecars(tx, src, dest, mode)
    size = dest.stat().st_size
    for e in episodes:
        e.has_file, e.file_path, e.quality, e.status, e.file_size_bytes = True, str(dest), quality.quality.name, "downloaded", size
    if not series.path:
        series.path = str(dest.parent.parent)
    label = f"{series.title} S{season:02d}E{episode_numbers[0]:02d}" + (f"-E{episode_numbers[-1]:02d}" if len(episode_numbers) > 1 else "")
    event = "upgraded" if upgrade else "imported"
    history(db, "tv", series.id, label, event, quality=quality.quality.name, download_id=queue_id, details=f"{used} → {dest}", data={"source": str(src)})
    audit(db, "import", f"episode.{event}", entity_type="series", entity_id=series.id, details={"file": str(dest), "episodes": episode_numbers})
    return label


def _check_existing(existing: Path | None, src: Path, current_q, new_q, profile) -> bool:
    """Duplicate / upgrade / conflict resolution. Returns True if this is an upgrade."""
    if existing is None or not existing.exists():
        if not profile.is_allowed(new_q.quality):
            raise ImportError_(f"{new_q.quality.name} is not allowed by profile '{profile.name}'")
        return False
    try:
        if existing.samefile(src):
            raise ImportError_("Duplicate: file is already the library file")
    except OSError:
        pass
    if existing.stat().st_size == src.stat().st_size and existing.name == src.name:
        raise ImportError_("Duplicate: identical file already in library")
    if not profile.is_upgrade(current_q, new_q):
        raise ImportError_(f"Not an upgrade: existing {current_q.quality.name if current_q else 'file'} is equal or better")
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


def import_queue_item(queue_id: int) -> ImportResult:
    settings = get_settings()
    result = ImportResult()
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is None:
            raise ImportError_("Queue item not found")
        if q.status not in ("completed", "import_failed", "importing"):
            result.skipped.append(f"Queue item is {q.status}")
            return result
        q.status = "importing"
        info = (q.download_path, q.media_type, q.media_id, q.season_num, q.episode_numbers or ([q.ep_num] if q.ep_num else []), q.protocol, q.release_title)
    bus.publish(EventType.QUEUE_CHANGED)
    download_path, media_type, media_id, season, episodes, protocol, release_title = info

    failure = None
    try:
        if not download_path or not Path(download_path).exists():
            raise ImportError_(f"Download path not found: {download_path}")
        videos, partial = find_video_files(Path(download_path))
        if partial and not videos:
            with session_scope() as db:
                db.get(models.DownloadQueueItem, queue_id).status = "completed"
            result.skipped.append("Download still has partial files; will retry")
            return result
        if not videos:
            raise ImportError_("No video files found in download")
        mode = _mode_for(protocol)
        for video in videos:
            candidate = inspect(video, settings.min_import_size_mb)
            if candidate.state != "ok":
                result.failed.append(f"{video.name}: {candidate.reason or candidate.state}")
                continue
            file_season, file_eps = season, episodes
            if media_type == "tv":
                parsed = parse_episode(video.name) or parse_episode(release_title)
                if parsed and parsed.episodes:
                    file_season, file_eps = parsed.season if parsed.season is not None else season, parsed.episodes
                elif len(videos) > 1 or not episodes:
                    result.failed.append(f"{video.name}: cannot determine episode")
                    continue
            quality = candidate.quality if candidate.quality.quality.weight else parse_quality(release_title)
            label = _run_import(video, quality, media_type, media_id, file_season, list(file_eps or []), mode, release_title, queue_id)
            result.imported.append(label)
    except ImportError_ as exc:
        failure = str(exc)
    except Exception as exc:
        log.exception("Import of queue item %s crashed", queue_id)
        failure = f"Unexpected error: {exc}"

    if failure or (result.failed and not result.imported):
        failure = failure or "; ".join(result.failed)
    with session_scope() as db:
        q = db.get(models.DownloadQueueItem, queue_id)
        if q is not None:
            if failure:
                q.status = "import_failed"
                q.error_message = failure
                history(db, q.media_type, q.media_id, q.release_title, "import_failed", quality=q.quality, download_id=q.id, details=failure)
            else:
                q.status = "imported"
                q.error_message = "; ".join(result.failed) or None
    if failure:
        IMPORTS_TOTAL.labels(media_type, "failed").inc()
        bus.publish(EventType.IMPORT_FAILED, queue_id=queue_id, title=release_title, reason=failure, media_type=media_type, media_id=media_id)
    else:
        IMPORTS_TOTAL.labels(media_type, "imported").inc()
        bus.publish(EventType.IMPORT_COMPLETED, queue_id=queue_id, title=release_title, items=result.imported, media_type=media_type, media_id=media_id)
    bus.publish(EventType.QUEUE_CHANGED)
    if failure:
        result.failed = [failure]
    return result


def _run_import(src: Path, quality, media_type, media_id, season, episodes, mode, original, queue_id) -> str:
    tx = fileops.FileTransaction()
    try:
        with session_scope() as db:  # commit happens on exit; failure below rolls files back too
            label = _import_one(db, tx, src, quality, media_type, media_id, season, episodes, mode, original, queue_id)
        return label
    except TemplateError as exc:
        tx.rollback()
        raise ImportError_(f"Naming template error: {exc}") from exc
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


def manual_import(items: list[dict], mode: str | None, actor: Actor) -> ImportResult:
    result = ImportResult()
    mode = mode or get_settings().import_mode
    for item in items:
        src = Path(item["path"])
        check = fileops.check_video_file(src, 0)
        if not check.ok:
            result.failed.append(f"{src.name}: {check.reason}")
            result.failed_paths[item["path"]] = check.reason or "Invalid file"
            continue
        try:
            label = _run_import(src, parse_quality(item.get("quality") or src.name), item["media_type"], int(item["media_id"]),
                                item.get("season"), list(item.get("episodes") or []), mode, src.name, None)
            result.imported.append(label)
            IMPORTS_TOTAL.labels(item["media_type"], "imported").inc()
        except Exception as exc:
            result.failed.append(f"{src.name}: {exc}")
            result.failed_paths[item["path"]] = str(exc)
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
        if upper.startswith(("_UNPACK_", "_FAILED_", "_ADMIN_")):  # SABnzbd/NZBGet working folders
            report["corrupt" if upper.startswith("_FAILED_") else "partial"] += 1
            continue
        if entry.is_dir() and entry.name.lower() in ("movies", "tv", "series", "complete", "sonarr", "radarr", "maniac-media"):
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
            except Exception as exc:
                log.info("Untracked import of %s skipped: %s", video.name, exc)
    return report


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def failed_imports(db: Session) -> int:
    from sqlalchemy import func

    return db.scalar(select(func.count(models.DownloadQueueItem.id)).where(models.DownloadQueueItem.status == "import_failed")) or 0


__all__ = ["import_queue_item", "manual_scan", "manual_import", "annotate_match", "delete_candidates", "scan_download_folder", "failed_imports", "SYSTEM"]
