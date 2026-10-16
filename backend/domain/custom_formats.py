"""Custom formats & advanced release scoring.

Pure domain logic – no I/O, no ORM. Implements Radarr/Sonarr-compatible custom format
specification evaluation, per-profile scoring, and ReDoS-safe regex validation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.domain.parser import parse_episode, release_group
from backend.domain.quality import Codec, ParsedQuality, Resolution, Source, parse_quality


class SpecKind(StrEnum):
    RELEASE_TITLE = "release_title"     # regex on full release name
    RELEASE_GROUP = "release_group"     # regex on parsed group
    SOURCE = "source"                   # Source enum value
    RESOLUTION = "resolution"           # Resolution enum value
    CODEC = "codec"                     # Codec enum value
    QUALITY_MODIFIER = "quality_modifier"  # remux | proper | repack | hdr
    SIZE = "size"                       # min_gb / max_gb (per episode for TV)
    INDEXER = "indexer"                 # indexer id
    PROTOCOL = "protocol"               # torrent | usenet


VALID_QUALITY_MODIFIERS = {"remux", "proper", "repack", "hdr"}
VALID_PROTOCOLS = {"torrent", "usenet"}
VALID_SOURCES = {
    **{s.value.lower(): s.value for s in Source if s != Source.UNKNOWN},
    "webdl": Source.WEBDL.value,
}
VALID_RESOLUTIONS = {r.value.lower(): r.value for r in Resolution if r != Resolution.UNKNOWN}
VALID_CODECS = {c.value.lower(): c.value for c in Codec if c != Codec.UNKNOWN}


@dataclass(frozen=True)
class Specification:
    name: str
    kind: SpecKind
    value: Any  # regex string, enum value, int, or {min_gb, max_gb}
    negate: bool = False
    required: bool = False
    disabled: bool = False  # used when importing unsupported future spec kinds

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind.value if isinstance(self.kind, SpecKind) else str(self.kind),
            "value": self.value,
            "negate": self.negate,
            "required": self.required,
        }
        if self.disabled:
            out["disabled"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Specification:
        kind_raw = data.get("kind", "")
        disabled = bool(data.get("disabled", False))
        try:
            kind = SpecKind(kind_raw)
        except ValueError:
            # Keep unsupported imported specs disabled so they don't crash or affect matching
            kind = SpecKind.RELEASE_TITLE
            disabled = True
        return cls(
            name=str(data.get("name") or kind_raw),
            kind=kind,
            value=data.get("value", ""),
            negate=bool(data.get("negate", False)),
            required=bool(data.get("required", False)),
            disabled=disabled,
        )


@dataclass(frozen=True)
class CustomFormat:
    id: int
    name: str
    specifications: tuple[Specification, ...]
    include_in_rename: bool = False


@dataclass(frozen=True)
class ReleaseFacts:
    """What the matcher sees; built from ReleaseInfo or a MediaFile / release title."""

    title: str
    group: str | None
    quality: ParsedQuality
    size_bytes: int = 0
    episode_count: int = 1
    indexer_id: int | None = None
    protocol: str | None = None

    @classmethod
    def from_title(
        cls,
        title: str,
        *,
        size_bytes: int = 0,
        episode_count: int | None = None,
        indexer_id: int | None = None,
        protocol: str | None = None,
        quality: ParsedQuality | None = None,
    ) -> ReleaseFacts:
        q = quality or parse_quality(title)
        grp = release_group(title)
        if episode_count is None:
            ep = parse_episode(title)
            episode_count = max(1, len(ep.episodes)) if ep and ep.episodes else 1
        return cls(
            title=title,
            group=grp,
            quality=q,
            size_bytes=size_bytes or 0,
            episode_count=max(1, episode_count),
            indexer_id=indexer_id,
            protocol=protocol,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "group": self.group,
            "quality": self.quality.quality.name,
            "resolution": self.quality.quality.resolution.value,
            "source": self.quality.quality.source.value,
            "codec": self.quality.codec.value,
            "proper": self.quality.proper,
            "repack": self.quality.repack,
            "hdr": self.quality.hdr,
            "hdr_format": self.quality.hdr_format,
            "audio": self.quality.audio,
            "audio_channels": self.quality.audio_channels,
            "streaming_service": self.quality.streaming_service,
            "size_bytes": self.size_bytes,
            "episode_count": self.episode_count,
            "indexer_id": self.indexer_id,
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class FormatMatch:
    format_id: int
    name: str
    matched_specs: tuple[str, ...]
    include_in_rename: bool = False


# Nested unbounded quantifiers heuristic: e.g. (a+)+, (.*)*, ([a-z]+)+, (a|aa)+
_CATASTROPHIC_RX = re.compile(
    r"\("
    r"(?:\?:|\?=|\?!|\?<=|\?<!|\?>|\?P<[^>]+>)?"  # optional group prefix
    r"[^()]*?"
    r"(?:[+*]|\{\d*,\d*\})"                       # inner quantifier
    r"[^()]*?"
    r"\)"
    r"\s*(?:[+*?]|\{\d*,\d*\})"                   # outer quantifier
)
# Overlapping alternation inside quantified group: e.g. (a|a)+
_DUP_ALT_QUANT_RX = re.compile(
    r"\((?:\?:)?([^()|]+)\|\1\)\s*(?:[+*]|\{\d*,\d*\})"
)


def validate_regex(pattern: str) -> str | None:
    """None if OK, else the error message. Rejects >500 chars, compile errors, and obviously
    catastrophic patterns (nested unbounded quantifiers like (a+)+, (.*)*)."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "Regex pattern must not be empty"
    if len(pattern) > 500:
        return f"Regex pattern exceeds maximum length of 500 characters ({len(pattern)})"
    # Strip escaped characters before running structural heuristics so \( or \+ don't false-trigger
    sanitized = re.sub(r"\\.", "x", pattern)
    if _CATASTROPHIC_RX.search(sanitized) or _DUP_ALT_QUANT_RX.search(sanitized):
        return "Regex contains nested or overlapping quantifiers (catastrophic backtracking risk)"
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Invalid regular expression: {exc}"
    return None


