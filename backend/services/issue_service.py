"""Library issues: reconciliation during detection passes, summaries and typed resolve actions.

Issues have a stable identity (``fingerprint``) and a lifecycle (open / resolved / dismissed), so
a scan *reconciles* what it finds against what is already stored instead of rebuilding the list:

* still detected + open      -> ``last_seen_at`` / ``details`` / ``data`` refreshed
* still detected + dismissed -> stays dismissed, unless ``data["signature"]`` changed materially
* newly detected             -> inserted
* not detected any more      -> open rows auto-resolve, but only for media the pass actually
  visited (``touch``) so incremental scans never resolve issues for folders they skipped.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.events import EventType, bus
from backend.core.logging import redact
from backend.core.observability import LIBRARY_ISSUES_OPEN
from backend.core.settings import get_settings
from backend.db import models
from backend.db.base import utcnow
from backend.db.repositories import MediaFileRepository, MovieRepository, SeriesRepository
from backend.db.session import session_scope
from backend.domain.issues import REGISTRY, IssueType, Status, default_severity, fingerprint, spec_for
from backend.services import fileops, media_file_service
from backend.services.audit import Actor, audit

log = logging.getLogger("maniac.issues")

ALL = "*"
OPEN, RESOLVED, DISMISSED = Status.OPEN.value, Status.RESOLVED.value, Status.DISMISSED.value  # plain str for every DB driver
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


class IssueActionError(ValueError):
    """Unknown or disallowed resolve action (mapped to HTTP 422)."""


@dataclass
class RecorderStats:
    new: int = 0
    still_open: int = 0
    auto_resolved: int = 0
    reopened: int = 0
    created: list[models.LibraryIssue] = field(default_factory=list, repr=False)  # rows inserted by this pass

    def changed(self) -> bool:
        return bool(self.new or self.auto_resolved or self.reopened)


@dataclass
class _Reported:
    issue_type: str
    media_type: str | None
    media_id: int | None
    episode_id: int | None
    path: str | None
    details: str | None
    data: dict[str, Any] | None
    severity: str


@dataclass
class IssueRecorder:
    """Collects the issues found by one detection pass and reconciles them against stored rows."""

    db: Session
    media_type: str
    _reported: dict[str, _Reported] = field(default_factory=dict)
    _touched: dict[str, set[int]] = field(default_factory=dict)  # issue type (or ALL) -> media ids
    _touched_paths: dict[str, set[str]] = field(default_factory=dict)  # issue type (or ALL) -> normalised path keys
    _touched_all: set[str] = field(default_factory=set)  # issue types (or ALL) reconciled for every media

    def report(self, issue_type: str, media_type: str | None, media_id: int | None, path: str | None, details: str | None = None, *,
               data: dict[str, Any] | None = None, episode_id: int | None = None, discriminator: str | None = None,
               severity: str | None = None) -> None:
        issue_type = str(issue_type)
        fp = fingerprint(issue_type, media_type, media_id, path, discriminator)
        if fp in self._reported:  # e.g. a multi-episode file reported once per episode
            return
        self._reported[fp] = _Reported(issue_type, media_type, media_id, episode_id, path[:1024] if path else None, details, data,
                                       severity or default_severity(issue_type))

    def touch(self, media_id: int, *issue_types: str) -> None:
        """Declare that ``media_id`` was fully inspected for ``issue_types`` (default: all)."""
        for t in map(str, issue_types or (ALL,)):
            self._touched.setdefault(t, set()).add(media_id)

    def touch_path(self, path: str, *issue_types: str) -> None:
        """Declare that ``path`` was fully inspected for ``issue_types`` (default: all)."""
        from backend.domain.parser import normalise_path_key

        norm = normalise_path_key(path)
        for t in map(str, issue_types or (ALL,)):
            self._touched_paths.setdefault(t, set()).add(norm)

    def touch_all(self, *issue_types: str) -> None:
        """Declare that a whole-library detector ran for ``issue_types`` (default: all)."""
        self._touched_all.update(map(str, issue_types or (ALL,)))

    def _in_scope(self, row: models.LibraryIssue) -> bool:
        from backend.domain.parser import normalise_path_key

        norm_path = normalise_path_key(row.path) if row.path else None
        for key in (ALL, row.issue_type):
            if key in self._touched_all:
                return True
            if row.media_id is not None and row.media_id in self._touched.get(key, ()):
                return True
            if norm_path and norm_path in self._touched_paths.get(key, ()):
                return True
        return False

    def finish(self) -> RecorderStats:
        stats = RecorderStats()
        now = utcnow()
        stored: dict[str, models.LibraryIssue] = {}
        rows = self.db.scalars(
            select(models.LibraryIssue).where(models.LibraryIssue.media_type == self.media_type,
                                              models.LibraryIssue.status.in_((OPEN, DISMISSED)))
        ).all()
        for row in rows:
            fp = row.fingerprint or fingerprint(row.issue_type, row.media_type, row.media_id, row.path)
            if fp not in stored or row.status == OPEN:  # an open row wins over a dismissed twin
                stored[fp] = row

        for fp, rep in self._reported.items():
            row = stored.get(fp)
            if row is None:
                row = models.LibraryIssue(
                    issue_type=rep.issue_type, media_type=rep.media_type, media_id=rep.media_id, episode_id=rep.episode_id,
                    path=rep.path, details=rep.details, data=rep.data, severity=rep.severity, fingerprint=fp,
                    status=OPEN, resolved=False, detected_at=now, first_detected_at=now, last_seen_at=now,
                )
                self.db.add(row)
                stats.created.append(row)
                stats.new += 1
                continue
            old_signature = (row.data or {}).get("signature")
            row.last_seen_at = now
            data = rep.data
            if (row.data or {}).get("search") and not (data or {}).get("search"):  # detectors don't know about searches
                data = {**(data or {}), "search": row.data["search"]}
            row.details, row.data, row.severity, row.fingerprint = rep.details, data, rep.severity, fp
            row.episode_id = rep.episode_id or row.episode_id
            if row.status == DISMISSED:
                new_signature = (rep.data or {}).get("signature")
                if new_signature is not None and new_signature != old_signature:
                    row.status, row.resolved, row.resolved_at, row.resolution = OPEN, False, None, None
                    stats.reopened += 1
                # otherwise it stays dismissed – that's the user's decision
            else:
                stats.still_open += 1

        for fp, row in stored.items():
            if row.status == OPEN and fp not in self._reported and self._in_scope(row):
                row.status, row.resolved, row.resolved_at, row.resolution = RESOLVED, True, now, "auto"
                stats.auto_resolved += 1

        self.db.flush()
        publish_changed(self.db, stats, self.media_type)
        return stats


# --------------------------------------------------------------------------- summaries / metrics
def summary(db: Session) -> dict:
    by_type: dict[str, dict] = {}
    by_severity: dict[str, int] = {}
    rows = db.execute(
        select(models.LibraryIssue.issue_type, models.LibraryIssue.status, models.LibraryIssue.severity, func.count(models.LibraryIssue.id))
        .where(models.LibraryIssue.status.in_((OPEN, DISMISSED)))
        .group_by(models.LibraryIssue.issue_type, models.LibraryIssue.status, models.LibraryIssue.severity)
    ).all()
    for issue_type, status, severity, n in rows:
        entry = by_type.setdefault(issue_type, {"open": 0, "dismissed": 0, "severity": None})
        entry[status] += n
        if status == OPEN:
            by_severity[severity] = by_severity.get(severity, 0) + n
            if entry["severity"] is None or SEVERITY_RANK.get(severity, 1) > SEVERITY_RANK.get(entry["severity"], 1):
                entry["severity"] = severity  # worst open severity for the type
    for issue_type, entry in by_type.items():
        entry["severity"] = entry["severity"] or default_severity(issue_type)
    return {"by_type": by_type, "by_severity": by_severity}


def refresh_metrics(db: Session) -> None:
    rows = db.execute(
        select(models.LibraryIssue.issue_type, models.LibraryIssue.severity, func.count(models.LibraryIssue.id))
        .where(models.LibraryIssue.status == OPEN).group_by(models.LibraryIssue.issue_type, models.LibraryIssue.severity)
    ).all()
    LIBRARY_ISSUES_OPEN.clear()
    for issue_type, severity, n in rows:
        LIBRARY_ISSUES_OPEN.labels(type=issue_type, severity=severity).set(n)


def publish_changed(db: Session, stats: RecorderStats | None = None, media_type: str | None = None) -> None:
    refresh_metrics(db)
    payload: dict[str, Any] = {"media_type": media_type}
    if stats:
        payload.update(new=stats.new, still_open=stats.still_open, auto_resolved=stats.auto_resolved, reopened=stats.reopened)
    bus.publish(EventType.ISSUES_CHANGED, **payload)


def serialize(issue: models.LibraryIssue, title: str | None = None) -> dict:
    spec = spec_for(issue.issue_type)
    first = issue.first_detected_at or issue.detected_at
    return {
        "id": issue.id, "issue_type": issue.issue_type,
        "label": spec.label if spec else issue.issue_type.replace("_", " ").capitalize(),
        "media_type": issue.media_type, "media_id": issue.media_id, "title": title, "episode_id": issue.episode_id, "path": issue.path,
        "details": issue.details, "data": issue.data, "severity": issue.severity, "status": issue.status,
        "detected_at": first, "first_detected_at": first, "last_seen_at": issue.last_seen_at,
        "resolved_at": issue.resolved_at, "resolution": issue.resolution,
        "actions": list(spec.allowed_actions()) if spec else ["dismiss", "reopen"],
    }


def serialize_page(db: Session, issues: Sequence[models.LibraryIssue]) -> list[dict]:
    """``serialize`` for a page of issues, with movie / series titles fetched in one query per media type."""
    titles = {
        "movie": MovieRepository(db).titles([i.media_id for i in issues if i.media_type == "movie" and i.media_id is not None]),
        "tv": SeriesRepository(db).titles([i.media_id for i in issues if i.media_type == "tv" and i.media_id is not None]),
    }
    return [serialize(i, titles.get(i.media_type, {}).get(i.media_id)) for i in issues]


# --------------------------------------------------------------------------- resolve actions
def allowed_actions(issue: models.LibraryIssue) -> tuple[str, ...]:
    spec = spec_for(issue.issue_type)
    return spec.allowed_actions() if spec else ("dismiss", "reopen")


def _close(issue: models.LibraryIssue, status: str, resolution: str) -> None:
    issue.status, issue.resolved, issue.resolved_at, issue.resolution = status, True, utcnow(), resolution


def resolve(db: Session, issue: models.LibraryIssue, action: str, params: dict | None, actor: Actor) -> str:
    """Apply a typed action to an issue; returns a human-readable outcome. Raises IssueActionError."""
    if action not in allowed_actions(issue):
        raise IssueActionError(f"Action '{action}' is not allowed for {issue.issue_type} issues")
    message = _DISPATCH[action](db, issue, params or {}, actor)
    audit(db, "media", f"issue.{action}", actor=actor, entity_type="issue", entity_id=issue.id,
          details={"issue_type": issue.issue_type, "media_type": issue.media_type, "media_id": issue.media_id, "path": issue.path})
    db.flush()
    return message


def _dismiss(db, issue, params, actor) -> str:
    if issue.status == OPEN:
        _close(issue, DISMISSED, "dismissed")
    return "Issue dismissed"


def _reopen(db, issue, params, actor) -> str:
    if issue.status != OPEN:
        issue.status, issue.resolved, issue.resolved_at, issue.resolution = OPEN, False, None, None
        issue.last_seen_at = utcnow()
    return "Issue reopened"


def _search_target(db: Session, issue: models.LibraryIssue) -> dict:
    if issue.media_type == "movie":
        return {"media_type": "movie", "media_id": issue.media_id}
    target: dict[str, Any] = {"media_type": "tv", "media_id": issue.media_id}
    if issue.episode_id and (ep := db.get(models.Episode, issue.episode_id)):
        target.update(season=ep.season_number, episodes=[ep.episode_number])
    return target


def _search(db, issue, params, actor) -> str:
    from backend.workers.queue import job_queue

    if issue.media_id is None:
        raise IssueActionError("Issue is not linked to a title")
    job_queue.enqueue("automatic_search", {**_search_target(db, issue), "issue_id": issue.id, "user_id": actor.user_id,
                                           "username": actor.username},
                      dedupe_key=f"issue-search:{issue.media_type}:{issue.media_id}:{issue.episode_id}")
    _note_search(issue, "queued")
    return "Search queued"


def _rescan(db, issue, params, actor) -> str:
    from backend.workers.queue import job_queue

    if issue.media_id is None:
        raise IssueActionError("Issue is not linked to a title")
    scope = "movies" if issue.media_type == "movie" else "tv"
    job_queue.enqueue("library_scan", {"scope": scope, "incremental": False, "media_id": issue.media_id},
                      dedupe_key=f"scan:{scope}:{issue.media_id}")
    return "Rescan queued"


def _unmonitor(db, issue, params, actor) -> str:
    from backend.services import media_service

    if issue.media_id is None:
        raise IssueActionError("Issue is not linked to a title")
    if issue.media_type == "movie":
        movie = db.get(models.Movie, issue.media_id)
        if movie is not None:
            media_service.update_movie(db, movie, {"monitored": False}, actor)
    else:
        ep = db.get(models.Episode, issue.episode_id) if issue.episode_id else None
        if ep is not None:
            ep.monitored = False
        else:
            series = db.get(models.Series, issue.media_id)
            if series is not None:
                media_service.update_series(db, series, {"monitored": False}, actor)
    _close(issue, RESOLVED, "unmonitor")
    return "Unmonitored"


def _delete_file(db, issue, params, actor) -> str:
    if not issue.path:
        raise IssueActionError("Issue has no file to delete")
    from backend.services import media_service

    path = Path(issue.path)
    mf = MediaFileRepository(db).by_path(issue.path)
    if mf is not None:
        recycled = media_service.delete_media_file(db, mf, actor=actor)
    else:
        recycled = fileops.recycle(path, Path(get_settings().recycle_bin)) if path.is_file() else None
    _close(issue, RESOLVED, "delete_file")
    return f"File moved to recycle bin ({recycled.name})" if recycled else "File record removed"


def _match(db, issue, params, actor) -> str:
    return "Open the Import page to match this folder"  # UI-only action; no state change


def _relabel(db, issue, params, actor) -> str:
    from backend.domain.quality import Resolution

    if not issue.path:
        raise IssueActionError("Issue has no file to relabel")
    mf = MediaFileRepository(db).by_path(issue.path)
    if mf is None:
        raise IssueActionError("Media file record not found")
    actual_str = (issue.data or {}).get("actual") or mf.video_resolution
    if not actual_str:
        raise IssueActionError("Actual resolution is unknown")
    try:
        target_res = Resolution(actual_str)
    except ValueError:
        raise IssueActionError(f"Unrecognised resolution: {actual_str}")

    new_label = media_file_service.relabel(db, mf, target_res)
    _close(issue, RESOLVED, "relabel")
    return f"Relabelled quality to {new_label}"


_DISPATCH = {
    "dismiss": _dismiss, "reopen": _reopen, "search": _search, "rescan": _rescan,
    "unmonitor": _unmonitor, "delete_file": _delete_file, "match": _match, "relabel": _relabel,
}
assert {a for s in REGISTRY.values() for a in s.actions} <= set(_DISPATCH), "registry action without a handler"


# --------------------------------------------------------------------------- searches for missing files
# More files going missing in one scan than this looks like an unmounted drive or share, not deletions;
# searching for all of them would re-download the library, so those issues are only noted.
AUTO_SEARCH_LIMIT = 10
MAX_RECORDED_RELEASES = 50


def _note_search(issue: models.LibraryIssue, status: str, **fields: Any) -> None:
    """Latest search for the issue, in ``data["search"]``: queued | skipped | grabbed | held | not_found | failed."""
    issue.data = {**(issue.data or {}), "search": {"status": status, "at": utcnow().isoformat(), **fields}}


def _monitored(db: Session, issue: models.LibraryIssue) -> bool:
    if issue.media_type == "movie":
        movie = db.get(models.Movie, issue.media_id)
        return bool(movie and movie.monitored)
    series = db.get(models.Series, issue.media_id)
    episode = db.get(models.Episode, issue.episode_id) if issue.episode_id else None
    return bool(series and series.monitored and (episode is None or episode.monitored))


def plan_missing_file_searches(db: Session, created: Sequence[models.LibraryIssue]) -> list[tuple[dict, str]]:
    """``automatic_search`` jobs (payload, dedupe key) for files a scan just found missing; notes the decision on each issue.

    Hand the result to ``enqueue_searches`` only after the scan's session has committed: the job queue writes
    through its own session, which would wait on the scan's SQLite write lock.
    """
    missing = [i for i in created if i.issue_type == IssueType.MISSING_FILE and i.media_id is not None]
    if not missing or not get_settings().search_missing_files:
        return []
    if len(missing) > AUTO_SEARCH_LIMIT:
        log.warning("%d files went missing in one scan; not searching automatically (is the media root mounted?)", len(missing))
        for issue in missing:
            _note_search(issue, "skipped", reason=f"Not searched automatically: {len(missing)} files went missing in one scan, "
                                                    "which usually means a drive or share isn't mounted")
        return []
    jobs = []
    for issue in missing:
        if not _monitored(db, issue):
            _note_search(issue, "skipped", reason="Not searched automatically: not monitored")
            continue
        _note_search(issue, "queued")
        jobs.append(({**_search_target(db, issue), "issue_id": issue.id},
                     f"issue-search:{issue.media_type}:{issue.media_id}:{issue.episode_id}"))
    return jobs


def enqueue_searches(jobs: Sequence[tuple[dict, str]]) -> None:
    from backend.workers.queue import job_queue

    for payload, dedupe_key in jobs:
        job_queue.enqueue("automatic_search", payload, trigger="event", dedupe_key=dedupe_key)


def _search_outcome(result: dict, error: str | None) -> tuple[str, dict]:
    if error:
        return "failed", {"reason": redact(error)}
    if result.get("grabbed"):
        grabbed = result["grabbed"]
        return "grabbed", {"grabbed": grabbed if isinstance(grabbed, list) else [grabbed], "quality": result.get("quality")}
    if result.get("held"):
        return "held", {"reason": result.get("reason")}
    if "indexers" not in result:  # e.g. already in the download queue
        return "skipped", {"reason": result.get("reason")}
    indexers = [{**r, "error": redact(r["error"]) if r.get("error") else None} for r in result["indexers"]]
    releases = result.get("rejected") or []
    return "not_found", {
        "reason": "No indexers are enabled for search" if not indexers else result.get("reason") or "No acceptable releases found",
        "indexers": indexers, "candidates": result.get("candidates", len(releases)), "releases": releases[:MAX_RECORDED_RELEASES],
    }


def record_search(issue_id: int, result: dict | None, error: str | None = None) -> None:
    """Note an ``automatic_search`` outcome (or its error) on the issue that asked for it."""
    with session_scope() as db:
        issue = db.get(models.LibraryIssue, issue_id)
        if issue is None:
            return
        status, fields = _search_outcome(result or {}, error)
        _note_search(issue, status, **fields)
        db.commit()  # before the event, so the refetch it triggers sees the note
        publish_changed(db, media_type=issue.media_type)
