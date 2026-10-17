"""Media management use-cases: add / edit / monitor / delete movies and series."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import delete as sa_delete, or_, select, update
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, MovieRepository, SeriesRepository
from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, render
from backend.domain.parser import is_sample, is_video
from backend.domain.quality import parse_quality
from backend.domain.types import MediaMetadata
from backend.services import fileops, metadata_service
from backend.services.audit import Actor, audit, history

log = logging.getLogger("maniac.media")


class MediaError(ValueError):
    pass


class ConflictError(MediaError):
    pass


def _movie_folder(movie: models.Movie, root: str) -> str:
    rel = render(get_settings().movie_folder_format, {"title": movie.title, "year": movie.year, "imdbid": movie.imdb_id, "tmdbid": movie.tmdb_id}, MOVIE_TOKENS)
    return str(Path(root) / Path(rel))


def _series_folder(series: models.Series, root: str) -> str:
    rel = render(get_settings().series_folder_format, {"title": series.title, "year": series.year, "tvdbid": series.tvdb_id, "imdbid": series.imdb_id}, EPISODE_TOKENS)
    return str(Path(root) / Path(rel))


# --------------------------------------------------------------------------- movies
def add_movie(db: Session, meta: MediaMetadata, *, quality_profile_id: int, root_folder: str | None, monitored: bool, actor: Actor) -> models.Movie:
    repo = MovieRepository(db)
    if meta.tmdb_id and repo.by_tmdb(meta.tmdb_id):
        raise ConflictError("Movie already in library")
    if meta.imdb_id and repo.by_imdb(meta.imdb_id):
        raise ConflictError("Movie already in library")
    root = root_folder or get_settings().media_root_movies
    movie = models.Movie(
        title=meta.title,
        year=meta.year or 0,
        monitored=monitored,
        quality_profile_id=quality_profile_id,
        root_folder=root,
        has_file=False,
        file_size_bytes=0,
        minimum_availability="released",
    )
    metadata_service.apply_movie(db, movie, meta)
    movie.path = _movie_folder(movie, root)
    repo.add(movie)
    history(db, "movie", movie.id, movie.title, "added", details=f"Added '{movie.title} ({movie.year})' to the library")
    audit(db, "media", "movie.add", actor=actor, entity_type="movie", entity_id=movie.id, details={"title": movie.title, "year": movie.year})
    return movie


def update_movie(db: Session, movie: models.Movie, changes: dict, actor: Actor) -> models.Movie:
    allowed = {"monitored", "quality_profile_id", "root_folder", "path", "minimum_availability", "title", "year"}
    applied = {k: v for k, v in changes.items() if k in allowed and v is not None}
    for key, value in applied.items():
        setattr(movie, key, value)
    if "title" in applied:
        movie.sort_title = metadata_service._sort_title(movie.title)
    audit(db, "media", "movie.update", actor=actor, entity_type="movie", entity_id=movie.id, details=applied)
    return movie


def delete_movie(db: Session, movie: models.Movie, *, delete_files: bool, actor: Actor) -> None:
    recycled = None
    if delete_files:
        # every tracked file (primary + extras/duplicates), not just the legacy mirrored path
        paths = [f.path for f in movie.files] or ([movie.file_path] if movie.file_path else [])
        for path in paths:
            if Path(path).exists():
                target = str(fileops.recycle(Path(path), Path(get_settings().recycle_bin)))
                recycled = recycled or target
    history(db, "movie", movie.id, movie.title, "deleted", details=f"Removed from library{'; file moved to recycle bin' if recycled else ''}")
    audit(db, "delete", "movie.delete", actor=actor, entity_type="movie", entity_id=movie.id,
          details={"title": movie.title, "delete_files": delete_files, "recycled_to": recycled}, severity="warning")
    db.delete(movie)


# --------------------------------------------------------------------------- series
def add_series(
    db: Session, meta: MediaMetadata, *, quality_profile_id: int, root_folder: str | None, monitored: bool,
    monitor: str, actor: Actor,
) -> models.Series:
    """``monitor``: all | future | missing | none | latest | first."""
    repo = SeriesRepository(db)
    if meta.tvdb_id and repo.by_tvdb(meta.tvdb_id):
        raise ConflictError("Series already in library")
    if meta.tvmaze_id and repo.by_tvmaze(meta.tvmaze_id):
        raise ConflictError("Series already in library")
    if meta.tmdb_id and repo.by_tmdb(meta.tmdb_id):
        raise ConflictError("Series already in library")
    root = root_folder or get_settings().media_root_tv
    series = models.Series(
        title=meta.title,
        year=meta.year or 0,
        monitored=monitored,
        quality_profile_id=quality_profile_id,
        root_folder=root,
        total_seasons=meta.season_count or 1,
        series_type="standard",
        monitor_new_seasons=monitor != "none",
    )
    repo.add(series)
    stats = metadata_service.apply_series(db, series, meta)
    series.path = _series_folder(series, root)
    if not meta.episodes:
        for n in range(1, (meta.season_count or 1) + 1):
            db.add(models.Season(series_id=series.id, season_number=n, monitored=True))
    db.flush()
    apply_monitoring_preset(db, series, monitor)
    history(db, "tv", series.id, series.title, "added", details=f"Added series with {stats['added']} episodes")
    audit(db, "media", "series.add", actor=actor, entity_type="series", entity_id=series.id, details={"title": series.title, "monitor": monitor})
    return series


def apply_monitoring_preset(db: Session, series: models.Series, preset: str) -> None:
    from datetime import date

    today = date.today().isoformat()
    seasons = list(db.scalars(select(models.Season).where(models.Season.series_id == series.id)))
    episodes = list(db.scalars(select(models.Episode).where(models.Episode.series_id == series.id)))
    real_seasons = sorted(s.season_number for s in seasons if s.season_number > 0)
    for s in seasons:
        s.monitored = s.season_number > 0 and preset != "none"
        if preset == "latest":
            s.monitored = bool(real_seasons) and s.season_number == real_seasons[-1]
        if preset == "first":
            s.monitored = bool(real_seasons) and s.season_number == real_seasons[0]
    season_monitored = {s.season_number: s.monitored for s in seasons}
    for e in episodes:
        monitored = season_monitored.get(e.season_number, False)
        if preset == "future":
            monitored = monitored and (e.air_date is None or e.air_date > today)
        elif preset == "missing":
            monitored = monitored and not e.has_file
        e.monitored = monitored


def update_series(db: Session, series: models.Series, changes: dict, actor: Actor) -> models.Series:
    allowed = {"monitored", "quality_profile_id", "root_folder", "path", "series_type", "monitor_new_seasons", "title", "year"}
    applied = {k: v for k, v in changes.items() if k in allowed and v is not None}
    for key, value in applied.items():
        setattr(series, key, value)
    audit(db, "media", "series.update", actor=actor, entity_type="series", entity_id=series.id, details=applied)
    return series


def set_season_monitored(db: Session, series_id: int, season_number: int, monitored: bool, actor: Actor) -> None:
    season = db.scalar(select(models.Season).where(models.Season.series_id == series_id, models.Season.season_number == season_number))
    if season is None:
        raise MediaError("Season not found")
    season.monitored = monitored
    db.execute(
        update(models.Episode)
        .where(models.Episode.series_id == series_id, models.Episode.season_number == season_number)
        .values(monitored=monitored)
    )
    audit(db, "media", "season.monitor", actor=actor, entity_type="season", entity_id=season.id, details={"monitored": monitored})


def set_episodes_monitored(db: Session, episode_ids: list[int], monitored: bool, actor: Actor) -> int:
    result = db.execute(update(models.Episode).where(models.Episode.id.in_(episode_ids)).values(monitored=monitored))
    audit(db, "media", "episode.monitor", actor=actor, entity_type="episode", details={"ids": episode_ids[:50], "monitored": monitored})
    return result.rowcount or 0


def add_season(db: Session, series: models.Series, season_number: int, episode_count: int, actor: Actor) -> models.Season:
    if season_number < 0:
        raise MediaError("Season number must be >= 0")
    existing = db.scalar(select(models.Season).where(models.Season.series_id == series.id, models.Season.season_number == season_number))
    if existing:
        raise ConflictError(f"Season {season_number} already exists")
    season = models.Season(series_id=series.id, season_number=season_number, monitored=True)
    db.add(season)
    db.flush()
    for n in range(1, max(0, episode_count) + 1):
        db.add(models.Episode(series_id=series.id, season_id=season.id, season_number=season_number, episode_number=n,
                              title=f"Episode {n}", monitored=True, has_file=False, status="missing"))
    series.total_seasons = max(series.total_seasons or 0, season_number)
    audit(db, "media", "season.add", actor=actor, entity_type="series", entity_id=series.id, details={"season": season_number, "episodes": episode_count})
    return season


def add_episode(db: Session, series: models.Series, season_number: int, episode_number: int, title: str, air_date: str | None, actor: Actor) -> models.Episode:
    if EpisodeRepository(db).find(series.id, season_number, episode_number):
        raise ConflictError("Episode already exists")
    season = db.scalar(select(models.Season).where(models.Season.series_id == series.id, models.Season.season_number == season_number))
    if season is None:
        season = add_season(db, series, season_number, 0, actor)
    ep = models.Episode(series_id=series.id, season_id=season.id, season_number=season_number, episode_number=episode_number,
                        title=title or f"Episode {episode_number}", air_date=air_date, monitored=True, has_file=False, status="missing")
    db.add(ep)
    db.flush()
    audit(db, "media", "episode.add", actor=actor, entity_type="episode", entity_id=ep.id, details={"series": series.title, "s": season_number, "e": episode_number})
    return ep


def prune_stale_seasons(db: Session, series: models.Series, meta: MediaMetadata, *, apply: bool, actor: Actor) -> dict:
    """Preview (apply=False) or remove file-less episodes/seasons the current metadata no longer contains."""
    episodes, seasons, kept = metadata_service.stale_rows(db, series, meta)
    per_season: dict[int, int] = {}
    for ep in episodes:
        per_season[ep.season_number] = per_season.get(ep.season_number, 0) + 1
    removed_whole = {s.season_number for s in seasons}
    result = {
        "provider": meta.provider,
        "episodes": len(episodes),
        "kept_with_files": kept,
        "seasons": [
            {"season_number": n, "episodes": per_season.get(n, 0), "removed": n in removed_whole}
            for n in sorted(set(per_season) | removed_whole)
        ],
    }
    if not apply or not (episodes or seasons):
        return result
    for ep in episodes:
        db.delete(ep)
    db.flush()
    for season in seasons:
        db.delete(season)
    db.flush()
    real = db.scalars(select(models.Season.season_number).where(models.Season.series_id == series.id, models.Season.season_number > 0)).all()
    series.total_seasons = max(real) if real else (series.total_seasons or 1)
    history(db, "tv", series.id, series.title, "updated",
            details=f"Removed {len(episodes)} stale episode(s) and {len(seasons)} stale season(s) not in {meta.provider} metadata")
    audit(db, "media", "series.prune_stale", actor=actor, entity_type="series", entity_id=series.id,
          details={"episodes": len(episodes), "seasons": sorted(removed_whole), "provider": meta.provider}, severity="warning")
    return result


def remove_episodes(db: Session, series: models.Series, episodes: list[models.Episode], *, delete_files: bool,
                    apply: bool, actor: Actor, drop_season: int | None = None) -> dict:
    """Preview (apply=False) or remove episodes from the library, optionally recycling their files.

    A file also linked to episodes outside this removal (multi-episode file) is never recycled.
    ``drop_season`` additionally deletes that (now empty) season row.
    """
    ids = {e.id for e in episodes}
    files: dict[int, models.MediaFile] = {}
    for ep in episodes:
        for mf in ep.files:
            files[mf.id] = mf
    infos = []
    for mf in files.values():
        shared = any(e.id not in ids for e in mf.episodes)
        infos.append((mf, shared, Path(mf.path).exists()))
    result = {
        "episodes": len(episodes),
        "files": [{"path": mf.path, "exists": exists, "shared": shared} for mf, shared, exists in infos],
        "files_recycled": 0,
        "applied": False,
    }
    if not apply:
        return result
    from backend.services import media_file_service

    recycle_root = Path(get_settings().recycle_bin)
    recycled = 0
    doomed = []
    for mf, shared, exists in infos:
        if shared:
            continue
        if delete_files and exists:
            fileops.recycle(Path(mf.path), recycle_root)
            recycled += 1
        media_file_service.retire_file_issues(db, mf)
        doomed.append(mf.id)
    # drop the link rows in one statement so the ORM doesn't also try to delete them via both relationships
    db.execute(sa_delete(models.EpisodeFile).where(models.EpisodeFile.episode_id.in_(ids)))
    if doomed:
        db.execute(sa_delete(models.MediaFile).where(models.MediaFile.id.in_(doomed)))
    db.expire_all()
    for ep in episodes:
        db.delete(ep)
    db.flush()
    if drop_season is not None:
        season = db.scalar(select(models.Season).where(models.Season.series_id == series.id, models.Season.season_number == drop_season))
        if season is not None:
            db.refresh(season)
            db.delete(season)
            db.flush()
    real = db.scalars(select(models.Season.season_number).where(models.Season.series_id == series.id, models.Season.season_number > 0)).all()
    series.total_seasons = max(real) if real else (series.total_seasons or 1)
    what = f"season {drop_season}" if drop_season is not None else "episode"
    history(db, "tv", series.id, series.title, "deleted", details=f"Removed {len(episodes)} {what} episode(s); {recycled} file(s) recycled")
    audit(db, "delete", "series.remove_episodes", actor=actor, entity_type="series", entity_id=series.id,
          details={"episodes": len(episodes), "season": drop_season, "delete_files": delete_files, "recycled": recycled}, severity="warning")
    result["files_recycled"] = recycled
    result["applied"] = True
    return result


def delete_series(db: Session, series: models.Series, *, delete_files: bool, actor: Actor) -> None:
    recycled = 0
    if delete_files:
        recycle_root = Path(get_settings().recycle_bin)
        paths = {f.path for f in series.files}
        paths.update(
            ep.file_path for ep in db.scalars(select(models.Episode).where(models.Episode.series_id == series.id, models.Episode.has_file.is_(True)))
            if ep.file_path
        )
        for path in sorted(paths):
            if Path(path).exists():
                fileops.recycle(Path(path), recycle_root)
                recycled += 1
    history(db, "tv", series.id, series.title, "deleted", details=f"Removed from library; {recycled} file(s) recycled")
    audit(db, "delete", "series.delete", actor=actor, entity_type="series", entity_id=series.id,
          details={"title": series.title, "delete_files": delete_files, "recycled": recycled}, severity="warning")
    db.delete(series)


# --------------------------------------------------------------------------- linking (fix match)
def link_clash(db: Session, item: models.Movie | models.Series, meta: MediaMetadata) -> models.Movie | models.Series | None:
    """Another library entry that already owns one of ``meta``'s ids (linking would make a duplicate)."""
    model = type(item)
    if isinstance(item, models.Movie):
        conds = [model.tmdb_id == meta.tmdb_id if meta.tmdb_id else None, model.imdb_id == meta.imdb_id if meta.imdb_id else None]
    else:
        conds = [
            model.tvdb_id == meta.tvdb_id if meta.tvdb_id else None,
            model.tvmaze_id == meta.tvmaze_id if meta.tvmaze_id else None,
            model.imdb_id == meta.imdb_id if meta.imdb_id else None,
        ]
    conds = [c for c in conds if c is not None]
    if not conds:
        return None
    return db.scalar(select(model).where(or_(*conds), model.id != item.id).limit(1))