def validate_specification(spec: Specification, index: int = 0) -> list[str]:
    """Validate a single specification and return human-readable error strings."""
    prefix = f"Specification #{index + 1} ('{spec.name or spec.kind}')"
    if spec.disabled:
        return []
    if not spec.name or not spec.name.strip():
        return [f"Specification #{index + 1}: name is required"]

    kind = spec.kind
    val = spec.value
    if kind in (SpecKind.RELEASE_TITLE, SpecKind.RELEASE_GROUP):
        if not isinstance(val, str):
            return [f"{prefix}: regex value must be a string"]
        err = validate_regex(val)
        return [f"{prefix}: {err}"] if err else []

    if kind == SpecKind.SOURCE:
        if not isinstance(val, str) or val.lower() not in VALID_SOURCES:
            return [f"{prefix}: unknown source '{val}' (expected one of {', '.join(VALID_SOURCES.values())})"]
        return []

    if kind == SpecKind.RESOLUTION:
        if not isinstance(val, str) or val.lower() not in VALID_RESOLUTIONS:
            return [f"{prefix}: unknown resolution '{val}' (expected one of {', '.join(VALID_RESOLUTIONS.values())})"]
        return []

    if kind == SpecKind.CODEC:
        if not isinstance(val, str) or val.lower() not in VALID_CODECS:
            return [f"{prefix}: unknown codec '{val}' (expected one of {', '.join(VALID_CODECS.values())})"]
        return []

    if kind == SpecKind.QUALITY_MODIFIER:
        if not isinstance(val, str) or val.lower() not in VALID_QUALITY_MODIFIERS:
            return [f"{prefix}: unknown quality modifier '{val}' (expected one of {', '.join(sorted(VALID_QUALITY_MODIFIERS))})"]
        return []

    if kind == SpecKind.SIZE:
        if not isinstance(val, dict):
            return [f"{prefix}: size value must be an object with min_gb and/or max_gb"]
        min_gb = val.get("min_gb", val.get("min", 0))
        max_gb = val.get("max_gb", val.get("max", 0))
        try:
            min_f = float(min_gb or 0)
            max_f = float(max_gb or 0)
        except (TypeError, ValueError):
            return [f"{prefix}: min_gb and max_gb must be numbers"]
        if min_f < 0 or max_f < 0:
            return [f"{prefix}: size bounds must be non-negative"]
        if max_f > 0 and min_f > max_f:
            return [f"{prefix}: min_gb ({min_f}) cannot exceed max_gb ({max_f})"]
        return []

    if kind == SpecKind.INDEXER:
        try:
            idx = int(val)
            if idx <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return [f"{prefix}: indexer value must be a positive integer indexer ID"]
        return []

    if kind == SpecKind.PROTOCOL:
        if not isinstance(val, str) or val.lower() not in VALID_PROTOCOLS:
            return [f"{prefix}: protocol must be 'torrent' or 'usenet'"]
        return []

    return [f"{prefix}: unknown specification kind '{kind}'"]


