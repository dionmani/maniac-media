"""Alembic environment.

Migrations are normally executed programmatically by ``backend.db.migrator`` at startup
(which passes an open connection). The CLI also works: ``alembic upgrade head``.
"""
from __future__ import annotations

from alembic import context

from backend.db import models  # noqa: F401  (register tables)
from backend.db.base import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    from backend.db.session import db_manager

    with db_manager.engine.connect() as conn:
        _run(conn)
        conn.commit()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():  # pragma: no cover
    raise SystemExit("Offline migrations are not supported; run against a live database.")
run_migrations_online()
