"""Movies and TV go into their own download-client categories ("movies" / "tv"), so the client can sort them apart."""
from __future__ import annotations

import asyncio

import pytest

from backend.db import models
from backend.db.session import session_scope
from backend.domain.types import ClientItem, ReleaseInfo
from backend.services import download_service
from backend.services.audit import SYSTEM
from backend.services.download_service import ClientRef


class _Client:
    protocol = "usenet"

    def __init__(self, listed: dict[str, list[ClientItem]] | None = None):
        self.added: list[tuple[str, str]] = []
        self.listed, self.polled = listed or {}, []

    async def add(self, release, category):
        self.added.append((release.title, category))
        return f"nzo_cat_{len(self.added)}"

    async def get_items(self, category):
        self.polled.append(category)
        return self.listed.get(category, [])

    async def find_items(self, item_ids):
        return []


def _release(title: str) -> ReleaseInfo:
    return ReleaseInfo(title, title, f"https://idx/{title}", "Idx", "usenet", size_bytes=1024**3)


@pytest.fixture
def client_row(database):
    def make(category: str = "movies", tv_category: str | None = "tv") -> int:
        with session_scope() as db:
            row = models.DownloadClient(name="cat-sab", client_type="sabnzbd", enabled=False, category=category,
                                        tv_category=tv_category, priority=99)
            db.add(row)
            db.flush()
            return row.id
    return make


def _use(monkeypatch, client_id: int, client: _Client, category: str, tv_category: str = "") -> None:
    monkeypatch.setattr(download_service, "_clients",
                        lambda protocol=None: [ClientRef(client_id, "sab", category, client, False, tv_category)])


def test_grab_sends_movies_and_tv_to_their_own_category(client_row, monkeypatch):
    client = _Client()
    _use(monkeypatch, client_row(), client, "movies", "tv")
    asyncio.run(download_service.grab(_release("Cat Film 2024 1080p WEB-DL x264-AAA"), media_type="movie", media_id=987001))
    asyncio.run(download_service.grab(_release("Cat.Show.S01E01.1080p.WEB-DL.x264-AAA"), media_type="tv", media_id=987002))
    assert [c for _, c in client.added] == ["movies", "tv"]


def test_without_a_tv_category_everything_shares_one(client_row, monkeypatch):
    client = _Client()
    _use(monkeypatch, client_row("maniac-media", None), client, "maniac-media")
    asyncio.run(download_service.grab(_release("Cat.Show.S01E02.1080p.WEB-DL.x264-AAA"), media_type="tv", media_id=987003))
    assert client.added[-1][1] == "maniac-media"


def test_sync_polls_both_categories(client_row, monkeypatch):
    client_id = client_row()
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Show", media_type="tv", media_id=1, release_title="Cat.Show.S02E01", status="queued",
                                     download_client_id=client_id, client_item_id="nzo_tv_only", protocol="usenet")
        db.add(q)
        db.flush()
        queue_id = q.id
    client = _Client({"tv": [ClientItem(id="nzo_tv_only", name="x", status="downloading", progress=40.0)]})
    _use(monkeypatch, client_id, client, "movies", "tv")
    asyncio.run(download_service.sync_clients())
    assert client.polled == ["movies", "tv"]
    with session_scope() as db:
        assert db.get(models.DownloadQueueItem, queue_id).status == "downloading"


def test_retry_resends_to_the_media_types_category(client_row, monkeypatch):
    client_id = client_row()
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Show", media_type="tv", media_id=1, release_title="Cat.Show.S03E01", status="failed",
                                     download_client_id=client_id, client_item_id="nzo_old", protocol="usenet",
                                     download_url="https://idx/Cat.Show.S03E01")
        db.add(q)
        db.flush()
        queue_id = q.id
    client = _Client()
    monkeypatch.setattr(download_service, "build", lambda row: client)
    asyncio.run(download_service.retry(queue_id, SYSTEM))
    assert client.added == [("Cat.Show.S03E01", "tv")]


def test_api_round_trips_the_tv_category(client, admin_headers):
    body = {"name": "SAB cats", "client_type": "sabnzbd", "host": "sab", "port": 8080, "category": "movies",
            "tv_category": "tv", "enabled": False}
    created = client.post("/api/v1/download-clients", json=body, headers=admin_headers)
    assert created.status_code == 201, created.text
    assert (created.json()["category"], created.json()["tv_category"]) == ("movies", "tv")
    listed = {c["id"]: c for c in client.get("/api/v1/download-clients", headers=admin_headers).json()}
    assert listed[created.json()["id"]]["tv_category"] == "tv"
