"""Transmission RPC."""
from __future__ import annotations

import base64
import os

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, fetch_torrent, map_remote_path
from backend.plugins.registry import register

FIELDS = ["id", "hashString", "name", "status", "percentDone", "totalSize", "leftUntilDone", "rateDownload",
          "eta", "downloadDir", "errorString", "error", "labels", "isFinished", "queuePosition"]


@register
class TransmissionClient(DownloadClientPlugin):
    key = "transmission"
    name = "Transmission"
    description = "Transmission 2.9+ via JSON-RPC."
    protocol = "torrent"
    default_port = 9091
    fields = [
        FieldSpec("host", "Host", required=True, default="localhost"),
        FieldSpec("port", "Port", "number", default=9091),
        FieldSpec("use_ssl", "Use SSL", "bool", default=False),
        FieldSpec("url_base", "URL base", default="transmission"),
        FieldSpec("username", "Username"),
        FieldSpec("password", "Password", "password", secret=True),
        FieldSpec("category", "Label", default="maniac-media"),
    ]

    async def _rpc(self, method: str, arguments: dict | None = None) -> dict:
        url = base_url({**self.settings, "url_base": self.setting("url_base", "transmission")}, "/rpc")
        auth = (self.setting("username"), self.setting("password", "")) if self.setting("username") else None
        body = {"method": method, "arguments": arguments or {}}
        async with http.client(timeout=20, auth=auth) as c:
            session_id = ""
            for _ in range(2):
                resp = await http.request(c, "POST", url, json=body, headers={"X-Transmission-Session-Id": session_id}, raise_for_status=False)
                if resp.status_code == 409:
                    session_id = resp.headers.get("X-Transmission-Session-Id", "")
                    continue
                if resp.status_code == 401:
                    raise PluginError("Transmission rejected the credentials")
                if resp.status_code >= 400:
                    raise PluginError(f"Transmission HTTP {resp.status_code}")
                data = resp.json()
                if data.get("result") != "success":
                    raise PluginError(f"Transmission error: {data.get('result')}")
                return data.get("arguments", {})
        raise PluginError("Transmission session handshake failed")

    async def add(self, release: ReleaseInfo, category: str) -> str:
        payload = await fetch_torrent(release)
        args: dict = {"labels": [category]}
        if payload.magnet:
            args["filename"] = payload.magnet
        else:
            args["metainfo"] = base64.b64encode(payload.torrent or b"").decode()
        result = await self._rpc("torrent-add", args)
        torrent = result.get("torrent-added") or result.get("torrent-duplicate") or {}
        return (torrent.get("hashString") or payload.info_hash or "").lower()

    async def get_items(self, category: str) -> list[ClientItem]:
        result = await self._rpc("torrent-get", {"fields": FIELDS})
        items = []
        for t in result.get("torrents", []):
            labels = t.get("labels") or []
            if category and category not in labels and not t.get("downloadDir", "").rstrip("/\\").endswith(category):
                continue
            done = t.get("percentDone", 0) >= 1 or t.get("isFinished")
            status = "failed" if t.get("error") in (3,) else "completed" if done else {0: "paused", 3: "queued"}.get(t.get("status"), "downloading")
            if t.get("error") in (1, 2) and not done:
                status = "warning"
            items.append(
                ClientItem(
                    id=t["hashString"].lower(),
                    name=t["name"],
                    status=status,
                    progress=round(t.get("percentDone", 0) * 100, 2),
                    size_bytes=t.get("totalSize", 0),
                    remaining_bytes=t.get("leftUntilDone", 0),
                    speed_bps=t.get("rateDownload", 0),
                    eta_seconds=t.get("eta") if t.get("eta", -1) >= 0 else None,
                    output_path=map_remote_path(os.path.join(t.get("downloadDir", ""), t["name"]), self.settings),
                    category=category,
                    message=t.get("errorString") or None,
                    position=None if done else t.get("queuePosition"),
                )
            )
        return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        await self._rpc("torrent-remove", {"ids": [item_id], "delete-local-data": delete_data})

    async def test(self) -> tuple[bool, str]:
        try:
            session = await self._rpc("session-get", {"fields": ["version"]})
            return True, f"Connected to Transmission {session.get('version', '')}."
        except Exception as exc:
            return False, str(exc)
