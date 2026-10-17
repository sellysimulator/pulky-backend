# ruff: noqa: F811
"""Black-box redaction tests for the play loop (plan section 16, 3.5; criteria 8, 14).

Failure modes 2, 9 and 10. One day of real peer trades is played with explicit
numbers: P1 orders far more than the supplier ships, holds buy limits of
$23.17, and buys one unit from P2 at $17.43 and one from P3 at $18.91; P4 sits
the day out. The two prices are the only way the test can tell a leaked trade
from public data, so no default (price, capacity, quality) is assumed.

Persistence seam: the spy of test_submit (see its docstring).
"""

from collections.abc import Iterable
from typing import Any

import pytest

from app.sockets.handlers.play import request_state
from tests.conftest import contains_key, contains_value
from tests.test_sockets.conftest import Game, World, all_keys, leaf_paths
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    collect_values,
    persist,
    play_day,
    submit,
)

aio = pytest.mark.asyncio

BUY_CAP = 23.17
PRICE_P2 = 17.43
PRICE_P3 = 18.91
BIG_ORDER = 4321
PEER_PATCH: dict[str, Any] = {
    "starting_cash": 100_000,
    "duration_days": 7,
    "futures": {"horizon_days": 3},
}

# spec 11.1: keys no room broadcast may carry (game_finished excepted)
ROOM_BANNED_KEYS: tuple[str, ...] = (
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
    "identity",
    "session_token",
    "host_secret",
)
ROOM_BANNED_PREFIXES: tuple[str, ...] = ("cost_", "revenue_", "refund_")
SHOWN_CONFIG_PATHS = {
    "demand.retail_price",
    "transport.unit_cost",
    "stock.conservation_cost_per_unit",
    "stock.conservation_extra_fresh_days",
    "peer_market.enabled",
}


def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


# ------------------------------------------------------------ audit helpers
def room_payloads(
    world: World, code: str, exempt: Iterable[str] = ("game_finished",)
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (r[1], r[2])
        for r in world.sm.records
        if r[0] == code and r[1] not in tuple(exempt)
    ]


def player_facing(
    world: World, code: str, host_sid: str
) -> list[tuple[str, dict[str, Any]]]:
    """Room emits and emits to any sid but the host's."""
    return [(r[1], r[2]) for r in world.sm.records if r[0] == code or r[0] != host_sid]


def banned_keys_in(payload: Any) -> list[str]:
    bad = []
    for key in all_keys(payload):
        if key in ROOM_BANNED_KEYS or key.startswith(ROOM_BANNED_PREFIXES):
            bad.append(key)
    return bad


def assert_room_clean(world: World, code: str) -> None:
    emits = room_payloads(world, code)
    assert emits, "no room emit was recorded"
    for event, payload in emits:
        assert banned_keys_in(payload) == [], (event, banned_keys_in(payload))


def sid_payloads(world: World, sid: str, code: str) -> list[dict[str, Any]]:
    """Everything that reached ``sid``: its own emits and the room's."""
    return [r[2] for r in world.sm.records if r[0] == sid or (r[0] == code)]


# -------------------------------------------------------------- the scenario
async def peer_day(world: World, patch: dict[str, Any] | None = None) -> Game:
    """Day 1 of a four-player game with two one-unit peer trades; it resolves."""
    game = await world.running_game(merge(PEER_PATCH, patch or {}), players=4)
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
    return game


def day_closed_public(world: World) -> dict[str, Any]:
    found = [r[2] for r in world.sm.records if r[1] == "day_closed"]
    assert found
    return found[-1]["public"]


# --------------------------------------------------------- the day happened
@aio
async def test_the_scenario_produces_two_public_trades_and_a_public_average(
    world: World,
) -> None:
    game = await peer_day(world)
    public = day_closed_public(world)
    assert public["peer_units"] == 2
    assert public["peer_avg_price"] == pytest.approx(18.17, abs=0.005)
    assert (await world.room(game.code))["engine"] is not None


