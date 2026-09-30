"""SABnzbd API."""
from __future__ import annotations

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, map_remote_path
from backend.plugins.registry import register

QUEUE_STATUS = {"Downloading": "downloading", "Queued": "queued", "Paused": "paused", "Fetching": "queued", "Grabbing": "queued",
                "Propagating": "queued", "Checking": "queued"}


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
        FieldSpec("url_base", "URL base"),
        FieldSpec("api_key", "API key", "password", required=True, secret=True),
        FieldSpec("category", "Category", default="maniac-media"),
    ]

    async def _api(self, mode: str, **params) -> dict:
        url = base_url(self.settings, "/api")
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

    def _queue_items(self, queue: dict) -> list[ClientItem]:
        items = []
        speed_assigned = False
        for s in queue.get("slots", []):
            mb, mbleft = float(s.get("mb", 0)), float(s.get("mbleft", 0))
            status = QUEUE_STATUS.get(s.get("status"), "queued")
            # SABnzbd labels every unpaused job "Downloading"; only the one with bytes transferred is really active
            if status == "downloading" and mbleft >= mb:
                status = "queued"
            speed = 0
            if status == "downloading" and not speed_assigned:
                speed, speed_assigned = int(float(queue.get("kbpersec", 0)) * 1024), True
            items.append(
                ClientItem(
                    id=s["nzo_id"],
                    name=s.get("filename", ""),
                    status=status,
                    progress=float(s.get("percentage", 0)),
                    size_bytes=int(mb * 1024 * 1024),
                    remaining_bytes=int(mbleft * 1024 * 1024),
                    speed_bps=speed,
                    eta_seconds=_hms(s.get("timeleft")) or None,  # "0:00:00" means unknown (paused, propagating, no speed)
                    category=s.get("cat"),
                )
            )
        return items

    def _history_items(self, history: dict) -> list[ClientItem]:
        items = []
        for s in history.get("slots", []):
            status = s.get("status")
            # anything else in history is post-processing (Verifying, Repairing, Extracting, Moving, Running...)
            mapped = "completed" if status == "Completed" else "failed" if status == "Failed" else "downloading"
            items.append(
                ClientItem(
                    id=s["nzo_id"],
                    name=s.get("name", ""),
                    status=mapped,
                    progress=0.0 if mapped == "failed" else 100.0,
                    size_bytes=int(s.get("bytes", 0)),
                    output_path=map_remote_path(s.get("storage"), self.settings),
                    category=s.get("category"),
                    message=s.get("fail_message") or None,
                )
            )
        return items

    async def get_items(self, category: str) -> list[ClientItem]:
        queue = (await self._api("queue", category=category, limit=0)).get("queue", {})
        history = (await self._api("history", category=category, limit=100)).get("history", {})
        return self._queue_items(queue) + self._history_items(history)

    async def find_items(self, item_ids: list[str]) -> list[ClientItem]:
        # Unfiltered by category: an unknown category makes SABnzbd file the job under Default instead
        wanted = {i.lower() for i in item_ids}
        nzo_ids = ",".join(item_ids)
        queue = (await self._api("queue", nzo_ids=nzo_ids, limit=0)).get("queue", {})
        history = (await self._api("history", nzo_ids=nzo_ids, limit=len(item_ids))).get("history", {})
        # older SABnzbd versions ignore nzo_ids, so filter here as well
        return [i for i in self._queue_items(queue) + self._history_items(history) if i.id.lower() in wanted]

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        flag = 1 if delete_data else 0
        await self._api("queue", name="delete", value=item_id, del_files=flag)
        await self._api("history", name="delete", value=item_id, del_files=flag)

    async def test(self) -> tuple[bool, str]:
        try:
            version = (await self._api("version")).get("version")
            await self._api("queue", limit=1)  # verifies the API key
            category = self.setting("category", "")
            cats = (await self._api("get_cats")).get("categories") or []
            if category and category.lower() not in {c.lower() for c in cats}:
                return True, (f"Connected to SABnzbd {version}, but category '{category}' does not exist there; "
                              "create it in SABnzbd so downloads are sorted correctly.")
            return True, f"Connected to SABnzbd {version}."
        except Exception as exc:
            return False, str(exc)
