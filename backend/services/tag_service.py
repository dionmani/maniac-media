"""Tags on movies/series and request attribution.

Request managers send tag ids with every add/update (Radarr/Sonarr semantics: the list replaces the
current one). When a requester tag (see ``domain/tags.py``) newly lands on a title, a ``requested``
history entry records who asked for it.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from backend.db import models
from backend.db.repositories import TagRepository
from backend.domain.tags import requester_name
from backend.services.audit import Actor, audit, history


class TagError(ValueError):
    pass


def list_tags(db: Session) -> Sequence[models.Tag]:
    return TagRepository(db).all()


def create_tag(db: Session, label: str, actor: Actor) -> models.Tag:
    """Idempotent: an existing tag with the same label (any case) is returned instead."""
    label = label.strip()
    if not label:
        raise TagError("Tag label is required")
    repo = TagRepository(db)
    if (existing := repo.by_label(label)) is not None:
        return existing
    tag = repo.add(models.Tag(label=label))
    audit(db, "settings", "tag.create", actor=actor, entity_type="tag", entity_id=tag.id, details={"label": label})
    return tag


def rename_tag(db: Session, tag_id: int, label: str, actor: Actor) -> models.Tag:
    repo = TagRepository(db)
    tag = repo.get(tag_id)
    if tag is None:
        raise TagError("Tag not found")
    label = label.strip()
    clash = repo.by_label(label)
    if clash is not None and clash.id != tag.id:
        raise TagError(f"Tag '{label}' already exists")
    old, tag.label = tag.label, label
    audit(db, "settings", "tag.rename", actor=actor, entity_type="tag", entity_id=tag.id, details={"from": old, "to": label})
    return tag


def requesters(db: Session, tag_ids: Sequence[int] | None) -> list[str]:
    """Names of the users whose requester tags are on a title, in tag order."""
    labels = TagRepository(db).labels(tag_ids or [])
    return [name for i in (tag_ids or []) if (name := requester_name(labels.get(i, ""))) is not None]


def set_tags(db: Session, item: models.Movie | models.Series, tag_ids: Sequence[int], *, actor: Actor, source: str,
             seasons: Sequence[int] = ()) -> list[str]:
    """Replace ``item``'s tags (unknown ids are dropped) and record a ``requested`` history entry for
    each requester tag that is new on it. ``seasons`` names the TV seasons this request switched on.
    Returns the names of the newly attributed requesters."""
    labels = TagRepository(db).labels(tag_ids)
    before = set(item.tags or [])
    item.tags = [i for i in dict.fromkeys(tag_ids) if i in labels]
    media_type = "movie" if isinstance(item, models.Movie) else "tv"
    new_names = [name for i in item.tags if i not in before and (name := requester_name(labels[i])) is not None]
    scope = f" (season{'s' if len(seasons) > 1 else ''} {', '.join(map(str, seasons))})" if seasons else ""
    for name in new_names:
        history(db, media_type, item.id, item.title, "requested", details=f"Requested by {name} via {source}{scope}",
                data={"requested_by": name, "source": source, "seasons": list(seasons)})
    if seasons and not new_names:
        # more seasons asked for by someone already tagged on the show: still worth a line, just unnamed
        history(db, media_type, item.id, item.title, "requested", details=f"Requested via {source}{scope}",
                data={"source": source, "seasons": list(seasons)})
    return new_names
