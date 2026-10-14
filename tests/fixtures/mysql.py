"""Shared MySQL fixtures (plan section 17, used by 17, 18 and 19).

A Testcontainers MySQL 8 under ``STRICT_ALL_TABLES`` with ``alembic upgrade head``
applied. Nothing here ever connects to a database named by ``settings``.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    """An Alembic config aimed explicitly at ``url`` (a Testcontainers MySQL)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def mysql_url() -> Iterator[str]:
    from testcontainers.community.mysql import MySqlContainer

    from alembic import command

    container = MySqlContainer("mysql:8.0", dialect="pymysql")
    container.with_command("--sql-mode=STRICT_ALL_TABLES")
    with container as mysql:
        url = mysql.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture()
def db_session(mysql_url: str) -> Iterator[Session]:
    engine = create_engine(mysql_url)
    session = Session(engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            names = conn.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME <> 'alembic_version'"
                )
            ).scalars()
            for name in list(names):
                conn.execute(text(f"TRUNCATE TABLE `{name}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        engine.dispose()
