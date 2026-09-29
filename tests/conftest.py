"""Test harness: every test session gets an isolated data directory and master key.

Environment variables must be set before ``backend`` is imported, because paths and the
secret provider are resolved at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="maniac-test-"))
os.environ["MANIAC_DATA_DIR"] = str(_DATA)
os.environ["MANIAC_MASTER_KEY"] = "pytest-master-key-passphrase"
os.environ.setdefault("MANIAC_LOG_LEVEL", "WARNING")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _DATA


@pytest.fixture(scope="session")
def library(data_dir: Path) -> dict[str, Path]:
    dirs = {name: data_dir / "lib" / name for name in ("movies", "tv", "downloads", "recycle")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    from backend.core.settings import settings_manager

    settings_manager.update(
        media_root_movies=str(dirs["movies"]), media_root_tv=str(dirs["tv"]), download_folder=str(dirs["downloads"]),
        recycle_bin=str(dirs["recycle"]), watch_download_folder=False, metadata_providers={
            "tmdb": {"enabled": False}, "tvdb": {"enabled": False}, "tvmaze": {"enabled": False}, "imdb": {"enabled": False},
        },
    )
    return dirs


@pytest.fixture(scope="session")
def database(library):
    from backend.main import bootstrap_database

    bootstrap_database()
    return True


@pytest.fixture(scope="session")
def client(database):
    from fastapi.testclient import TestClient

    from backend.core.ratelimit import api_limiter
    from backend.main import app

    with TestClient(app) as c:
        api_limiter.reset()
        yield c


@pytest.fixture(scope="session")
def admin_headers(client) -> dict[str, str]:
    r = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "Adm1n-Password!"})
    if r.status_code == 409:
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "Adm1n-Password!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


MKV = bytes.fromhex("1A45DFA3A34286810142F7810142F2810442F381084282886D6174726F736B61") + b"\x00" * 2048


@pytest.fixture
def make_video():
    def _make(path: Path, data: bytes = MKV) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    return _make
