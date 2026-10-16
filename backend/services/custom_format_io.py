"""Radarr / Sonarr / TRaSH Guides Custom Format JSON import and export."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import http
from backend.db import models
from backend.domain.custom_formats import Specification, SpecKind, validate_specification
from backend.domain.quality import Resolution, Source
from backend.services.quality_service import bump_formats_version

MAX_IMPORT_BYTES = 1024 * 1024  # 1 MB cap

# Radarr Source enum -> our Source
RADARR_SOURCE_TO_SOURCE: dict[int | str, str] = {
    1: Source.HDTV.value,      # Television
    2: Source.HDTV.value,      # TelevisionRaw
    3: Source.WEBDL.value,     # Webdl
    4: Source.WEBRIP.value,    # WebRip
    5: Source.DVD.value,       # Dvd
    6: Source.BLURAY.value,    # Bluray
    7: Source.REMUX.value,     # BlurayRaw / Remux
    "television": Source.HDTV.value,
    "hdtv": Source.HDTV.value,
    "webdl": Source.WEBDL.value,
    "web-dl": Source.WEBDL.value,
    "webrip": Source.WEBRIP.value,
    "dvd": Source.DVD.value,
    "bluray": Source.BLURAY.value,
    "remux": Source.REMUX.value,
    "blurayraw": Source.REMUX.value,
}

SOURCE_TO_RADARR: dict[str, int] = {
    Source.HDTV.value.lower(): 1,
    Source.WEBDL.value.lower(): 3,
    Source.WEBRIP.value.lower(): 4,
    Source.DVD.value.lower(): 5,
    Source.BLURAY.value.lower(): 6,
    Source.REMUX.value.lower(): 7,
}

RADARR_RESOLUTION_TO_RES: dict[int | str, str] = {
    360: Resolution.SD.value,
    480: Resolution.SD.value,
    540: Resolution.SD.value,
    576: Resolution.SD.value,
    720: Resolution.R720.value,
    1080: Resolution.R1080.value,
    2160: Resolution.R2160.value,
    "sd": Resolution.SD.value,
    "480p": Resolution.SD.value,
    "576p": Resolution.SD.value,
    "720p": Resolution.R720.value,
    "1080p": Resolution.R1080.value,
    "2160p": Resolution.R2160.value,
}

RES_TO_RADARR: dict[str, int] = {
    Resolution.SD.value.lower(): 480,
    Resolution.R720.value.lower(): 720,
    Resolution.R1080.value.lower(): 1080,
    Resolution.R2160.value.lower(): 2160,
}

RADARR_MODIFIER_TO_MOD: dict[int | str, str] = {
    1: "remux",     # Regional -> fallback
    2: "proper",    # Screener -> fallback
    3: "proper",    # RAWHD
    4: "remux",     # BRDISK
    5: "remux",     # REMUX
    "remux": "remux",
    "proper": "proper",
    "repack": "repack",
    "hdr": "hdr",
}

MOD_TO_RADARR: dict[str, int] = {
    "remux": 5,
    "proper": 2,
    "repack": 2,
    "hdr": 5,
}

UNSUPPORTED_IMPLEMENTATIONS = {
    "indexerflagspecification": "IndexerFlagSpecification",
    "languagespecification": "LanguageSpecification",
    "editionspecification": "EditionSpecification",
    "releaseTypespecification": "ReleaseTypeSpecification",
}


class CustomFormatImportError(ValueError):
    pass


def _extract_fields(fields_raw: Any) -> dict[str, Any]:
    if isinstance(fields_raw, dict):
        return fields_raw
    if isinstance(fields_raw, list):
        out: dict[str, Any] = {}
        for item in fields_raw:
            if isinstance(item, dict) and "name" in item:
                out[str(item["name"])] = item.get("value")
        return out
    return {}


def _convert_radarr_spec(raw: dict[str, Any], fmt_name: str, warnings: list[str]) -> dict[str, Any]:
    # Support native Maniac Media spec shape if passed directly
    if "kind" in raw and "implementation" not in raw:
        return Specification.from_dict(raw).to_dict()

    spec_name = str(raw.get("name") or "Specification").strip()
    impl = str(raw.get("implementation") or "").strip()
    impl_lower = impl.lower()
    negate = bool(raw.get("negate", False))
    required = bool(raw.get("required", False))
    fields = _extract_fields(raw.get("fields"))
    val = fields.get("value", raw.get("value"))

    if impl_lower == "releasetitlespecification":
        return {
            "name": spec_name,
            "kind": SpecKind.RELEASE_TITLE.value,
            "value": str(val or ""),
            "negate": negate,
            "required": required,
        }

    if impl_lower == "releasegroupspecification":
        return {
            "name": spec_name,
            "kind": SpecKind.RELEASE_GROUP.value,
            "value": str(val or ""),
            "negate": negate,
            "required": required,
        }

    if impl_lower == "sourcespecification":
        mapped = RADARR_SOURCE_TO_SOURCE.get(val)
        if mapped is None and isinstance(val, str) and val.isdigit():
            mapped = RADARR_SOURCE_TO_SOURCE.get(int(val))
        if mapped is None and isinstance(val, str):
            mapped = RADARR_SOURCE_TO_SOURCE.get(val.lower())
        if mapped is None:
            warnings.append(f"{fmt_name}: unsupported source value '{val}' in '{spec_name}'; kept disabled")
            return {
                "name": spec_name,
                "kind": SpecKind.SOURCE.value,
                "value": str(val),
                "negate": negate,
                "required": required,
                "disabled": True,
            }
        return {
            "name": spec_name,
            "kind": SpecKind.SOURCE.value,
            "value": mapped,
            "negate": negate,
            "required": required,
        }

    if impl_lower == "resolutionspecification":
        mapped = RADARR_RESOLUTION_TO_RES.get(val)
        if mapped is None and isinstance(val, str) and val.isdigit():
            mapped = RADARR_RESOLUTION_TO_RES.get(int(val))
        if mapped is None and isinstance(val, str):
            mapped = RADARR_RESOLUTION_TO_RES.get(val.lower())
        if mapped is None:
            warnings.append(f"{fmt_name}: unsupported resolution value '{val}' in '{spec_name}'; kept disabled")
            return {
                "name": spec_name,
                "kind": SpecKind.RESOLUTION.value,
                "value": str(val),
                "negate": negate,
                "required": required,
                "disabled": True,
            }
        return {
            "name": spec_name,
            "kind": SpecKind.RESOLUTION.value,
            "value": mapped,
            "negate": negate,
            "required": required,
        }

    if impl_lower == "qualitymodifierspecification":
        mapped = RADARR_MODIFIER_TO_MOD.get(val)
        if mapped is None and isinstance(val, str) and val.isdigit():
            mapped = RADARR_MODIFIER_TO_MOD.get(int(val))
        if mapped is None and isinstance(val, str):
            mapped = RADARR_MODIFIER_TO_MOD.get(val.lower())
        if mapped is None:
            mapped = "remux"
        return {
            "name": spec_name,
            "kind": SpecKind.QUALITY_MODIFIER.value,
            "value": mapped,
            "negate": negate,
            "required": required,
        }

    if impl_lower == "sizespecification":
        min_v = fields.get("min", val.get("min", val.get("min_gb", 0)) if isinstance(val, dict) else 0)
        max_v = fields.get("max", val.get("max", val.get("max_gb", 0)) if isinstance(val, dict) else 0)
        return {
            "name": spec_name,
            "kind": SpecKind.SIZE.value,
            "value": {"min_gb": float(min_v or 0), "max_gb": float(max_v or 0)},
            "negate": negate,
            "required": required,
        }

    if impl_lower == "codecspecification":
        return {
            "name": spec_name,
            "kind": SpecKind.CODEC.value,
            "value": str(val or "H265"),
            "negate": negate,
            "required": required,
        }

    if impl_lower == "indexerspecification":
        return {
            "name": spec_name,
            "kind": SpecKind.INDEXER.value,
            "value": int(val or 1),
            "negate": negate,
            "required": required,
        }

    if impl_lower == "protocolspecification":
        return {
            "name": spec_name,
            "kind": SpecKind.PROTOCOL.value,
            "value": str(val or "torrent").lower(),
            "negate": negate,
            "required": required,
        }

    # Unsupported Radarr/Sonarr specification (LanguageSpecification, EditionSpecification, IndexerFlagSpecification, etc.)
    warnings.append(f"{fmt_name}: specification '{spec_name}' ({impl or 'unknown'}) is not supported yet and was kept disabled")
    return {
        "name": spec_name,
        "kind": impl_lower.replace("specification", "") or "unsupported",
        "value": val if val is not None else fields,
        "negate": negate,
        "required": required,
        "disabled": True,
        "implementation": impl,
    }


def import_formats_data(db: Session, payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
    """Import one or more Radarr/Sonarr/TRaSH custom formats into the database."""
    if isinstance(payload, dict):
        items = payload.get("formats") if isinstance(payload.get("formats"), list) else [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        raise CustomFormatImportError("JSON payload must be a custom format object or an array of custom formats")

    if not items:
        raise CustomFormatImportError("No custom formats found in payload")

    imported: list[str] = []
    updated: list[str] = []
    warnings: list[str] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise CustomFormatImportError(f"Item #{idx + 1} is not a JSON object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise CustomFormatImportError(f"Item #{idx + 1} is missing a 'name'")
        include_in_rename = bool(
            item.get("includeCustomFormatWhenRenaming", item.get("include_in_rename", False))
        )
        trash_id = item.get("trash_id")
        if not trash_id and isinstance(item.get("trash_ids"), dict) and item["trash_ids"]:
            trash_id = next(iter(item["trash_ids"].keys()), None)
        elif not trash_id and isinstance(item.get("trash_ids"), list) and item["trash_ids"]:
            trash_id = item["trash_ids"][0]
        source = f"trash:{trash_id}" if trash_id else str(item.get("source") or "user")[:64]

        raw_specs = item.get("specifications")
        if not isinstance(raw_specs, list) or not raw_specs:
            raise CustomFormatImportError(f"Custom format '{name}' has no specifications")

        converted_specs: list[dict[str, Any]] = []
        for s_idx, raw_s in enumerate(raw_specs):
            if not isinstance(raw_s, dict):
                raise CustomFormatImportError(f"Custom format '{name}' specification #{s_idx + 1} is not an object")
            spec_dict = _convert_radarr_spec(raw_s, name, warnings)
            spec_obj = Specification.from_dict(spec_dict)
            errs = validate_specification(spec_obj, s_idx)
            if errs:
                raise CustomFormatImportError(f"Custom format '{name}': {'; '.join(errs)}")
            converted_specs.append(spec_dict)

        existing = db.scalar(select(models.CustomFormat).where(func.lower(models.CustomFormat.name) == name.lower()))
        if existing is not None:
            existing.name = name
            existing.specifications = converted_specs
            existing.include_in_rename = include_in_rename
            existing.source = source
            updated.append(name)
        else:
            db.add(
                models.CustomFormat(
                    name=name,
                    specifications=converted_specs,
                    include_in_rename=include_in_rename,
                    source=source,
                )
            )
            imported.append(name)

    db.flush()
    bump_formats_version()
    return {"imported": imported, "updated": updated, "warnings": warnings}


async def fetch_and_parse_url(url: str) -> Any:
    """Fetch Radarr/TRaSH JSON from a URL (1 MB cap, JSON only)."""
    if not url or not url.startswith(("http://", "https://")):
        raise CustomFormatImportError("URL must start with http:// or https://")
    async with http.client(timeout=15.0) as client:
        resp = await http.request(client, "GET", url, retries=2)
        if len(resp.content) > MAX_IMPORT_BYTES:
            raise CustomFormatImportError("Remote JSON exceeds 1 MB limit")
        try:
            return json.loads(resp.content.decode("utf-8"))
        except Exception as exc:
            raise CustomFormatImportError(f"Response is not valid JSON: {exc}") from exc


def _export_spec(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or "")
    kind = str(raw.get("kind") or "")
    val = raw.get("value")
    negate = bool(raw.get("negate", False))
    required = bool(raw.get("required", False))

    if raw.get("disabled") and raw.get("implementation"):
        return {
            "name": name,
            "implementation": raw["implementation"],
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }

    if kind == SpecKind.RELEASE_TITLE.value:
        return {
            "name": name,
            "implementation": "ReleaseTitleSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }
    if kind == SpecKind.RELEASE_GROUP.value:
        return {
            "name": name,
            "implementation": "ReleaseGroupSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }
    if kind == SpecKind.SOURCE.value:
        radarr_val = SOURCE_TO_RADARR.get(str(val).lower(), val)
        return {
            "name": name,
            "implementation": "SourceSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": radarr_val},
        }
    if kind == SpecKind.RESOLUTION.value:
        radarr_val = RES_TO_RADARR.get(str(val).lower(), val)
        return {
            "name": name,
            "implementation": "ResolutionSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": radarr_val},
        }
    if kind == SpecKind.QUALITY_MODIFIER.value:
        radarr_val = MOD_TO_RADARR.get(str(val).lower(), val)
        return {
            "name": name,
            "implementation": "QualityModifierSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": radarr_val},
        }
    if kind == SpecKind.SIZE.value:
        min_gb = val.get("min_gb", val.get("min", 0)) if isinstance(val, dict) else 0
        max_gb = val.get("max_gb", val.get("max", 0)) if isinstance(val, dict) else 0
        return {
            "name": name,
            "implementation": "SizeSpecification",
            "negate": negate,
            "required": required,
            "fields": {"min": min_gb, "max": max_gb},
        }
    if kind == SpecKind.CODEC.value:
        return {
            "name": name,
            "implementation": "CodecSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }
    if kind == SpecKind.INDEXER.value:
        return {
            "name": name,
            "implementation": "IndexerSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }
    if kind == SpecKind.PROTOCOL.value:
        return {
            "name": name,
            "implementation": "ProtocolSpecification",
            "negate": negate,
            "required": required,
            "fields": {"value": val},
        }
    return {
        "name": name,
        "implementation": raw.get("implementation") or "ReleaseTitleSpecification",
        "negate": negate,
        "required": required,
        "fields": {"value": val},
    }


def export_formats(db: Session, ids: list[int] | None = None) -> list[dict[str, Any]]:
    stmt = select(models.CustomFormat).order_by(models.CustomFormat.id)
    if ids:
        stmt = stmt.where(models.CustomFormat.id.in_(ids))
    rows = db.scalars(stmt).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "name": r.name,
            "includeCustomFormatWhenRenaming": bool(r.include_in_rename),
            "specifications": [_export_spec(s) for s in (r.specifications or [])],
        }
        if r.source and r.source.startswith("trash:"):
            item["trash_id"] = r.source.split(":", 1)[1]
        out.append(item)
    return out
