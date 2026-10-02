"""Repository pattern: query construction lives here, not in routers or services.

Repositories expose intention-revealing methods and a generic, whitelisted
filter/sort/paginate helper so the API can offer flexible listing without ever
interpolating user input into SQL.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from backend.db import models
from backend.db.base import Base
from backend.domain.parser import normalise_path_key

T = TypeVar("T", bound=Base)


@dataclass
class Page(Generic[T]):
    items: Sequence[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size)) if self.page_size else 1


class Repository(Generic[T]):
    model: type[T]
    sortable: dict[str, Any] = {}
    default_sort: str = "id"

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, id_: Any) -> T | None:
        return self.db.get(self.model, id_)

    def add(self, entity: T) -> T:
        self.db.add(entity)
        self.db.flush()
        return entity

    def delete(self, entity: T) -> None:
        self.db.delete(entity)

    def paginate(self, stmt: Select, *, page: int = 1, page_size: int = 50, sort: str | None = None, order: str = "asc") -> Page[T]:
        page = max(1, page)
        page_size = max(1, min(page_size, 500))
        total = self.db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        column = self.sortable.get(sort or self.default_sort, self.sortable.get(self.default_sort))
        if column is not None:
            stmt = stmt.order_by(column.desc() if order == "desc" else column.asc(), self.model.id)  # type: ignore[attr-defined]
        items = self.db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).unique().all()
        return Page(items, total, page, page_size)


class MovieRepository(Repository[models.Movie]):
    model = models.Movie
    default_sort = "title"
    sortable = {
        "title": func.coalesce(models.Movie.sort_title, models.Movie.title),
        "year": models.Movie.year,
        "added": models.Movie.added_date,
        "size": models.Movie.file_size_bytes,
        "rating": models.Movie.rating,
        "runtime": models.Movie.runtime_minutes,
        "id": models.Movie.id,
    }

    def query(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        monitored: bool | None = None,
        has_file: bool | None = None,
        genre: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        quality_profile_id: int | None = None,
    ) -> Select:
        stmt = select(models.Movie)
        conds = []
        if search:
            like = f"%{search.strip()}%"
            conds.append(or_(models.Movie.title.ilike(like), models.Movie.original_title.ilike(like), models.Movie.imdb_id == search.strip()))
        if status:
            conds.append(models.Movie.status == status)
        if monitored is not None:
            conds.append(models.Movie.monitored.is_(monitored))
        if has_file is not None:
            conds.append(models.Movie.has_file.is_(has_file))
        if genre:
            conds.append(models.Movie.genres.ilike(f"%{genre}%"))
        if year_from:
            conds.append(models.Movie.year >= year_from)
        if year_to:
            conds.append(models.Movie.year <= year_to)
        if quality_profile_id:
            conds.append(models.Movie.quality_profile_id == quality_profile_id)
        return stmt.where(and_(*conds)) if conds else stmt

    def by_tmdb(self, tmdb_id: int) -> models.Movie | None:
        return self.db.scalar(select(models.Movie).where(models.Movie.tmdb_id == tmdb_id))

    def by_imdb(self, imdb_id: str) -> models.Movie | None:
        return self.db.scalar(select(models.Movie).where(models.Movie.imdb_id == imdb_id))

    def wanted_missing(self) -> Select:
        return select(models.Movie).where(models.Movie.monitored.is_(True), models.Movie.has_file.is_(False))


class SeriesRepository(Repository[models.Series]):
    model = models.Series
    default_sort = "title"
    sortable = {
        "title": func.coalesce(models.Series.sort_title, models.Series.title),
        "year": models.Series.year,
        "added": models.Series.added_date,
        "rating": models.Series.rating,
        "id": models.Series.id,
    }

    def query(self, *, search: str | None = None, status: str | None = None, monitored: bool | None = None, network: str | None = None) -> Select:
        stmt = select(models.Series)
        conds = []
        if search:
            conds.append(models.Series.title.ilike(f"%{search.strip()}%"))
        if status:
            conds.append(models.Series.status == status)
        if monitored is not None:
            conds.append(models.Series.monitored.is_(monitored))
        if network:
            conds.append(models.Series.network.ilike(f"%{network}%"))
        return stmt.where(and_(*conds)) if conds else stmt

    def with_episodes(self, series_id: int) -> models.Series | None:
        return self.db.scalar(
            select(models.Series)
            .where(models.Series.id == series_id)
            .options(selectinload(models.Series.seasons).selectinload(models.Season.episodes))
        )

    def episode_stats(self, series_ids: Sequence[int]) -> dict[int, dict[str, int]]:
        """Aggregate counts in one query instead of loading 15k episode rows.

        An episode that hasn't aired yet (air_date in the future) can't be missing, so it is
        excluded from ``missing_count`` and from ``aired_episode_count``. Undated episodes are
        treated as aired, matching the wanted/search queries.
        """
        if not series_ids:
            return {}
        today = date.today().isoformat()
        aired = or_(models.Episode.air_date.is_(None), models.Episode.air_date <= today)
        rows = self.db.execute(
            select(
                models.Episode.series_id,
                func.count(models.Episode.id),
                func.sum(case((models.Episode.has_file.is_(True), 1), else_=0)),
                func.sum(case((and_(models.Episode.monitored.is_(True), models.Episode.has_file.is_(False), aired), 1), else_=0)),
                func.sum(func.coalesce(models.Episode.file_size_bytes, 0)),
                func.sum(case((or_(aired, models.Episode.has_file.is_(True)), 1), else_=0)),
            )
            .where(models.Episode.series_id.in_(series_ids))
            .group_by(models.Episode.series_id)
        ).all()
        return {
            r[0]: {"episode_count": r[1] or 0, "episode_file_count": int(r[2] or 0), "missing_count": int(r[3] or 0),
                   "size_on_disk": int(r[4] or 0), "aired_episode_count": int(r[5] or 0)}
            for r in rows
        }

    def by_tvdb(self, tvdb_id: int) -> models.Series | None:
        return self.db.scalar(select(models.Series).where(models.Series.tvdb_id == tvdb_id))

    def by_tvmaze(self, tvmaze_id: int) -> models.Series | None:
        return self.db.scalar(select(models.Series).where(models.Series.tvmaze_id == tvmaze_id))


class EpisodeRepository(Repository[models.Episode]):
    model = models.Episode
    default_sort = "air_date"
    sortable = {"air_date": models.Episode.air_date, "id": models.Episode.id, "season": models.Episode.season_number}

    def find(self, series_id: int, season: int, episode: int) -> models.Episode | None:
        return self.db.scalar(
            select(models.Episode).where(
                models.Episode.series_id == series_id,
                models.Episode.season_number == season,
                models.Episode.episode_number == episode,
            )
        )

    def wanted_missing(self, today: str) -> Select:
        return (
            select(models.Episode)
            .join(models.Series, models.Series.id == models.Episode.series_id)
            .join(models.Season, models.Season.id == models.Episode.season_id)
            .where(
                models.Episode.monitored.is_(True),
                models.Episode.has_file.is_(False),
                models.Series.monitored.is_(True),
                models.Season.monitored.is_(True),
                models.Episode.season_number > 0,
                or_(models.Episode.air_date.is_(None), models.Episode.air_date <= today),
            )
        )


class QueueRepository(Repository[models.DownloadQueueItem]):
    model = models.DownloadQueueItem
    ACTIVE = ("queued", "downloading", "paused", "completed", "importing", "warning")
    # Activity view order: what's moving now, then the waiting line, then things needing attention
    DISPLAY_ORDER = ("downloading", "importing", "completed", "queued", "paused", "warning", "import_failed", "imported")

    def for_display(self, include_finished: bool = False, limit: int = 500) -> Sequence[models.DownloadQueueItem]:
        """The queue as the Activity view shows it, mirroring each download client's own queue order.

        Failed downloads are left out: they're removed once recorded in History, so a "failed" row is either
        mid-hand-off or a leftover for housekeeping to purge."""
        q = models.DownloadQueueItem
        stmt = select(q).where(q.status != "failed").order_by(
            case({s: i for i, s in enumerate(self.DISPLAY_ORDER)}, value=q.status, else_=len(self.DISPLAY_ORDER)),
            case((q.client_position.is_(None), 1), else_=0),  # portable NULLS LAST
            q.client_position,
            q.added_at.desc(),
            q.id.desc(),
        )
        if not include_finished:
            stmt = stmt.where(q.status.in_((*self.ACTIVE, "import_failed")))
        return self.db.scalars(stmt.limit(limit)).all()

    def active(self) -> Sequence[models.DownloadQueueItem]:
        return self.db.scalars(
            select(models.DownloadQueueItem).where(models.DownloadQueueItem.status.in_(self.ACTIVE)).order_by(models.DownloadQueueItem.added_at)
        ).all()

    def is_queued(self, media_type: str, media_id: int, season: int | None = None, episode: int | None = None) -> bool:
        stmt = select(models.DownloadQueueItem.id).where(
            models.DownloadQueueItem.media_type == media_type,
            models.DownloadQueueItem.media_id == media_id,
            models.DownloadQueueItem.status.in_(self.ACTIVE),
        )
        if season is not None:
            stmt = stmt.where(models.DownloadQueueItem.season_num == season)
        if episode is not None:
            stmt = stmt.where(or_(models.DownloadQueueItem.ep_num == episode, models.DownloadQueueItem.ep_num.is_(None)))
        return self.db.scalar(stmt.limit(1)) is not None


class HistoryRepository(Repository[models.History]):
    model = models.History
    default_sort = "timestamp"
    sortable = {"timestamp": models.History.timestamp, "id": models.History.id}


class AuditRepository(Repository[models.AuditLog]):
    model = models.AuditLog
    default_sort = "timestamp"
    sortable = {"timestamp": models.AuditLog.timestamp, "id": models.AuditLog.id}


class UserRepository(Repository[models.User]):
    model = models.User
    default_sort = "username"
    sortable = {"username": models.User.username, "id": models.User.id}


class JobRepository(Repository[models.Job]):
    model = models.Job
    default_sort = "created"
    sortable = {"created": models.Job.created_at, "id": models.Job.id}


class MediaFileRepository(Repository[models.MediaFile]):
    model = models.MediaFile
    default_sort = "added"
    sortable = {
        "size": models.MediaFile.size_bytes,
        "added": models.MediaFile.added_at,
        "quality": models.MediaFile.quality,
        "id": models.MediaFile.id,
    }

    def by_path(self, path: str | Path) -> models.MediaFile | None:
        h = hashlib.sha256(normalise_path_key(str(path)).encode()).hexdigest()
        return self.db.scalar(select(models.MediaFile).where(models.MediaFile.path_hash == h))

    def for_movie(self, movie_id: int) -> Sequence[models.MediaFile]:
        return self.db.scalars(
            select(models.MediaFile)
            .where(models.MediaFile.movie_id == movie_id)
            .order_by(models.MediaFile.is_primary.desc(), models.MediaFile.id)
        ).all()

    def for_series(self, series_id: int) -> Sequence[models.MediaFile]:
        return self.db.scalars(
            select(models.MediaFile)
            .where(models.MediaFile.series_id == series_id)
            .order_by(models.MediaFile.is_primary.desc(), models.MediaFile.id)
        ).all()

    def for_episode(self, episode_id: int) -> Sequence[models.MediaFile]:
        return self.db.scalars(
            select(models.MediaFile)
            .join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id)
            .where(models.EpisodeFile.episode_id == episode_id)
            .order_by(models.MediaFile.is_primary.desc(), models.MediaFile.id)
        ).all()

    def total_bytes(self, media_type: str | None = None) -> int:
        stmt = select(func.coalesce(func.sum(models.MediaFile.size_bytes), 0))
        if media_type:
            stmt = stmt.where(models.MediaFile.media_type == media_type)
        return int(self.db.scalar(stmt) or 0)

    def uninspected(self, limit: int = 100) -> Sequence[models.MediaFile]:
        return self.db.scalars(
            select(models.MediaFile)
            .where(models.MediaFile.codec.is_(None))
            .limit(limit)
        ).all()

    def query(
        self,
        *,
        movie_id: int | None = None,
        series_id: int | None = None,
        episode_id: int | None = None,
    ) -> Select:
        stmt = select(models.MediaFile)
        if episode_id is not None:
            stmt = stmt.join(models.EpisodeFile, models.EpisodeFile.media_file_id == models.MediaFile.id).where(
                models.EpisodeFile.episode_id == episode_id
            )
        elif movie_id is not None:
            stmt = stmt.where(models.MediaFile.movie_id == movie_id)
        elif series_id is not None:
            stmt = stmt.where(models.MediaFile.series_id == series_id)
        return stmt

