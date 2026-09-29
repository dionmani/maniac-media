"""Engine & session management with support for hot-swapping the active database."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.core import paths
from backend.core.secrets import SECRET_DB_URL, secret_store

log = logging.getLogger("maniac.db")

DRIVERS = {
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pyodbc",
}
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "mssql": 1433}


def build_url(
    db_type: str,
    *,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
    sqlite_path: str | None = None,
    options: dict[str, str] | None = None,
) -> str:
    """Build a connection URL with correct escaping of credentials."""
    if db_type == "sqlite":
        target = sqlite_path or str(paths.default_database_file())
        return f"sqlite:///{target}"
    if db_type not in DRIVERS:
        raise ValueError(f"Unsupported database engine '{db_type}'")
    query = dict(options or {})
    if db_type == "mssql":
        query.setdefault("driver", "ODBC Driver 18 for SQL Server")
        query.setdefault("TrustServerCertificate", "yes")
    if db_type in ("mysql", "mariadb"):
        query.setdefault("charset", "utf8mb4")
    url = URL.create(
        DRIVERS[db_type],
        username=username or None,
        password=password or None,
        host=host or "localhost",
        port=port or DEFAULT_PORTS[db_type],
        database=database or "maniac_media",
        query=query,
    )
    return url.render_as_string(hide_password=False)


def mask_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid url>"


def create_db_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800)


def test_connection(url: str) -> tuple[bool, str]:
    try:
        engine = create_db_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            version = conn.dialect.server_version_info
        engine.dispose()
        v = ".".join(str(p) for p in version) if version else ""
        return True, f"Connected successfully{f' (server {v})' if v else ''}."
    except ModuleNotFoundError as exc:
        return False, f"Database driver not installed: {exc.name}. See requirements-optional.txt."
    except Exception as exc:
        from backend.core.logging import redact

        return False, f"Connection failed: {redact(str(exc).splitlines()[0])}"


class DatabaseManager:
    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None
        self._url: str | None = None
        self._lock = threading.RLock()

    def resolve_url(self) -> str:
        url = secret_store.get(SECRET_DB_URL)
        if not url:
            url = build_url("sqlite")
            secret_store.set(SECRET_DB_URL, url)
        return url

    def configure(self, url: str | None = None) -> Engine:
        with self._lock:
            url = url or self.resolve_url()
            old = self._engine
            self._engine = create_db_engine(url)
            self._factory = sessionmaker(bind=self._engine, autoflush=False, expire_on_commit=False)
            self._url = url
            if old is not None:
                old.dispose()
            log.info("Database engine configured: %s", mask_url(url))
            return self._engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self.configure()
        assert self._engine is not None
        return self._engine

    @property
    def url(self) -> str:
        self.engine
        assert self._url is not None
        return self._url

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    def session(self) -> Session:
        self.engine
        assert self._factory is not None
        return self._factory()

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()


db_manager = DatabaseManager()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = db_manager.session()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Unit of work for background code: commit on success, rollback on error."""
    db = db_manager.session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
