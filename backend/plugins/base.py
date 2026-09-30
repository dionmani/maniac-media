"""Plugin contracts.

A plugin is a class with a unique ``key`` that declares its configuration ``fields``.
The core only talks to these abstract interfaces and resolves implementations through the
registry by key, so adding a provider never requires touching core code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from backend.domain.types import ClientItem, MediaMetadata, NotificationMessage, ReleaseInfo, SearchCriteria


class PluginKind(StrEnum):
    METADATA = "metadata"
    INDEXER = "indexer"
    DOWNLOAD_CLIENT = "download_client"
    NOTIFICATION = "notification"


@dataclass
class FieldSpec:
    name: str
    label: str
    type: str = "text"  # text | password | number | bool | select | list
    required: bool = False
    default: Any = None
    secret: bool = False
    help: str | None = None
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PluginError(RuntimeError):
    """Raised by plugins for expected, user-facing failures (bad credentials, offline...)."""


class Plugin(ABC):
    kind: ClassVar[PluginKind]
    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    fields: ClassVar[list[FieldSpec]] = []
    builtin: ClassVar[bool] = True

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = {f.name: f.default for f in self.fields}
        self.settings.update({k: v for k, v in (settings or {}).items() if v is not None})

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.settings.get(name)
        return default if value in (None, "") else value

    async def test(self) -> tuple[bool, str]:
        return True, "OK"

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "kind": cls.kind.value,
            "key": cls.key,
            "name": cls.name,
            "description": cls.description,
            "builtin": cls.builtin,
            "fields": [f.to_dict() for f in cls.fields],
            **cls.extra_description(),
        }

    @classmethod
    def extra_description(cls) -> dict[str, Any]:
        return {}


class MetadataProvider(Plugin):
    kind = PluginKind.METADATA
    supports_movies: ClassVar[bool] = True
    supports_series: ClassVar[bool] = True
    requires_api_key: ClassVar[bool] = False

    def is_configured(self) -> bool:
        return not self.requires_api_key or bool(self.setting("api_key"))

    async def search_movies(self, query: str, year: int | None = None) -> list[MediaMetadata]:
        return []

    async def search_series(self, query: str) -> list[MediaMetadata]:
        return []

    async def get_movie(self, *, tmdb_id: int | None = None, imdb_id: str | None = None) -> MediaMetadata | None:
        return None

    async def get_series(
        self, *, tvdb_id: int | None = None, tmdb_id: int | None = None, tvmaze_id: int | None = None, imdb_id: str | None = None
    ) -> MediaMetadata | None:
        return None

    @classmethod
    def extra_description(cls) -> dict[str, Any]:
        return {"supports_movies": cls.supports_movies, "supports_series": cls.supports_series, "requires_api_key": cls.requires_api_key}


class IndexerPlugin(Plugin):
    kind = PluginKind.INDEXER
    protocol: ClassVar[str] = "torrent"
    supports_search: ClassVar[bool] = True
    supports_rss: ClassVar[bool] = True

    @abstractmethod
    async def search(self, criteria: SearchCriteria) -> list[ReleaseInfo]: ...

    async def rss(self) -> list[ReleaseInfo]:
        return []

    @classmethod
    def extra_description(cls) -> dict[str, Any]:
        return {"protocol": cls.protocol, "supports_search": cls.supports_search, "supports_rss": cls.supports_rss}


class DownloadClientPlugin(Plugin):
    kind = PluginKind.DOWNLOAD_CLIENT
    protocol: ClassVar[str] = "torrent"
    default_port: ClassVar[int] = 8080

    @abstractmethod
    async def add(self, release: ReleaseInfo, category: str) -> str:
        """Hand a release to the client, returning the client's id for it."""

    @abstractmethod
    async def get_items(self, category: str) -> list[ClientItem]: ...

    async def find_items(self, item_ids: list[str]) -> list[ClientItem]:
        """Look up specific items regardless of category filters or listing limits.

        Called before a tracked download that vanished from ``get_items`` is written off, so clients
        whose listings are filtered or truncated (e.g. SABnzbd history) can confirm it really is gone.
        """
        return []

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        return None

    @classmethod
    def extra_description(cls) -> dict[str, Any]:
        return {"protocol": cls.protocol, "default_port": cls.default_port}


class NotificationPlugin(Plugin):
    kind = PluginKind.NOTIFICATION

    @abstractmethod
    async def send(self, message: NotificationMessage) -> None: ...

    async def test(self) -> tuple[bool, str]:
        try:
            await self.send(
                NotificationMessage(event="test", title="Maniac Media test", body="If you can read this, notifications work.")
            )
            return True, "Test notification sent."
        except Exception as exc:
            return False, str(exc)
