"""Simulator download client – opt-in stand-in that "downloads" into the watch folder.

Progress is derived from elapsed time (state persisted to disk, so it survives restarts).
Completed items are written as tiny files with a valid Matroska (EBML) header so they
exercise the real import, validation and rename pipeline.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from backend.core import paths
from backend.domain.types import ClientItem, ReleaseInfo
from backend.plugins.base import DownloadClientPlugin, FieldSpec
from backend.plugins.registry import register

EBML_HEADER = bytes.fromhex("1A45DFA3A34286810142F7810142F2810442F381084282886D6174726F736B61")
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(paths.SIMULATOR_STATE_FILE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    paths.SIMULATOR_STATE_FILE.write_text(json.dumps(state, indent=1))


@register
class SimulatorClient(DownloadClientPlugin):
    key = "simulator"
    name = "Simulator"
    description = "Fake download client for demos and testing. Writes placeholder files into the download folder."
    protocol = "torrent"
    default_port = 0
    fields = [
        FieldSpec("speed_mb", "Simulated speed (MB/s)", "number", default=400),
        FieldSpec("category", "Category", default="maniac-media"),
    ]

    def _folder(self) -> Path:
        return Path(self.setting("download_folder") or paths.BASE_DIR / "downloads")

    async def add(self, release: ReleaseInfo, category: str) -> str:
        item_id = f"sim-{int(time.time() * 1000)}"
        with _lock:
            state = _load()
            state[item_id] = {"name": release.title, "size": max(release.size_bytes, 50 * 1024**2), "added": time.time(), "category": category}
            _save(state)
        return item_id

    async def get_items(self, category: str) -> list[ClientItem]:
        speed = max(1, int(self.setting("speed_mb", 400))) * 1024 * 1024
        items = []
        with _lock:
            state = _load()
            for item_id, s in state.items():
                if s.get("category") != category:
                    continue
                # never take longer than 15 s, however large the fake release is
                effective = max(speed, s["size"] / 15)
                done = int((time.time() - s["added"]) * effective)
                progress = min(100.0, done * 100 / s["size"])
                output = self._folder() / s["name"]
                if progress >= 100 and not s.get("written"):
                    output.mkdir(parents=True, exist_ok=True)
                    (output / f"{s['name']}.mkv").write_bytes(EBML_HEADER + b"\x00" * 4096)
                    s["written"] = True
                items.append(
                    ClientItem(
                        id=item_id,
                        name=s["name"],
                        status="completed" if progress >= 100 else "downloading",
                        progress=round(progress, 1),
                        size_bytes=s["size"],
                        remaining_bytes=max(0, s["size"] - done),
                        speed_bps=0 if progress >= 100 else int(effective),
                        eta_seconds=max(0, int((s["size"] - done) / effective)) if progress < 100 else 0,
                        output_path=str(output),
                        category=category,
                    )
                )
            _save(state)
        return items

    async def remove(self, item_id: str, delete_data: bool = False) -> None:
        with _lock:
            state = _load()
            state.pop(item_id, None)
            _save(state)

    async def test(self) -> tuple[bool, str]:
        return True, "Simulator ready."
