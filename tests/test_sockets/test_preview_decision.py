# ruff: noqa: F811
"""``preview_decision`` (spec 12.1, 12.4): the decision form's figures for a draft,
answered as the event's acknowledgement to the asking player only. Read-only: no
emit, no save, no seq."""

from typing import Any

import pytest

from app.services import game_service
from app.sockets.handlers.play import preview_decision
from tests.test_sockets.conftest import World, count_engine_builds
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    dec,
    persist,
    room_engine,
    submit,
)

aio = pytest.mark.asyncio
MSG_NOT_IN_ROOM = "You are not in this room."
MSG_NOT_RUNNING = "The game is not running."


def ask(code: str, day: int, **over: Any) -> dict[str, Any]:
    decision = dec(code, day, **over)
    decision.pop("room_id")
    return {"room_id": code, "decision": decision}


@aio
async def test_a_seated_player_gets_the_engines_preview_and_nothing_else(
    world: World,
) -> None:
    game = await world.running_game()
    before_room = await world.room(game.code)
    before = world.mark()
    answer = await preview_decision("p1", ask(game.code, 1, order_spot=12))
    assert set(answer) == {"preview"}
    engine = game_service.load_engine(before_room)
    assert engine is not None
    alias = before_room["sid_to_alias"]["p1"]
    expected = engine.decision_preview(
        alias, ask(game.code, 1, order_spot=12)["decision"]
    )
    assert answer["preview"] == expected
    assert answer["preview"]["outlay"]["spot"] > 0
    # read-only: no emit to anyone, the room and its seq unchanged
    assert world.mark() == before
    assert await world.room(game.code) == before_room


@aio
async def test_a_preview_after_submitting_still_answers(world: World) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1, order_spot=4)
    answer = await preview_decision(game.sids["P1"], ask(game.code, 1, order_spot=4))
    assert answer["preview"]["errors"] == []


@aio
async def test_field_errors_come_back_with_their_spec_messages(world: World) -> None:
    game = await world.running_game()
    answer = await preview_decision("p1", ask(game.code, 1, order_spot=None))
    assert answer["preview"]["errors"] == [
        {
            "field": "order_spot",
            "message": "Orders must be whole numbers from 0 to 9,999.",
        }
    ]


@aio
async def test_not_seated_or_no_room_is_not_in_room(world: World) -> None:
    game = await world.running_game()
    await world.connect("ghost", 77)
    for sid, code in (
        ("ghost", game.code),
        (game.host_sid, game.code),
        ("p1", "QQQQQQ"),
    ):
        answer = await preview_decision(sid, ask(code, 1))
        assert answer == {"code": "NOT_IN_ROOM", "message": MSG_NOT_IN_ROOM}


@aio
async def test_a_lobby_or_finished_room_is_not_running(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code)
    await world.seat(code, "p1", 1)
    answer = await preview_decision("p1", ask(code, 1))
    assert answer == {"code": "GAME_NOT_RUNNING", "message": MSG_NOT_RUNNING}
    game = await world.running_game()
    await world.finish_early(game.code)
    answer = await preview_decision("p1", ask(game.code, 1))
    assert answer == {"code": "GAME_NOT_RUNNING", "message": MSG_NOT_RUNNING}


# ------------------------------------------------------------ engine reuse
@aio
async def test_previews_of_an_unchanged_room_build_its_engine_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails against a preview that rebuilds the engine from the room every time."""
    game = await world.running_game()
    room = await world.room(game.code)
    built = count_engine_builds(monkeypatch)
    first = await preview_decision("p1", ask(game.code, 1, order_spot=12))
    second = await preview_decision("p1", ask(game.code, 1, order_spot=20))
    assert len(built) == 1
    engine = room_engine(room)
    draft = ask(game.code, 1, order_spot=20)["decision"]
    assert second["preview"] == engine.decision_preview("P1", draft)
    assert first["preview"]["outlay"] != second["preview"]["outlay"]


@aio
async def test_a_preview_follows_the_room_once_it_changes(world: World) -> None:
    game = await world.running_game()
    await preview_decision("p1", ask(game.code, 1))
    await submit(game, "P1", 1)
    await submit(game, "P2", 1)  # day 1 resolves
    answer = await preview_decision("p1", ask(game.code, 2, order_spot=5))
    engine = room_engine(await world.room(game.code))
    draft = ask(game.code, 2, order_spot=5)["decision"]
    assert answer["preview"] == engine.decision_preview("P1", draft)
    assert answer["preview"]["day"] == 2


@aio
async def test_a_reissued_room_code_is_never_served_the_old_rooms_engine(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same code and seq, another host secret: another room, so another engine."""
    game = await world.running_game()
    await preview_decision("p1", ask(game.code, 1))
    seq = (await world.room(game.code))["seq"]

    def reissue(room: dict[str, Any]) -> None:
        room["host_secret"] = "another-room"
        room["seq"] = seq

    await world.mutate(game.code, reissue)
    built = count_engine_builds(monkeypatch)
    await preview_decision("p1", ask(game.code, 1))
    assert len(built) == 1