# ------------------------------------------------------ decisions (FM 2)
@aio
async def test_a_decision_never_appears_on_a_room_broadcast(world: World) -> None:
    game = await world.running_game(PEER_PATCH, players=4)
    start = world.mark()
    await submit(
        game,
        "P1",
        1,
        order_spot=BIG_ORDER,
        peer_buy={"max_price": BUY_CAP, "min_quality": 0},
    )
    submitted = world.sent(game.code, "decision_submitted", since=start)
    assert set(submitted[0]) == {"seq", "day", "alias", "awaiting"}
    for _, payload in room_payloads(world, game.code):
        assert not contains_value(payload, BIG_ORDER)
        assert not contains_value(payload, BUY_CAP)

    for alias, price in (("P2", PRICE_P2), ("P3", PRICE_P3), ("P4", None)):
        over: dict[str, Any] = {}
        if price:
            over["peer_sell"] = {"price": price, "max_quality": 100, "max_units": 1}
        await submit(game, alias, 1, **over)
    await play_day(game, 2)

    for _, payload in room_payloads(world, game.code):
        assert not contains_value(payload, BIG_ORDER)
        assert not contains_value(payload, BUY_CAP)
    assert_room_clean(world, game.code)
    # the order is in P1's own payloads (and the host's), so the search can find it
    own = [r[2] for r in world.sm.records if r[0] == "p1" and r[1] == "your_day_closed"]
    assert contains_value(own[0], BIG_ORDER)
    host = [r[2] for r in world.sm.records if r[0] == "host-1"]
    assert any(contains_value(p, BIG_ORDER) for p in host)
    for other in ("p2", "p3", "p4"):
        for payload in sid_payloads(world, other, game.code):
            assert not contains_value(payload, BIG_ORDER)


# ------------------------------------------------------- prices (FM 9)
@aio
async def test_trade_prices_stay_off_the_room_and_reach_the_parties(
    world: World,
) -> None:
    game = await peer_day(world)
    code = game.code
    for _, payload in room_payloads(world, code):
        assert not contains_value(payload, PRICE_P2)
        assert not contains_value(payload, PRICE_P3)
    assert_room_clean(world, code)

    p1 = world.last("p1", "your_day_closed")["record"]
    prices = sorted(t["price"] for t in p1["peer_trades"])
    assert prices == [PRICE_P2, PRICE_P3]
    assert contains_value(world.last("p2", "your_day_closed"), PRICE_P2)
    assert contains_value(world.last("p3", "your_day_closed"), PRICE_P3)
    host = world.last("host-1", "host_state")
    assert contains_value(host, PRICE_P2) and contains_value(host, PRICE_P3)

    public = day_closed_public(world)
    assert public["peer_units"] == 2
    assert public["peer_avg_price"] == pytest.approx(18.17, abs=0.005)
    assert "peer_trades" not in public


# --------------------------------------------------- buy limits (FM 10)
@aio
async def test_the_buyers_limits_reach_neither_the_room_nor_the_sellers(
    world: World,
) -> None:
    game = await peer_day(world)
    code = game.code
    # every full-state answer is audited too
    for sid in ("p2", "p3", "p4", "p1", "host-1"):
        await request_state(sid, {"room_id": code})
    for _, payload in room_payloads(world, code):
        assert not contains_value(payload, BUY_CAP)
    for sid in ("p2", "p3", "p4"):
        for payload in sid_payloads(world, sid, code):
            assert not contains_value(payload, BUY_CAP)
            for held in collect_values(payload, "peer_buy"):
                assert held is None  # only their own, and they set none
    # the buyer and the host do carry them
    assert any(contains_value(r[2], BUY_CAP) for r in world.sm.records if r[0] == "p1")
    assert any(
        contains_value(r[2], BUY_CAP) for r in world.sm.records if r[0] == "host-1"
    )


@aio
async def test_the_buyer_never_receives_a_sellers_offer(world: World) -> None:
    game = await peer_day(world)
    await request_state("p1", {"room_id": game.code})
    for payload in sid_payloads(world, "p1", game.code):
        for held in collect_values(payload, "peer_sell"):
            assert held is None
        assert not contains_key(payload, "max_units")