def _eval_raw_spec(spec: Specification, compiled_rx: re.Pattern[str] | None, facts: ReleaseFacts) -> bool:
    kind = spec.kind
    val = spec.value

    if kind == SpecKind.RELEASE_TITLE:
        return bool(compiled_rx and compiled_rx.search(facts.title or ""))

    if kind == SpecKind.RELEASE_GROUP:
        if not facts.group:
            return False
        return bool(compiled_rx and compiled_rx.search(facts.group))

    if kind == SpecKind.SOURCE:
        return facts.quality.quality.source.value.lower().replace("-", "") == str(val).lower().replace("-", "")

    if kind == SpecKind.RESOLUTION:
        return facts.quality.quality.resolution.value.lower() == str(val).lower()

    if kind == SpecKind.CODEC:
        return facts.quality.codec.value.lower() == str(val).lower()

    if kind == SpecKind.QUALITY_MODIFIER:
        mod = str(val).lower()
        if mod == "remux":
            return facts.quality.quality.source == Source.REMUX
        if mod == "proper":
            return facts.quality.proper
        if mod == "repack":
            return facts.quality.repack
        if mod == "hdr":
            return facts.quality.hdr
        return False

    if kind == SpecKind.SIZE:
        if not isinstance(val, dict):
            return False
        min_gb = float(val.get("min_gb", val.get("min", 0)) or 0)
        max_gb = float(val.get("max_gb", val.get("max", 0)) or 0)
        units = max(1, facts.episode_count)
        size_gb = (facts.size_bytes or 0) / (1024**3) / units
        if min_gb > 0 and size_gb < min_gb:
            return False
        if max_gb > 0 and size_gb > max_gb:
            return False
        return True

    if kind == SpecKind.INDEXER:
        if facts.indexer_id is None:
            return False
        try:
            return int(facts.indexer_id) == int(val)
        except (TypeError, ValueError):
            return False

    if kind == SpecKind.PROTOCOL:
        if not facts.protocol:
            return False
        return facts.protocol.lower() == str(val).lower()

    return False


