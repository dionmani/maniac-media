"""ffprobe-based media inspection.

Only the ffprobe subprocess itself is awaited on the event loop; every DB / filesystem step runs
in a worker thread with its own short session (``asyncio.to_thread``), so a backlog over a large
library never stalls API requests. Library-issue reconciliation is batched: one
:class:`IssueRecorder` pass (and one ``issues_changed`` event) per batch, not per file.
"""
import asyncio
import json
import logging
import shutil
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db.base import utcnow
from backend.db.models import EpisodeFile, MediaFile
from backend.db.session import session_scope
from backend.domain.inspection import InspectionParseError, compare, parse_ffprobe, resolution_class
from backend.domain.quality import parse_quality
from backend.services.fileops import long_path
from backend.services.issue_service import IssueRecorder

log = logging.getLogger("maniac.inspection")

INSPECTION_ISSUE_TYPES = ("quality_mismatch", "runtime_mismatch", "truncated", "missing_audio", "language_missing", "corrupt_file")


def ffprobe_path() -> str | None:
    settings = get_settings()
    if settings.ffprobe_path:
        return settings.ffprobe_path
    return shutil.which("ffprobe")


async def probe(path: Path, timeout: float = 30) -> dict[str, Any]:
    ffprobe = ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe not found")

    abs_path = long_path(path)

    args = [
        ffprobe,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-protocol_whitelist", "file",
        "-i", f"file:{abs_path}"
    ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap it, or the killed ffprobe lingers as a zombie
        raise RuntimeError("ffprobe timeout")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"ffprobe failed: {err}")

    out = stdout.decode("utf-8", errors="replace")
    if len(out) > 4 * 1024 * 1024:
        raise RuntimeError("ffprobe output exceeded 4MB limit")

    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe output is not valid JSON: {e}")


@dataclass(frozen=True)
class InspectionOutcome:
    status: Literal["inspected", "skipped", "error"]
    error: str | None = None


@dataclass
class _IssueBatch:
    """Issue reports collected while inspecting, reconciled in one pass by :func:`_reconcile`."""
    touched: list[tuple[str, int]] = field(default_factory=list)  # (media_type, media_id) fully re-checked
    reports: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.touched or self.reports)


def _reconcile(batch: _IssueBatch) -> None:
    if not batch:
        return
    with session_scope() as db:
        recorders: dict[str, IssueRecorder] = {}

        def recorder(media_type: str) -> IssueRecorder:
            if media_type not in recorders:
                recorders[media_type] = IssueRecorder(db, media_type)
            return recorders[media_type]

        for media_type, media_id in batch.touched:
            recorder(media_type).touch(media_id, *INSPECTION_ISSUE_TYPES)
        for report in batch.reports:
            recorder(report["media_type"]).report(**report)
        for rec in recorders.values():
            rec.finish()


def evaluate_findings(db: Session, media_file: MediaFile, inspection) -> list:
    settings = get_settings()

    expected_runtime_min = None
    is_movie = media_file.media_type == "movie"
    if is_movie and media_file.movie:
        expected_runtime_min = media_file.movie.runtime_minutes
    elif not is_movie:
        ef = db.scalar(select(EpisodeFile).where(EpisodeFile.media_file_id == media_file.id).limit(1))
        if ef and ef.episode:
            expected_runtime_min = ef.episode.runtime_minutes
            if not expected_runtime_min and media_file.series:
                expected_runtime_min = media_file.series.runtime_minutes

    claimed = parse_quality(media_file.quality or "")

    return compare(
        claimed,
        inspection,
        expected_runtime_min=expected_runtime_min,
        preferred_audio_languages=settings.preferred_audio_languages,
        is_movie=is_movie
    )


# One semaphore per event loop: an asyncio.Semaphore is bound to the loop it first waits on.
_probe_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _probe_semaphores.get(loop)
    if sem is None:
        sem = _probe_semaphores[loop] = asyncio.Semaphore(max(1, get_settings().inspection_concurrency))
    return sem


# --------------------------------------------------------------------------- blocking steps (worker thread)
def _media_id(mf: MediaFile) -> int | None:
    return mf.movie_id if mf.media_type == "movie" else mf.series_id


def _prepare(media_file_id: int, force: bool) -> tuple[Path | None, InspectionOutcome | None]:
    """Return the path to probe, or the final outcome when there's nothing to probe."""
    with session_scope() as db:
        mf = db.get(MediaFile, media_file_id)
        if not mf:
            return None, InspectionOutcome("error", "Media file not found")
        if not force and mf.inspected_at is not None and mf.inspected_size == mf.size_bytes and mf.inspected_mtime == mf.mtime:
            return None, InspectionOutcome("skipped")
        path = Path(mf.path)
        if not path.exists():
            mf.inspection_error = "File not found on disk"
            return None, InspectionOutcome("error", mf.inspection_error)
        if not ffprobe_path():
            mf.inspection_error = "ffprobe not found"
            return None, InspectionOutcome("error", mf.inspection_error)
        return path, None


def _record_error(media_file_id: int, error: str, batch: _IssueBatch) -> None:
    with session_scope() as db:
        mf = db.get(MediaFile, media_file_id)
        if mf is None:
            return
        mf.inspection_error = error[:512] or "Inspection failed"
        if "Invalid data found when processing input" in error:
            batch.reports.append({"issue_type": "corrupt_file", "media_type": mf.media_type, "media_id": _media_id(mf),
                                  "path": mf.path, "details": "ffprobe reported invalid data"})


