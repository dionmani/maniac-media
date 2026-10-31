"""ffprobe-based media inspection.

Only the ffprobe subprocess itself is awaited on the event loop; every DB / filesystem step runs
in a worker thread with its own short session (``asyncio.to_thread``), so a backlog over a large
library never stalls API requests. Library-issue reconciliation is batched: one
:class:`IssueRecorder` pass (and one ``issues_changed`` event) per batch, not per file.
"""
import asyncio
import json
import logging
import re
import shutil
import subprocess
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db.base import utcnow
from backend.db.models import EpisodeFile, MediaFile
from backend.db.repositories import LibraryIssueRepository
from backend.db.session import session_scope
from backend.domain.inspection import (
    DecodePlan,
    Finding,
    InspectionParseError,
    MediaInspection,
    classify_decode,
    compare,
    decode_plan,
    parse_ffprobe,
    resolution_class,
)
from backend.domain.issues import INSPECTION_ISSUE_TYPES  # corrupt_file is the scanner's (header check)
from backend.domain.quality import Resolution, parse_quality
from backend.services import media_file_service
from backend.services.fileops import long_path
from backend.services.issue_service import IssueRecorder

log = logging.getLogger("maniac.inspection")

_version_cache: dict[str, str | None] = {}


def ffprobe_path() -> str | None:
    settings = get_settings()
    if settings.ffprobe_path:
        p = settings.ffprobe_path.strip()
        if Path(p).is_file():
            return p
        resolved = shutil.which(p)
        if resolved:
            return resolved
        return None
    return shutil.which("ffprobe")


def ffprobe_version(binary: str | None = None) -> str | None:
    exe = binary or ffprobe_path()
    if not exe:
        return None
    if exe in _version_cache:
        return _version_cache[exe]
    try:
        res = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=3, check=False)
        first = (res.stdout or "").splitlines()[0] if res.stdout else ""
        m = re.search(r"ffprobe version\s+([^\s]+)", first)
        ver = m.group(1) if m else (first.strip()[:64] or None)
    except Exception:
        ver = None
    _version_cache[exe] = ver
    return ver


_MAX_OUTPUT = 4 * 1024 * 1024
DECODE_TIMEOUT = 180.0  # three 10 s windows of 4K HEVC decode in well under a minute on modest CPUs


def _input_args(path: Path) -> list[str]:
    # `file:` + a file-only protocol whitelist: a crafted name (concat:, http://, subfile,,…) can never
    # make ffprobe read another resource. Every invocation goes through here.
    return ["-protocol_whitelist", "file", "-i", f"file:{long_path(path)}"]


def _probe_argv(ffprobe: str, path: Path) -> list[str]:
    return [ffprobe, "-v", "error", "-hide_banner", "-print_format", "json",
            "-show_format", "-show_streams", "-show_chapters", *_input_args(path)]


def _decode_argv(ffprobe: str, path: Path, intervals: str) -> list[str]:
    # -count_frames makes ffprobe *decode* the selected windows; decoder errors land on stderr
    return [ffprobe, "-v", "error", "-hide_banner", "-read_intervals", intervals, "-count_frames",
            "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-print_format", "json",
            *_input_args(path)]


async def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap it, or the killed ffprobe lingers as a zombie
        raise RuntimeError("ffprobe timeout")
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode(errors="replace")


