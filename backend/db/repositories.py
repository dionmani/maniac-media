"""Repository pattern: query construction lives here, not in routers or services.

Repositories expose intention-revealing methods and a generic, whitelisted
filter/sort/paginate helper so the API can offer flexible listing without ever
interpolating user input into SQL.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from backend.db import models
from backend.db.base import Base, utcnow
from backend.domain.availability import catalog_cutoff, date_only_cutoff
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


# Library HDR filter → stored hdr_format values (exact matches; layered formats count for each layer).
# "Dolby Vision" covers rows inspected while the parser briefly used that label instead of "DV".
_HDR_FILTER_VALUES: dict[str, tuple[str, ...]] = {
    "DV": ("DV", "DV HDR10", "DV HDR10+", "Dolby Vision"),
    "HDR10+": ("HDR10+", "DV HDR10+"),
    "HDR10": ("HDR10", "DV HDR10"),
    "HLG": ("HLG",),
}


def media_file_filter(video_resolution: str | None = None, hdr_format: str | None = None,
                      video_codec: str | None = None, audio_codec: str | None = None):
    """Library filter on measured (ffprobe) facts of a ``MediaFile``, or None when no filter is set.

    Shared by the movie and series lists so both apply identical semantics; callers add the
    ownership correlation (movie_id / series_id) and wrap it in EXISTS.
    """
    mf = models.MediaFile
    conds = []
    if video_resolution:
        conds.append(mf.video_resolution == video_resolution)
    if hdr_format:
        key = hdr_format.upper()
        if key == "SDR":  # measured SDR, not "never inspected" (both have hdr_format NULL)
            conds.extend((mf.hdr_format.is_(None), mf.inspected_at.is_not(None)))
        elif key == "ANY_HDR":
            conds.append(mf.hdr_format.is_not(None))
        else:
            conds.append(mf.hdr_format.in_(_HDR_FILTER_VALUES.get(key, (hdr_format,))))
    if video_codec:
        conds.append(mf.video_codec == video_codec.lower())
    if audio_codec:
        conds.append(mf.audio_codec == audio_codec.lower())
    return and_(*conds) if conds else None


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
        video_resolution: str | None = None,
        hdr_format: str | None = None,
        video_codec: str | None = None,
        audio_codec: str | None = None,
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
        if (mf_cond := media_file_filter(video_resolution, hdr_format, video_codec, audio_codec)) is not None:
            mf = models.MediaFile
            conds.append(select(mf.id).where(mf.movie_id == models.Movie.id, mf.is_primary.is_(True), mf_cond).exists())
        return stmt.where(and_(*conds)) if conds else stmt

    def by_tmdb(self, tmdb_id: int) -> models.Movie | None:
        return self.db.scalar(select(models.Movie).where(models.Movie.tmdb_id == tmdb_id))

    def by_imdb(self, imdb_id: str) -> models.Movie | None:
        return self.db.scalar(select(models.Movie).where(models.Movie.imdb_id == imdb_id))

    def wanted_missing(self) -> Select:
        return select(models.Movie).where(models.Movie.monitored.is_(True), models.Movie.has_file.is_(False))

    @staticmethod
    def released(today: date, catalog_days: int):
        """SQL form of ``domain.availability.movie_availability`` - keep the two in step."""
        m, t = models.Movie, today.isoformat()
        conds = [and_(m.digital_release_date != "", m.digital_release_date <= t),
                 and_(m.physical_release_date != "", m.physical_release_date <= t)]
        cutoff = catalog_cutoff(today, catalog_days)
        if cutoff:
            conds += [and_(m.release_date != "", m.release_date <= cutoff.isoformat()),
                      and_(or_(m.release_date.is_(None), m.release_date == ""), m.year > 0, m.year < cutoff.year)]
        return or_(*conds)

    def ids_by_external(self, tmdb_ids: Sequence[int], imdb_ids: Sequence[str]) -> tuple[dict[int, int], dict[str, int]]:
        """Library movie ids for many external ids at once: ({tmdb_id: id}, {imdb_id: id})."""
        if not tmdb_ids and not imdb_ids:
            return {}, {}
        rows = self.db.execute(
            select(models.Movie.id, models.Movie.tmdb_id, models.Movie.imdb_id)
            .where(or_(models.Movie.tmdb_id.in_(tmdb_ids), models.Movie.imdb_id.in_(imdb_ids)))
        ).all()
        return {r.tmdb_id: r.id for r in rows if r.tmdb_id}, {r.imdb_id: r.id for r in rows if r.imdb_id}


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

    def query(self, *, search: str | None = None, status: str | None = None, monitored: bool | None = None, network: str | None = None,
              video_resolution: str | None = None, hdr_format: str | None = None, video_codec: str | None = None,
              audio_codec: str | None = None) -> Select:
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
        # a series matches when at least one of its episode files does
        if (mf_cond := media_file_filter(video_resolution, hdr_format, video_codec, audio_codec)) is not None:
            mf = models.MediaFile
            conds.append(select(mf.id).where(mf.series_id == models.Series.id, mf.media_type == "tv", mf_cond).exists())
        return stmt.where(and_(*conds)) if conds else stmt

    def media_summary(self, series_ids: Sequence[int]) -> dict[int, dict[str, list[str]]]:
        """Distinct measured video/audio facts across each series' inspected episode files (one query)."""
        if not series_ids:
            return {}
        mf = models.MediaFile
        rows = self.db.execute(
            select(mf.series_id, mf.video_resolution, mf.hdr_format, mf.video_codec, mf.audio_codec)
            .where(mf.series_id.in_(series_ids), mf.media_type == "tv", mf.inspected_at.is_not(None))
            .distinct()
        ).all()
        rank = {"2160p": 0, "1080p": 1, "720p": 2, "SD": 3}
        sets: dict[int, dict[str, set[str]]] = {}
        for series_id, res, hdr, vcodec, acodec in rows:
            agg = sets.setdefault(series_id, {"video_resolutions": set(), "hdr_formats": set(), "video_codecs": set(), "audio_codecs": set()})
            for key, value in (("video_resolutions", res), ("hdr_formats", hdr), ("video_codecs", vcodec), ("audio_codecs", acodec)):
                if value:
                    agg[key].add(value)
        return {
            sid: {
                "video_resolutions": sorted(agg["video_resolutions"], key=lambda r: rank.get(r, 9)),
                "hdr_formats": sorted(agg["hdr_formats"]),
                "video_codecs": sorted(agg["video_codecs"]),
                "audio_codecs": sorted(agg["audio_codecs"]),
            }
            for sid, agg in sets.items()
        }

    def with_episodes(self, series_id: int) -> models.Series | None:
        return self.db.scalar(
            select(models.Series)
            .where(models.Series.id == series_id)
            .options(selectinload(models.Series.seasons).selectinload(models.Season.episodes))
        )

    def episode_stats(self, series_ids: Sequence[int]) -> dict[int, dict[str, int]]:
        """Aggregate counts in one query instead of loading 15k episode rows.

        An episode that hasn't aired yet (see ``EpisodeRepository.aired`` - undated counts as not
        aired) can't be missing, so it is excluded from ``missing_count`` and from
        ``aired_episode_count``, matching the wanted/search queries.

        ``aired_episode_count`` is the "x / y episodes" denominator, i.e. Sonarr's episodeCount:
        episodes that have a file, or are monitored and have aired. Unmonitored episodes without a
        file (e.g. an unmonitored Specials season) aren't wanted, so they must not read as missing.
        """
        if not series_ids:
            return {}
        aired = EpisodeRepository.aired(utcnow())
        rows = self.db.execute(
            select(
                models.Episode.series_id,
                func.count(models.Episode.id),
                func.sum(case((models.Episode.has_file.is_(True), 1), else_=0)),
                func.sum(case((and_(models.Episode.monitored.is_(True), models.Episode.has_file.is_(False), aired), 1), else_=0)),
                func.sum(func.coalesce(models.Episode.file_size_bytes, 0)),
                func.sum(case((or_(models.Episode.has_file.is_(True), and_(models.Episode.monitored.is_(True), aired)), 1), else_=0)),
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

    def by_tmdb(self, tmdb_id: int) -> models.Series | None:
        return self.db.scalars(select(models.Series).where(models.Series.tmdb_id == tmdb_id).limit(1)).first()

    def by_imdb(self, imdb_id: str) -> models.Series | None:
        return self.db.scalars(select(models.Series).where(models.Series.imdb_id == imdb_id).limit(1)).first()

    def ids_by_external(self, tmdb_ids: Sequence[int], tvdb_ids: Sequence[int]) -> tuple[dict[int, int], dict[int, int]]:
        """Library series ids for many external ids at once: ({tmdb_id: id}, {tvdb_id: id})."""
        if not tmdb_ids and not tvdb_ids:
            return {}, {}
        rows = self.db.execute(
            select(models.Series.id, models.Series.tmdb_id, models.Series.tvdb_id)
            .where(or_(models.Series.tmdb_id.in_(tmdb_ids), models.Series.tvdb_id.in_(tvdb_ids)))
        ).all()
        return {r.tmdb_id: r.id for r in rows if r.tmdb_id}, {r.tvdb_id: r.id for r in rows if r.tvdb_id}

    def season_stats(self, series_ids: Sequence[int]) -> dict[int, dict[int, dict[str, Any]]]:
        """Per-season counts, Sonarr-style, in one query: ``{series_id: {season_number: stats}}``.

        ``episode_count`` is what Sonarr calls episodeCount: episodes that have a file, or are
        monitored and have aired (``EpisodeRepository.aired``). ``total_episode_count`` is every episode.
        """
        if not series_ids:
            return {}
        e = models.Episode
        aired = EpisodeRepository.aired(utcnow())
        rows = self.db.execute(
            select(
                e.series_id,
                e.season_number,
                func.count(e.id),
                func.sum(case((e.has_file.is_(True), 1), else_=0)),
                func.sum(case((or_(e.has_file.is_(True), and_(e.monitored.is_(True), aired)), 1), else_=0)),
                func.sum(case((e.has_file.is_(True), func.coalesce(e.file_size_bytes, 0)), else_=0)),
                func.max(case((aired, e.air_date), else_=None)),
            )
            .where(e.series_id.in_(series_ids))
            .group_by(e.series_id, e.season_number)
        ).all()
        out: dict[int, dict[int, dict[str, Any]]] = {}
        for series_id, season, total, files, count, size, last_aired in rows:
            out.setdefault(series_id, {})[season] = {
                "total_episode_count": total or 0, "episode_file_count": int(files or 0), "episode_count": int(count or 0),
                "size_on_disk": int(size or 0), "previous_airing": last_aired,
            }
        return out


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

    def for_series(self, series_id: int) -> Sequence[models.Episode]:
        return self.db.scalars(
            select(models.Episode).where(models.Episode.series_id == series_id)
            .order_by(models.Episode.season_number, models.Episode.episode_number)
        ).all()

    @staticmethod
    def aired(now: datetime):
        """SQL form of ``domain.availability.episode_availability`` - keep the two in step.

        An exact air time wins; a bare date counts once it is over everywhere; undated never counts.
        """
        e = models.Episode
        return or_(
            and_(e.air_date_utc.is_not(None), e.air_date_utc <= now),
            and_(e.air_date_utc.is_(None), e.air_date != "", e.air_date <= date_only_cutoff(now)),
        )

    def seasons_with_missing(self, series_id: int) -> list[int]:
        """Season numbers that have monitored, aired episodes without a file (what a series search should cover)."""
        return list(self.db.scalars(
            select(models.Episode.season_number).distinct()
            .join(models.Season, models.Season.id == models.Episode.season_id)
            .where(
                models.Episode.series_id == series_id,
                models.Episode.monitored.is_(True),
                models.Episode.has_file.is_(False),
                models.Season.monitored.is_(True),
                self.aired(utcnow()),
            )
            .order_by(models.Episode.season_number)
        ))

    def wanted_missing(self, now: datetime) -> Select:
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
                self.aired(now),
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

    def active_download_paths(self) -> list[str]:
        q = models.DownloadQueueItem
        return list(self.db.scalars(select(q.download_path).where(q.status.in_(self.ACTIVE), q.download_path.is_not(None))))

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


class TagRepository(Repository[models.Tag]):
    model = models.Tag

    def all(self) -> Sequence[models.Tag]:
        return self.db.scalars(select(models.Tag).order_by(models.Tag.id)).all()

    def by_label(self, label: str) -> models.Tag | None:
        """Case-insensitive, like Radarr/Sonarr."""
        return self.db.scalars(select(models.Tag).where(func.lower(models.Tag.label) == label.strip().lower()).limit(1)).first()

    def labels(self, ids: Sequence[int]) -> dict[int, str]:
        if not ids:
            return {}
        return dict(self.db.execute(select(models.Tag.id, models.Tag.label).where(models.Tag.id.in_(list(ids)))).all())


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


class DiscoverRepository(Repository[models.DiscoverExclusion]):
    """Discover state: when titles first appeared, and the ones the user is not interested in."""

    model = models.DiscoverExclusion
    default_sort = "created"
    sortable = {"created": models.DiscoverExclusion.created_at, "title": models.DiscoverExclusion.title, "id": models.DiscoverExclusion.id}

    def record_seen(self, media_type: str, section: str, tmdb_ids: Sequence[int]) -> dict[int, datetime]:
        """Remember the first time each title appeared in ``section``; returns {tmdb_id: first_seen_at}."""
        ids = list(dict.fromkeys(tmdb_ids))
        if not ids:
            return {}
        seen = self.first_seen(media_type, section, ids)
        missing = [i for i in ids if i not in seen]
        if missing:
            now = utcnow()
            try:
                with self.db.begin_nested():
                    self.db.add_all(models.DiscoverSeen(media_type=media_type, section=section, tmdb_id=i, first_seen_at=now) for i in missing)
            except IntegrityError:
                pass  # recorded concurrently; the re-read below picks those rows up
            seen = self.first_seen(media_type, section, ids)
        return seen

    def first_seen(self, media_type: str, section: str, tmdb_ids: Sequence[int]) -> dict[int, datetime]:
        rows = self.db.execute(
            select(models.DiscoverSeen.tmdb_id, models.DiscoverSeen.first_seen_at).where(
                models.DiscoverSeen.media_type == media_type, models.DiscoverSeen.section == section,
                models.DiscoverSeen.tmdb_id.in_(tmdb_ids))
        ).all()
        return {r.tmdb_id: r.first_seen_at for r in rows}

    def excluded_ids(self, media_type: str) -> set[int]:
        return set(self.db.scalars(select(models.DiscoverExclusion.tmdb_id).where(models.DiscoverExclusion.media_type == media_type)))

    def exclusions(self, media_type: str | None = None) -> Sequence[models.DiscoverExclusion]:
        stmt = select(models.DiscoverExclusion).order_by(models.DiscoverExclusion.created_at.desc(), models.DiscoverExclusion.id.desc())
        if media_type:
            stmt = stmt.where(models.DiscoverExclusion.media_type == media_type)
        return self.db.scalars(stmt).all()

    def exclusion(self, media_type: str, tmdb_id: int) -> models.DiscoverExclusion | None:
        return self.db.scalar(select(models.DiscoverExclusion).where(
            models.DiscoverExclusion.media_type == media_type, models.DiscoverExclusion.tmdb_id == tmdb_id))

    def add_exclusion(self, media_type: str, tmdb_id: int, title: str, year: int | None) -> models.DiscoverExclusion:
        """Idempotent: excluding a title twice keeps the original entry."""
        existing = self.exclusion(media_type, tmdb_id)
        if existing is not None:
            return existing
        return self.add(models.DiscoverExclusion(media_type=media_type, tmdb_id=tmdb_id, title=title, year=year))

    def remove_exclusion(self, media_type: str, tmdb_id: int) -> bool:
        existing = self.exclusion(media_type, tmdb_id)
        if existing is None:
            return False
        self.db.delete(existing)
        return True


class LibraryIssueRepository(Repository[models.LibraryIssue]):
    model = models.LibraryIssue

    def for_file(self, media_type: str, media_id: int | None, path: str, issue_types: Sequence[str],
                 statuses: Sequence[str]) -> list[models.LibraryIssue]:
        """Issues of ``issue_types`` recorded against ``path`` (compared via the normalised path key)."""
        key = normalise_path_key(path)
        stmt = select(models.LibraryIssue).where(
            models.LibraryIssue.media_type == media_type,
            models.LibraryIssue.issue_type.in_(issue_types),
            models.LibraryIssue.status.in_(statuses),
            models.LibraryIssue.path.is_not(None),
        )
        if media_id is not None:
            stmt = stmt.where(models.LibraryIssue.media_id == media_id)
        return [row for row in self.db.scalars(stmt) if normalise_path_key(row.path) == key]


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