def link_movie(db: Session, movie: models.Movie, meta: MediaMetadata, actor: Actor) -> models.Movie:
    """Re-point a (usually mis-parsed, scanner-created) movie at the title the user identified; files stay put."""
    if link_clash(db, movie, meta):
        raise ConflictError("Another movie in the library already has this id")
    old = f"{movie.title} ({movie.year})"
    # the scanner's guesses are what got this wrong – the user's pick replaces them, not merges with them
    movie.tmdb_id = None
    movie.imdb_id = None
    metadata_service.apply_movie(db, movie, meta, overwrite_title=True)
    movie.tmdb_id = meta.tmdb_id
    history(db, "movie", movie.id, movie.title, "updated", details=f"Linked '{old}' to {movie.title} ({movie.year}) via {meta.provider}")
    audit(db, "media", "movie.link", actor=actor, entity_type="movie", entity_id=movie.id,
          details={"from": old, "title": movie.title, "year": movie.year, "imdb_id": movie.imdb_id, "tmdb_id": movie.tmdb_id})
    return movie


def link_series(db: Session, series: models.Series, meta: MediaMetadata, actor: Actor) -> models.Series:
    """Series counterpart of :func:`link_movie`; episodes on disk are matched to the new episode list by number."""
    if link_clash(db, series, meta):
        raise ConflictError("Another series in the library already has this id")
    old = f"{series.title} ({series.year})"
    series.tvdb_id = series.tvmaze_id = series.tmdb_id = None
    series.imdb_id = None
    if meta.title:
        series.title = meta.title
    stats = metadata_service.apply_series(db, series, meta)
    # file-less episodes left over from a previous (wrong) match don't belong to this show
    prune_stale_seasons(db, series, meta, apply=True, actor=actor)
    history(db, "tv", series.id, series.title, "updated",
            details=f"Linked '{old}' to {series.title} ({series.year}) via {meta.provider}; {stats['added']} episode(s) added")
    audit(db, "media", "series.link", actor=actor, entity_type="series", entity_id=series.id,
          details={"from": old, "title": series.title, "year": series.year, "tvdb_id": series.tvdb_id, "imdb_id": series.imdb_id})
    return series


