from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from contracts.config import PROJECT_ROOT, get_settings


class Base(DeclarativeBase):
    """Shared SQLAlchemy 2.0 declarative base."""


def resolve_database_url(database_url: str | None = None) -> str:
    """Resolve the effective database URL from override, env, or shared settings."""
    if database_url:
        return database_url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    return get_settings().database_url


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create parent directory for file-backed SQLite databases if missing."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    db_path = url.database
    if not db_path or db_path == ":memory:":
        return
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine configured for SQLite (WAL) or PostgreSQL."""
    resolved_url = resolve_database_url(database_url)
    url = make_url(resolved_url)
    backend = url.get_backend_name()

    if backend == "sqlite":
        _ensure_sqlite_parent_dir(resolved_url)
        # Normalize relative sqlite paths against PROJECT_ROOT for consistent access across CWD
        if url.database and url.database != ":memory:" and not Path(url.database).is_absolute():
            abs_db = (PROJECT_ROOT / url.database).resolve()
            resolved_url = f"sqlite:///{abs_db}"

        db_engine = create_engine(
            resolved_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(db_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

        return db_engine

    return create_engine(resolved_url, pool_pre_ping=True, future=True)


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def configure_database(database_url: str | None = None) -> Engine:
    """Rebind module-level engine and SessionLocal (useful for tests)."""
    global engine
    engine = create_db_engine(database_url)
    SessionLocal.configure(bind=engine)
    return engine


def init_db(bind_engine: Engine | None = None) -> None:
    """Create all ERP and Ops tables if they do not already exist."""
    import services.mcp_erp.models  # noqa: F401

    Base.metadata.create_all(bind=bind_engine or engine)


def get_db() -> Generator[Session, None, None]:
    """Yield a scoped SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