# ---------------------------------------------------------- AC 14
@aio
async def test_each_party_sees_its_own_trades_with_the_other_partys_alias(
    world: World,
) -> None:
    await peer_day(world)
    p1 = world.last("p1", "your_day_closed")["record"]
    trades = sorted(p1["peer_trades"], key=lambda t: t["counterparty"])
    assert [(t["side"], t["counterparty"], t["qty"], t["price"]) for t in trades] == [
        ("buy", "P2", 1, PRICE_P2),
        ("buy", "P3", 1, PRICE_P3),
    ]
    assert all(isinstance(t["quality"], int) for t in trades)

    p2 = world.last("p2", "your_day_closed")["record"]
    assert [
        (t["side"], t["counterparty"], t["qty"], t["price"]) for t in p2["peer_trades"]
    ] == [("sell", "P1", 1, PRICE_P2)]
    assert isinstance(p2["peer_trades"][0]["quality"], int)
    p3 = world.last("p3", "your_day_closed")["record"]
    assert [
        (t["side"], t["counterparty"], t["qty"], t["price"]) for t in p3["peer_trades"]
    ] == [("sell", "P1", 1, PRICE_P3)]
    assert p1["peer_bought"] == 2 and p2["peer_sold"] == 1 and p3["peer_sold"] == 1
    assert world.last("p4", "your_day_closed")["record"]["peer_trades"] == []


@aio
async def test_the_host_state_carries_every_record_and_the_last_peer_totals(
    world: World,
) -> None:
    await peer_day(world)
    host = world.last("host-1", "host_state")
    players = host["players"]
    for alias in ("P1", "P2", "P3", "P4"):
        record = world.last(f"p{alias[1]}", "your_day_closed")["record"]
        assert players[alias]["last_record"] == record
        assert "last_peer_bought" in players[alias]
        assert "last_peer_sold" in players[alias]
    assert players["P1"]["last_peer_bought"]
    assert players["P2"]["last_peer_sold"] and players["P3"]["last_peer_sold"]
    assert not players["P4"]["last_peer_bought"]
    assert not players["P4"]["last_peer_sold"]


@aio
async def test_no_third_players_payload_contains_either_trade(world: World) -> None:
    game = await peer_day(world)
    code = game.code
    for sid in ("p1", "p2", "p3", "p4"):
        await request_state(sid, {"room_id": code})
    leaks = {
        "p4": (PRICE_P2, PRICE_P3),  # P4 traded with nobody
        "p3": (PRICE_P2,),  # P3 is not a party to the $17.43 trade
        "p2": (PRICE_P3,),  # P2 is not a party to the $18.91 trade
    }
    for sid, prices in leaks.items():
        for payload in sid_payloads(world, sid, code):
            for price in prices:
                assert not contains_value(payload, price), (sid, price)
    for payload in sid_payloads(world, "p4", code):
        for trades in collect_values(payload, "peer_trades"):
            assert trades == []
    for sid, other in (("p2", "P3"), ("p3", "P2")):
        for payload in sid_payloads(world, sid, code):
            for trades in collect_values(payload, "peer_trades"):
                assert all(t["counterparty"] != other for t in trades)


# --------------------------------------------------------------- AC 8 (day)
@aio
async def test_a_day_of_trades_leaks_no_secret_config_or_cellar_detail(
    world: World,
) -> None:
    game = await peer_day(
        world,
        {
            "supplier": {"capacity_sd_per_player": 4.321},
            "transport": {"failure_probability": 0.137, "loss_fraction": 0.271},
        },
    )
    await play_day(game, 2)
    facing = player_facing(world, game.code, game.host_sid)
    for _, payload in facing:
        for key in ("capacity_sd_per_player", "failure_probability", "loss_fraction"):
            assert not contains_key(payload, key)
        for value in (4.321, 0.137, 0.271):
            assert not contains_value(payload, value)
        assert not contains_key(payload, "fresh_days")
        assert not contains_key(payload, "max_days")
    configs = [v for _, p in facing for v in collect_values(p, "config")]
    assert configs, "game_started carries a config"
    assert all(leaf_paths(c) == SHOWN_CONFIG_PATHS for c in configs)