# --------------------------------------------------------------------------- merge (link onto an existing entry)
_MAX_FOLDER_ENTRIES = 2000


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _inside(child: str | Path, parent: str | Path) -> bool:
    return _path_key(child).startswith(_path_key(parent).rstrip(os.sep) + os.sep)


def _merge_cleanup_folder(source: models.Movie, target: models.Movie) -> Path | None:
    """The duplicate's folder, if it's safe to empty after the merge: a real directory strictly inside the
    movie root, and neither the target's folder nor one containing it. Left on disk, the scanner would
    re-create the duplicate from it on the next scan."""
    root = source.root_folder or get_settings().media_root_movies
    if not source.path or not root or not os.path.isdir(fileops.long_path(source.path)):
        return None
    folder = Path(source.path)
    if not _inside(folder, root):
        return None
    if target.path and (_path_key(folder) == _path_key(target.path) or _inside(target.path, folder)):
        return None
    return folder


def _loose_videos(folder: Path | None, known: set[str]) -> list[Path]:
    """Video files in the duplicate's folder the library doesn't track yet (e.g. nested too deep for the scanner)."""
    if folder is None:
        return []
    found, seen = [], 0
    for dirpath, _dirs, names in os.walk(fileops.long_path(folder)):
        for name in names:
            seen += 1
            if seen > _MAX_FOLDER_ENTRIES:
                return found
            path = Path(dirpath) / name
            if is_video(name) and not is_sample(name) and _path_key(path) not in known:
                found.append(path)
    return found


