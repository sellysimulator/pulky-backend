# ruff: noqa: F811
"""Event contract for the play loop (plan section 16, criterion 9; spec 12.1-12.2).

One run covers the lobby, a game played to its natural end, a second game ended
early, the errors play can raise, a request_state, and a disconnect/reconnect.
Every event name and every error code that appears must be one the spec lists.

Persistence seam: the spy of test_submit (see its docstring).
"""

import pytest

from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.host import end_game_early
from app.sockets.handlers.lobby import config_update, start_game
from app.sockets.handlers.play import request_state, submit_day
from tests.test_sockets.conftest import World
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    dec,
    persist,
    play_day,
    submit,
)

aio = pytest.mark.asyncio

# spec 12.2, "Server -> client" table, verbatim
SERVER_EVENTS = {
    "joined",
    "host_claimed",
    "join_error",
    "leave_ack",
    "lobby_update",
    "config_updated",
    "game_started",
    "day_opened",
    "your_state",
    "host_state",
    "decision_submitted",
    "day_closed",
    "your_day_closed",
    "participant_disconnected",
    "participant_reconnected",
    "host_disconnected",
    "host_reconnected",
    "game_finished",
    "game_persisted",
    "error",
}
# spec 12 intro: the events that carry no seq
NO_SEQ = {"joined", "host_claimed", "join_error", "leave_ack", "error"}
ERROR_CODES = {
    "NOT_IN_ROOM",
    "STALE_DAY",
    "ALREADY_SUBMITTED",
    "INVALID_DECISION",
    "INVALID_CONFIG",
    "INVALID_HOST_SECRET",
    "GAME_NOT_RUNNING",
    "GAME_FINISHED",
    "SERVER_ERROR",
}
PATCH = {"duration_days": 7, "futures": {"horizon_days": 3}}


@aio
async def test_every_event_and_error_code_emitted_in_a_game_is_in_the_spec(
    world: World, persist: PersistSpy
) -> None:
    # game A: lobby, config change, reconnect, errors, natural end
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code)
    await world.seat(code, "a1", 1)
    token = (await world.seat(code, "a2", 2))["session_token"]
    await config_update(
        "host-1", {"room_id": code, "host_secret": secret, "config": PATCH}
    )
    await disconnect("a2")
    await world.seat(code, "a2b", 2, token=token)
    await start_game("host-1", {"room_id": code, "host_secret": secret})
    await submit_day("a1", dec(code, 1))
    await submit_day("a1", dec(code, 1))  # ALREADY_SUBMITTED
    await submit_day("ghost", dec(code, 1))  # NOT_IN_ROOM
    await submit_day("a1", dec(code, 1, order_spot=10_000))  # INVALID_DECISION
    await submit_day("a2b", dec(code, 1))
    await submit_day("a1", dec(code, 1))  # STALE_DAY (+ your_state)
    await request_state("a1", {"room_id": code})
    await request_state("host-1", {"room_id": code})
    await end_game_early("a1", {"room_id": code})  # INVALID_HOST_SECRET

    sids = {"P1": "a1", "P2": "a2b"}
    for day in range(2, 8):
        for alias in ("P1", "P2"):
            await submit_day(sids[alias], dec(code, day))
    await end_game_early("host-1", {"room_id": code, "host_secret": secret})  # over
    await submit_day("a1", dec(code, 7))  # GAME_FINISHED

    # game B: ended early
    game = await world.running_game(PATCH)
    await play_day(game, 1)
    await submit(game, "P1", 2)
    await end_game_early(game.host_sid, game.start_data())

    emitted = [r for r in world.sm.records]
    names = {r[1] for r in emitted}
    assert names <= SERVER_EVENTS, names - SERVER_EVENTS
    # the run really did exercise the play loop
    assert {
        "decision_submitted",
        "day_closed",
        "your_day_closed",
        "your_state",
        "host_state",
        "day_opened",
        "game_finished",
        "game_persisted",
        "error",
        "participant_disconnected",
    } <= names

    codes = {r[2]["code"] for r in emitted if r[1] == "error"}
    assert codes <= ERROR_CODES, codes - ERROR_CODES
    assert {
        "ALREADY_SUBMITTED",
        "NOT_IN_ROOM",
        "INVALID_DECISION",
        "STALE_DAY",
        "INVALID_HOST_SECRET",
        "GAME_NOT_RUNNING",
        "GAME_FINISHED",
    } <= codes
    for record in emitted:
        payload = record[2]
        if record[1] == "error":
            assert isinstance(payload["message"], str) and payload["message"]
        if record[1] not in NO_SEQ:
            assert isinstance(payload["seq"], int), record[1]
