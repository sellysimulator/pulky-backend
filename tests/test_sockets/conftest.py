"""The socket suites' guard; their fixtures are in tests/fixtures/."""

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _persistence_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No socket test reaches a database or leaves a retry running: persisting fails,
    as with no database, unless the test installs its own stand-in (``persist`` of
    tests/fixtures/play_loop.py), and a failure is not retried unless the test sets
    the delays."""
    from app.services import db_service, play_service

    def unavailable(room: dict[str, Any], session_factory: Any = None) -> None:
        return None

    monkeypatch.setattr(db_service, "persist_finished_game", unavailable)
    monkeypatch.setattr(play_service, "PERSIST_RETRY_DELAYS", ())
