"""Cross-engine checks. Runs only when a server URL is supplied (CI provides PostgreSQL):

    MANIAC_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@host/db pytest tests/integration/test_databases.py
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

TARGETS = {k: v for k, v in {
    "postgresql": os.environ.get("MANIAC_TEST_POSTGRES_URL"),
    "mysql": os.environ.get("MANIAC_TEST_MYSQL_URL"),
    "mssql": os.environ.get("MANIAC_TEST_MSSQL_URL"),
}.items() if v}

pytestmark = pytest.mark.skipif(not TARGETS, reason="no external database configured")


@pytest.mark.parametrize("engine_name", sorted(TARGETS))
def test_schema_migrates_on_engine(engine_name):
    from backend.db import migrator
    from backend.db.session import create_db_engine
    from backend.services.database_service import verify_schema

    engine = create_db_engine(TARGETS[engine_name])
    migrator.upgrade(engine)
    assert verify_schema(engine) == []
    assert migrator.current_revision(engine) == migrator.head_revision()


@pytest.mark.parametrize("engine_name", sorted(TARGETS))
def test_wizard_copies_everything(engine_name, database):
    from backend.core.secrets import SECRET_DB_URL, secret_store
    from backend.db.session import create_db_engine, db_manager
    from backend.services import database_service

    original = db_manager.url
    target = TARGETS[engine_name]
    try:
        database_service.migrate(target, engine_name, overwrite=True)
        assert all(v["source"] == v["target"] for v in database_service.state.tables.values())
        with create_db_engine(target).connect() as conn:
            assert "movies" in inspect(conn).get_table_names()
            conn.execute(text("SELECT COUNT(*) FROM movies")).scalar()
    finally:
        secret_store.set(SECRET_DB_URL, original)
        db_manager.configure(original)