def _store(media_file_id: int, payload: dict[str, Any], batch: _IssueBatch) -> InspectionOutcome:
    with session_scope() as db:
        mf = db.get(MediaFile, media_file_id)
        if mf is None:
            return InspectionOutcome("error", "Media file not found")
        try:
            inspection = parse_ffprobe(payload)
        except InspectionParseError as e:
            mf.inspection_error = str(e)[:512]
            return InspectionOutcome("error", str(e))

        mf.inspected_at = utcnow()
        mf.inspected_size = mf.size_bytes
        mf.inspected_mtime = mf.mtime
        mf.inspection_error = None
        mf.container = inspection.container
        mf.duration_seconds = inspection.duration_seconds
        mf.overall_bitrate = inspection.overall_bitrate

        if inspection.video:
            mf.video_codec = inspection.video.codec
            mf.video_width = inspection.video.width
            mf.video_height = inspection.video.height
            mf.video_bit_depth = inspection.video.bit_depth
            mf.hdr_format = inspection.video.hdr_format
            mf.video_resolution = resolution_class(inspection.video.width, inspection.video.height).value

        if inspection.audio:
            primary_audio = next((a for a in inspection.audio if a.default), inspection.audio[0])
            mf.audio_codec = primary_audio.codec
            mf.audio_channels = primary_audio.channels
            mf.audio_languages = list({a.language for a in inspection.audio if a.language})

        mf.subtitle_languages = list({s.language for s in inspection.subtitles if s.language})
        mf.mediainfo = asdict(inspection)

        media_id = _media_id(mf)
        if media_id is not None:
            batch.touched.append((mf.media_type, media_id))
        for f in evaluate_findings(db, mf, inspection):
            batch.reports.append({"issue_type": f.kind, "media_type": mf.media_type, "media_id": media_id, "path": mf.path,
                                  "details": f.message, "data": f.data, "severity": f.severity})
    bus.publish(EventType.MEDIA_INSPECTED, media_file_id=media_file_id)
    return InspectionOutcome("inspected")


# --------------------------------------------------------------------------- async entry points
async def _inspect(media_file_id: int, force: bool, batch: _IssueBatch) -> InspectionOutcome:
    path, outcome = await asyncio.to_thread(_prepare, media_file_id, force)
    if outcome is not None:
        return outcome
    try:
        async with get_semaphore():
            payload = await probe(path)
    except Exception as e:
        error = str(e) or type(e).__name__
        await asyncio.to_thread(_record_error, media_file_id, error, batch)
        return InspectionOutcome("error", error)
    return await asyncio.to_thread(_store, media_file_id, payload, batch)


async def inspect_file(media_file_id: int, *, force: bool = False) -> InspectionOutcome:
    batch = _IssueBatch()
    try:
        return await _inspect(media_file_id, force, batch)
    finally:
        await asyncio.to_thread(_reconcile, batch)


def _needs_inspection():
    # Failed files (inspection_error set) wait until they change – attach_*_file clears the error then.
    # Without this guard a file whose size changed but can't be probed is re-picked by every backlog
    # batch, and the backlog job re-queues itself every 10 seconds forever.
    return and_(
        MediaFile.inspection_error.is_(None),
        or_(MediaFile.inspected_at.is_(None), MediaFile.inspected_size != MediaFile.size_bytes, MediaFile.inspected_mtime != MediaFile.mtime),
    )


def _pending_ids(limit: int) -> list[int]:
    with session_scope() as db:
        return list(db.scalars(select(MediaFile.id).where(_needs_inspection()).order_by(MediaFile.id).limit(limit)))


def _mark_internal_error(media_file_id: int, exc: Exception) -> None:
    """An unexpected crash must still take the file out of the backlog, or it would be retried forever."""
    try:
        with session_scope() as db:
            mf = db.get(MediaFile, media_file_id)
            if mf is not None:
                mf.inspection_error = f"Internal error: {exc}"[:512]
    except Exception:
        log.exception("Could not record inspection error for media file %s", media_file_id)


async def inspect_backlog(limit: int = 100, progress=None) -> dict[str, Any]:
    if not get_settings().inspection_enabled or not ffprobe_path():
        return {"processed": 0, "status": "disabled_or_missing_binary"}

    ids = await asyncio.to_thread(_pending_ids, limit)
    if not ids:
        return {"processed": 0, "status": "complete"}

    batch = _IssueBatch()
    workers = asyncio.Semaphore(max(1, get_settings().inspection_concurrency))
    done = errors = 0

    async def one(media_file_id: int) -> None:
        nonlocal done, errors
        async with workers:
            try:
                outcome = await _inspect(media_file_id, False, batch)
                if outcome.status == "error":
                    errors += 1
            except Exception as e:
                log.error("Error inspecting %s: %s", media_file_id, e)
                await asyncio.to_thread(_mark_internal_error, media_file_id, e)
                errors += 1
            done += 1
            if progress:
                await asyncio.to_thread(progress, done * 100 / len(ids), f"Inspected {done}/{len(ids)} files")

    try:
        await asyncio.gather(*(one(i) for i in ids))
    finally:
        await asyncio.to_thread(_reconcile, batch)

    has_more = bool(await asyncio.to_thread(_pending_ids, 1))
    return {
        "processed": done,
        "errors": errors,
        "status": "more_pending" if has_more else "complete"
    }
