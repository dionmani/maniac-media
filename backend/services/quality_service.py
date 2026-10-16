from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.events import Event, EventType, bus
from backend.db import models
from backend.db.base import utcnow
from backend.domain.custom_formats import (
    CustomFormat,
    FormatMatcher,
    ReleaseFacts,
    Specification,
    validate_specification,
)
from backend.domain.custom_formats import (
    score as score_formats,
)
from backend.domain.quality import DEFAULT_PROFILES, QUALITY_BY_NAME, QualityProfileSpec

_formats_version: int = 0
_cached_matcher: tuple[int, FormatMatcher] | None = None


def bump_formats_version() -> None:
    global _formats_version, _cached_matcher
    _formats_version += 1
    _cached_matcher = None
    bus.publish(EventType.SETTINGS_CHANGED, section="custom_formats")


async def _on_settings_changed(event: Event) -> None:
    global _formats_version, _cached_matcher
    if event.payload.get("section") == "custom_formats":
        _formats_version += 1
        _cached_matcher = None


bus.subscribe(EventType.SETTINGS_CHANGED, _on_settings_changed)


def to_domain_format(row: models.CustomFormat) -> CustomFormat:
    specs = tuple(Specification.from_dict(s) for s in (row.specifications or []))
    return CustomFormat(
        id=row.id,
        name=row.name,
        specifications=specs,
        include_in_rename=bool(row.include_in_rename),
    )


def load_formats(db: Session) -> list[CustomFormat]:
    rows = db.scalars(select(models.CustomFormat).order_by(models.CustomFormat.id)).all()
    return [to_domain_format(r) for r in rows]


def get_matcher(db: Session) -> FormatMatcher:
    global _cached_matcher
    if _cached_matcher is not None and _cached_matcher[0] == _formats_version:
        return _cached_matcher[1]
    matcher = FormatMatcher(load_formats(db))
    _cached_matcher = (_formats_version, matcher)
    return matcher


def ensure_default_profiles(db: Session) -> None:
    if db.scalar(select(models.QualityProfile.id).limit(1)) is not None:
        return
    for i, p in enumerate(DEFAULT_PROFILES, start=1):
        db.add(
            models.QualityProfile(
                id=i,
                name=p["name"],
                cutoff=p["cutoff"],
                items=p["items"],
                preferred_codecs=p["preferred_codecs"],
                rejected_codecs=[],
                upgrade_allowed=True,
                min_size_mb=0,
                max_size_mb=0,
                prefer_proper=True,
                format_scores={},
                min_format_score=0,
                upgrade_until_format_score=10000,
                min_format_upgrade_delta=1,
            )
        )
    db.flush()


def _parse_format_scores(raw: dict[Any, Any] | None) -> dict[int, int]:
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def to_spec(profile: models.QualityProfile | None) -> QualityProfileSpec:
    if profile is None:
        p = DEFAULT_PROFILES[0]
        return QualityProfileSpec(p["name"], p["items"], p["cutoff"], preferred_codecs=p["preferred_codecs"])
    items = [i for i in (profile.items or []) if i.lower() in QUALITY_BY_NAME]
    return QualityProfileSpec(
        name=profile.name,
        allowed=items or DEFAULT_PROFILES[0]["items"],
        cutoff=profile.cutoff,
        upgrade_allowed=profile.upgrade_allowed is not False,
        preferred_codecs=profile.preferred_codecs or [],
        rejected_codecs=profile.rejected_codecs or [],
        min_size_mb=profile.min_size_mb or 0,
        max_size_mb=profile.max_size_mb or 0,
        prefer_proper=profile.prefer_proper is not False,
        format_scores=_parse_format_scores(profile.format_scores),
        min_format_score=int(profile.min_format_score or 0),
        upgrade_until_format_score=int(profile.upgrade_until_format_score if profile.upgrade_until_format_score is not None else 10000),
        min_format_upgrade_delta=max(1, int(profile.min_format_upgrade_delta if profile.min_format_upgrade_delta is not None else 1)),
    )


def spec_for(db: Session, profile_id: int | None) -> QualityProfileSpec:
    profile = db.get(models.QualityProfile, profile_id or 1) or db.get(models.QualityProfile, 1)
    return to_spec(profile)


def validate_profile(
    items: list[str],
    cutoff: str | None,
    *,
    db: Session | None = None,
    format_scores: dict[Any, Any] | None = None,
) -> list[str]:
    errors = [f"Unknown quality '{i}'" for i in items if i.lower() not in QUALITY_BY_NAME]
    if not items:
        errors.append("A profile needs at least one allowed quality")
    if cutoff and cutoff.lower() not in {i.lower() for i in items}:
        errors.append("Cutoff must be one of the allowed qualities")
    if format_scores and db is not None:
        known_ids = set(db.scalars(select(models.CustomFormat.id)).all())
        for k, v in format_scores.items():
            try:
                fid = int(k)
                int(v)
            except (TypeError, ValueError):
                errors.append(f"Invalid format score entry '{k}: {v}'")
                continue
            if fid not in known_ids:
                errors.append(f"Unknown custom format id '{fid}' in format_scores")
    return errors


