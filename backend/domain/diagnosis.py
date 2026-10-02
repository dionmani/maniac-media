"""Import failure diagnosis vocabulary: reason codes, fix catalogue and pure classification helpers.

Pure logic, no I/O – the probe pipeline that gathers evidence lives in
``backend/services/import_diagnosis.py``. Keeping the vocabulary here means the API, the health
check and the UI all describe failures the same way.
"""
from __future__ import annotations

import errno as _errno
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ImportFailureReason(StrEnum):
    PATH_NOT_FOUND = "path_not_found"
    REMOTE_PATH_MAPPING = "remote_path_mapping"
    PERMISSION_DENIED = "permission_denied"
    DISK_FULL = "disk_full"
    CROSS_DEVICE_FAILED = "cross_device_failed"
    PATH_TOO_LONG = "path_too_long"
    DESTINATION_EXISTS = "destination_exists"
    STILL_DOWNLOADING = "still_downloading"
    NO_VIDEO_FILES = "no_video_files"
    ARCHIVE_NOT_EXTRACTED = "archive_not_extracted"
    SAMPLE_ONLY = "sample_only"
    CORRUPT_FILE = "corrupt_file"
    TOO_SMALL = "too_small"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNPARSEABLE_EPISODE = "unparseable_episode"
    NOT_MATCHED = "not_matched"
    MEDIA_DELETED = "media_deleted"
    QUALITY_NOT_ALLOWED = "quality_not_allowed"
    NOT_AN_UPGRADE = "not_an_upgrade"
    DUPLICATE = "duplicate"
    NAMING_TEMPLATE_ERROR = "naming_template_error"
    UNKNOWN = "unknown"


class FixKind(StrEnum):
    RETRY = "retry"                               # plain re-run (after the user fixed something outside)
    RETRY_WHEN_STABLE = "retry_when_stable"       # schedule re-run in N minutes
    APPLY_PATH_MAPPING = "apply_path_mapping"     # set client remote/local mapping, then retry all affected
    CREATE_FOLDER = "create_folder"               # create missing destination parent
    FORCE_IMPORT = "force_import"                 # override not_an_upgrade / quality_not_allowed for this item
    MANUAL_IMPORT = "manual_import"               # deep-link to Import page with folder prefilled
    BLOCKLIST_AND_SEARCH = "blocklist_and_search" # release is bad (corrupt/sample-only/no video)
    REMOVE = "remove"                             # drop from queue (+ optionally client data)
    OPEN_SETTINGS = "open_settings"               # link to relevant settings page (naming, root folders)


# The only fix that mutates settings today; the API requires SETTINGS_WRITE for these.
CHANGES_SETTINGS_FIX_KINDS = frozenset({FixKind.APPLY_PATH_MAPPING})


@dataclass(frozen=True)
class FixSuggestion:
    kind: FixKind
    label: str                     # "Map /downloads → D:\\Downloads on client 'qBittorrent'"
    params: dict[str, Any] = field(default_factory=dict)
    safe_auto: bool = False        # may run without a click when strategy=auto_fix
    changes_settings: bool = False
    bulk_applicable: bool = False  # one click fixes every failure with the same signature

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "params": self.params,
            "safe_auto": self.safe_auto,
            "changes_settings": self.changes_settings,
            "bulk_applicable": self.bulk_applicable,
        }


@dataclass(frozen=True)
class ImportFailureAnalysis:
    reason: ImportFailureReason
    summary: str                   # one human sentence
    evidence: dict[str, Any]       # probe results (paths, errno, free bytes…), never secrets
    fixes: tuple[FixSuggestion, ...]
    signature: str                 # groups identical root causes, e.g. "remote_path_mapping:client=2:/downloads"
    confidence: float              # 0..1, how sure the probe pipeline is
    analysed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "fixes": [f.to_dict() for f in self.fixes],
            "signature": self.signature,
            "confidence": self.confidence,
            "analysed_at": self.analysed_at,
        }


_SEP = re.compile(r"[\\/]")


def infer_path_mapping(reported: str, candidates_found: list[str]) -> tuple[str, str] | None:
    """Longest common *suffix* between the client-reported path and a path that exists locally.

    Segments are compared case-sensitively and separately per side (the client may report a
    POSIX path while the local candidate is a Windows path) – the whole point is that the
    *differing* root is where remote/local path mapping happens, so a case-only difference in
    the shared sub-path must not be folded into the match. Returns ``(remote_prefix,
    local_prefix)`` using each side's own separator style, or ``None`` when nothing lines up.
    """
    if not reported or not candidates_found:
        return None
    r_sep = "\\" if "\\" in reported and "/" not in reported else "/"
    r_segs = _SEP.split(reported)
    best: tuple[str, str] | None = None
    best_n = 0
    for candidate in candidates_found:
        if not candidate:
            continue
        c_sep = "\\" if "\\" in candidate and "/" not in candidate else "/"
        c_segs = _SEP.split(candidate)
        n = 0
        while (
            n < len(r_segs) - 1
            and n < len(c_segs) - 1
            and r_segs[len(r_segs) - 1 - n]
            and r_segs[len(r_segs) - 1 - n] == c_segs[len(c_segs) - 1 - n]
        ):
            n += 1
        if n > best_n:
            best_n = n
            remote_prefix = r_sep.join(r_segs[: len(r_segs) - n]) or r_sep
            local_prefix = c_sep.join(c_segs[: len(c_segs) - n]) or c_sep
            best = (remote_prefix, local_prefix)
    return best


def classify_oserror(errno_: int | None, winerror: int | None = None) -> ImportFailureReason:
    """Map a filesystem failure's errno/winerror onto a reason code."""
    if errno_ in (_errno.EACCES, _errno.EPERM):
        return ImportFailureReason.PERMISSION_DENIED
    if errno_ == _errno.ENOSPC:
        return ImportFailureReason.DISK_FULL
    if errno_ == _errno.EXDEV:
        return ImportFailureReason.CROSS_DEVICE_FAILED
    if errno_ == _errno.ENAMETOOLONG or winerror == 206:
        return ImportFailureReason.PATH_TOO_LONG
    if errno_ == _errno.EEXIST:
        return ImportFailureReason.DESTINATION_EXISTS
    return ImportFailureReason.UNKNOWN
