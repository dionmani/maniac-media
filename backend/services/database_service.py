"""Database migration wizard: SQLite → PostgreSQL / MySQL / MariaDB / MSSQL (or back).

Pipeline (each step must succeed before the next starts):
  1. **Validate** – connect, check server version, verify DDL permission.
  2. **Backup** – consistent copy of the current database (SQLite backup API, or a JSON dump).
  3. **Schema** – run Alembic migrations on the target so it is at the same revision.
  4. **Copy** – stream every table in foreign-key order inside one target transaction.
  5. **Verify** – row counts per table and schema comparison.
  6. **Switch** – store the new URL encrypted, hot-swap the engine.
On any failure the target transaction is rolled back and tables created by the wizard are
dropped; the application keeps running on the original database throughout.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from sqlalchemy import MetaData, Table, inspect, insert, select, text

from backend.core import paths
from backend.core.events import EventType, bus
from backend.core.secrets import SECRET_DB_URL, secret_store
from backend.core.settings import settings_manager
from backend.db import migrator
from backend.db.base import Base
from backend.db.session import create_db_engine, db_manager, mask_url, test_connection

log = logging.getLogger("maniac.dbmigrate")
CHUNK = 1000


@dataclass
class MigrationState:
    running: bool = False
    step: str = "idle"
    progress: float = 0.0
    message: str = ""
    target: str | None = None
    error: str | None = None
    backup: str | None = None
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    finished_at: str | None = None


state = MigrationState()
_lock = threading.Lock()
maintenance = threading.Event()  # while set, write requests are rejected


def _update(**kw) -> None:
    for k, v in kw.items():
        setattr(state, k, v)
    bus.publish(EventType.DATABASE_MIGRATION, **asdict(state))


def backup_current() -> str:
    engine = db_manager.engine
    if engine.dialect.name == "sqlite":
        path = migrator.backup_sqlite(engine, "pre-migration")
        return str(path) if path else ""
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.BACKUP_DIR / f"{engine.dialect.name}-pre-migration-{datetime.now():%Y%m%d-%H%M%S}.json.gz"
    with engine.connect() as conn, gzip.open(target, "wt", encoding="utf-8") as fh:
        for table in Base.metadata.sorted_tables:
            rows = [dict(r._mapping) for r in conn.execute(select(table))]
            fh.write(json.dumps({"table": table.name, "rows": rows}, default=_json_default) + "\n")
    return str(target)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def migrate(target_url: str, db_type: str, *, overwrite: bool = False) -> None:
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A database migration is already running")
    target_engine = None
    created_tables: list[str] = []
    try:
        _update(running=True, step="validate", progress=2, message="Validating connection", target=mask_url(target_url), error=None, tables={}, finished_at=None)
        ok, message = test_connection(target_url)
        if not ok:
            raise RuntimeError(message)
        target_engine = create_db_engine(target_url)
        if target_engine.url == db_manager.engine.url:
            raise RuntimeError("Target is the database already in use")
        existing = set(inspect(target_engine).get_table_names())
        ours = {t.name for t in Base.metadata.sorted_tables}
        populated = []
        with target_engine.connect() as conn:
            for name in existing & ours:
                if conn.execute(text(f"SELECT COUNT(*) FROM {target_engine.dialect.identifier_preparer.quote(name)}")).scalar():
                    populated.append(name)
        if populated and not overwrite:
            raise RuntimeError(f"Target already contains data in: {', '.join(sorted(populated))}. Enable overwrite to replace it.")

        maintenance.set()
        _quiesce_workers()
        _update(step="backup", progress=10, message="Backing up current database")
        backup = backup_current()
        _update(backup=backup)

        _update(step="schema", progress=20, message="Creating schema on target")
        created_tables = sorted(ours - existing)
        migrator.upgrade(target_engine)

        source_meta = MetaData()
        source_meta.reflect(bind=db_manager.engine)
        target_meta = MetaData()
        target_meta.reflect(bind=target_engine)

        tables = [t for t in Base.metadata.sorted_tables if t.name in source_meta.tables]
        totals = {}
        with db_manager.engine.connect() as src:
            for t in tables:
                totals[t.name] = src.execute(text(f"SELECT COUNT(*) FROM {db_manager.engine.dialect.identifier_preparer.quote(t.name)}")).scalar() or 0
        grand_total = max(1, sum(totals.values()))
        copied = 0

        _update(step="copy", progress=25, message="Copying data")
        with db_manager.engine.connect() as src, target_engine.begin() as dst:
            if populated:
                for t in reversed(tables):
                    dst.execute(target_meta.tables[t.name].delete())
            for t in tables:
                src_table: Table = source_meta.tables[t.name]
                dst_table: Table = target_meta.tables[t.name]
                columns = [c.name for c in dst_table.columns if c.name in src_table.columns]
                identity = target_engine.dialect.name == "mssql" and any(c.autoincrement is True or c.primary_key for c in dst_table.columns if c.name == "id")
                if identity:
                    dst.execute(text(f"SET IDENTITY_INSERT {dst_table.name} ON"))
                result = src.execution_options(stream_results=True).execute(select(*[src_table.c[c] for c in columns]))
                while True:
                    rows = result.fetchmany(CHUNK)
                    if not rows:
                        break
                    dst.execute(insert(dst_table), [dict(zip(columns, r)) for r in rows])
                    copied += len(rows)
                    _update(progress=25 + 60 * copied / grand_total, message=f"Copying {t.name} ({copied}/{grand_total} rows)")
                if identity:
                    dst.execute(text(f"SET IDENTITY_INSERT {dst_table.name} OFF"))
                if target_engine.dialect.name == "postgresql" and "id" in dst_table.c:
                    dst.execute(text(f"SELECT setval(pg_get_serial_sequence('{dst_table.name}', 'id'), COALESCE(MAX(id), 1)) FROM {dst_table.name}"))

            _update(step="verify", progress=88, message="Verifying row counts and schema")
            report = {}
            for t in tables:
                n = dst.execute(text(f"SELECT COUNT(*) FROM {target_engine.dialect.identifier_preparer.quote(t.name)}")).scalar() or 0
                report[t.name] = {"source": totals[t.name], "target": n}
                if n != totals[t.name]:
                    raise RuntimeError(f"Row count mismatch in {t.name}: {totals[t.name]} vs {n}")
            _update(tables=report)

        schema_problems = verify_schema(target_engine)
        if schema_problems:
            raise RuntimeError("Schema verification failed: " + "; ".join(schema_problems[:5]))

        _update(step="switch", progress=96, message="Switching application to the new database")
        secret_store.set(SECRET_DB_URL, target_url)
        settings_manager.update(db_type=db_type)
        db_manager.configure(target_url)
        _update(running=False, step="done", progress=100, message="Migration completed", finished_at=datetime.now().isoformat())
        log.info("Database migrated to %s", mask_url(target_url))
    except Exception as exc:
        log.error("Database migration failed: %s", exc)
        if target_engine is not None and created_tables:
            try:
                meta = MetaData()
                meta.reflect(bind=target_engine, only=[t for t in created_tables + ["alembic_version"] if t in inspect(target_engine).get_table_names()])
                meta.drop_all(bind=target_engine)
            except Exception as cleanup_exc:  # pragma: no cover
                log.error("Rollback cleanup failed: %s", cleanup_exc)
        _update(running=False, step="failed", error=str(exc), message="Migration failed – still running on the original database",
                finished_at=datetime.now().isoformat())
        raise
    finally:
        maintenance.clear()
        from backend.workers import queue as worker_queue

        if worker_queue.active_pool is not None:
            worker_queue.active_pool.paused = False
        if target_engine is not None:
            target_engine.dispose()
        _lock.release()


def _quiesce_workers(timeout: float = 120.0) -> None:
    """Stop claiming new jobs and wait for running ones so nothing writes mid-copy."""
    import time

    from backend.workers import queue as worker_queue

    if worker_queue.active_pool is not None:
        worker_queue.active_pool.paused = True
    _update(step="quiesce", progress=6, message="Waiting for background jobs to finish")
    deadline = time.monotonic() + timeout
    while worker_queue.running_job_count() > 0:
        if time.monotonic() > deadline:
            raise RuntimeError("Background jobs did not finish in time; try again when the system is idle")
        time.sleep(1)


def verify_schema(engine) -> list[str]:
    problems = []
    insp = inspect(engine)
    names = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in names:
            problems.append(f"missing table {table.name}")
            continue
        cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in cols:
                problems.append(f"missing column {table.name}.{col.name}")
    return problems