def merge_candidates(db: Session, source: models.Movie, target: models.Movie) -> dict:
    """Preview of merging ``source`` (the duplicate) into ``target``: every video file either one has, which of
    them we'd suggest keeping, and the folder that gets emptied into the recycle bin."""
    files = []
    for owner, movie in (("existing", target), ("this", source)):
        for mf in movie.files:
            exists = os.path.exists(fileops.long_path(mf.path))
            files.append({"path": mf.path, "size_bytes": mf.size_bytes or 0, "quality": mf.quality, "owner": owner, "exists": exists})
    folder = _merge_cleanup_folder(source, target)
    for path in _loose_videos(folder, {_path_key(f["path"]) for f in files}):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files.append({"path": str(path), "size_bytes": size, "quality": parse_quality(path.name).quality.name, "owner": "this", "exists": True})
    present = [f for f in files if f["exists"]]
    if present:
        best = max(present, key=lambda f: (parse_quality(f["quality"] or Path(f["path"]).name).quality.weight, f["size_bytes"]))
        for f in files:
            f["recommended"] = f is best
    return {"files": files, "cleanup_folder": str(folder) if folder else None}


def _recycle_sidecars(tx: fileops.FileTransaction, video: Path, recycle_root: Path) -> None:
    extras = {e.lower() for e in get_settings().extra_file_extensions}
    try:
        for sib in video.parent.iterdir():
            if sib.is_file() and sib.suffix.lower() in extras and sib.name.lower().startswith(video.stem.lower()):
                tx.recycle(sib, recycle_root)
    except OSError as exc:
        log.info("Sidecar cleanup skipped: %s", exc)


