import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.settings import get_settings
from backend.db.models import MediaFile, EpisodeFile, Episode
from backend.domain.inspection import compare, parse_ffprobe, InspectionParseError
from backend.domain.quality import parse_quality
from backend.services.issue_service import IssueRecorder
from backend.services.fileops import long_path

log = logging.getLogger("maniac.inspection")

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

def evaluate_findings(db: Session, media_file: MediaFile, inspection) -> list:
    settings = get_settings()
    
    expected_runtime_min = None
    is_movie = media_file.media_type == "movie"
    if is_movie and media_file.movie:
        expected_runtime_min = media_file.movie.runtime_minutes
    elif not is_movie:
        # Get episode
        ef = db.query(EpisodeFile).filter(EpisodeFile.media_file_id == media_file.id).first()
        if ef and ef.episode:
            expected_runtime_min = ef.episode.runtime_minutes
            if not expected_runtime_min and media_file.series:
                expected_runtime_min = media_file.series.runtime_minutes
                
    claimed = parse_quality(media_file.quality or "")
    
    findings = compare(
        claimed, 
        inspection, 
        expected_runtime_min=expected_runtime_min, 
        preferred_audio_languages=settings.preferred_audio_languages,
        is_movie=is_movie
    )
    return findings

# A global semaphore for concurrency
_probe_semaphore: asyncio.Semaphore | None = None

def get_semaphore() -> asyncio.Semaphore:
    global _probe_semaphore
    if _probe_semaphore is None:
        _probe_semaphore = asyncio.Semaphore(max(1, get_settings().inspection_concurrency))
    return _probe_semaphore

async def inspect_file(db: Session, media_file_id: int, *, force: bool = False) -> InspectionOutcome:
    media_file = db.get(MediaFile, media_file_id)
    if not media_file:
        return InspectionOutcome("error", "Media file not found")
        
    if not force and media_file.inspected_at is not None:
        if media_file.inspected_size == media_file.size_bytes and media_file.inspected_mtime == media_file.mtime:
            return InspectionOutcome("skipped")
            
    path = Path(media_file.path)
    if not path.exists():
        media_file.inspection_error = "File not found on disk"
        db.commit()
        return InspectionOutcome("error", "File not found on disk")
        
    # Check for ffprobe early
    if not ffprobe_path():
        media_file.inspection_error = "ffprobe not found"
        db.commit()
        return InspectionOutcome("error", "ffprobe not found")

    sem = get_semaphore()
    try:
        async with sem:
            payload = await probe(path)
    except Exception as e:
        media_file.inspection_error = str(e)
        db.commit()
        
        # Report corrupt_file if ffprobe invalid data
        if "Invalid data found when processing input" in str(e):
            recorder = IssueRecorder(db, media_file.media_type)
            recorder.report(
                issue_type="corrupt_file",
                media_type=media_file.media_type,
                media_id=media_file.movie_id if media_file.media_type == "movie" else media_file.series_id,
                path=media_file.path,
                details="ffprobe reported invalid data"
            )
            recorder.finish()
            
        return InspectionOutcome("error", str(e))
        
    try:
        inspection = parse_ffprobe(payload)
    except InspectionParseError as e:
        media_file.inspection_error = str(e)
        db.commit()
        return InspectionOutcome("error", str(e))
        
    # Update columns
    from backend.db.base import utcnow
    media_file.inspected_at = utcnow()
    media_file.inspected_size = media_file.size_bytes
    media_file.inspected_mtime = media_file.mtime
    media_file.inspection_error = None
    media_file.container = inspection.container
    media_file.duration_seconds = inspection.duration_seconds
    media_file.overall_bitrate = inspection.overall_bitrate
    
    if inspection.video:
        media_file.video_codec = inspection.video.codec
        media_file.video_width = inspection.video.width
        media_file.video_height = inspection.video.height
        media_file.video_bit_depth = inspection.video.bit_depth
        media_file.hdr_format = inspection.video.hdr_format
        
        from backend.domain.inspection import resolution_class
        media_file.video_resolution = resolution_class(inspection.video.width, inspection.video.height).value
    
    if inspection.audio:
        primary_audio = inspection.audio[0]
        for a in inspection.audio:
            if a.default:
                primary_audio = a
                break
        media_file.audio_codec = primary_audio.codec
        media_file.audio_channels = primary_audio.channels
        
        media_file.audio_languages = list({a.language for a in inspection.audio if a.language})
        
    media_file.subtitle_languages = list({s.language for s in inspection.subtitles if s.language})
    
    # Store full dict
    from dataclasses import asdict
    media_file.mediainfo = asdict(inspection)
    
    findings = evaluate_findings(db, media_file, inspection)
    
    recorder = IssueRecorder(db, media_file.media_type)
    media_id = media_file.movie_id if media_file.media_type == "movie" else media_file.series_id
    recorder.touch(media_id, "quality_mismatch", "runtime_mismatch", "truncated", "missing_audio", "language_missing", "corrupt_file")
    
    for f in findings:
        recorder.report(
            issue_type=f.kind,
            media_type=media_file.media_type,
            media_id=media_id,
            path=media_file.path,
            details=f.message,
            data=f.data,
            severity=f.severity
        )
        
    recorder.finish()
    
    db.commit()
    bus.publish(EventType.MEDIA_INSPECTED, media_file_id=media_file.id)
    
    return InspectionOutcome("inspected")

async def inspect_backlog(db: Session, limit: int = 100, progress=None) -> dict[str, Any]:
    if not get_settings().inspection_enabled or not ffprobe_path():
        return {"processed": 0, "status": "disabled_or_missing_binary"}
        
    files = db.query(MediaFile).filter(
        ((MediaFile.inspected_at == None) & (MediaFile.inspection_error == None)) |  # failed files wait until they change
        (MediaFile.inspected_size != MediaFile.size_bytes) |
        (MediaFile.inspected_mtime != MediaFile.mtime)
    ).limit(limit).all()
    
    if not files:
        return {"processed": 0, "status": "complete"}
        
    processed = 0
    errors = 0
    
    for i, f in enumerate(files):
        try:
            outcome = await inspect_file(db, f.id)
            if outcome.status == "error":
                errors += 1
            processed += 1
        except Exception as e:
            log.error(f"Error inspecting {f.id}: {e}")
            errors += 1
            
        if progress:
            progress((i + 1) * 100 / len(files), f"Inspected {i + 1}/{len(files)} files")
            
    has_more = db.query(MediaFile).filter(
        ((MediaFile.inspected_at == None) & (MediaFile.inspection_error == None)) |  # failed files wait until they change
        (MediaFile.inspected_size != MediaFile.size_bytes) |
        (MediaFile.inspected_mtime != MediaFile.mtime)
    ).first() is not None
    
    return {
        "processed": processed,
        "errors": errors,
        "status": "more_pending" if has_more else "complete"
    }
