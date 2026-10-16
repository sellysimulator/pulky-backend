"""PulkyEngine: one game, from start to final settlement (spec §4, §5, §7, §8)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.core import demand as demand_mod
from app.core.calendar import order_windows, weekday_of
from app.core.config_models import GameConfig
from app.core.enums import Incoterm, Phase, TradeSide
from app.core.futures import FuturesBook, fill_futures, make_contract
from app.core.insurance import plan_rows, refund_value, tier_fee
from app.core.kernels import weekday_multipliers
from app.core.limits import MAX_ORDER, MAX_PEER_PRICE, MAX_PLAYERS, MIN_PLAYERS_TO_START
from app.core.lots import Cellar, Lot, quality_guide
from app.core.money import round_cents, to_cents
from app.core.peer_market import Bid, Offer, OfferLot, clear_peer_market
from app.core.pipeline import Pipeline
from app.core.records import (
    MSG_CONSERVE_INVALID,
    MSG_PEER_OFF,
    MSG_QUALITY_BOUNDS,
    MSG_TIER_UNAVAILABLE,
    AlreadySubmitted,
    DayRecord,
    Decision,
    FinalSettlement,
    InvalidDecision,
    MarketDay,
    PeerTrade,
    PublicDayResults,
    PublicPlayerDay,
    Shipment,
    StaleDay,
    msg_offer_units_bounds,
    msg_peer_price_bounds,
    msg_unaffordable,
)
from app.core.reputation import fill_ratio, update_reputation, waste_ratio
from app.core.rng import stream
from app.core.sampling import halfup
from app.core.settlement import final_settlement
from app.core.stats import build_stats
from app.core.supply import AbstractSupplier, SupplierDay, allocate
from app.core.transport import draw_failure, transport_losses

_ALIAS_RE = re.compile(r"^P[1-9][0-9]*$")


_RECORD_FIELDS: tuple[str, ...] = tuple(DayRecord.__dataclass_fields__)


def _alias_number(alias: str) -> int:
    return int(alias[1:])


@dataclass(frozen=True)
class DayOutcome:
    day: int
    public: PublicDayResults
    records: dict[str, DayRecord]
    finished: bool


@dataclass
class _Player:
    cash: float
    reputation: float
    backlog: int
    cellar: Cellar
    pipeline: Pipeline[Shipment]
    book: FuturesBook
    pending: Decision | None = None
    last_decision: Decision | None = None
    records: list[DayRecord] = field(default_factory=list)
    settlement: FinalSettlement | None = None


@dataclass
class _Work:
    """Everything one player's day computes, before it becomes a DayRecord."""

    decision: Decision
    cash_open: float
    reputation_open: float
    backlog_open: int
    opening_stock: int
    outlay: float
    cost_conservation: float = 0.0
    lots_conserved: int = 0
    cost_futures: float = 0.0
    futures_due: int = 0
    futures_filled: int = 0
    refund_futures: float = 0.0
    allocated: int = 0
    shipped: int = 0
    unit_cost: float = 0.0
    cost_spot: float = 0.0
    cost_transport: float = 0.0
    cost_insurance: float = 0.0
    lost: int = 0
    delivered: int = 0
    refund_insurance: float = 0.0
    shortfall: int = 0
    trades: list[PeerTrade] = field(default_factory=list)
    peer_bought: int = 0
    peer_sold: int = 0
    cost_peer_cents: int = 0
    revenue_peer_cents: int = 0
    demand: int = 0
    owed: int = 0
    sold: int = 0
    backlog_end: int = 0
    spoiled: int = 0


