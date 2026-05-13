from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Base


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    path = database_url.removeprefix("sqlite:///")
    if path == ":memory:":
        return
    db_path = Path(path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def get_session_factory() -> sessionmaker[Session]:
    settings = get_settings()
    return sessionmaker(bind=get_engine(settings.database_url), autoflush=False, autocommit=False)


def init_database() -> None:
    settings = get_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_device_telemetry_columns(engine)


def _migrate_sqlite_device_telemetry_columns(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        columns = {
            str(row[1])
            for row in connection.execute(text("PRAGMA table_info(devices)")).fetchall()
        }
        if "telemetry_status" not in columns:
            connection.execute(text("ALTER TABLE devices ADD COLUMN telemetry_status VARCHAR(40) NOT NULL DEFAULT 'unknown'"))
        if "telemetry_updated_at" not in columns:
            connection.execute(text("ALTER TABLE devices ADD COLUMN telemetry_updated_at DATETIME"))


def get_session() -> Generator[Session, None, None]:
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
