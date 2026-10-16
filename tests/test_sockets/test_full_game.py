# ruff: noqa: F811
"""A full recorded game, lobby to game_persisted (plan section 16, criteria 4, 8).

The redaction audit of spec 11.1 runs over every emit of the game. The secret
config values are the distinctive ones of section 15 (SECRET_PATCH), and day 1
has two real peer trades (see test_play_redaction for the scenario).

Persistence seam: the spy of test_submit for every test but the last, which is
the ``dbschema`` test: it restores section 18's real function and points
``app.db.session.SessionLocal`` at the Testcontainers MySQL.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.services import db_service
from app.services.db_service import (
    find_game_by_public_id,
)
from app.services.db_service import (
    persist_finished_game as real_persist_finished_game,
)
from tests.conftest import contains_key, contains_value
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401
from tests.test_sockets.conftest import (
    SECRET_KEY_NAMES,
    SECRET_PATCH,
    SECRET_VALUES,
    Game,
    World,
    leaf_paths,
)
from tests.test_sockets.test_play_redaction import (
    BIG_ORDER,
    BUY_CAP,
    PEER_PATCH,
    PRICE_P2,
    PRICE_P3,
    SHOWN_CONFIG_PATHS,
    assert_room_clean,
    merge,
    player_facing,
    room_payloads,
)
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    collect_values,
    persist,
    play_day,
    room_engine,
    submit,
)

aio = pytest.mark.asyncio
DAYS = 7


async def play_whole_game(world: World) -> Game:
    """Lobby to the last submission; day 1 has two peer trades."""
    game = await world.running_game(merge(PEER_PATCH, SECRET_PATCH), players=4)
    await submit(
        game,
        "P1",
        1,
        order_spot=BIG_ORDER,
        peer_buy={"max_price": BUY_CAP, "min_quality": 0},
    )
    await submit(
        game,
        "P2",
        1,
        peer_sell={"price": PRICE_P2, "max_quality": 100, "max_units": 1},
    )
    await submit(
        game,
        "P3",
        1,
        peer_sell={"price": PRICE_P3, "max_quality": 100, "max_units": 1},
    )
    await submit(game, "P4", 1)
    for day in range(2, DAYS + 1):
        await play_day(game, day)
    return game


# ----------------------------------------------------------- the whole game
@aio
async def test_a_game_runs_from_the_lobby_to_game_persisted(
    world: World, persist: PersistSpy
) -> None:
    game = await play_whole_game(world)
    records = world.sm.records
    assert records[-1][1] == "game_persisted"
    assert records[-1][0] == game.code
    assert records[-1][2]["game_id"] == persist.result
    assert records[-2][1] == "game_finished"
    days_closed = [r[2]["day"] for r in records if r[1] == "day_closed"]
    assert days_closed == list(range(1, DAYS + 1))
    assert len([r for r in records if r[1] == "day_opened"]) == DAYS  # start + 6
    assert len([r for r in records if r[1] == "game_finished"]) == 1

    room = await world.room(game.code)
    assert room["state"] == "finished"
    assert room["persisted"] is True
    assert room["game_id"] == persist.result
    assert room["finished_at"]
    assert room_engine(room).ended_early is False

    # seq never goes backwards, and the room counter ends on the last one
    seqs = [r[2]["seq"] for r in records if "seq" in r[2]]
    assert seqs == sorted(seqs)
    assert room["seq"] == seqs[-1]


@aio
async def test_the_peer_trades_of_day_one_happened_and_are_public_only_in_total(
    world: World, persist: PersistSpy
) -> None:
    await play_whole_game(world)
    first = next(r[2] for r in world.sm.records if r[1] == "day_closed")
    assert first["public"]["peer_units"] == 2
    assert first["public"]["peer_avg_price"] == pytest.approx(18.17, abs=0.005)
    assert all(
        "peer_units" in r[2]["public"] and "peer_avg_price" in r[2]["public"]
        for r in world.sm.records
        if r[1] == "day_closed"
    )


# ---------------------------------------------------------- redaction (AC 8)
@aio
async def test_no_room_emit_carries_private_data_except_game_finished(
    world: World, persist: PersistSpy
) -> None:
    game = await play_whole_game(world)
    assert_room_clean(world, game.code)
    # the exemption is real: game_finished carries the final stats
    finished = next(r[2] for r in world.sm.records if r[1] == "game_finished")
    assert contains_key(finished, "final_settlement")
    # and the room emits are not a trivial set
    events = {event for event, _ in room_payloads(world, game.code)}
    assert {"lobby_update", "game_started", "day_opened", "day_closed"} <= events
    assert {"decision_submitted", "game_persisted"} <= events


@aio
async def test_no_player_facing_emit_carries_a_secret_config_key_or_value(
    world: World, persist: PersistSpy
) -> None:
    game = await play_whole_game(world)
    facing = player_facing(world, game.code, game.host_sid)
    assert len(facing) > 50
    for event, payload in facing:
        for key in SECRET_KEY_NAMES:
            assert not contains_key(payload, key), (event, key)
        for value in SECRET_VALUES:
            assert not contains_value(payload, value), (event, value)
        assert not contains_key(payload, "fresh_days"), event
        assert not contains_key(payload, "max_days"), event
    configs = [c for _, p in facing for c in collect_values(p, "config")]
    assert configs
    for config in configs:
        assert leaf_paths(config) == SHOWN_CONFIG_PATHS
    # the host does see them: the search is capable of finding what it hunts
    host = [r[2] for r in world.sm.records if r[0] == game.host_sid]
    assert any(contains_value(p, 918273645) for p in host)


@aio
async def test_credentials_and_identities_never_travel_beyond_their_owner(
    world: World, persist: PersistSpy
) -> None:
    game = await play_whole_game(world)
    code = game.code
    identities = [
        i for sid, i in world.sm.sid_to_identity.items() if sid != game.host_sid
    ]
    identities.append(world.sm.sid_to_identity[game.host_sid])
    assert len(identities) == 5
    for record in world.sm.records:
        event, payload = record[1], record[2]
        if event != "joined":
            assert not contains_key(payload, "session_token"), event
        if event != "host_claimed":
            assert not contains_key(payload, "host_secret"), event
        assert not contains_key(payload, "identity"), event
        for identity in identities:
            assert not contains_value(payload, identity), event
        if event != "joined":
            for token in game.tokens.values():
                assert not contains_value(payload, token), event
        if event != "host_claimed":
            assert not contains_value(payload, game.secret), event
    # each token went only to its owner
    for alias, token in game.tokens.items():
        joined = [r for r in world.sm.records if r[1] == "joined"]
        owners = [r[0] for r in joined if r[2]["session_token"] == token]
        assert owners == [game.sids[alias]]
    assert code not in {r[0] for r in world.sm.records if r[1] == "joined"}


@aio
async def test_the_decision_of_day_one_is_in_no_broadcast_for_the_whole_game(
    world: World, persist: PersistSpy
) -> None:
    game = await play_whole_game(world)
    for _, payload in room_payloads(world, game.code):
        assert not contains_value(payload, BIG_ORDER)
        assert not contains_value(payload, BUY_CAP)
        assert not contains_value(payload, PRICE_P2)
        assert not contains_value(payload, PRICE_P3)
    # game_finished (excepted from the room rule) carries stats, not decisions
    finished = next(r[2] for r in world.sm.records if r[1] == "game_finished")
    assert not contains_key(finished, "order_spot")
    assert not contains_key(finished, "peer_buy")


# ------------------------------------------------------ real database (AC 4)
@pytest.fixture
def real_persistence(
    mysql_url: str, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> Iterator[None]:
    from app.db import session as db_session_module

    engine = create_engine(mysql_url)
    monkeypatch.setattr(db_service, "persist_finished_game", real_persist_finished_game)
    monkeypatch.setattr(db_session_module, "SessionLocal", sessionmaker(bind=engine))
    yield
    engine.dispose()


@aio
@pytest.mark.dbschema
async def test_a_full_game_persists_to_mysql_and_announces_the_game_id(
    world: World, real_persistence: None, db_session: Session  # noqa: F811
) -> None:
    game = await play_whole_game(world)
    records = world.sm.records
    assert [r[1] for r in records[-2:]] == ["game_finished", "game_persisted"]
    game_id = records[-1][2]["game_id"]
    assert isinstance(game_id, str) and game_id

    room = await world.room(game.code)
    assert room["persisted"] is True
    assert room["game_id"] == game_id
    row = find_game_by_public_id(db_session, game_id)
    assert row is not None
    assert row.room_code == game.code

    def count(table: str) -> Any:
        return db_session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()

    assert count("games") == 1
    assert count("participants") == 4
    assert count("player_days") == 4 * DAYS
    assert count("market_days") == DAYS
