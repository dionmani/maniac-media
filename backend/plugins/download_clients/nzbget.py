"""NZBGet JSON-RPC."""
from __future__ import annotations

from backend.core import http
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec, PluginError
from backend.plugins.download_clients._common import base_url, map_remote_path
from backend.plugins.registry import register


@register
class NzbgetClient(DownloadClientPlugin):
    key = "nzbget"
    name = "NZBGet"
    description = "NZBGet 16+ via JSON-RPC."
    protocol = "usenet"
    default_port = 6789
    fields = [
        FieldSpec("host", "Host", required=True, default="localhost"),
        FieldSpec("port", "Port", "number", default=6789),
        FieldSpec("use_ssl", "Use SSL", "bool", default=False),
        FieldSpec("url_base", "URL base"),
        FieldSpec("username", "Username", default="nzbget"),
        FieldSpec("password", "Password", "password", secret=True),
        FieldSpec("category", "Category", default="maniac-media"),
    ]

    async def _rpc(self, method: str, *params):
        url = base_url(self.settings, "/jsonrpc")
        auth = (self.setting("username", ""), self.setting("password", ""))
        async with http.client(timeout=20, auth=auth) as c:
            resp = await http.request(c, "POST", url, json={"method": method, "params": list(params)}, raise_for_status=False)
            if resp.status_code == 401:
                raise PluginError("NZBGet rejected the credentials")
            if resp.status_code >= 400:
                raise PluginError(f"NZBGet HTTP {resp.status_code}")
            data = resp.json()
            if data.get("error"):
                raise PluginError(f"NZBGet: {data['error'].get('message')}")
            return data.get("result")

    async def add(self, release: ReleaseInfo, category: str) -> str:
        nzb_id = await self._rpc("append", f"{release.title}.nzb", release.download_url, category, 0, False, False, "", 0, "SCORE", [])
        if not nzb_id or int(nzb_id) <= 0:
            raise PluginError("NZBGet did not accept the NZB")
        return str(nzb_id)

    async def get_items(self, category: str) -> list[ClientItem]:
        items = []
        for g in await self._rpc("listgroups", 0) or []:
            if category and g.get("Category") != category:
                continue
            size = int(g.get("FileSizeMB", 0)) * 1024 * 1024
            remaining = int(g.get("RemainingSizeMB", 0)) * 1024 * 1024
            status = g.get("Status", "")
            items.append(
                ClientItem(
                    id=str(g["NZBID"]),
                    name=g.get("NZBName", ""),
                    status="paused" if "PAUSED" in status else "queued" if status == "QUEUED" else "downloading",
                    progress=round(100 * (size - remaining) / size, 2) if size else 0,
                    size_bytes=size,
                    remaining_bytes=remaining,
                    category=g.get("Category"),
                )
            )
        for h in await self._rpc("history", False) or []:
            if category and h.get("Category") != category:
                continue
            status = h.get("Status", "")
            mapped = "completed" if status.startswith("SUCCESS") else "failed" if status.startswith(("FAILURE", "DELETED")) else "warning"
            items.append(
                ClientItem(
                    id=str(h["NZBID"]),
                    name=h.get("Name", ""),
                    status=mapped,
                    progress=100.0 if mapped == "completed" else 0.0,
                    size_bytes=int(h.get("FileSizeMB", 0)) * 1024 * 1024,
                    output_path=map_remote_path(h.get("FinalDir") or h.get("DestDir"), self.settings),
                    category=h.get("Category"),
                    message=status if mapped != "completed" else None,
                )
            )
        return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        await self._rpc("editqueue", "GroupFinalDelete" if delete_data else "GroupDelete", "", [int(item_id)])
        await self._rpc("editqueue", "HistoryFinalDelete" if delete_data else "HistoryDelete", "", [int(item_id)])

    async def test(self) -> tuple[bool, str]:
        try:
            return True, f"Connected to NZBGet {await self._rpc('version')}."
        except Exception as exc:
            return False, str(exc)
