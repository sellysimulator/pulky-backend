"""Database base and session (spec 13.5): AC13, AC14, failure mode 6."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Integer, create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.db import session as db_session
from app.models.base import Base, TimestampMixin

ROOT = Path(__file__).resolve().parents[2]


def test_db_base_imports_in_a_fresh_interpreter() -> None:
    """Failure mode 6: a self-import in app/db/base.py raises ImportError."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.db.base as b; import app.models.base as m;"
            "assert b.Base is m.Base",
        ],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-1500:]


def test_db_base_reexports_the_models_base() -> None:
    from app.db.base import Base as ExportedBase

    assert ExportedBase is Base


@pytest.fixture
def stamped_table() -> Iterator[Any]:
    class Stamped(Base, TimestampMixin):
        __tablename__ = "skeleton_stamped_probe"
        id = Column(Integer, primary_key=True)

    yield Stamped.__table__
    Base.metadata.remove(Stamped.__table__)


def test_timestamp_columns_carry_a_server_default(stamped_table: Any) -> None:
    for name in ("created_at", "updated_at"):
        column = stamped_table.c[name]
        assert column.server_default is not None, name
        assert column.nullable is False, name
    ddl = str(CreateTable(stamped_table).compile(dialect=postgresql.dialect()))
    for line in ddl.splitlines():
        if "created_at" in line or "updated_at" in line:
            assert "DEFAULT" in line


def test_a_raw_insert_without_timestamps_gets_them(stamped_table: Any) -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    stamped_table.create(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO skeleton_stamped_probe (id) VALUES (1)"))
        row = conn.execute(
            text("SELECT created_at, updated_at FROM skeleton_stamped_probe")
        ).one()
    assert row[0] is not None and row[1] is not None


def test_get_engine_is_lazy_cached_and_a_psycopg_engine() -> None:
    engine = db_session.get_engine()
    assert isinstance(engine, Engine)
    assert db_session.get_engine() is engine
    assert engine.url.drivername == "postgresql+psycopg"
    # 2 + 1 per instance: Selly, Pulky and Beery 3 each, Lemony 4, plus a
    # side-by-side deploy instance (at most 4) and alembic and psql (1 each)
    # fit the shared 20-connection service (DEPLOY.md)
    assert engine.pool.size() == 2  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == 1  # type: ignore[attr-defined]
    pulky = engine.pool.size() + engine.pool._max_overflow  # type: ignore[attr-defined]
    assert 2 * 3 + pulky + 4 + 4 + 2 <= 20
    assert engine.pool.timeout() == 10  # type: ignore[attr-defined]


def test_engine_connects_with_the_session_time_zone_set_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The time zone travels in the startup packet (``options``), not in a
    statement run on each new connection."""
    seen: dict[str, Any] = {}

    def fake_create_engine(url: str, **kw: Any) -> Engine:
        seen.update(kw)
        return create_engine("sqlite://", poolclass=StaticPool)

    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)
    db_session.get_engine()
    assert seen["connect_args"]["options"] == "-c timezone=UTC"


def test_session_local_is_a_sessionmaker() -> None:
    assert isinstance(db_session.SessionLocal, sessionmaker)


def test_get_db_yields_a_session_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    generator = db_session.get_db()
    session = next(generator)
    assert isinstance(session, Session)
    assert session.execute(text("SELECT 1")).scalar() == 1
    with pytest.raises(StopIteration):
        next(generator)
    assert session.in_transaction() is False
