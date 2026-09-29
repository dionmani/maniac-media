"""Shared helpers for download client plugins."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from backend.core import http
from backend.domain.types import ReleaseInfo
from backend.plugins.base import PluginError


def base_url(settings: dict, default_path: str = "") -> str:
    scheme = "https" if settings.get("use_ssl") else "http"
    host = (settings.get("host") or "localhost").strip().rstrip("/")
    host = re.sub(r"^https?://", "", host)
    port = settings.get("port")
    url_base = (settings.get("url_base") or "").strip("/")
    root = f"{scheme}://{host}{f':{port}' if port else ''}"
    if url_base:
        root += f"/{url_base}"
    return root + default_path


# ---------------------------------------------------------------- torrent helpers
def _bdecode_info_span(data: bytes) -> tuple[int, int]:
    """Return the byte span of the top-level ``info`` dictionary in a bencoded torrent."""

    def parse(i: int) -> int:
        c = data[i : i + 1]
        if c == b"i":
            return data.index(b"e", i) + 1
        if c in (b"l", b"d"):
            i += 1
            while data[i : i + 1] != b"e":
                i = parse(i)
            return i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            return colon + 1 + int(data[i:colon])
        raise ValueError("invalid bencode")

    if data[:1] != b"d":
        raise ValueError("torrent is not a dictionary")
    i = 1
    while data[i : i + 1] != b"e":
        colon = data.index(b":", i)
        key_end = colon + 1 + int(data[i:colon])
        key = data[colon + 1 : key_end]
        value_end = parse(key_end)
        if key == b"info":
            return key_end, value_end
        i = value_end
    raise ValueError("torrent has no info dictionary")


def torrent_info_hash(data: bytes) -> str:
    start, end = _bdecode_info_span(data)
    return hashlib.sha1(data[start:end]).hexdigest()


def magnet_info_hash(magnet: str) -> str | None:
    for xt in parse_qs(urlparse(magnet).query).get("xt", []):
        if xt.startswith("urn:btih:"):
            value = xt[9:]
            if len(value) == 32:  # base32
                return base64.b32decode(value.upper()).hex()
            return value.lower()
    return None


@dataclass
class TorrentPayload:
    info_hash: str | None
    magnet: str | None = None
    torrent: bytes | None = None


async def fetch_torrent(release: ReleaseInfo) -> TorrentPayload:
    """Resolve a release to either a magnet link or .torrent bytes, computing the info hash."""
    url = release.magnet_url if (release.magnet_url and not release.download_url.startswith("http")) else release.download_url
    if url.startswith("magnet:"):
        return TorrentPayload(magnet_info_hash(url) or release.info_hash, magnet=url)
    async with http.client(timeout=30, follow_redirects=False) as c:
        for _ in range(5):
            resp = await http.request(c, "GET", url, raise_for_status=False)
            if resp.is_redirect:
                location = resp.headers.get("Location", "")
                if location.startswith("magnet:"):
                    return TorrentPayload(magnet_info_hash(location) or release.info_hash, magnet=location)
                url = str(httpx.URL(url).join(location))
                continue
            if resp.status_code >= 400:
                raise PluginError(f"Indexer refused the torrent download (HTTP {resp.status_code})")
            data = resp.content
            try:
                return TorrentPayload(torrent_info_hash(data), torrent=data)
            except ValueError as exc:
                if release.magnet_url:
                    return TorrentPayload(magnet_info_hash(release.magnet_url), magnet=release.magnet_url)
                raise PluginError("Downloaded file is not a valid torrent") from exc
    raise PluginError("Too many redirects fetching torrent")


def map_remote_path(path: str | None, settings: dict) -> str | None:
    """Translate a path reported by the client into a local path (remote path mapping)."""
    if not path:
        return path
    remote, local = settings.get("remote_path"), settings.get("local_path")
    if remote and local:
        norm_path, norm_remote = path.replace("\\", "/"), remote.replace("\\", "/").rstrip("/")
        if norm_path.lower().startswith(norm_remote.lower()):
            rest = norm_path[len(norm_remote):].lstrip("/")
            sep = "\\" if "\\" in local else "/"
            return local.rstrip("\\/") + (sep + rest.replace("/", sep) if rest else "")
    return path
