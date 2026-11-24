"""Shared PostgreSQL fixtures (used by the model, persistence and REST suites).

A Testcontainers ``postgres:18`` with ``alembic upgrade head`` applied. Nothing here
ever connects to a database named by ``settings``.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "postgres:18"


def alembic_config(url: str) -> Config:
    """An Alembic config aimed explicitly at ``url`` (a Testcontainers PostgreSQL)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def pg_container() -> Iterator[Any]:
    from testcontainers.postgres import PostgresContainer

    from alembic import command

    with PostgresContainer(IMAGE, driver="psycopg") as container:
        command.upgrade(alembic_config(container.get_connection_url()), "head")
        yield container


@pytest.fixture(scope="session")
def pg_url(pg_container: Any) -> str:
    return str(pg_container.get_connection_url())


@pytest.fixture()
def db_session(pg_url: str) -> Iterator[Session]:
    engine = create_engine(pg_url)
    session = Session(engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            names = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_type = 'BASE TABLE' "
                    "AND table_name <> 'alembic_version'"
                )
            ).scalars()
            quoted = ", ".join(f'"{name}"' for name in names)
            if quoted:
                conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        engine.dispose()


@pytest.fixture()
def db_engine(pg_url: str) -> Iterator[Engine]:
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(db_engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=db_engine)


def raw_insert(session: Session, table: str, **values: Any) -> int:
    """Raw INSERT naming every NOT NULL column without a database default, then
    commit; the new ``id`` (0 for ``user_stats``, keyed by ``user_id``).

    Columns are read from ``information_schema`` and filled by type, so a test
    states only what it cares about. Never names ``created_at``/``updated_at``;
    a serial ``id`` has a default and is never named either.
    """
    rows = session.execute(
        text(
            "SELECT column_name, data_type, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "AND is_nullable = 'NO' AND column_default IS NULL"
        ),
        {"t": table},
    ).all()
    filled: dict[str, Any] = {}
    for name, data_type, length in rows:
        if data_type in ("json", "jsonb"):
            filled[name] = "[]"
        elif data_type in ("character", "character varying"):
            filled[name] = "x" * min(3, int(length))
        elif data_type.startswith("timestamp"):
            filled[name] = datetime(2026, 10, 12, 14, 0, 0, tzinfo=UTC)
        elif data_type == "boolean":
            filled[name] = False
        else:
            filled[name] = 0
    filled.update(values)
    cols = ", ".join(f'"{c}"' for c in filled)
    marks = ", ".join(f":{c}" for c in filled)
    returning = "" if table == "user_stats" else " RETURNING id"
    result = session.execute(
        text(f'INSERT INTO "{table}" ({cols}) VALUES ({marks}){returning}'), filled
    )
    new_id = int(result.scalar_one()) if returning else 0
    session.commit()
    return new_id
