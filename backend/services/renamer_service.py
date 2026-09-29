"""Template-driven bulk renaming with preview and dry-run.

Two granularities:
* **file** (default) – rename video files inside their current folder/season folder.
* **folders** – additionally rename the movie/series folder itself (one atomic directory
  rename, so artwork, NFOs and extras travel with it).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import select

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db import models
from backend.db.session import session_scope
from backend.domain.naming import EPISODE_TOKENS, MOVIE_TOKENS, TemplateError, render
from backend.domain.parser import parse_episode, parse_movie
from backend.domain.quality import get_quality, parse_quality
from backend.services import fileops
from backend.services.audit import Actor, audit, history

log = logging.getLogger("maniac.renamer")


def _quality_of(stored: str | None, path: Path):
    parsed = parse_quality(path.name)
    if stored and stored != "Detected (File)":
        q = get_quality(stored)
        if q.weight:
            return parsed.__class__(q, parsed.codec, parsed.proper, parsed.repack, parsed.hdr)
    return parsed


def _preview_movie(m: models.Movie, include_folders: bool) -> dict | None:
    settings = get_settings()
    current = Path(m.file_path)
    if not current.exists():
        return None
    q = _quality_of(m.quality, current)
    values = {
        "title": m.title, "year": m.year, "quality": q.quality.name if q.quality.weight else "",
        "resolution": q.quality.resolution.value if q.quality.weight else "", "source": q.quality.source.value if q.quality.weight else "",
        "codec": q.codec.value if q.codec.value != "unknown" else "", "imdbid": m.imdb_id, "tmdbid": m.tmdb_id,
        "releasegroup": parse_movie(current.name).release_group, "originalname": current.stem,
    }
    folder = current.parent
    if include_folders:
        root = Path(m.root_folder) if m.root_folder else folder.parent
        folder = root / render(settings.movie_folder_format, values, MOVIE_TOKENS)
    proposed = folder / (render(settings.movie_format, values, MOVIE_TOKENS) + current.suffix.lower())
    if proposed == current:
        return None
    return {
        "id": f"movie-{m.id}", "media_type": "movie", "media_id": m.id, "title": f"{m.title} ({m.year})",
        "current_path": str(current), "proposed_path": str(proposed), "quality": values["quality"] or "Unknown",
        "folder_change": folder != current.parent, "current_folder": str(current.parent), "proposed_folder": str(folder),
    }


def _preview_episode(ep: models.Episode, series: models.Series, include_folders: bool, siblings: dict[str, list[models.Episode]]) -> dict | None:
    settings = get_settings()
    current = Path(ep.file_path)
    group = sorted(siblings.get(ep.file_path, [ep]), key=lambda e: e.episode_number)
    if group[0].id != ep.id or not current.exists():
        return None  # multi-episode files are previewed once, via their first episode
    q = _quality_of(ep.quality, current)
    series_dir = current.parent.parent if current.parent.name.lower().startswith(("season", "specials", "s0", "s1")) else current.parent
    if include_folders:
        root = Path(series.root_folder) if series.root_folder else series_dir.parent
        series_dir = root / render(settings.series_folder_format, {"title": series.title, "year": series.year, "tvdbid": series.tvdb_id}, EPISODE_TOKENS)
    title = " + ".join(e.title for e in group) if len(group) > 1 else ep.title
    values = {
        "title": series.title, "year": series.year, "season": ep.season_number, "episode": ep.episode_number, "episodetitle": title,
        "airdate": ep.air_date, "quality": q.quality.name if q.quality.weight else "", "resolution": q.quality.resolution.value if q.quality.weight else "",
        "source": q.quality.source.value if q.quality.weight else "", "codec": q.codec.value if q.codec.value != "unknown" else "",
        "tvdbid": series.tvdb_id, "imdbid": series.imdb_id, "releasegroup": (parse_episode(current.name) or parse_movie(current.name)).release_group,
        "originalname": current.stem,
    }
    rel = render(settings.tv_format, values, EPISODE_TOKENS)
    if len(group) > 1:
        rel = rel.replace(f"E{ep.episode_number:02d}", f"E{ep.episode_number:02d}-E{group[-1].episode_number:02d}", 1)
    proposed = series_dir / (rel + current.suffix.lower())
    if proposed == current:
        return None
    label = f"{series.title} S{ep.season_number:02d}E{ep.episode_number:02d}" + (f"-E{group[-1].episode_number:02d}" if len(group) > 1 else "")
    return {
        "id": f"episode-{ep.id}", "media_type": "tv", "media_id": ep.id, "series_id": series.id, "title": label,
        "current_path": str(current), "proposed_path": str(proposed), "quality": values["quality"] or "Unknown",
        "folder_change": False, "episode_ids": [e.id for e in group],
    }


def preview(media_type: str = "all", *, include_folders: bool = False, media_id: int | None = None, limit: int = 5000) -> dict:
    items, errors = [], []
    with session_scope() as db:
        if media_type in ("movie", "all"):
            stmt = select(models.Movie).where(models.Movie.has_file.is_(True), models.Movie.file_path.is_not(None))
            if media_id and media_type == "movie":
                stmt = stmt.where(models.Movie.id == media_id)
            for m in db.scalars(stmt):
                try:
                    p = _preview_movie(m, include_folders)
                    if p:
                        items.append(p)
                except TemplateError as exc:
                    errors.append(str(exc))
                    break
                if len(items) >= limit:
                    break
        if media_type in ("tv", "all") and len(items) < limit:
            series_q = select(models.Series)
            if media_id and media_type == "tv":
                series_q = series_q.where(models.Series.id == media_id)
            for series in db.scalars(series_q):
                eps = list(db.scalars(select(models.Episode).where(models.Episode.series_id == series.id, models.Episode.has_file.is_(True), models.Episode.file_path.is_not(None))))
                siblings: dict[str, list[models.Episode]] = {}
                for e in eps:
                    siblings.setdefault(e.file_path, []).append(e)
                for e in eps:
                    try:
                        p = _preview_episode(e, series, include_folders, siblings)
                        if p:
                            items.append(p)
                    except TemplateError as exc:
                        errors.append(str(exc))
                        break
                if len(items) >= limit:
                    break
    return {"items": items, "errors": sorted(set(errors)), "truncated": len(items) >= limit}


def execute(items: list[dict], *, dry_run: bool, include_folders: bool, actor: Actor) -> dict:
    """Re-computes every proposal server-side – client-supplied paths are never trusted."""
    renamed, skipped, errors, planned = 0, 0, [], []
    for item in items:
        try:
            with session_scope() as db:
                if item["media_type"] == "movie":
                    m = db.get(models.Movie, int(item["media_id"]))
                    proposal = _preview_movie(m, include_folders) if m and m.file_path else None
                else:
                    ep = db.get(models.Episode, int(item["media_id"]))
                    if not ep or not ep.file_path:
                        proposal = None
                    else:
                        series = db.get(models.Series, ep.series_id)
                        group = list(db.scalars(select(models.Episode).where(models.Episode.file_path == ep.file_path)))
                        proposal = _preview_episode(ep, series, include_folders, {ep.file_path: group})
                if proposal is None:
                    skipped += 1
                    continue
                current, target = Path(proposal["current_path"]), Path(proposal["proposed_path"])
                planned.append({"from": str(current), "to": str(target)})
                if dry_run:
                    continue
                if target.exists() and target != current:
                    errors.append(f"{target.name}: destination already exists")
                    continue
                tx = fileops.FileTransaction()
                try:
                    if proposal.get("folder_change") and item["media_type"] == "movie":
                        old_dir, new_dir = current.parent, Path(proposal["proposed_folder"])
                        if new_dir.exists():
                            raise FileExistsError(f"Folder {new_dir} already exists")
                        tx.mkdirs(new_dir.parent)
                        os.rename(fileops.long_path(old_dir), fileops.long_path(new_dir))
                        tx.ops.append(fileops._Op("moved", old_dir, new_dir))
                        current = new_dir / current.name
                        m.path = str(new_dir)
                    if current != target:
                        tx.place(current, target, "move")
                        _rename_sidecars(tx, current, target)
                    new_path = str(target)
                    if item["media_type"] == "movie":
                        m.file_path = new_path
                        m.path = str(target.parent)
                        history(db, "movie", m.id, m.title, "renamed", quality=m.quality, details=f"'{Path(proposal['current_path']).name}' → '{target.name}'")
                    else:
                        for e in db.scalars(select(models.Episode).where(models.Episode.id.in_(proposal["episode_ids"]))):
                            e.file_path = new_path
                        history(db, "tv", proposal["series_id"], proposal["title"], "renamed", details=f"'{Path(proposal['current_path']).name}' → '{target.name}'")
                    db.flush()
                except Exception:
                    tx.rollback()
                    raise
                fileops.remove_empty_dirs(Path(proposal["current_path"]).parent, Path(proposal["current_path"]).parent.parent.parent)
                renamed += 1
        except Exception as exc:
            errors.append(f"{item.get('title', item.get('media_id'))}: {exc}")
    if not dry_run:
        with session_scope() as db:
            audit(db, "rename", "bulk_rename", actor=actor, details={"renamed": renamed, "errors": errors[:20], "folders": include_folders}, success=not errors)
        if renamed:
            bus.publish(EventType.RENAME_COMPLETED, renamed=renamed)
    return {"dry_run": dry_run, "renamed": renamed, "skipped": skipped, "errors": errors, "planned": planned[:500]}


def _rename_sidecars(tx: fileops.FileTransaction, old: Path, new: Path) -> None:
    extras = {e.lower() for e in get_settings().extra_file_extensions}
    try:
        for sib in old.parent.iterdir():
            if sib.is_file() and sib.suffix.lower() in extras and sib.name.lower().startswith(old.stem.lower()):
                target = new.with_name(new.stem + sib.name[len(old.stem):])
                if not target.exists():
                    tx.place(sib, target, "move")
    except OSError as exc:
        log.info("Sidecar rename skipped: %s", exc)
