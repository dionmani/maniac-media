"""qBittorrent Web API v2."""
from __future__ import annotations

import os

import httpx

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, fetch_torrent, map_remote_path
from backend.plugins.registry import register

STATE_MAP = {
    "downloading": "downloading", "forcedDL": "downloading", "metaDL": "downloading", "forcedMetaDL": "downloading",
    "stalledDL": "downloading", "checkingDL": "downloading", "allocating": "downloading", "moving": "downloading",
    "queuedDL": "queued", "pausedDL": "paused", "stoppedDL": "paused",
    "uploading": "completed", "forcedUP": "completed", "stalledUP": "completed", "queuedUP": "completed",
    "pausedUP": "completed", "stoppedUP": "completed", "checkingUP": "completed",
    "error": "failed", "missingFiles": "failed", "checkingResumeData": "queued",
}

COMMON_FIELDS = [
    FieldSpec("host", "Host", required=True, default="localhost"),
    FieldSpec("port", "Port", "number", default=8080),
    FieldSpec("use_ssl", "Use SSL", "bool", default=False),
    FieldSpec("url_base", "URL base"),
    FieldSpec("username", "Username"),
    FieldSpec("password", "Password", "password", secret=True),
    FieldSpec("category", "Category", default="maniac-media"),
]


@register
class QBittorrentClient(DownloadClientPlugin):
    key = "qbittorrent"
    name = "qBittorrent"
    description = "qBittorrent 4.1+ via the Web API."
    protocol = "torrent"
    default_port = 8080
    fields = COMMON_FIELDS

    async def _session(self) -> httpx.AsyncClient:
        root = base_url(self.settings)
        c = http.client(timeout=20, base_url=root, headers={"Referer": root})
        resp = await http.request(
            c, "POST", "/api/v2/auth/login",
            data={"username": self.setting("username", ""), "password": self.setting("password", "")},
            raise_for_status=False, retries=1,
        )
        if resp.status_code == 403:
            await c.aclose()
            raise PluginError("qBittorrent banned this IP after too many failed logins")
        if resp.status_code != 200 or resp.text.strip() not in ("Ok.", ""):
            await c.aclose()
            raise PluginError("qBittorrent rejected the credentials")
        return c

    async def add(self, release: ReleaseInfo, category: str) -> str:
        payload = await fetch_torrent(release)
        async with await self._session() as c:
            await http.request(c, "POST", "/api/v2/torrents/createCategory", data={"category": category}, raise_for_status=False)
            data = {"category": category}
            if payload.magnet:
                resp = await http.request(c, "POST", "/api/v2/torrents/add", data={**data, "urls": payload.magnet}, raise_for_status=False)
            else:
                files = {"torrents": (f"{release.title}.torrent", payload.torrent, "application/x-bittorrent")}
                resp = await http.request(c, "POST", "/api/v2/torrents/add", data=data, files=files, raise_for_status=False)
            if resp.status_code != 200 or "Fails" in resp.text:
                raise PluginError(f"qBittorrent refused the torrent: {resp.text.strip()[:120]}")
        if not payload.info_hash:
            raise PluginError("Could not determine torrent hash")
        return payload.info_hash.lower()

    async def get_items(self, category: str) -> list[ClientItem]:
        async with await self._session() as c:
            resp = await http.request(c, "GET", "/api/v2/torrents/info", params={"category": category})
            items = []
            for t in resp.json():
                content = t.get("content_path") or os.path.join(t.get("save_path", ""), t.get("name", ""))
                items.append(
                    ClientItem(
                        id=t["hash"].lower(),
                        name=t.get("name", ""),
                        status=STATE_MAP.get(t.get("state", ""), "downloading"),
                        progress=round(float(t.get("progress", 0)) * 100, 2),
                        size_bytes=int(t.get("size", 0)),
                        remaining_bytes=int(t.get("amount_left", 0)),
                        speed_bps=int(t.get("dlspeed", 0)),
                        eta_seconds=t.get("eta") if t.get("eta", 8640000) < 8640000 else None,
                        output_path=map_remote_path(content, self.settings),
                        category=t.get("category"),
                        message="Missing files" if t.get("state") == "missingFiles" else None,
                    )
                )
            return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        async with await self._session() as c:
            await http.request(c, "POST", "/api/v2/torrents/delete", data={"hashes": item_id, "deleteFiles": str(delete_data).lower()})

    async def test(self) -> tuple[bool, str]:
        try:
            async with await self._session() as c:
                resp = await http.request(c, "GET", "/api/v2/app/version")
                return True, f"Connected to qBittorrent {resp.text.strip()}."
        except Exception as exc:
            return False, str(exc)