class FormatMatcher:
    """Precompiles regexes once and evaluates releases against all custom formats."""

    def __init__(self, formats: Iterable[CustomFormat]) -> None:
        self.formats: tuple[CustomFormat, ...] = tuple(formats)
        self._compiled: dict[tuple[int, int], re.Pattern[str]] = {}
        rx_pool: dict[str, re.Pattern[str]] = {}
        self._fast_formats: list[
            tuple[
                CustomFormat,
                tuple[tuple[Specification, re.Pattern[str] | None], ...],
                tuple[tuple[SpecKind, tuple[tuple[Specification, re.Pattern[str] | None], ...]], ...],
            ]
        ] = []
        for fmt in self.formats:
            req_specs: list[tuple[Specification, re.Pattern[str] | None]] = []
            opt_by_kind: dict[SpecKind, list[tuple[Specification, re.Pattern[str] | None]]] = defaultdict(list)
            has_active = False
            for idx, spec in enumerate(fmt.specifications):
                if spec.disabled:
                    continue
                has_active = True
                rx: re.Pattern[str] | None = None
                if spec.kind in (SpecKind.RELEASE_TITLE, SpecKind.RELEASE_GROUP):
                    if isinstance(spec.value, str) and validate_regex(spec.value) is None:
                        rx = rx_pool.get(spec.value)
                        if rx is None:
                            rx = re.compile(spec.value, re.IGNORECASE)
                            rx_pool[spec.value] = rx
                        self._compiled[(fmt.id, idx)] = rx
                if spec.required:
                    req_specs.append((spec, rx))
                else:
                    opt_by_kind[spec.kind].append((spec, rx))
            if has_active:
                self._fast_formats.append(
                    (
                        fmt,
                        tuple(req_specs),
                        tuple((k, tuple(v)) for k, v in opt_by_kind.items()),
                    )
                )

    def evaluate_format(self, fmt: CustomFormat, facts: ReleaseFacts) -> tuple[bool, list[dict[str, Any]]]:
        """Evaluate one format against ``facts``, returning (matched, per-spec details)."""
        active_specs = [(idx, s) for idx, s in enumerate(fmt.specifications) if not s.disabled]
        spec_results: list[dict[str, Any]] = []
        if not active_specs:
            for s in fmt.specifications:
                spec_results.append({
                    "name": s.name,
                    "kind": s.kind.value,
                    "required": s.required,
                    "negate": s.negate,
                    "disabled": True,
                    "passed": False,
                })
            return False, spec_results

        by_kind_optional: dict[SpecKind, list[bool]] = defaultdict(list)
        all_required_passed = True
        passed_names: list[str] = []

        for idx, spec in enumerate(fmt.specifications):
            if spec.disabled:
                spec_results.append({
                    "name": spec.name,
                    "kind": spec.kind.value,
                    "required": spec.required,
                    "negate": spec.negate,
                    "disabled": True,
                    "passed": False,
                })
                continue
            raw = _eval_raw_spec(spec, self._compiled.get((fmt.id, idx)), facts)
            passed = (not raw) if spec.negate else raw
            spec_results.append({
                "name": spec.name,
                "kind": spec.kind.value,
                "required": spec.required,
                "negate": spec.negate,
                "disabled": False,
                "passed": passed,
            })
            if passed:
                passed_names.append(spec.name)
            if spec.required:
                if not passed:
                    all_required_passed = False
            else:
                by_kind_optional[spec.kind].append(passed)

        matched = all_required_passed and all(any(results) for results in by_kind_optional.values())
        return matched, spec_results

    def match(self, facts: ReleaseFacts) -> list[FormatMatch]:
        if not self._fast_formats:
            return []
        out: list[FormatMatch] = []
        rx_title_cache: dict[re.Pattern[str], bool] = {}
        rx_group_cache: dict[re.Pattern[str], bool] = {}
        title_str = facts.title or ""
        group_str = facts.group

        def eval_fast(spec: Specification, rx: re.Pattern[str] | None) -> bool:
            kind = spec.kind
            if kind == SpecKind.RELEASE_TITLE:
                if rx is None:
                    return False
                res = rx_title_cache.get(rx)
                if res is None:
                    res = bool(rx.search(title_str))
                    rx_title_cache[rx] = res
                return res
            if kind == SpecKind.RELEASE_GROUP:
                if not group_str or rx is None:
                    return False
                res = rx_group_cache.get(rx)
                if res is None:
                    res = bool(rx.search(group_str))
                    rx_group_cache[rx] = res
                return res
            return _eval_raw_spec(spec, rx, facts)

        for fmt, req_specs, opt_groups in self._fast_formats:
            passed_names: list[str] = []
            ok = True
            for spec, rx in req_specs:
                raw = eval_fast(spec, rx)
                passed = (not raw) if spec.negate else raw
                if not passed:
                    ok = False
                    break
                passed_names.append(spec.name)
            if not ok:
                continue
            for _kind, specs in opt_groups:
                kind_ok = False
                for spec, rx in specs:
                    raw = eval_fast(spec, rx)
                    passed = (not raw) if spec.negate else raw
                    if passed:
                        kind_ok = True
                        passed_names.append(spec.name)
                if not kind_ok:
                    ok = False
                    break
            if ok:
                out.append(FormatMatch(fmt.id, fmt.name, tuple(passed_names), fmt.include_in_rename))
        return out

    match_all = match

    def test(self, facts: ReleaseFacts) -> list[dict[str, Any]]:
        """Detailed evaluation across all formats for the tester endpoint."""
        results: list[dict[str, Any]] = []
        for fmt in self.formats:
            matched, spec_results = self.evaluate_format(fmt, facts)
            results.append({
                "format_id": fmt.id,
                "name": fmt.name,
                "matched": matched,
                "matched_specs": [r["name"] for r in spec_results if r["passed"]],
                "specifications": spec_results,
            })
        return results


def score(matches: list[FormatMatch], format_scores: dict[int, int]) -> tuple[int, list[tuple[str, int]]]:
    """Total score + breakdown [(format name, score)], used for the 'why' panel."""
    total = 0
    breakdown: list[tuple[str, int]] = []
    for m in matches:
        s = int(format_scores.get(m.format_id, 0))
        total += s
        breakdown.append((m.name, s))
    return total, breakdown
