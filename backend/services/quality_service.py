from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import models
from backend.domain.quality import DEFAULT_PROFILES, QUALITY_BY_NAME, QualityProfileSpec


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
            )
        )
    db.flush()


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
    )


def spec_for(db: Session, profile_id: int | None) -> QualityProfileSpec:
    profile = db.get(models.QualityProfile, profile_id or 1) or db.get(models.QualityProfile, 1)
    return to_spec(profile)


def validate_profile(items: list[str], cutoff: str | None) -> list[str]:
    errors = [f"Unknown quality '{i}'" for i in items if i.lower() not in QUALITY_BY_NAME]
    if not items:
        errors.append("A profile needs at least one allowed quality")
    if cutoff and cutoff.lower() not in {i.lower() for i in items}:
        errors.append("Cutoff must be one of the allowed qualities")
    return errors
