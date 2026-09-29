"""SABnzbd API."""
from __future__ import annotations

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, map_remote_path
from backend.plugins.registry import register

QUEUE_STATUS = {"Downloading": "downloading", "Queued": "queued", "Paused": "paused", "Fetching": "downloading", "Grabbing": "downloading", "Propagating": "queued"}


def _hms(value: str | None) -> int | None:
    try:
        parts = [int(p) for p in (value or "").split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


@register
class SabnzbdClient(DownloadClientPlugin):
    key = "sabnzbd"
    name = "SABnzbd"
    description = "SABnzbd 3.x/4.x via its HTTP API."
    protocol = "usenet"
    default_port = 8080
    fields = [
        FieldSpec("host", "Host", required=True, default="localhost"),
        FieldSpec("port", "Port", "number", default=8080),
        FieldSpec("use_ssl", "Use SSL", "bool", default=False),
        FieldSpec("url_base", "URL base", default="sabnzbd"),
        FieldSpec("api_key", "API key", "password", required=True, secret=True),
        FieldSpec("category", "Category", default="maniac-media"),
    ]

    async def _api(self, mode: str, **params) -> dict:
        url = base_url({**self.settings, "url_base": self.setting("url_base", "sabnzbd")}, "/api")
        async with http.client(timeout=20) as c:
            resp = await http.request(c, "GET", url, params={"mode": mode, "output": "json", "apikey": self.setting("api_key", ""), **params})
            data = resp.json()
            if isinstance(data, dict) and data.get("status") is False:
                raise PluginError(f"SABnzbd: {data.get('error', 'request failed')}")
            return data

    async def add(self, release: ReleaseInfo, category: str) -> str:
        data = await self._api("addurl", name=release.download_url, cat=category, nzbname=release.title)
        ids = data.get("nzo_ids") or []
        if not ids:
            raise PluginError("SABnzbd did not accept the NZB")
        return ids[0]

    async def get_items(self, category: str) -> list[ClientItem]:
        queue = (await self._api("queue", category=category)).get("queue", {})
        items = []
        for s in queue.get("slots", []):
            mb, mbleft = float(s.get("mb", 0)), float(s.get("mbleft", 0))
            items.append(
                ClientItem(
                    id=s["nzo_id"],
                    name=s.get("filename", ""),
                    status=QUEUE_STATUS.get(s.get("status"), "downloading"),
                    progress=float(s.get("percentage", 0)),
                    size_bytes=int(mb * 1024 * 1024),
                    remaining_bytes=int(mbleft * 1024 * 1024),
                    speed_bps=int(float(queue.get("kbpersec", 0)) * 1024) if s.get("status") == "Downloading" else 0,
                    eta_seconds=_hms(s.get("timeleft")),
                    category=s.get("cat"),
                )
            )
        history = (await self._api("history", category=category, limit=100)).get("history", {})
        for s in history.get("slots", []):
            status = s.get("status")
            mapped = "completed" if status == "Completed" else "failed" if status == "Failed" else "downloading"
            items.append(
                ClientItem(
                    id=s["nzo_id"],
                    name=s.get("name", ""),
                    status=mapped,
                    progress=100.0 if mapped == "completed" else 0.0,
                    size_bytes=int(s.get("bytes", 0)),
                    output_path=map_remote_path(s.get("storage"), self.settings),
                    category=s.get("category"),
                    message=s.get("fail_message") or None,
                )
            )
        return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        flag = 1 if delete_data else 0
        await self._api("queue", name="delete", value=item_id, del_files=flag)
        await self._api("history", name="delete", value=item_id, del_files=flag)

    async def test(self) -> tuple[bool, str]:
        try:
            version = (await self._api("version")).get("version")
            await self._api("queue", limit=1)  # verifies the API key
            return True, f"Connected to SABnzbd {version}."
        except Exception as exc:
            return False, str(exc)
