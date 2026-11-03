# ruff: noqa: F811
"""``preview_decision`` (spec 12.1, 12.4): the decision form's figures for a draft,
answered as the event's acknowledgement to the asking player only. Read-only: no
emit, no save, no seq."""

from typing import Any

import pytest

from app.services import game_service
from app.sockets.handlers.play import preview_decision
from tests.test_sockets.conftest import World
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    dec,
    persist,
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