class PulkyEngine:
    """One game. Pure, deterministic from its seed, rehydrated from `to_payload()`."""

    def __init__(
        self,
        config: GameConfig,
        seed: int,
        seats: Mapping[str, str],
        players: dict[str, _Player],
        day: int,
        phase: Phase,
        ended_early: bool,
        supplier_day: SupplierDay,
        public_history: list[PublicDayResults],
        market_history: list[MarketDay],
    ) -> None:
        self._config = config
        self._seed = seed
        self._names = {a: seats[a] for a in sorted(seats, key=_alias_number)}
        self._players = {a: players[a] for a in self._names}
        self._day = day
        self._phase = phase
        self._ended_early = ended_early
        self._supplier_day = supplier_day
        self._public_history = public_history
        self._market_history = market_history
        self._supplier = self._make_supplier()
        self._tiers = config.tier_terms()
        self._multipliers = weekday_multipliers(
            config.demand.weekday_peak.from_, config.demand.weekday_peak.to
        )

    # ------------------------------------------------------------------ build

    def _make_supplier(self) -> AbstractSupplier:
        s = self._config.supplier
        return AbstractSupplier(
            seed=self._seed,
            n_players=len(self._names),
            capacity_mean_per_player=s.capacity_mean_per_player,
            capacity_sd_per_player=s.capacity_sd_per_player,
            disruption_probability=s.disruption_probability,
            disruption_factor=s.disruption_factor,
            base_price=s.base_price,
            spot_spread=s.spot_spread,
        )

    @classmethod
    def start(
        cls, config: GameConfig, seats: Mapping[str, str], seed: int
    ) -> PulkyEngine:
        n = len(seats)
        if not MIN_PLAYERS_TO_START <= n <= MAX_PLAYERS:
            raise ValueError("a game needs between 2 and 12 players")
        if any(not _ALIAS_RE.match(a) for a in seats):
            raise ValueError("aliases must look like P1, P2, ...")
        sup = config.supplier
        lead = sup.lead_time_days
        players: dict[str, _Player] = {}
        for alias in sorted(seats, key=_alias_number):
            lots = []
            if config.stock.initial_stock > 0:
                lots.append(
                    Lot(
                        id="L0",
                        qty=config.stock.initial_stock,
                        age=0,
                        unit_cost=sup.base_price,
                        initial_quality=sup.quality_max,
                        fresh_days=config.stock.fresh_days,
                        max_days=config.stock.max_days,
                        conserved=False,
                    )
                )
            initial = [
                Shipment(
                    qty=sup.initial_pipeline_qty,
                    lost=0,
                    unit_cost=sup.base_price,
                    incoterm=Incoterm.EXW,
                    shipped_day=k - lead,
                )
                for k in range(1, lead + 1)
            ]
            players[alias] = _Player(
                cash=config.starting_cash,
                reputation=config.reputation.starting,
                backlog=0,
                cellar=Cellar(lots),
                pipeline=Pipeline(lead, initial),
                book=FuturesBook(),
            )
        engine = cls(
            config=config,
            seed=seed,
            seats=seats,
            players=players,
            day=1,
            phase=Phase.DECIDING,
            ended_early=False,
            supplier_day=SupplierDay(0, False, 0.0),
            public_history=[],
            market_history=[],
        )
        engine._supplier_day = engine._supplier.draw_day(1)
        return engine

    # -------------------------------------------------------------- read-only

    @property
    def config(self) -> GameConfig:
        return self._config

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def day(self) -> int:
        return self._day

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def ended_early(self) -> bool:
        return self._ended_early

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._names)

    @property
    def display_names(self) -> dict[str, str]:
        return dict(self._names)

    # ------------------------------------------------------------------ brief

    def _lambda_today(self) -> float:
        return demand_mod.market_lambda(
            self._config.demand.per_player_peak,
            len(self._names),
            self._multipliers[weekday_of(self._day)],
        )

    def _windows(self) -> Any:
        c = self._config
        return order_windows(
            self._day,
            c.duration_days,
            c.supplier.lead_time_days,
            c.futures.horizon_days,
            c.supplier.base_price,
        )

    def brief(self) -> dict | None:
        if self._phase == Phase.FINISHED:
            return None
        c = self._config
        d = self._day
        wd = weekday_of(d)
        win = self._windows()
        sd = self._supplier_day
        message = None
        if sd.disrupted:
            factor = halfup(c.supplier.disruption_factor * 100)
            message = (
                f"Day {d} ({wd.value.capitalize()}): "
                f"supplier capacity cut to {factor}%"
            )
        return {
            "day": d,
            "duration_days": c.duration_days,
            "weekday": wd.value,
            "weekday_multiplier": self._multipliers[wd],
            "weekday_multipliers": {w.value: m for w, m in self._multipliers.items()},
            "expected_demand": round(self._lambda_today(), 2),
            "supplier": {
                "capacity": sd.capacity,
                "disrupted": sd.disrupted,
                "disruption_message": message,
                "spot_price": sd.spot_price,
                "base_price": c.supplier.base_price,
                "lead_time_days": c.supplier.lead_time_days,
            },
            "spot": {"open": win.spot_open, "arrives_day": win.spot_arrives_day},
            "futures": {
                "open": win.futures_open,
                "price": c.supplier.base_price,
                "horizon_days": c.futures.horizon_days,
                "delivery_day": win.futures_delivery_day,
                "last_day_to_buy": win.last_day_to_buy,
                "days_left_to_buy": win.days_left_to_buy,
            },
            "transport": {"shared": c.transport.shared},
            "reputation": {a: p.reputation for a, p in self._players.items()},
        }

    # ----------------------------------------------------------------- submit

    def outlay(self, alias: str, decision: Decision) -> float:
        player = self._players[alias]
        c = self._config
        fee = tier_fee(decision.incoterm, self._tiers)
        conserved_units = 0
        for lot_id in decision.conserve_lots:
            lot = player.cellar.get(lot_id)
            if lot is not None:
                conserved_units += lot.qty
        return (
            self._supplier_day.spot_price * decision.order_spot
            + c.supplier.base_price * decision.order_futures
            + (c.transport.unit_cost + fee) * decision.order_spot
            + fee * player.book.due(self._day)
            + c.stock.conservation_cost_per_unit * conserved_units
        )

    def _check_peer_limits(self, decision: Decision) -> None:
        buy = decision.peer_buy
        sell = decision.peer_sell
        if buy is not None:
            self._check_price(buy.max_price)
            self._check_quality(buy.min_quality)
        if sell is not None:
            self._check_price(sell.price)
            self._check_quality(sell.max_quality)
            if not 1 <= sell.max_units <= MAX_ORDER:
                raise InvalidDecision(msg_offer_units_bounds())

    @staticmethod
    def _check_price(price: float) -> None:
        cents = price * 100
        whole = abs(cents - halfup(cents)) < 1e-6
        if not whole or not 0.01 <= round_cents(price) <= MAX_PEER_PRICE:
            raise InvalidDecision(msg_peer_price_bounds())

    @staticmethod
    def _check_quality(quality: int) -> None:
        if not 0 <= quality <= 100:
            raise InvalidDecision(MSG_QUALITY_BOUNDS)

    def submit(self, alias: str, decision: Decision) -> None:
        if self._phase == Phase.FINISHED or alias not in self._players:
            raise ValueError("cannot submit: the game is finished or alias unseated")
        player = self._players[alias]
        if decision.day != self._day:
            raise StaleDay()
        if player.pending is not None:
            raise AlreadySubmitted()
        win = self._windows()
        if decision.order_spot > 0 and not win.spot_open:
            raise InvalidDecision(win.spot_message or "")
        if decision.order_futures > 0 and not win.futures_open:
            raise InvalidDecision(win.futures_message)
        if (
            decision.incoterm != Incoterm.EXW
            and not self._tiers[decision.incoterm].enabled
        ):
            raise InvalidDecision(MSG_TIER_UNAVAILABLE)
        if not self._config.peer_market.enabled and (
            decision.peer_buy is not None or decision.peer_sell is not None
        ):
            raise InvalidDecision(MSG_PEER_OFF)
        self._check_peer_limits(decision)
        seen: set[str] = set()
        for lot_id in decision.conserve_lots:
            lot = player.cellar.get(lot_id)
            if lot is None or lot.conserved or lot_id in seen:
                raise InvalidDecision(MSG_CONSERVE_INVALID)
            seen.add(lot_id)
        outlay = self.outlay(alias, decision)
        if outlay > 0 and round_cents(outlay) > player.cash:
            raise InvalidDecision(msg_unaffordable(round_cents(outlay), player.cash))
        player.pending = decision
        player.last_decision = decision

    def has_submitted(self, alias: str) -> bool:
        if self._phase == Phase.FINISHED:
            return False
        return self._players[alias].pending is not None

    def awaiting(self) -> list[str]:
        if self._phase == Phase.FINISHED:
            return []
        return [a for a, p in self._players.items() if p.pending is None]

    def all_in(self) -> bool:
        return self._phase == Phase.DECIDING and not self.awaiting()

    # ------------------------------------------------------------- resolution

    def resolve_day(self) -> DayOutcome:
        if not self.all_in():
            raise ValueError("every player must submit before the day resolves")
        c = self._config
        d = self._day
        seed = self._seed
        sd = self._supplier_day
        base = c.supplier.base_price
        spot = sd.spot_price
        retail = c.demand.retail_price
        aliases = list(self._names)
        wd = weekday_of(d)
        multiplier = self._multipliers[wd]
        lam = self._lambda_today()

        work: dict[str, _Work] = {}
        for a in aliases:
            p = self._players[a]
            pending = p.pending
            assert pending is not None
            work[a] = _Work(
                decision=pending,
                cash_open=p.cash,
                reputation_open=p.reputation,
                backlog_open=p.backlog,
                opening_stock=p.cellar.total(),
                outlay=self.outlay(a, pending),
            )

        # S1 conservation
        for a in aliases:
            w = work[a]
            units = self._players[a].cellar.conserve(
                list(w.decision.conserve_lots), c.stock.conservation_extra_fresh_days
            )
            w.cost_conservation = c.stock.conservation_cost_per_unit * units
            w.lots_conserved = len(w.decision.conserve_lots)

        # S2 cooperation
        cooperators = sum(1 for a in aliases if work[a].decision.cooperate)
        cooperation = cooperators == len(aliases)

        # S3 new futures contracts
        for a in aliases:
            w = work[a]
            q = w.decision.order_futures
            if q > 0:
                self._players[a].book.add(
                    make_contract(
                        d, q, c.futures.horizon_days, c.supplier.lead_time_days
                    )
                )
                w.cost_futures = base * q

        # S4 futures due today have first claim on capacity
        due = {a: self._players[a].book.due(d) for a in aliases}
        filled = fill_futures(due, sd.capacity, stream(seed, d, "alloc_futures"))
        for a in aliases:
            w = work[a]
            self._players[a].book.take_due(d)
            w.futures_due = due[a]
            w.futures_filled = filled[a]
            w.refund_futures = base * (due[a] - filled[a])

        # S5 spot allocation on what is left
        remaining = sd.capacity - sum(filled.values())
        allocated = allocate(
            {a: work[a].decision.order_spot for a in aliases},
            remaining,
            cooperation,
            stream(seed, d, "alloc_spot"),
        )

        # S6 / S7 spot purchases, shipments, transport and insurance
        for a in aliases:
            w = work[a]
            w.allocated = allocated[a]
            w.cost_spot = spot * w.allocated
            w.shipped = w.allocated + w.futures_filled
            if w.shipped > 0:
                w.unit_cost = (spot * w.allocated + base * w.futures_filled) / w.shipped
            w.cost_transport = c.transport.unit_cost * w.shipped
            w.cost_insurance = tier_fee(w.decision.incoterm, self._tiers) * w.shipped

        # S8 refrigeration failure
        failure = draw_failure(
            stream(seed, d, "transport"), c.transport.failure_probability
        )
        losses = transport_losses(
            {a: work[a].shipped for a in aliases},
            failure,
            c.transport.loss_fraction,
            c.transport.shared,
        )

        # S9 pipeline: push today's shipment, pop today's arrival
        for a in aliases:
            w = work[a]
            p = self._players[a]
            w.lost = losses[a]
            p.pipeline.push(
                Shipment(
                    qty=w.shipped - w.lost,
                    lost=w.lost,
                    unit_cost=w.unit_cost,
                    incoterm=w.decision.incoterm,
                    shipped_day=d,
                )
            )
            arrival = p.pipeline.pop_front()
            w.delivered = arrival.qty
            if arrival.qty > 0:
                if arrival.shipped_day <= 0:
                    quality = c.supplier.quality_max
                else:
                    quality = stream(seed, d, f"quality:{a}").randint(
                        c.supplier.quality_min, c.supplier.quality_max
                    )
                p.cellar.add(
                    Lot(
                        id=f"L{d}",
                        qty=arrival.qty,
                        age=0,
                        unit_cost=arrival.unit_cost,
                        initial_quality=quality,
                        fresh_days=c.stock.fresh_days,
                        max_days=c.stock.max_days,
                        conserved=False,
                    )
                )
            w.refund_insurance = arrival.lost * refund_value(
                arrival.incoterm,
                paid_unit_cost=arrival.unit_cost,
                arrival_spot_price=spot,
                retail_price=retail,
            )
            w.shortfall = w.decision.order_spot - w.allocated

        # S10 peer market
        if c.peer_market.enabled:
            self._clear_peer_market(work, aliases)

        # S11 demand, serving
        reps = {a: work[a].reputation_open for a in aliases}
        share = demand_mod.shares(reps)
        demands = demand_mod.draw_demands(seed, d, {a: lam * share[a] for a in aliases})
        for a in aliases:
            w = work[a]
            p = self._players[a]
            served = demand_mod.serve(w.backlog_open, demands[a], p.cellar.total())
            p.cellar.consume(served.sold)
            w.demand = demands[a]
            w.owed = served.owed
            w.sold = served.sold
            w.backlog_end = served.backlog_end

        # S12 night
        for a in aliases:
            work[a].spoiled = self._players[a].cellar.nightly_spoil(
                stream(seed, d, f"spoil:{a}")
            )

        # S13 - S15 ledger, reputation, records
        market_share = demand_mod.market_shares({a: work[a].sold for a in aliases})
        records: dict[str, DayRecord] = {}
        public_players: dict[str, PublicPlayerDay] = {}
        for a in aliases:
            w = work[a]
            p = self._players[a]
            closing = p.cellar.total()
            revenue_sales = retail * w.sold
            revenue_salvage = c.stock.salvage_value * w.spoiled
            revenue_peer = w.revenue_peer_cents / 100
            cost_peer = w.cost_peer_cents / 100
            cost_holding = c.stock.holding_cost * closing
            cost_backlog = c.demand.backlog_penalty * w.backlog_end
            profit = (
                revenue_sales
                + revenue_salvage
                + revenue_peer
                + w.refund_insurance
                + w.refund_futures
                - w.cost_spot
                - w.cost_futures
                - cost_peer
                - w.cost_transport
                - w.cost_insurance
                - w.cost_conservation
                - cost_holding
                - cost_backlog
            )
            profit_r = round_cents(profit)
            cash_end = round_cents(w.cash_open + profit_r)
            fill = fill_ratio(w.sold, w.owed)
            waste = waste_ratio(w.spoiled, w.sold)
            rep_end = update_reputation(
                w.reputation_open,
                fill,
                waste,
                c.reputation.smoothing,
                c.reputation.waste_weight,
            )
            dec = w.decision
            records[a] = DayRecord(
                day=d,
                weekday=wd,
                order_spot=dec.order_spot,
                order_futures=dec.order_futures,
                incoterm=dec.incoterm,
                conserve_lots=dec.conserve_lots,
                lots_conserved=w.lots_conserved,
                cooperate=dec.cooperate,
                peer_buy=dec.peer_buy,
                peer_sell=dec.peer_sell,
                outlay=round_cents(w.outlay),
                opening_stock=w.opening_stock,
                futures_due=w.futures_due,
                futures_filled=w.futures_filled,
                futures_unfilled=w.futures_due - w.futures_filled,
                allocated_spot=w.allocated,
                shortfall_spot=w.shortfall,
                shipped=w.shipped,
                lost_transport=w.lost,
                delivered=w.delivered,
                peer_bought=w.peer_bought,
                peer_sold=w.peer_sold,
                peer_trades=tuple(w.trades),
                demand=w.demand,
                backlog_open=w.backlog_open,
                sold=w.sold,
                backlog_end=w.backlog_end,
                spoiled=w.spoiled,
                closing_stock=closing,
                revenue_sales=round_cents(revenue_sales),
                revenue_salvage=round_cents(revenue_salvage),
                revenue_peer=round_cents(revenue_peer),
                refund_insurance=round_cents(w.refund_insurance),
                refund_futures=round_cents(w.refund_futures),
                cost_spot=round_cents(w.cost_spot),
                cost_futures=round_cents(w.cost_futures),
                cost_peer=round_cents(cost_peer),
                cost_transport=round_cents(w.cost_transport),
                cost_insurance=round_cents(w.cost_insurance),
                cost_conservation=round_cents(w.cost_conservation),
                cost_holding=round_cents(cost_holding),
                cost_backlog=round_cents(cost_backlog),
                profit=profit_r,
                cash_open=w.cash_open,
                cash_end=cash_end,
                futures_vs_spot=round_cents((spot - base) * w.futures_filled),
                fill=fill,
                waste=waste,
                reputation_open=w.reputation_open,
                reputation_end=rep_end,
                market_share=market_share[a],
                refrigeration_failure=failure,
            )
            public_players[a] = PublicPlayerDay(w.sold, market_share[a], rep_end)
            p.cash = cash_end
            p.reputation = rep_end
            p.backlog = w.backlog_end
            p.records.append(records[a])
            p.pending = None

        peer_units = sum(w.peer_bought for w in work.values())
        peer_cents = sum(w.cost_peer_cents for w in work.values())
        peer_avg = round_cents(peer_cents / 100 / peer_units) if peer_units else None
        demand_total = sum(w.demand for w in work.values())
        sold_total = sum(w.sold for w in work.values())
        public = PublicDayResults(
            day=d,
            weekday=wd,
            demand_total=demand_total,
            units_sold_total=sold_total,
            spot_price=spot,
            capacity=sd.capacity,
            disrupted=sd.disrupted,
            cooperation=cooperation,
            cooperators=cooperators,
            refrigeration_failure=failure,
            peer_units=peer_units,
            peer_avg_price=peer_avg,
            players=public_players,
        )
        self._public_history.append(public)
        self._market_history.append(
            MarketDay(
                day=d,
                weekday=wd,
                weekday_multiplier=multiplier,
                expected_demand=round(lam, 2),
                demand_total=demand_total,
                capacity=sd.capacity,
                disrupted=sd.disrupted,
                spot_price=spot,
                cooperation=cooperation,
                cooperators=cooperators,
                refrigeration_failure=failure,
                units_sold_total=sold_total,
                peer_units=peer_units,
                peer_avg_price=peer_avg,
            )
        )

        # S16 end of game, or the next morning
        finished = d == c.duration_days
        if finished:
            for a in aliases:
                p = self._players[a]
                settlement = final_settlement(
                    cellar_units=p.cellar.total(),
                    in_transit=p.pipeline.items(),
                    open_futures_units=p.book.open_units(),
                    ended_early=False,
                    salvage_value=c.stock.salvage_value,
                    base_price=base,
                    retail_price=retail,
                )
                p.settlement = settlement
                p.cash = round_cents(p.cash + settlement.settlement_total)
            self._phase = Phase.FINISHED
        else:
            self._day = d + 1
            self._supplier_day = self._supplier.draw_day(self._day)
        return DayOutcome(day=d, public=public, records=records, finished=finished)

    def _clear_peer_market(self, work: dict[str, _Work], aliases: list[str]) -> None:
        d = self._day
        bids: dict[str, Bid] = {}
        offers: dict[str, Offer] = {}
        for a in aliases:
            w = work[a]
            dec = w.decision
            if w.shortfall > 0 and dec.peer_buy is not None:
                cash_market = (
                    w.cash_open
                    + w.refund_futures
                    + w.refund_insurance
                    - w.cost_conservation
                    - w.cost_futures
                    - w.cost_spot
                    - w.cost_transport
                    - w.cost_insurance
                )
                bids[a] = Bid(
                    w.shortfall,
                    to_cents(dec.peer_buy.max_price),
                    dec.peer_buy.min_quality,
                    max(0, to_cents(cash_market)),
                )
            if dec.peer_sell is not None:
                sell = dec.peer_sell
                offers[a] = Offer(
                    to_cents(sell.price),
                    sell.max_units,
                    tuple(
                        OfferLot(lot.id, lot.qty, lot.quality(), lot.days_left())
                        for lot in self._players[a].cellar.lots
                        if lot.quality() <= sell.max_quality
                    ),
                )
        trades = clear_peer_market(
            bids, offers, aliases, stream(self._seed, d, "peer_market")
        )
        counters: dict[str, int] = {}
        for t in trades:
            seller = self._players[t.seller]
            buyer = self._players[t.buyer]
            part = seller.cellar.take(t.seller_lot, t.qty)
            counters[t.buyer] = counters.get(t.buyer, 0) + 1
            new_id = f"B{d}-{counters[t.buyer]}"
            price = t.price_cents / 100
            buyer.cellar.insert(replace(part, id=new_id, unit_cost=price))
            wb, ws = work[t.buyer], work[t.seller]
            wb.trades.append(
                PeerTrade(TradeSide.BUY, t.seller, new_id, t.qty, t.quality, price)
            )
            ws.trades.append(
                PeerTrade(
                    TradeSide.SELL, t.buyer, t.seller_lot, t.qty, t.quality, price
                )
            )
            wb.peer_bought += t.qty
            ws.peer_sold += t.qty
            wb.cost_peer_cents += t.qty * t.price_cents
            ws.revenue_peer_cents += t.qty * t.price_cents

    # ----------------------------------------------------------------- early

    def end_early(self) -> None:
        if self._phase != Phase.DECIDING:
            raise ValueError("the game is not running")
        c = self._config
        for p in self._players.values():
            settlement = final_settlement(
                cellar_units=p.cellar.total(),
                in_transit=p.pipeline.items(),
                open_futures_units=p.book.open_units(),
                ended_early=True,
                salvage_value=c.stock.salvage_value,
                base_price=c.supplier.base_price,
                retail_price=c.demand.retail_price,
            )
            p.pending = None
            p.book.cancel_all()
            p.settlement = settlement
            p.cash = round_cents(p.cash + settlement.settlement_total)
        self._ended_early = True
        self._phase = Phase.FINISHED

    # ---------------------------------------------------------------- history

    def records(self, alias: str) -> list[DayRecord]:
        return list(self._players[alias].records)

    def public_history(self) -> list[PublicDayResults]:
        return list(self._public_history)

    def market_history(self) -> list[MarketDay]:
        return list(self._market_history)

    def settlements(self) -> dict[str, FinalSettlement]:
        if self._phase != Phase.FINISHED:
            return {}
        out: dict[str, FinalSettlement] = {}
        for a, p in self._players.items():
            assert p.settlement is not None
            out[a] = p.settlement
        return out

    def public_results(self, day: int) -> dict:
        for item in self._public_history:
            if item.day == day:
                return item.to_payload(self._names)
        raise ValueError(f"day {day} has not been resolved")

    # ------------------------------------------------------------------ views

    def _cumulative_profit(self, alias: str) -> float:
        return round_cents(sum(r.profit for r in self._players[alias].records))

    def _own_share(self, alias: str) -> float:
        share = demand_mod.shares({a: p.reputation for a, p in self._players.items()})
        return share[alias]

    def _prefill(self, alias: str) -> dict | None:
        if self._phase == Phase.FINISHED:
            return None
        last = self._players[alias].last_decision
        if self._day == 1 or last is None:
            return Decision.zero(self._day).to_payload()
        win = self._windows()
        return replace(
            last,
            day=self._day,
            conserve_lots=(),
            order_spot=last.order_spot if win.spot_open else 0,
            order_futures=last.order_futures if win.futures_open else 0,
        ).to_payload()

    def _yesterday(self, alias: str) -> dict | None:
        records = self._players[alias].records
        if not records:
            return None
        r = records[-1]
        return {
            "day": r.day,
            "demand": r.demand,
            "sold": r.sold,
            "backlog_end": r.backlog_end,
            "backlog_change": r.backlog_end - r.backlog_open,
            "spoiled": r.spoiled,
            "revenue_salvage": r.revenue_salvage,
            "reputation": r.reputation_end,
            "reputation_change": r.reputation_end - r.reputation_open,
            "profit": r.profit,
            "cash_end": r.cash_end,
            "futures_bought": r.order_futures,
            "futures_filled": r.futures_filled,
            "futures_vs_spot": r.futures_vs_spot,
            "futures_unfilled": r.futures_unfilled,
            "refund_futures": r.refund_futures,
            "refrigeration_failure": r.refrigeration_failure,
            "lost_transport": r.lost_transport,
            "refund_insurance": r.refund_insurance,
            "market_share": r.market_share,
            "shortfall_spot": r.shortfall_spot,
            "peer_bought": r.peer_bought,
            "cost_peer": r.cost_peer,
            "peer_sold": r.peer_sold,
            "revenue_peer": r.revenue_peer,
        }

    def _in_transit(self, alias: str) -> list[dict]:
        lead = self._config.supplier.lead_time_days
        return [
            {
                "shipped_day": s.shipped_day,
                "arrives_day": s.shipped_day + lead,
                "qty": s.qty,
                "lost": s.lost,
                "incoterm": s.incoterm.value,
            }
            for s in self._players[alias].pipeline.items()
            if not (s.qty == 0 and s.lost == 0)
        ]

    def player_view(self, alias: str, full: bool = True) -> dict:
        p = self._players[alias]
        c = self._config
        finished = self._phase == Phase.FINISHED
        win = self._windows()
        view: dict[str, Any] = {
            "alias": alias,
            "display_name": self._names[alias],
            "day": self._day,
            "duration_days": c.duration_days,
            "weekday": weekday_of(self._day).value,
            "phase": self._phase.value,
            "has_submitted": self.has_submitted(alias),
            "awaiting": self.awaiting(),
            "brief": self.brief(),
            "yesterday": self._yesterday(alias),
            "cellar": p.cellar.view(self._day),
            "stock_by_age": p.cellar.stock_by_age(),
            "quality_guide": (
                quality_guide(self._day, c.stock.fresh_days, c.stock.max_days)
                if c.peer_market.enabled
                else []
            ),
            "expected_spoil_tonight": p.cellar.expected_spoil_tonight(),
            "in_transit": self._in_transit(alias),
            "futures_book": p.book.view(),
            "backlog": p.backlog,
            "expected_demand_own": round(
                self._lambda_today() * self._own_share(alias), 2
            ),
            "reputation": p.reputation,
            "market_share": p.records[-1].market_share if p.records else 0.0,
            "cash": p.cash,
            "cumulative_profit": self._cumulative_profit(alias),
            "insurance_plans": plan_rows(
                self._tiers,
                today_spot_price=self._supplier_day.spot_price,
                retail_price=c.demand.retail_price,
            ),
            "order_windows": (
                None
                if finished
                else {
                    "spot_open": win.spot_open,
                    "futures_open": win.futures_open,
                    "futures_days_left_to_buy": win.days_left_to_buy,
                    "futures_message": win.futures_message,
                    "spot_message": win.spot_message,
                }
            ),
            "prefill_decision": self._prefill(alias),
            "final_settlement": (
                p.settlement.to_payload() if p.settlement is not None else None
            ),
        }
        if full:
            view["own_history"] = [r.to_payload() for r in p.records]
            view["public_history"] = [
                h.to_payload(self._names) for h in self._public_history
            ]
        return view

    def _fill_waste(self, alias: str) -> tuple[float | None, float | None]:
        records = self._players[alias].records
        demand = sum(r.demand for r in records)
        sold = sum(r.sold for r in records)
        spoiled = sum(r.spoiled for r in records)
        fill = sold / demand if demand > 0 else None
        waste = spoiled / (sold + spoiled) if sold + spoiled > 0 else None
        return fill, waste

    def host_view(self, full: bool = True) -> dict:
        c = self._config
        players: dict[str, Any] = {}
        for a, p in self._players.items():
            fill, waste = self._fill_waste(a)
            last = p.records[-1] if p.records else None
            entry: dict[str, Any] = {
                "display_name": self._names[a],
                "has_submitted": self.has_submitted(a),
                "stock_units": p.cellar.total(),
                "expected_spoil_tonight": p.cellar.expected_spoil_tonight(),
                "in_transit_units": sum(s.qty for s in p.pipeline.items()),
                "open_futures_units": p.book.open_units(),
                "backlog": p.backlog,
                "reputation": p.reputation,
                "market_share": last.market_share if last else 0.0,
                "cumulative_profit": self._cumulative_profit(a),
                "cash": p.cash,
                "fill_rate": fill,
                "waste_rate": waste,
                "last_incoterm": last.incoterm.value if last else None,
                "last_peer_bought": last.peer_bought if last else None,
                "last_peer_sold": last.peer_sold if last else None,
            }
            if full:
                entry["own_history"] = [r.to_payload() for r in p.records]
            else:
                entry["last_record"] = last.to_payload() if last else None
            players[a] = entry
        view: dict[str, Any] = {
            "day": self._day,
            "duration_days": c.duration_days,
            "weekday": weekday_of(self._day).value,
            "phase": self._phase.value,
            "brief": self.brief(),
            "seed": self._seed,
            "config": c.to_dict(),
            "awaiting": self.awaiting(),
            "players": players,
            "leaderboard": self.standings()["leaderboard"],
        }
        if full:
            view["public_history"] = [
                h.to_payload(self._names) for h in self._public_history
            ]
            view["market_history"] = [m.to_payload() for m in self._market_history]
        else:
            view["last_market_day"] = (
                self._market_history[-1].to_payload() if self._market_history else None
            )
        return view

    def standings(self) -> dict:
        settlements = self.settlements() or {
            a: FinalSettlement(0, 0.0, 0.0, 0, 0.0, 0.0) for a in self._players
        }
        return build_stats(
            records={a: p.records for a, p in self._players.items()},
            market=self._market_history,
            settlements=settlements,
            display_names=self._names,
            starting_reputation=self._config.reputation.starting,
            seed=self._seed,
        )

    def results(self) -> dict:
        if self._phase != Phase.FINISHED:
            raise ValueError("the game has not finished")
        stats = self.standings()
        return {
            "days_played": len(self._market_history),
            "ended_early": self._ended_early,
            "leaderboard": stats["leaderboard"],
            "stats": {"players": stats["players"], "market": stats["market"]},
        }

    # ---------------------------------------------------------------- payload

    def to_payload(self) -> dict:
        players: dict[str, Any] = {}
        for a, p in self._players.items():
            players[a] = {
                "cash": p.cash,
                "reputation": p.reputation,
                "backlog": p.backlog,
                "cellar": p.cellar.to_payload(),
                "pipeline": p.pipeline.to_payload(lambda s: s.to_payload()),
                "futures_book": p.book.to_payload(),
                "pending": p.pending.to_payload() if p.pending else None,
                "last_decision": (
                    p.last_decision.to_payload() if p.last_decision else None
                ),
                "records": [list(r.to_payload().values()) for r in p.records],
                "settlement": p.settlement.to_payload() if p.settlement else None,
            }
        sd = self._supplier_day
        return {
            "day": self._day,
            "phase": self._phase.value,
            "ended_early": self._ended_early,
            "seed": self._seed,
            "seats": dict(self._names),
            "supplier_day": {
                "capacity": sd.capacity,
                "disrupted": sd.disrupted,
                "spot_price": sd.spot_price,
            },
            "record_fields": list(_RECORD_FIELDS),
            "players": players,
            "public_history": [h.to_payload() for h in self._public_history],
            "market_history": [m.to_payload() for m in self._market_history],
        }

    @classmethod
    def from_payload(cls, payload: dict, config: GameConfig) -> PulkyEngine:
        fields = payload["record_fields"]
        players: dict[str, _Player] = {}
        for a, data in payload["players"].items():
            players[a] = _Player(
                cash=float(data["cash"]),
                reputation=float(data["reputation"]),
                backlog=int(data["backlog"]),
                cellar=Cellar.from_payload(data["cellar"]),
                pipeline=Pipeline.from_payload(data["pipeline"], Shipment.from_payload),
                book=FuturesBook.from_payload(data["futures_book"]),
                pending=(
                    Decision.from_payload(data["pending"]) if data["pending"] else None
                ),
                last_decision=(
                    Decision.from_payload(data["last_decision"])
                    if data["last_decision"]
                    else None
                ),
                records=[
                    DayRecord.from_payload(dict(zip(fields, row, strict=True)))
                    for row in data["records"]
                ],
                settlement=(
                    FinalSettlement.from_payload(data["settlement"])
                    if data["settlement"]
                    else None
                ),
            )
        sd = payload["supplier_day"]
        return cls(
            config=config,
            seed=int(payload["seed"]),
            seats=payload["seats"],
            players=players,
            day=int(payload["day"]),
            phase=Phase(payload["phase"]),
            ended_early=bool(payload["ended_early"]),
            supplier_day=SupplierDay(
                int(sd["capacity"]), bool(sd["disrupted"]), float(sd["spot_price"])
            ),
            public_history=[
                PublicDayResults.from_payload(h) for h in payload["public_history"]
            ],
            market_history=[
                MarketDay.from_payload(m) for m in payload["market_history"]
            ],
        )
