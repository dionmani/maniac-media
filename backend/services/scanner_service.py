"""Library scanner.

* **Full scan** visits every folder. **Incremental scan** skips folders whose modification
  time is unchanged since the last scan (stored in ``scan_state``) – the common case for large,
  mostly static libraries (100k+ files).
* All lookups are pre-loaded into dictionaries; the scan issues O(changed folders) queries,
  not O(files), and commits in batches.
* Detection: missing files, duplicate files, corrupt files and upgrade opportunities (quality
  below the profile cutoff) are recorded as ``library_issues``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, select, update

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import MediaFileRepository
from backend.db.session import session_scope
from backend.domain.parser import is_sample, is_video, normalize_title, parse_episode, parse_folder_title
from backend.domain.quality import parse_quality
from backend.services import fileops, media_file_service
from backend.services.quality_service import spec_for, to_spec

log = logging.getLogger("maniac.scanner")
Progress = Callable[[float, str], None]
SEASON_DIR = re.compile(r"^(?:season|series|s)[\s._-]*(\d{1,3})$|^(specials?)$", re.I)
MULTIPART = re.compile(r"(cd|disc|disk|part|pt)[\s._-]*\d", re.I)
BATCH = 200


def _hash(path: str) -> str:
    return hashlib.sha256(path.lower().encode()).hexdigest()


def _dir_entries(path: str):
    try:
        with os.scandir(fileops.long_path(path)) as it:
            return [e for e in it if not e.name.startswith(".")]
    except OSError:
        return []


class _ScanState:
    def __init__(self, db, media_type: str) -> None:
        self.db = db
        self.rows = {r.path_hash: r for r in db.scalars(select(models.ScanState).where(models.ScanState.media_type == media_type))}
        self.media_type = media_type

    def unchanged(self, path: str, mtime: float) -> bool:
        row = self.rows.get(_hash(path))
        return row is not None and abs(row.mtime - mtime) < 0.001

    def mark(self, path: str, mtime: float) -> None:
        h = _hash(path)
        row = self.rows.get(h)
        if row is None:
            row = models.ScanState(path_hash=h, path=path[:1024], media_type=self.media_type, mtime=mtime, last_scanned=utcnow())
            self.db.add(row)
            self.rows[h] = row
        else:
            row.mtime, row.last_scanned = mtime, utcnow()


def _issue(db, issue_type: str, media_type: str, media_id: int | None, path: str | None, details: str) -> None:
    db.add(models.LibraryIssue(issue_type=issue_type, media_type=media_type, media_id=media_id, path=path, details=details, resolved=False))


# --------------------------------------------------------------------------- movies
def scan_movies(incremental: bool = True, progress: Progress | None = None) -> dict:
    settings = get_settings()
    root = settings.media_root_movies
    stats = {"folders": 0, "scanned": 0, "skipped": 0, "new": 0, "updated": 0, "missing": 0, "duplicates": 0, "upgrades": 0, "corrupt": 0}
    if not root or not os.path.isdir(root):
        raise FileNotFoundError(f"Movie root folder not found: {root}")

    entries = [e for e in _dir_entries(root) if e.is_dir()]
    stats["folders"] = len(entries)
    with session_scope() as db:
        db.execute(update(models.LibraryIssue).where(models.LibraryIssue.media_type == "movie", models.LibraryIssue.resolved.is_(False))
                   .values(resolved=True))
        state = _ScanState(db, "movie")
        by_path = {}
        by_imdb = {}
        by_title: dict[tuple[str, int | None], models.Movie] = {}
        for m in db.scalars(select(models.Movie)):
            if m.path:
                by_path[os.path.normcase(os.path.normpath(m.path))] = m
            if m.imdb_id:
                by_imdb[m.imdb_id] = m
            by_title[(normalize_title(m.title), m.year)] = m
            by_title.setdefault((normalize_title(m.title), None), m)
        profiles = {p.id: to_spec(p) for p in db.scalars(select(models.QualityProfile))}

        for i, entry in enumerate(entries, 1):
            folder = entry.path
            try:
                mtime = os.stat(fileops.long_path(entry.path)).st_mtime  # DirEntry caches can lag on Windows
            except OSError:
                continue
            known = by_path.get(os.path.normcase(os.path.normpath(folder)))
            if incremental and known is not None and state.unchanged(folder, mtime):
                stats["skipped"] += 1
            else:
                stats["scanned"] += 1
                _scan_movie_folder(db, entry, root, known, by_imdb, by_title, profiles, stats)
                state.mark(folder, mtime)
            if i % BATCH == 0:
                db.commit()
                if progress:
                    progress(i * 90 / max(len(entries), 1), f"Movies: {i}/{len(entries)} folders")
        db.commit()

        # missing-file detection across the whole library (cheap stat per file)
        for m in db.scalars(select(models.Movie).where(models.Movie.has_file.is_(True))):
            if not m.file_path or not os.path.exists(fileops.long_path(m.file_path)):
                mf = MediaFileRepository(db).by_path(m.file_path) if m.file_path else None
                if mf:
                    media_file_service.detach_file(db, mf, reason="missing")
                else:
                    m.has_file = False
                _issue(db, "missing_file", "movie", m.id, m.file_path or "", "File referenced by the library no longer exists")
                stats["missing"] += 1
            elif m.quality_profile_id is not None:
                profile = profiles.get(m.quality_profile_id) or next(iter(profiles.values()), None)
                if profile and m.monitored and profile.upgrade_allowed and not profile.cutoff_met(parse_quality(m.quality or m.file_path).quality):
                    _issue(db, "upgrade_available", "movie", m.id, m.file_path, f"{m.quality or 'Unknown'} is below the profile cutoff {profile.cutoff}")
                    stats["upgrades"] += 1
    if progress:
        progress(100, "Movie scan complete")
    return stats


def _scan_movie_folder(db, entry, root, known, by_imdb, by_title, profiles, stats) -> None:
    files = [e for e in _dir_entries(entry.path) if e.is_file() and is_video(e.name) and not is_sample(e.name)]
    # one level deeper handles "Movie (2009)/Movie.2009.1080p.BluRay/movie.mkv" layouts
    if not files:
        for sub in (e for e in _dir_entries(entry.path) if e.is_dir()):
            files.extend(e for e in _dir_entries(sub.path) if e.is_file() and is_video(e.name) and not is_sample(e.name))
    title, year, imdb = parse_folder_title(entry.name)
    movie = known or (by_imdb.get(imdb) if imdb else None) or by_title.get((normalize_title(title), year))
    if movie is None and year is None:
        movie = by_title.get((normalize_title(title), None))

    main, extras = None, []
    if files:
        files.sort(key=lambda e: e.stat().st_size, reverse=True)
        main = files[0]
        extras = [f for f in files[1:] if not MULTIPART.search(f.name) and f.stat().st_size > main.stat().st_size * 0.4]
        if extras:
            stats["duplicates"] += 1
    if movie is None:
        movie = models.Movie(title=title, year=year or 0, status="released", monitored=True, quality_profile_id=1, root_folder=root,
                             path=entry.path, has_file=False, file_size_bytes=0, minimum_availability="released", imdb_id=imdb)
        db.add(movie)
        db.flush()
        by_title[(normalize_title(title), year)] = movie
        stats["new"] += 1
    else:
        stats["updated"] += 1
    movie.path = entry.path
    movie.root_folder = movie.root_folder or root
    if imdb and not movie.imdb_id:
        movie.imdb_id = imdb
    if extras:
        _issue(db, "duplicate_file", "movie", movie.id, entry.path, f"{len(extras) + 1} full-size video files: " + ", ".join(f.name for f in [main, *extras][:4]))
    if main is not None:
        check = fileops.check_video_file(Path(main.path))
        if check.state == "corrupt":
            _issue(db, "corrupt_file", "movie", movie.id, main.path, check.reason or "Corrupt")
            stats["corrupt"] += 1
        parsed = parse_quality(main.name)
        media_file_service.attach_movie_file(
            db, movie, Path(main.path), parsed, scene_name=Path(main.name).name, source="scan", make_primary=True
        )
        for extra in extras:
            extra_parsed = parse_quality(extra.name)
            media_file_service.attach_movie_file(
                db, movie, Path(extra.path), extra_parsed, scene_name=Path(extra.name).name, source="scan", make_primary=False
            )
    elif movie.has_file and movie.file_path and not os.path.exists(movie.file_path):
        mf = MediaFileRepository(db).by_path(movie.file_path)
        if mf:
            media_file_service.detach_file(db, mf, reason="missing")
        else:
            movie.has_file = False
        _issue(db, "missing_file", "movie", movie.id, movie.file_path, "File referenced by the library no longer exists")
        stats["missing"] += 1


# --------------------------------------------------------------------------- series
def scan_series(incremental: bool = True, progress: Progress | None = None) -> dict:
    settings = get_settings()
    root = settings.media_root_tv
    stats = {"folders": 0, "scanned": 0, "skipped": 0, "new_series": 0, "new_episodes": 0, "files": 0, "missing": 0, "duplicates": 0, "upgrades": 0}
    if not root or not os.path.isdir(root):
        raise FileNotFoundError(f"TV root folder not found: {root}")
    entries = [e for e in _dir_entries(root) if e.is_dir()]
    stats["folders"] = len(entries)
    with session_scope() as db:
        db.execute(update(models.LibraryIssue).where(models.LibraryIssue.media_type == "tv", models.LibraryIssue.resolved.is_(False)).values(resolved=True))
        state = _ScanState(db, "tv")
        by_path, by_title = {}, {}
        for s in db.scalars(select(models.Series)):
            if s.path:
                by_path[os.path.normcase(os.path.normpath(s.path))] = s
            by_title.setdefault(normalize_title(s.title), s)

        for i, entry in enumerate(entries, 1):
            season_dirs = [e for e in _dir_entries(entry.path) if e.is_dir() and SEASON_DIR.match(e.name)]
            try:
                mtime = max([os.stat(fileops.long_path(entry.path)).st_mtime] + [os.stat(fileops.long_path(d.path)).st_mtime for d in season_dirs])
            except OSError:
                continue
            known = by_path.get(os.path.normcase(os.path.normpath(entry.path)))
            if incremental and known is not None and state.unchanged(entry.path, mtime):
                stats["skipped"] += 1
            else:
                stats["scanned"] += 1
                _scan_series_folder(db, entry, season_dirs, root, known, by_title, stats)
                state.mark(entry.path, mtime)
            if i % 25 == 0:
                db.commit()
                if progress:
                    progress(i * 90 / max(len(entries), 1), f"TV: {i}/{len(entries)} series")
        db.commit()

        profiles = {}
        series_monitored = {s.id: (s.monitored, s.quality_profile_id) for s in db.scalars(select(models.Series))}
        for ep in db.scalars(select(models.Episode).where(models.Episode.has_file.is_(True))):
            if not ep.file_path or not os.path.exists(fileops.long_path(ep.file_path)):
                mf = MediaFileRepository(db).by_path(ep.file_path) if ep.file_path else None
                if mf:
                    media_file_service.detach_file(db, mf, reason="missing")
                else:
                    ep.has_file, ep.status = False, "missing"
                _issue(db, "missing_file", "tv", ep.series_id, ep.file_path or "", f"S{ep.season_number:02d}E{ep.episode_number:02d} file no longer exists")
                stats["missing"] += 1
                continue
            monitored, profile_id = series_monitored.get(ep.series_id, (False, 1))
            if monitored and ep.monitored:
                profile = profiles.get(profile_id) or profiles.setdefault(profile_id, spec_for(db, profile_id))
                if profile.upgrade_allowed and ep.quality and ep.quality != "Detected (File)" and not profile.cutoff_met(parse_quality(ep.quality).quality):
                    stats["upgrades"] += 1
    if progress:
        progress(100, "TV scan complete")
    return stats


def _scan_series_folder(db, entry, season_dirs, root, known, by_title, stats) -> None:
    title, year, imdb = parse_folder_title(entry.name)
    series = known or by_title.get(normalize_title(title))
    if series is None:
        series = models.Series(title=title, year=year or 0, status="continuing", monitored=True, quality_profile_id=1, root_folder=root,
                               path=entry.path, total_seasons=1, imdb_id=imdb, series_type="standard", monitor_new_seasons=True)
        db.add(series)
        db.flush()
        by_title[normalize_title(title)] = series
        stats["new_series"] += 1
    series.path = entry.path

    seasons = {s.season_number: s for s in db.scalars(select(models.Season).where(models.Season.series_id == series.id))}
    episodes = {(e.season_number, e.episode_number): e for e in db.scalars(select(models.Episode).where(models.Episode.series_id == series.id))}
    seen: dict[tuple[int, int], str] = {}

    sources = [(None, entry.path)] + [
        (0 if (m := SEASON_DIR.match(d.name)).group(2) else int(m.group(1)), d.path) for d in season_dirs
    ]
    for folder_season, folder in sources:
        for f in _dir_entries(folder):
            if not f.is_file() or not is_video(f.name) or is_sample(f.name):
                continue
            parsed = parse_episode(f.name)
            if not parsed or not parsed.episodes:
                continue
            season_no = parsed.season if parsed.season is not None else folder_season
            if season_no is None:
                continue
            stats["files"] += 1
            quality = parse_quality(f.name)
            file_episodes = []
            for number in parsed.episodes:
                key = (season_no, number)
                if key in seen and seen[key] != f.path:
                    stats["duplicates"] += 1
                    _issue(db, "duplicate_file", "tv", series.id, f.path, f"S{season_no:02d}E{number:02d} also in {Path(seen[key]).name}")
                    continue
                seen[key] = f.path
                season = seasons.get(season_no)
                if season is None:
                    season = models.Season(series_id=series.id, season_number=season_no, monitored=season_no > 0)
                    db.add(season)
                    db.flush()
                    seasons[season_no] = season
                ep = episodes.get(key)
                if ep is None:
                    title_match = re.search(r"[Ss]\d{1,2}[Ee]\d{1,3}(?:-?E\d{1,3})*\s*-\s*([^\[]+?)(?:\s*\[|$)", Path(f.name).stem)
                    ep = models.Episode(series_id=series.id, season_id=season.id, season_number=season_no, episode_number=number,
                                        title=title_match.group(1).strip() if title_match else f"Episode {number}", monitored=season.monitored)
                    db.add(ep)
                    episodes[key] = ep
                    stats["new_episodes"] += 1
                file_episodes.append(ep)
            if file_episodes:
                media_file_service.attach_episode_file(
                    db, series, file_episodes, Path(f.path), quality, scene_name=Path(f.name).name, source="scan", make_primary=True
                )
    real = [n for n in seasons if n > 0]
    series.total_seasons = max(real) if real else (series.total_seasons or 1)


# --------------------------------------------------------------------------- entry points
def scan(scope: str = "all", incremental: bool = True, progress: Progress | None = None) -> dict:
    result: dict = {}
    if scope in ("movies", "movie", "all"):
        result["movies"] = scan_movies(incremental, (lambda p, m: progress(p / 2 if scope == "all" else p, m)) if progress else None)
    if scope in ("tv", "all"):
        result["tv"] = scan_series(incremental, (lambda p, m: progress(50 + p / 2 if scope == "all" else p, m)) if progress else None)
    bus.publish(EventType.SCAN_COMPLETED, scope=scope, result=result)
    return result


def reset_scan_state(media_type: str | None = None) -> None:
    with session_scope() as db:
        stmt = delete(models.ScanState)
        if media_type:
            stmt = stmt.where(models.ScanState.media_type == media_type)
        db.execute(stmt)
