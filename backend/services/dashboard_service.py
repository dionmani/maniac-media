"""Dashboard aggregates – a handful of indexed COUNT/SUM queries, well under the 200 ms budget."""
from __future__ import annotations

import shutil
from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from backend.api.schemas import public_art
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import EpisodeRepository, MediaFileRepository, MovieRepository


def _disk(path: str) -> dict | None:
    try:
        usage = shutil.disk_usage(path)
        return {"path": path, "total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return None


def stats(db: Session) -> dict:
    settings = get_settings()
    today = date.today().isoformat()
    count = lambda stmt: db.scalar(stmt) or 0  # noqa: E731

    movies_total = count(select(func.count(models.Movie.id)))
    movies_with_file = count(select(func.count(models.Movie.id)).where(models.Movie.has_file.is_(True)))
    movies_missing = count(select(func.count(models.Movie.id)).where(
        models.Movie.monitored.is_(True), models.Movie.has_file.is_(False), MovieRepository.released(date.today(), settings.release_gate_catalog_days)))
    series_total = count(select(func.count(models.Series.id)))
    episodes_total = count(select(func.count(models.Episode.id)))
    episodes_with_file = count(select(func.count(models.Episode.id)).where(models.Episode.has_file.is_(True)))
    episodes_missing = count(
        select(func.count(models.Episode.id))
        .join(models.Series, models.Series.id == models.Episode.series_id)
        .where(models.Episode.monitored.is_(True), models.Episode.has_file.is_(False), models.Series.monitored.is_(True),
               models.Episode.season_number > 0, EpisodeRepository.aired(utcnow()))
    )
    repo = MediaFileRepository(db)
    movie_bytes = repo.total_bytes("movie")
    episode_bytes = repo.total_bytes("tv")
    active = count(select(func.count(models.DownloadQueueItem.id)).where(models.DownloadQueueItem.status.in_(("queued", "downloading", "paused"))))
    failed_imports = count(select(func.count(models.DownloadQueueItem.id)).where(models.DownloadQueueItem.status == "import_failed"))
    failed_by_reason = dict(db.execute(
        select(models.DownloadQueueItem.failure_reason, func.count(models.DownloadQueueItem.id))
        .where(models.DownloadQueueItem.status == "import_failed", models.DownloadQueueItem.failure_reason.is_not(None))
        .group_by(models.DownloadQueueItem.failure_reason)
    ).all())
    failed_recent = count(
        select(func.count(models.History.id)).where(
            models.History.event_type.in_(("import_failed", "download_failed")), models.History.timestamp >= utcnow() - timedelta(days=7)
        )
    )
    issues = dict(
        db.execute(select(models.LibraryIssue.issue_type, func.count(models.LibraryIssue.id)).where(models.LibraryIssue.status == "open")
                   .group_by(models.LibraryIssue.issue_type)).all()
    )
    upcoming = db.execute(
        select(models.Episode.id, models.Episode.series_id, models.Series.title, models.Episode.season_number, models.Episode.episode_number,
               models.Episode.title, models.Episode.air_date, models.Series.poster_path)
        .join(models.Series, models.Series.id == models.Episode.series_id)
        .where(and_(models.Episode.air_date >= today, models.Episode.air_date <= (date.today() + timedelta(days=7)).isoformat(), models.Series.monitored.is_(True)))
        .order_by(models.Episode.air_date)
        .limit(12)
    ).all()
    recent = db.scalars(select(models.History).order_by(models.History.timestamp.desc()).limit(15)).all()
    disks = [d for d in {p: _disk(p) for p in {settings.media_root_movies, settings.media_root_tv, settings.download_folder}}.values() if d]

    return {
        "movies": {"total": movies_total, "with_file": movies_with_file, "missing": movies_missing},
        "series": {"total": series_total, "episodes": episodes_total, "episodes_with_file": episodes_with_file, "episodes_missing": episodes_missing},
        "missing_total": movies_missing + episodes_missing,
        "downloads": {"active": active},
        "failed_imports": failed_imports,
        "failed_last_7_days": failed_recent,
        "failed_top_reasons": [{"reason": k, "count": v} for k, v in sorted(failed_by_reason.items(), key=lambda kv: -kv[1])[:3]],
        "storage": {"library_bytes": movie_bytes + episode_bytes, "movies_bytes": movie_bytes, "tv_bytes": episode_bytes, "disks": disks},
        "issues": issues,
        "upcoming": [
            {"episode_id": r[0], "series_id": r[1], "series": r[2], "season": r[3], "episode": r[4], "title": r[5], "air_date": r[6],
             "poster": public_art("tv", r[1], r[7])}
            for r in upcoming
        ],
        "recent_activity": [
            {"id": h.id, "media_type": h.media_type, "media_id": h.media_id, "title": h.title, "event_type": h.event_type,
             "quality": h.quality, "details": h.details, "timestamp": h.timestamp.isoformat() if h.timestamp else None}
            for h in recent
        ],
    }
