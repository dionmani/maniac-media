"""Deluge via the Web UI JSON-RPC API."""
from __future__ import annotations

import base64
import itertools
import os

import httpx

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, fetch_torrent, map_remote_path
from backend.plugins.registry import register

STATE_MAP = {"Downloading": "downloading", "Seeding": "completed", "Paused": "paused", "Checking": "downloading",
             "Queued": "queued", "Error": "failed", "Moving": "downloading", "Allocating": "downloading"}
FIELDS = ["name", "state", "progress", "save_path", "total_size", "total_done", "download_payload_rate", "eta", "message", "label", "is_finished", "queue"]


@register
class DelugeClient(DownloadClientPlugin):
    key = "deluge"
    name = "Deluge"
    description = "Deluge 2.x via the Web UI (label plugin recommended)."
    protocol = "torrent"
    default_port = 8112
    fields = [
        FieldSpec("host", "Host", required=True, default="localhost"),
        FieldSpec("port", "Port", "number", default=8112),
        FieldSpec("use_ssl", "Use SSL", "bool", default=False),
        FieldSpec("url_base", "URL base"),
        FieldSpec("password", "Web UI password", "password", secret=True, default="deluge"),
        FieldSpec("category", "Label", default="maniac-media"),
    ]
    _ids = itertools.count(1)

    async def _call(self, c: httpx.AsyncClient, method: str, *params):
        resp = await http.request(c, "POST", "/json", json={"method": method, "params": list(params), "id": next(self._ids)})
        data = resp.json()
        if data.get("error"):
            raise PluginError(f"Deluge: {data['error'].get('message')}")
        return data.get("result")

    async def _session(self) -> httpx.AsyncClient:
        c = http.client(timeout=20, base_url=base_url(self.settings))
        if not await self._call(c, "auth.login", self.setting("password", "")):
            await c.aclose()
            raise PluginError("Deluge rejected the password")
        if not await self._call(c, "web.connected"):
            hosts = await self._call(c, "web.get_hosts") or []
            if not hosts:
                await c.aclose()
                raise PluginError("Deluge Web UI is not connected to a daemon")
            await self._call(c, "web.connect", hosts[0][0])
        return c

    async def add(self, release: ReleaseInfo, category: str) -> str:
        payload = await fetch_torrent(release)
        async with await self._session() as c:
            if payload.magnet:
                torrent_id = await self._call(c, "core.add_torrent_magnet", payload.magnet, {})
            else:
                torrent_id = await self._call(c, "core.add_torrent_file", f"{release.title}.torrent", base64.b64encode(payload.torrent or b"").decode(), {})
            torrent_id = (torrent_id or payload.info_hash or "").lower()
            try:
                labels = await self._call(c, "label.get_labels") or []
                if category not in labels:
                    await self._call(c, "label.add", category)
                await self._call(c, "label.set_torrent", torrent_id, category)
            except PluginError:
                pass  # label plugin not enabled – tracked by hash anyway
            return torrent_id

    async def get_items(self, category: str) -> list[ClientItem]:
        async with await self._session() as c:
            try:
                torrents = await self._call(c, "core.get_torrents_status", {"label": category}, FIELDS)
            except PluginError:
                torrents = await self._call(c, "core.get_torrents_status", {}, FIELDS)
        items = []
        for tid, t in (torrents or {}).items():
            finished = t.get("is_finished") or t.get("progress", 0) >= 100
            items.append(
                ClientItem(
                    id=tid.lower(),
                    name=t.get("name", ""),
                    status="completed" if finished and t.get("state") != "Error" else STATE_MAP.get(t.get("state"), "downloading"),
                    progress=round(float(t.get("progress", 0)), 2),
                    size_bytes=int(t.get("total_size", 0)),
                    remaining_bytes=max(0, int(t.get("total_size", 0)) - int(t.get("total_done", 0))),
                    speed_bps=int(t.get("download_payload_rate", 0)),
                    eta_seconds=int(t["eta"]) if t.get("eta") else None,
                    output_path=map_remote_path(os.path.join(t.get("save_path", ""), t.get("name", "")), self.settings),
                    category=t.get("label"),
                    message=t.get("message") if t.get("state") == "Error" else None,
                    position=t["queue"] if int(t.get("queue", -1)) >= 0 else None,  # -1 once finished
                )
            )
        return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        async with await self._session() as c:
            await self._call(c, "core.remove_torrent", item_id, delete_data)

    async def test(self) -> tuple[bool, str]:
        try:
            async with await self._session() as c:
                version = await self._call(c, "daemon.info")
            return True, f"Connected to Deluge {version or ''}."
        except Exception as exc:
            return False, str(exc)
