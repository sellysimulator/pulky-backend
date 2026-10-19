"""Redaction across a whole lobby-to-finish flow (section 15, criterion 10).

Spec 11.1: nothing player-facing carries a secret, an identity, a session token
or a host secret, and the shown config is exactly the five keys of spec 9.3.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.defaults import PLAYER_CONFIG_KEYS
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import config_update, join, join_waiting, start_game
from tests.conftest import contains_key, contains_value
from tests.test_sockets.conftest import (
    SECRET_KEY_NAMES,
    SECRET_PATCH,
    SECRET_VALUES,
    World,
    all_keys,
    leaf_paths,
)

aio = pytest.mark.asyncio

ROOM_ONLY_FORBIDDEN_KEYS = {
    "cellar",
    "lots",
    "in_transit",
    "futures_book",
    "order_spot",
    "order_futures",
    "incoterm_tier",
    "conserve_lots",
    "allocated_spot",
    "futures_filled",
    "shortfall_spot",
    "peer_buy",
    "peer_sell",
    "peer_trades",
    "peer_bought",
    "peer_sold",
    "demand_own",
    "backlog",
    "profit",
    "cash",
    "ledger",
    "yesterday",
    "final_settlement",
}
ROOM_ONLY_FORBIDDEN_PREFIXES = ("cost_", "revenue_", "refund_")
HOST_SIDS = {"host-1", "host-2", "host-3"}


@dataclass
class Flow:
    code: str
    secret: str
    identities: dict[str, str] = field(default_factory=dict)  # name -> identity
    tokens: dict[str, str] = field(default_factory=dict)  # name -> session token
    owner_sid: dict[str, str] = field(default_factory=dict)  # name -> joined sid


async def lobby_to_finish(world: World) -> Flow:
    """Host claim, config save, joins, a leave, start, drops, reconnects, the end."""
    code = await world.create_room(SECRET_PATCH)
    secret = str((await world.room(code))["host_secret"])
    flow = Flow(code=code, secret=secret)
    await world.claim_host(code, "host-1", 900)
    await config_update(
        "host-1",
        {
            "room_id": code,
            "host_secret": secret,
            "config": {"demand": {"retail_price": 31}},
        },
    )

    async def seat(name: str, sid: str, who: int | str) -> None:
        joined = await world.seat(code, sid, who, name=name)
        flow.tokens[name] = joined["session_token"]
        flow.owner_sid[name] = sid
        flow.identities[name] = world.sm.sid_to_identity[sid]

    await seat("Alice", "p1", "firebase-uid-alice-7731")
    await seat("Beto", "p2", 2)
    await seat("Chela", "p3", 3)
    await world.leave(code, "p3")
    await start_game("host-1", {"room_id": code, "host_secret": secret})

    await disconnect("p1")  # Alice reconnects mid-game
    await world.connect("p1b", "firebase-uid-alice-7731")
    await join("p1b", {"room_id": code, "session_token": flow.tokens["Alice"]})
    await disconnect("host-1")  # the host returns by identity
    await world.connect("host-2", 900)
    await join_waiting("host-2", {"room_id": code})

    await world.finish_early(code, game_id="pub-game-1")
    await disconnect("p2")  # Beto reconnects after the end
    await world.connect("p2b", 2)
    await join("p2b", {"room_id": code, "session_token": flow.tokens["Beto"]})
    await disconnect("host-2")
    await world.connect("host-3", 900)
    await join_waiting("host-3", {"room_id": code, "host_secret": secret})
    return flow


def player_facing(world: World, flow: Flow) -> list[tuple[str, str, Any, str | None]]:
    return [r for r in world.sm.records if r[0] == flow.code or r[0] not in HOST_SIDS]


def room_emits(world: World, flow: Flow) -> list[tuple[str, str, Any, str | None]]:
    return [r for r in world.sm.records if r[0] == flow.code]


@aio
async def test_the_flow_exercises_the_events_under_test(world: World) -> None:
    flow = await lobby_to_finish(world)
    room_events = {r[1] for r in room_emits(world, flow)}
    assert {
        "lobby_update",
        "config_updated",
        "game_started",
        "day_opened",
        "participant_disconnected",
        "participant_reconnected",
        "host_disconnected",
        "host_reconnected",
    } <= room_events
    sid_events = {r[1] for r in player_facing(world, flow) if r[0] != flow.code}
    assert {"joined", "your_state", "game_finished", "game_persisted"} <= sid_events
    host_events = {r[1] for r in world.sm.records if r[0] in HOST_SIDS}
    assert {"host_claimed", "config_updated", "host_state"} <= host_events


@aio
async def test_no_player_facing_emit_carries_a_secret_key_or_value(
    world: World,
) -> None:
    flow = await lobby_to_finish(world)
    facing = player_facing(world, flow)
    assert facing
    for target, event, data, _ in facing:
        for key in SECRET_KEY_NAMES:
            assert not contains_key(data, key), f"{key} in {event} to {target}"
        for value in SECRET_VALUES:
            assert not contains_value(data, value), f"{value!r} in {event}"


@aio
async def test_no_player_facing_emit_carries_the_supplier_capacity(
    world: World,
) -> None:
    flow = await lobby_to_finish(world)
    facing = [r for r in player_facing(world, flow) if r[1] != "game_finished"]
    assert facing
    for target, event, data, _ in facing:
        assert not contains_key(data, "capacity"), f"capacity in {event} to {target}"


@aio
async def test_shown_config_is_exactly_the_five_keys(world: World) -> None:
    flow = await lobby_to_finish(world)
    shown = [
        r for r in room_emits(world, flow) if r[1] in ("config_updated", "game_started")
    ]
    assert {r[1] for r in shown} == {"config_updated", "game_started"}
    for _, event, data, _ in shown:
        assert leaf_paths(data["config"]) == set(PLAYER_CONFIG_KEYS), event
        assert data["config"]["demand"]["retail_price"] in (30, 31)
    assert not contains_key([r[2] for r in shown], "spot_spread")
    assert not contains_key([r[2] for r in shown], "holding_cost")


@aio
async def test_the_host_does_receive_the_full_config(world: World) -> None:
    flow = await lobby_to_finish(world)
    room = await world.room(flow.code)
    fulls = [
        r[2] for r in world.sm.records if r[0] == "host-1" and r[1] == "config_updated"
    ]
    assert fulls and fulls[-1]["config"] == room["config"]
    assert contains_value(fulls[-1], 918273645)
    states = [
        r[2] for r in world.sm.records if r[0] in HOST_SIDS and r[1] == "host_state"
    ]
    assert states and contains_value(states[-1], 918273645)


@aio
async def test_room_broadcasts_carry_no_private_game_keys(world: World) -> None:
    flow = await lobby_to_finish(world)
    broadcasts = [r for r in room_emits(world, flow) if r[1] != "game_finished"]
    assert broadcasts
    for _, event, data, _ in broadcasts:
        for key in all_keys(data):
            assert key not in ROOM_ONLY_FORBIDDEN_KEYS, f"{key} in room {event}"
            assert not key.startswith(
                ROOM_ONLY_FORBIDDEN_PREFIXES
            ), f"{key} in room {event}"


@aio
async def test_identity_and_host_secret_appear_nowhere_they_should_not(
    world: World,
) -> None:
    flow = await lobby_to_finish(world)
    for _target, event, data, _ in world.sm.records:
        assert not contains_key(data, "identity"), f"identity key in {event}"
        assert not contains_key(data, "host_secret") or event == "host_claimed"
        for ident in flow.identities.values():
            assert not contains_value(data, ident), f"identity value in {event}"
            assert ident not in str(data)
        assert flow.secret not in str(data) or event == "host_claimed"
    claimed = [r for r in world.sm.records if r[1] == "host_claimed"]
    assert claimed and all(r[0] in HOST_SIDS for r in claimed)


@aio
async def test_no_room_emit_carries_a_session_token(world: World) -> None:
    # Failure mode 8.
    flow = await lobby_to_finish(world)
    broadcasts = room_emits(world, flow)
    assert broadcasts
    for _, event, data, _ in broadcasts:
        assert not contains_key(data, "session_token"), event
        for token in flow.tokens.values():
            assert not contains_value(data, token), event
            assert token not in str(data), event


@aio
async def test_session_tokens_reach_only_their_owner_inside_joined(
    world: World,
) -> None:
    flow = await lobby_to_finish(world)
    for name, token in flow.tokens.items():
        hits = [r for r in world.sm.records if token in str(r[2])]
        assert hits, name
        assert {r[1] for r in hits} == {"joined"}
        owners = {r[0] for r in hits}
        assert owners <= {flow.owner_sid[name], f"{flow.owner_sid[name]}b"}