def validate_custom_format(
    name: str,
    specifications: list[dict[str, Any]],
    *,
    db: Session | None = None,
    exclude_id: int | None = None,
) -> list[str]:
    errors: list[str] = []
    clean_name = (name or "").strip()
    if not clean_name:
        errors.append("Custom format name is required")
    elif len(clean_name) > 128:
        errors.append("Custom format name must not exceed 128 characters")
    if db is not None and clean_name:
        stmt = select(models.CustomFormat.id).where(func.lower(models.CustomFormat.name) == clean_name.lower())
        if exclude_id is not None:
            stmt = stmt.where(models.CustomFormat.id != exclude_id)
        if db.scalar(stmt.limit(1)) is not None:
            errors.append(f"A custom format named '{clean_name}' already exists")

    if not specifications:
        errors.append("At least one specification is required")
    else:
        for i, raw in enumerate(specifications):
            if not isinstance(raw, dict):
                errors.append(f"Specification #{i + 1}: must be an object")
                continue
            kind_raw = raw.get("kind", "")
            from backend.domain.custom_formats import SpecKind

            if not raw.get("disabled"):
                try:
                    SpecKind(kind_raw)
                except ValueError:
                    errors.append(f"Specification #{i + 1}: unknown specification kind '{kind_raw}'")
                    continue
            spec = Specification.from_dict(raw)
            errors.extend(validate_specification(spec, i))
    return errors


def delete_custom_format(db: Session, fmt: models.CustomFormat) -> None:
    """Delete a custom format and strip its ID from every quality profile in the same transaction."""
    fid_str = str(fmt.id)
    for profile in db.scalars(select(models.QualityProfile)).all():
        scores = dict(profile.format_scores or {})
        if fid_str in scores or fmt.id in scores:
            scores.pop(fid_str, None)
            scores.pop(fmt.id, None)  # type: ignore[arg-type]
            profile.format_scores = scores
    db.delete(fmt)
    db.flush()
    bump_formats_version()


def test_release(
    db: Session,
    title: str,
    *,
    size_bytes: int = 0,
    indexer_id: int | None = None,
    profile_id: int | None = None,
    protocol: str | None = None,
    specifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the matcher (or ad-hoc specifications from the editor) against a sample release title."""
    facts = ReleaseFacts.from_title(
        title,
        size_bytes=size_bytes,
        indexer_id=indexer_id,
        protocol=protocol,
    )
    profile = spec_for(db, profile_id) if profile_id is not None else to_spec(db.scalar(select(models.QualityProfile).order_by(models.QualityProfile.id).limit(1)))
    matcher = get_matcher(db)
    matches = matcher.match(facts)
    total_score, breakdown = score_formats(matches, profile.format_scores)
    eval_list = matcher.test(facts)
    for item in eval_list:
        item["score"] = int(profile.format_scores.get(item["format_id"], 0))

    adhoc_result = None
    if specifications is not None:
        errors: list[str] = []
        specs_objs: list[Specification] = []
        for idx, raw in enumerate(specifications):
            s = Specification.from_dict(raw)
            errs = validate_specification(s, idx)
            errors.extend(errs)
            specs_objs.append(s)
        adhoc_fmt = CustomFormat(id=0, name="Preview", specifications=tuple(specs_objs))
        adhoc_matcher = FormatMatcher([adhoc_fmt])
        matched, spec_details = adhoc_matcher.evaluate_format(adhoc_fmt, facts)
        adhoc_result = {
            "matched": matched,
            "specifications": spec_details,
            "errors": errors,
        }

    return {
        "facts": facts.to_dict(),
        "matches": [
            {
                "id": m.format_id,
                "name": m.name,
                "matched_specs": list(m.matched_specs),
                "score": int(profile.format_scores.get(m.format_id, 0)),
            }
            for m in matches
        ],
        "formats": eval_list,
        "total_score": total_score,
        "score_breakdown": [{"label": name, "value": val} for name, val in breakdown],
        "adhoc": adhoc_result,
    }


__all__ = [
    "bump_formats_version",
    "delete_custom_format",
    "ensure_default_profiles",
    "get_matcher",
    "load_formats",
    "spec_for",
    "test_release",
    "to_domain_format",
    "to_spec",
    "utcnow",
    "validate_custom_format",
    "validate_profile",
]
