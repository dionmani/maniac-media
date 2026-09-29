"""Filesystem locations used by the application.

Every path can be overridden through environment variables so the same image runs
unchanged on Windows, Linux and inside Docker (where /data and /config are volumes).
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("MANIAC_DATA_DIR", BASE_DIR / "data")).resolve()

CONFIG_FILE = DATA_DIR / "config.json"
SECRETS_DB_FILE = DATA_DIR / "encrypted_config.db"
DEFAULT_SQLITE_FILE = DATA_DIR / "app.db"
LEGACY_SQLITE_FILE = DATA_DIR / "maniac_media.db"
LEGACY_MASTER_KEY_FILE = DATA_DIR / "master.key"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
PLUGIN_DIR = DATA_DIR / "plugins"
RECYCLE_DIR = DATA_DIR / "recycle"
SIMULATOR_STATE_FILE = DATA_DIR / "simulator_state.json"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


def ensure_dirs() -> None:
    for d in (DATA_DIR, BACKUP_DIR, LOG_DIR, PLUGIN_DIR):
        d.mkdir(parents=True, exist_ok=True)


def default_database_file() -> Path:
    """v1 installs used ``maniac_media.db``; keep using it so upgrades are seamless."""
    return LEGACY_SQLITE_FILE if LEGACY_SQLITE_FILE.exists() else DEFAULT_SQLITE_FILE