def _run_sync(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Blocking twin of :func:`_run` for the import path, which already runs in a worker thread."""
    try:
        res = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timeout")
    return res.returncode, res.stdout.decode("utf-8", errors="replace"), res.stderr.decode(errors="replace")


def _parse_probe_output(returncode: int, out: str, err: str) -> dict[str, Any]:
    if returncode != 0:
        raise RuntimeError(f"ffprobe failed: {err}")
    if len(out) > _MAX_OUTPUT:
        raise RuntimeError("ffprobe output exceeded 4MB limit")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe output is not valid JSON: {e}")


def _decode_result(out: str, err: str, plan: DecodePlan, inspection: MediaInspection) -> Finding | None:
    frames = None
    try:
        streams = json.loads(out[:_MAX_OUTPUT] or "{}").get("streams") or []
        if streams and str(streams[0].get("nb_read_frames", "")).isdigit():
            frames = int(streams[0]["nb_read_frames"])
    except (json.JSONDecodeError, AttributeError):
        pass
    return classify_decode(err, frames, plan, inspection.video.frame_rate if inspection.video else None)


async def probe(path: Path, timeout: float = 30) -> dict[str, Any]:
    ffprobe = ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe not found")
    return _parse_probe_output(*await _run(_probe_argv(ffprobe, path), timeout))


def probe_sync(path: Path, timeout: float = 30) -> dict[str, Any]:
    ffprobe = ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe not found")
    return _parse_probe_output(*_run_sync(_probe_argv(ffprobe, path), timeout))


async def decode_check(path: Path, inspection: MediaInspection, timeout: float = DECODE_TIMEOUT) -> Finding | None:
    """Quick decode test (see ``domain.inspection.decode_plan``); None = sampled windows decoded cleanly."""
    ffprobe = ffprobe_path()
    if not ffprobe or inspection.video is None:
        return None
    plan = decode_plan(inspection.duration_seconds)
    _, out, err = await _run(_decode_argv(ffprobe, path, plan.intervals), timeout)
    return _decode_result(out, err, plan, inspection)


def decode_check_sync(path: Path, inspection: MediaInspection, timeout: float = DECODE_TIMEOUT) -> Finding | None:
    ffprobe = ffprobe_path()
    if not ffprobe or inspection.video is None:
        return None
    plan = decode_plan(inspection.duration_seconds)
    _, out, err = _run_sync(_decode_argv(ffprobe, path, plan.intervals), timeout)
    return _decode_result(out, err, plan, inspection)


# --------------------------------------------------------------------------- pre-import check
@dataclass(frozen=True)
class PreImportCheck:
    """Result of probing a download before it is placed (``inspect_before_import``)."""
    inspection: MediaInspection | None = None
    rejection: str | None = None  # set = do not import
    evidence: dict[str, Any] = field(default_factory=dict)


def pre_import_check(path: Path) -> PreImportCheck | None:
    """Probe (and optionally decode-test) a download before import. Blocking; call from a worker thread.

    Returns None when the check is off or couldn't run (no binary, timeout, odd output): the import
    then proceeds exactly as without it – the check only ever *rejects* on positive evidence.
    """
    settings = get_settings()
    if not (settings.inspection_enabled and settings.inspect_before_import and ffprobe_path()):
        return None
    try:
        inspection = parse_ffprobe(probe_sync(path))
    except RuntimeError as e:
        if "Invalid data found when processing input" in str(e):
            return PreImportCheck(rejection="ffprobe reported invalid data – the file is unreadable",
                                  evidence={"file": path.name, "check": "probe", "error": str(e)[:300]})
        log.warning("pre-import probe of %s did not run (%s); importing without it", path.name, e)
        return None
    except InspectionParseError as e:
        log.warning("pre-import probe of %s returned unparseable output (%s); importing without it", path.name, e)
        return None
    if inspection.video is None:
        return PreImportCheck(inspection, "No video stream found", {"file": path.name, "check": "probe"})
    if settings.inspection_decode_test:
        try:
            finding = decode_check_sync(path, inspection)
        except RuntimeError as e:
            log.warning("pre-import decode test of %s did not finish (%s); not rejecting on it", path.name, e)
            finding = None
        if finding is not None:
            return PreImportCheck(inspection, finding.message, {"file": path.name, "check": "decode", **finding.data})
    return PreImportCheck(inspection)


def expected_runtime_minutes(runtimes: list[int | None], fallback: int | None) -> int | None:
    """Expected runtime of a file covering these items (several for a multi-episode file).

    Each missing per-item runtime falls back to ``fallback`` (the series runtime); unknown → None.
    """
    resolved = [r or fallback for r in runtimes]
    if not resolved or any(not r for r in resolved):
        return None
    return sum(resolved)


def truncation_finding(inspection: MediaInspection, expected_min: int | None, size_bytes: int, *, is_movie: bool) -> Finding | None:
    """The domain's truncation rule (short *and* far too small) on its own, for the pre-import gate."""
    findings = compare(parse_quality(""), inspection, expected_runtime_min=expected_min, preferred_audio_languages=[],
                       is_movie=is_movie, file_size_bytes=size_bytes)
    return next((f for f in findings if f.kind == "truncated_file"), None)


@dataclass(frozen=True)
class InspectionOutcome:
    status: Literal["inspected", "skipped", "error"]
    error: str | None = None


@dataclass
class _IssueBatch:
    """Issue reports collected while inspecting, reconciled in one pass by :func:`_reconcile`."""
    touched: list[tuple[str, int]] = field(default_factory=list)  # (media_type, media_id) fully re-checked
    touched_paths: list[tuple[str, str]] = field(default_factory=list)  # (media_type, path) file-scoped
    reports: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.touched or self.touched_paths or self.reports)


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
        for media_type, path in batch.touched_paths:
            recorder(media_type).touch_path(path, *INSPECTION_ISSUE_TYPES)
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
        # a multi-episode file (S01E01-E02) runs for all of its episodes, not just the first
        links = db.scalars(select(EpisodeFile).where(EpisodeFile.media_file_id == media_file.id)).all()
        series_runtime = media_file.series.runtime_minutes if media_file.series else None
        expected_runtime_min = expected_runtime_minutes([ef.episode.runtime_minutes for ef in links if ef.episode], series_runtime)

    claimed = parse_quality(media_file.quality or "")

    return compare(
        claimed,
        inspection,
        expected_runtime_min=expected_runtime_min,
        preferred_audio_languages=settings.preferred_audio_languages,
        is_movie=is_movie,
        runtime_tolerance_episode_pct=settings.runtime_tolerance_episode_pct,
        runtime_tolerance_movie_pct=settings.runtime_tolerance_movie_pct,
        file_size_bytes=media_file.size_bytes,
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
            ep_id = None
            if mf.media_type == "tv":
                ef = db.scalar(select(EpisodeFile).where(EpisodeFile.media_file_id == mf.id).limit(1))
                ep_id = ef.episode_id if ef else None
            batch.reports.append({"issue_type": "invalid_media", "media_type": mf.media_type, "media_id": _media_id(mf),
                                  "episode_id": ep_id, "path": mf.path, "details": "ffprobe reported invalid data",
                                  "data": {"reason": "invalid_data"}})


@dataclass(frozen=True)
class _DecodeRun:
    finding: Finding | None  # None = the sampled windows decoded cleanly


def _carried_decode_reports(db: Session, mf: MediaFile, media_id: int | None) -> list[Finding]:
    """Open decode-test findings for this exact file content, re-reported when this pass didn't decode.

    Otherwise a later header-only inspection (the backlog, or with the test switched off) would
    auto-resolve damage it never looked for. Tied to size+mtime, so replaced content drops them.
    """
    rows = LibraryIssueRepository(db).for_file(mf.media_type, media_id, mf.path, ("truncated_file", "invalid_media"), ("open",))
    return [Finding(r.issue_type, r.severity, r.details or "", r.data) for r in rows
            if (r.data or {}).get("source") == "decode"
            and (r.data or {}).get("size") == mf.size_bytes and (r.data or {}).get("mtime") == mf.mtime]


def _store(media_file_id: int, payload: dict[str, Any], batch: _IssueBatch, decode: _DecodeRun | None = None) -> InspectionOutcome:
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
        else:
            mf.video_codec = mf.video_width = mf.video_height = mf.video_bit_depth = None
            mf.hdr_format = mf.video_resolution = None

        if inspection.audio:
            primary_audio = next((a for a in inspection.audio if a.default), inspection.audio[0])
            mf.audio_codec = primary_audio.codec
            mf.audio_channels = primary_audio.channels
            mf.audio_languages = sorted({a.language for a in inspection.audio if a.language})
        else:
            mf.audio_codec = None
            mf.audio_channels = None
            mf.audio_languages = []

        mf.subtitle_languages = sorted({s.language for s in inspection.subtitles if s.language})
        mf.mediainfo = asdict(inspection)
        # nothing in the name says what this is (a DVR recording, "Show - S01E01.ts"): label it by what was measured
        if not mf.quality_locked and parse_quality(mf.quality or "").quality.weight == 0 \
                and mf.video_resolution not in (None, Resolution.UNKNOWN.value):
            label = media_file_service.relabel(db, mf, Resolution(mf.video_resolution))
            log.info("Classified %s as %s from its measured resolution", mf.path, label)

        media_id = _media_id(mf)
        episode_id = None
        if mf.media_type == "tv":
            ef = db.scalar(select(EpisodeFile).where(EpisodeFile.media_file_id == mf.id).limit(1))
            episode_id = ef.episode_id if ef else None

        if mf.path:
            batch.touched_paths.append((mf.media_type, mf.path))
        if mf.media_type == "movie" and media_id is not None and (not mf.movie or len(mf.movie.files) <= 1):
            batch.touched.append((mf.media_type, media_id))

        findings = evaluate_findings(db, mf, inspection)
        if decode is None:
            findings = _carried_decode_reports(db, mf, media_id) + findings
        elif decode.finding is not None:  # first, so it wins over a same-type duration finding
            stamped = {**decode.finding.data, "size": mf.size_bytes, "mtime": mf.mtime}
            findings = [Finding(decode.finding.kind, decode.finding.severity, decode.finding.message, stamped)] + findings
        for f in findings:
            batch.reports.append({
                "issue_type": f.kind,
                "media_type": mf.media_type,
                "media_id": media_id,
                "episode_id": episode_id,
                "path": mf.path,
                "details": f.message,
                "data": f.data,
                "severity": f.severity,
            })
    bus.publish(EventType.MEDIA_INSPECTED, media_file_id=media_file_id)
    return InspectionOutcome("inspected")


# --------------------------------------------------------------------------- async entry points
async def _inspect(media_file_id: int, force: bool, batch: _IssueBatch, *, decode: bool = False) -> InspectionOutcome:
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

    decode_result: _DecodeRun | None = None
    if decode and get_settings().inspection_decode_test:
        try:
            inspection = parse_ffprobe(payload)
            async with get_semaphore():
                decode_result = _DecodeRun(await decode_check(path, inspection))
        except (InspectionParseError, RuntimeError) as e:  # a slow/odd decode must not fail the inspection
            log.warning("decode test of media file %s did not finish: %s", media_file_id, e)
    return await asyncio.to_thread(_store, media_file_id, payload, batch, decode_result)


async def inspect_file(media_file_id: int, *, force: bool = False, decode: bool = True) -> InspectionOutcome:
    """Inspect one file. Runs the quick decode test too when enabled (the backlog never does)."""
    batch = _IssueBatch()
    try:
        return await _inspect(media_file_id, force, batch, decode=decode)
    finally:
        await asyncio.to_thread(_reconcile, batch)


def _needs_inspection(scope: str = "all"):
    # Failed files (inspection_error set) wait until they change – attach_*_file clears the error then.
    # Without this guard a file whose size changed but can't be probed is re-picked by every backlog
    # batch, and the backlog job re-queues itself every 10 seconds forever.
    conds = [
        MediaFile.inspection_error.is_(None),
        or_(MediaFile.inspected_at.is_(None), MediaFile.inspected_size != MediaFile.size_bytes, MediaFile.inspected_mtime != MediaFile.mtime),
    ]
    if scope == "movies":
        conds.append(MediaFile.media_type == "movie")
    elif scope == "tv":
        conds.append(MediaFile.media_type == "tv")
    return and_(*conds)


def _reset_inspected(scope: str = "all") -> None:
    with session_scope() as db:
        stmt = update(MediaFile).values(inspected_at=None, inspection_error=None)
        if scope == "movies":
            stmt = stmt.where(MediaFile.media_type == "movie")
        elif scope == "tv":
            stmt = stmt.where(MediaFile.media_type == "tv")
        db.execute(stmt)


def _pending_ids(limit: int, scope: str = "all") -> list[int]:
    with session_scope() as db:
        return list(db.scalars(select(MediaFile.id).where(_needs_inspection(scope)).order_by(MediaFile.id).limit(limit)))


def _mark_internal_error(media_file_id: int, exc: Exception) -> None:
    """An unexpected crash must still take the file out of the backlog, or it would be retried forever."""
    try:
        with session_scope() as db:
            mf = db.get(MediaFile, media_file_id)
            if mf is not None:
                mf.inspection_error = f"Internal error: {exc}"[:512]
    except Exception:
        log.exception("Could not record inspection error for media file %s", media_file_id)


async def inspect_backlog(limit: int = 100, progress=None, *, scope: str = "all", only_uninspected: bool = True) -> dict[str, Any]:
    if not get_settings().inspection_enabled or not ffprobe_path():
        return {"processed": 0, "status": "disabled_or_missing_binary"}

    if not only_uninspected:
        await asyncio.to_thread(_reset_inspected, scope)

    ids = await asyncio.to_thread(_pending_ids, limit, scope)
    if not ids:
        return {"processed": 0, "status": "complete"}

    batch = _IssueBatch()
    workers = asyncio.Semaphore(max(1, get_settings().inspection_concurrency))
    done = errors = 0

    async def one(media_file_id: int) -> None:
        nonlocal done, errors
        async with workers:
            try:
                outcome = await _inspect(media_file_id, False, batch)  # header-only: no decode test in the backlog
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

    has_more = bool(await asyncio.to_thread(_pending_ids, 1, scope))
    return {
        "processed": done,
        "errors": errors,
        "status": "more_pending" if has_more else "complete"
    }
