# ruff: noqa: F811
"""Live/stored parity (section 18 AC 6, spec 13.12): ``build_stats`` over a
stored game equals ``engine.standings()`` exactly, peer stats and series included.
"""

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from app.core.engine import PulkyEngine
from app.core.stats import build_stats
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_services.test_persistence import (
    db_engine,  # noqa: F401
    factory,  # noqa: F401
    make_room,
    persist,
    play_big_game,
    play_peer_game,
    stored,
)

pytestmark = pytest.mark.dbschema


def stats_of_stored(g) -> dict:  # type: ignore[no-untyped-def]
    return build_stats(
        records=g.records,
        market=g.market,
        settlements=g.settlements,
        display_names=g.display_names,
        starting_reputation=g.config["reputation"]["starting"],
        seed=g.seed,
    )


def parity(engine: PulkyEngine, factory: Callable[[], Session], db: Session) -> dict:
    g = stored(db, persist(make_room(engine), factory, db))
    rebuilt = stats_of_stored(g)
    assert rebuilt == engine.standings()
    return rebuilt


def test_ac6_parity_for_a_game_that_ended_naturally(
    factory: Callable[[], Session], db_session: Session
) -> None:
    engine = play_peer_game(seed=11)
    assert engine.ended_early is False
    rebuilt = parity(engine, factory, db_session)
    # peer stats and series travel through the database too (not a vacuous pass)
    market = rebuilt["market"]
    assert market["peer_units_total"] > 0 and market["peer_trade_days"] == 1
    assert market["avg_peer_price"] is not None
    assert market["series"]["peer_avg_price"].count(None) == 6
    assert market["failure_days"] >= 1
    p1, p2 = rebuilt["players"]["P1"], rebuilt["players"]["P2"]
    assert p1["peer_units_bought"] > 0 and p2["peer_units_sold"] > 0
    assert p1["cost_peer"] == p2["revenue_peer"] > 0
    assert sum(p1["series"]["peer_bought"]) == p1["peer_units_bought"]
    assert p1["final_settlement"] == engine.settlements()["P1"].to_payload()


def test_ac6_parity_for_a_game_ended_early(
    factory: Callable[[], Session], db_session: Session
) -> None:
    engine = play_peer_game(seed=11, early=True)
    assert engine.ended_early is True
    rebuilt = parity(engine, factory, db_session)
    assert len(rebuilt["market"]["series"]["demand"]) == 3


@pytest.mark.parametrize("seed", [3, 5])
def test_ac6_parity_across_seeds(
    seed: int, factory: Callable[[], Session], db_session: Session
) -> None:
    parity(play_peer_game(seed=seed), factory, db_session)


def test_ac6_parity_when_the_peer_market_is_off(
    factory: Callable[[], Session], db_session: Session
) -> None:
    engine = play_peer_game(peer_enabled=False)
    rebuilt = parity(engine, factory, db_session)
    assert rebuilt["market"]["peer_units_total"] == 0
    assert rebuilt["market"]["avg_peer_price"] is None
    assert set(rebuilt["market"]["series"]["peer_avg_price"]) == {None}


def test_ac6_parity_for_a_full_table_of_twelve_players(
    factory: Callable[[], Session], db_session: Session
) -> None:
    rebuilt = parity(play_big_game(players=12, days=20), factory, db_session)
    assert [row["rank"] for row in rebuilt["leaderboard"]] == list(range(1, 13))
