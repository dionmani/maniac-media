"""Tag label conventions.

Request managers attribute requests with per-user tags. Seerr/Overseerr label them
``<seerr user id>-<display name>`` (older releases: ``<id> - <name>``); any other label is a plain tag.
"""
from __future__ import annotations

import re

_REQUESTER = re.compile(r"^\s*(\d+)\s*-\s*(\S.*?)\s*$")


def requester_name(label: str) -> str | None:
    """The requesting user's name if ``label`` is a requester tag, else ``None``."""
    match = _REQUESTER.match(label or "")
    return match.group(2) if match else None