def _move_sidecars(tx: fileops.FileTransaction, old: Path, new: Path) -> None:
    extras = {e.lower() for e in get_settings().extra_file_extensions}
    try:
        for sib in old.parent.iterdir():
            if sib.is_file() and sib.suffix.lower() in extras and sib.name.lower().startswith(old.stem.lower()):
                dest = new.with_name(new.stem + sib.name[len(old.stem):])
                if not dest.exists():
                    tx.place(sib, dest, "move")
    except OSError as exc:
        log.info("Sidecar move skipped: %s", exc)


def merge_movie(db: Session, source: models.Movie, target: models.Movie, keep_path: str | None, actor: Actor) -> dict:
    """Fold the duplicate ``source`` into ``target``: keep one video file (moved into the target's folder),
    send every other file plus the duplicate's leftover folder to the recycle bin, carry history / queue /
    blocklist rows over, and delete the duplicate entry. Filesystem changes roll back if anything fails."""
    from backend.services import media_file_service
    from backend.services.issue_service import OPEN, RESOLVED

    if source.id == target.id:
        raise MediaError("A movie can't be merged into itself")
    plan = merge_candidates(db, source, target)
    present = [f for f in plan["files"] if f["exists"]]
    if keep_path:
        keep = next((f for f in present if _path_key(f["path"]) == _path_key(keep_path)), None)
        if keep is None:
            raise MediaError("The file to keep isn't one of the files being merged")
    elif len(present) > 1:
        raise MediaError("Both entries have files – pick the one to keep")
    else:
        keep = present[0] if present else None

    settings = get_settings()
    recycle_root = Path(settings.recycle_bin)
    tracked = {_path_key(mf.path): mf for mf in [*target.files, *source.files]}
    keep_key = _path_key(keep["path"]) if keep else None
    source_label = f"{source.title} ({source.year})" if source.year else source.title
    tx = fileops.FileTransaction()
    recycled = 0
    try:
        for f in present:
            if f is not keep:
                tx.recycle(Path(f["path"]), recycle_root)
                _recycle_sidecars(tx, Path(f["path"]), recycle_root)
                recycled += 1
        for key, mf in tracked.items():
            if key != keep_key:
                media_file_service.detach_file(db, mf, reason="merged")

        kept = None
        if keep is not None:
            src = Path(keep["path"])
            mf = tracked.get(keep_key)
            if keep["owner"] == "existing":
                kept = src
            else:
                folder = Path(target.path or _movie_folder(target, target.root_folder or settings.media_root_movies))
                kept = folder / src.name
                if _path_key(kept) != _path_key(src):
                    if kept.exists():
                        raise MediaError(f"{kept} already exists")
                    tx.place(src, kept, "move")
                    _move_sidecars(tx, src, kept)
            if mf is not None:
                mf.movie = target
                db.flush()
                media_file_service.move_file(db, mf, kept)
                media_file_service.set_primary(db, mf)
            else:
                media_file_service.attach_movie_file(db, target, kept, parse_quality(src.name), scene_name=src.name,
                                                     source="manual", make_primary=True)

        # whatever is left in the duplicate's folder (art, NFOs, junk) – or the scanner brings the duplicate back
        cleanup = Path(plan["cleanup_folder"]) if plan["cleanup_folder"] else None
        if cleanup is not None and os.path.isdir(fileops.long_path(cleanup)):
            for dirpath, _dirs, names in os.walk(fileops.long_path(cleanup), topdown=False):
                for name in names:
                    tx.recycle(Path(dirpath) / name, recycle_root)
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

        for model in (models.History, models.Blocklist, models.DownloadQueueItem):
            db.execute(update(model).where(model.media_type == "movie", model.media_id == source.id).values(media_id=target.id))
        db.execute(update(models.LibraryIssue)
                   .where(models.LibraryIssue.media_type == "movie", models.LibraryIssue.media_id == source.id,
                          models.LibraryIssue.status == OPEN)
                   .values(status=RESOLVED, resolved=True, resolved_at=utcnow(), resolution="merged"))
        history(db, "movie", target.id, target.title, "updated",
                details=f"Merged '{source_label}' into this movie; kept {kept.name if kept else 'no file'}, {recycled} file(s) recycled")
        audit(db, "media", "movie.merge", actor=actor, entity_type="movie", entity_id=target.id,
              details={"from_id": source.id, "from": source_label, "kept": str(kept) if kept else None, "recycled": recycled,
                       "folder_removed": str(cleanup) if cleanup else None})
        db.delete(source)
        db.flush()
        media_file_service.sync_legacy(db, target)
    except Exception:
        tx.rollback()
        raise
    return {"kept": str(kept) if kept else None, "recycled": recycled}


def publish_change(kind: str, media_type: str, media_id: int, **extra) -> None:
    bus.publish(kind, media_type=media_type, media_id=media_id, **extra)


__all__ = [
    "MediaError", "ConflictError", "add_movie", "update_movie", "delete_movie", "add_series", "update_series",
    "set_season_monitored", "set_episodes_monitored", "add_season", "add_episode", "prune_stale_seasons", "remove_episodes", "delete_series",
    "link_clash", "link_movie", "link_series", "merge_candidates", "merge_movie",
    "apply_monitoring_preset", "publish_change", "EventType",
]
