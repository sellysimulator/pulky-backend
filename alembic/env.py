"""Alembic environment (spec 13.5): the URL comes from ``settings.db_url``.

A caller may set ``sqlalchemy.url`` explicitly (the test suite points it at a
Testcontainers MySQL); otherwise the application settings decide.
"""

from typing import Any

from sqlalchemy import create_engine, pool

import app.models.game  # noqa: F401
import app.models.user  # noqa: F401
from alembic import context
from app.config import settings
from app.db.base import Base

config = context.config
target_metadata = Base.metadata


def _target() -> tuple[str, dict[str, Any]]:
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit, {}
    return settings.db_url, settings.db_connect_args


def run_migrations_offline() -> None:
    url, _ = _target()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, connect_args = _target()
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
