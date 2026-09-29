"""Schema migration runner (Alembic) with automatic pre-migration backups."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url

from backend.core import paths

log = logging.getLogger("maniac.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BASELINE = "0001_baseline"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def backup_sqlite(engine: Engine, label: str) -> Path | None:
    """Consistent online copy using SQLite's backup API (safe with WAL)."""
    if engine.dialect.name != "sqlite":
        return None
    db_path = make_url(str(engine.url)).database
    if not db_path or db_path == ":memory:" or not Path(db_path).exists():
        return None
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.BACKUP_DIR / f"{Path(db_path).stem}-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.info("Database backed up to %s", target)
    return target


def upgrade(engine: Engine) -> dict:
    """Bring ``engine`` to head. Returns a summary for health/diagnostics."""
    cfg = alembic_config()
    head = head_revision()
    tables = set(inspect(engine).get_table_names())
    current = current_revision(engine)
    backup = None

    if current == head:
        return {"from": current, "to": head, "backup": None, "changed": False}

    if tables and current is None and "movies" in tables:
        # A v1 database: tables exist but Alembic never ran. Adopt it at the baseline.
        backup = backup_sqlite(engine, "pre-v2")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.stamp(cfg, BASELINE)
        current = BASELINE
        log.info("Adopted existing v1 database at revision %s", BASELINE)
    elif tables and current is not None:
        backup = backup_sqlite(engine, f"pre-{head}")

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    log.info("Database schema upgraded %s -> %s", current or "empty", head)
    return {"from": current, "to": head, "backup": str(backup) if backup else None, "changed": True}
