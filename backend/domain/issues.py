"""Library issue vocabulary: types, severities, allowed resolve actions and stable fingerprints.

Pure logic, no I/O. The registry is served to the UI (``GET /library/issue-types``) so labels,
actions and severities are never hard-coded on the client.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from backend.domain.parser import normalise_path_key


class IssueType(StrEnum):
    MISSING_FILE = "missing_file"
    DUPLICATE_FILE = "duplicate_file"
    UPGRADE_AVAILABLE = "upgrade_available"
    CORRUPT_FILE = "corrupt_file"
    UNMATCHED_FOLDER = "unmatched_folder"
    # reserved for later features (registered so the UI can render them generically)
    QUALITY_MISMATCH = "quality_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    MISSING_AUDIO = "missing_audio"
    LANGUAGE_MISSING = "language_missing"
    MISSING_SUBTITLES = "missing_subtitles"
    # ffprobe findings. Deliberately separate from CORRUPT_FILE, which belongs to the scanner's
    # header-magic check: each detector auto-resolves only the types it owns.
    TRUNCATED_FILE = "truncated_file"
    INVALID_MEDIA = "invalid_media"
    STORAGE_CANDIDATE = "storage_optimization_candidate"


# Issue types raised by ffprobe inspection (and therefore auto-resolved by it).
INSPECTION_ISSUE_TYPES: tuple[str, ...] = tuple(t.value for t in (
    IssueType.QUALITY_MISMATCH, IssueType.RUNTIME_MISMATCH, IssueType.TRUNCATED_FILE,
    IssueType.INVALID_MEDIA, IssueType.MISSING_AUDIO, IssueType.LANGUAGE_MISSING,
))
# Issues that describe one file's contents: they follow the file when it is moved/renamed and close
# when its record is removed. Media-level types (missing_file, upgrade_available, …) are not here.
FILE_SCOPED_ISSUE_TYPES: tuple[str, ...] = (*INSPECTION_ISSUE_TYPES, IssueType.CORRUPT_FILE.value)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Status(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# Actions every issue supports; type-specific actions come from ``IssueTypeSpec.actions``.
BASE_ACTIONS = ("dismiss", "reopen")


@dataclass(frozen=True)
class IssueTypeSpec:
    type: IssueType
    label: str
    severity: Severity  # default
    actions: tuple[str, ...]  # allowed resolve actions besides dismiss/reopen
    health_component: str | None  # integrity | quality | duplicates | completeness

    def allowed_actions(self) -> tuple[str, ...]:
        return (*self.actions, *BASE_ACTIONS)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "label": self.label,
            "severity": self.severity.value,
            "actions": list(self.allowed_actions()),
            "health_component": self.health_component,
        }


def _spec(t: IssueType, label: str, sev: Severity, actions: tuple[str, ...], component: str | None) -> tuple[IssueType, IssueTypeSpec]:
    return t, IssueTypeSpec(t, label, sev, actions, component)


REGISTRY: dict[IssueType, IssueTypeSpec] = dict([
    _spec(IssueType.MISSING_FILE, "Missing file", Severity.ERROR, ("rescan", "search", "unmonitor"), "integrity"),
    _spec(IssueType.CORRUPT_FILE, "Corrupt file", Severity.ERROR, ("rescan", "search", "delete_file"), "integrity"),
    _spec(IssueType.DUPLICATE_FILE, "Duplicate files", Severity.WARNING, (), "duplicates"),  # keep_* actions arrive with 05
    _spec(IssueType.UPGRADE_AVAILABLE, "Upgrade available", Severity.INFO, ("search",), "quality"),
    _spec(IssueType.UNMATCHED_FOLDER, "Unmatched folder", Severity.WARNING, ("match",), "completeness"),  # `match` opens the Import page (UI-only, no state change)
    _spec(IssueType.QUALITY_MISMATCH, "Quality mismatch", Severity.WARNING, ("search", "relabel"), "integrity"),
    _spec(IssueType.RUNTIME_MISMATCH, "Runtime mismatch", Severity.WARNING, ("search", "match"), "integrity"),
    _spec(IssueType.TRUNCATED_FILE, "Truncated file", Severity.ERROR, ("search", "delete_file"), "integrity"),
    _spec(IssueType.INVALID_MEDIA, "Invalid media", Severity.ERROR, ("search", "delete_file"), "integrity"),
    _spec(IssueType.MISSING_AUDIO, "Missing audio", Severity.ERROR, ("search", "delete_file"), "integrity"),
    _spec(IssueType.LANGUAGE_MISSING, "Language missing", Severity.INFO, ("search",), "quality"),
    _spec(IssueType.MISSING_SUBTITLES, "Missing subtitles", Severity.INFO, (), "completeness"),
    _spec(IssueType.STORAGE_CANDIDATE, "Storage optimisation candidate", Severity.INFO, (), None),
])


def spec_for(issue_type: str) -> IssueTypeSpec | None:
    try:
        return REGISTRY[IssueType(issue_type)]
    except ValueError:
        return None


def default_severity(issue_type: str) -> str:
    spec = spec_for(issue_type)
    return (spec.severity if spec else Severity.WARNING).value


def fingerprint(issue_type: str, media_type: str | None, media_id: int | None,
                path: str | None, discriminator: str | None = None) -> str:
    """sha256 over the normalised tuple.

    ``discriminator`` separates e.g. S01E02 vs S01E03 duplicates of the same series. Paths go
    through ``normalise_path_key`` (case-folded, forward slashes) so Windows and Linux agree.
    """
    parts = (
        str(issue_type),
        media_type or "",
        "" if media_id is None else str(media_id),
        normalise_path_key(path) if path else "",
        (discriminator or "").casefold(),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
