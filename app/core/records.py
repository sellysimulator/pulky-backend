"""Records that cross module boundaries (spec §4.3, §5.6, §5.7, §8.1, §8.2, §12.2)."""

import math
from dataclasses import dataclass
from typing import Any, ClassVar

from app.core.enums import IncotermTier, TradeSide, Weekday
from app.core.limits import MAX_CONSERVE_LOTS, MAX_ORDER, MAX_PEER_PRICE
from app.core.money import format_money, format_number, round_cents

# ---- decision errors (spec §4.3 "Decision messages") ----


class DecisionError(Exception):
    code: ClassVar[str]

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StaleDay(DecisionError):
    code = "STALE_DAY"

    def __init__(
        self, message: str = "That day is over. Your screen has been updated."
    ) -> None:
        super().__init__(message)


class AlreadySubmitted(DecisionError):
    code = "ALREADY_SUBMITTED"

    def __init__(
        self, message: str = "You have already submitted today's decision."
    ) -> None:
        super().__init__(message)


class InvalidDecision(DecisionError):
    code = "INVALID_DECISION"


MSG_UNREADABLE = "Your decision could not be read. Reload the page and try again."
MSG_TIER_UNAVAILABLE = "That shipping insurance is not available in this game."
MSG_CONSERVE_INVALID = (
    "You can only conserve lots in your cellar that are not conserved yet."
)
MSG_PEER_OFF = "The peer market is off in this game."
MSG_QUALITY_BOUNDS = "Quality limits must be whole numbers from 0 to 100."


def msg_order_bounds() -> str:
    return f"Orders must be whole numbers from 0 to {format_number(MAX_ORDER)}."


def msg_conserve_too_many() -> str:
    return f"You can conserve at most {format_number(MAX_CONSERVE_LOTS)} lots a day."


def msg_peer_price_bounds() -> str:
    return (
        f"Peer prices must be from $0.01 to {format_money(MAX_PEER_PRICE)}, "
        "in whole cents."
    )


def msg_offer_units_bounds() -> str:
    return f"You can offer from 1 to {format_number(MAX_ORDER)} units."


def msg_unaffordable(outlay: float, cash: float) -> str:
    return (
        f"Today's purchases cost {format_money(outlay)} "
        f"but you only have {format_money(cash)}"
    )


# ---- peer-market limits ----


@dataclass(frozen=True)
class PeerBuy:
    max_price: float
    min_quality: int

    def to_payload(self) -> dict:
        return {"max_price": self.max_price, "min_quality": self.min_quality}

    @classmethod
    def from_payload(cls, data: dict) -> "PeerBuy":
        return cls(float(data["max_price"]), int(data["min_quality"]))


@dataclass(frozen=True)
class PeerSell:
    price: float
    max_quality: int
    max_units: int

    def to_payload(self) -> dict:
        return {
            "price": self.price,
            "max_quality": self.max_quality,
            "max_units": self.max_units,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "PeerSell":
        return cls(
            float(data["price"]), int(data["max_quality"]), int(data["max_units"])
        )


@dataclass(frozen=True)
class PeerTrade:
    side: TradeSide
    counterparty: str
    lot_id: str
    qty: int
    quality: int
    price: float

    def to_payload(self) -> dict:
        return {
            "side": self.side.value,
            "counterparty": self.counterparty,
            "lot_id": self.lot_id,
            "qty": self.qty,
            "quality": self.quality,
            "price": self.price,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "PeerTrade":
        return cls(
            TradeSide(data["side"]),
            data["counterparty"],
            data["lot_id"],
            data["qty"],
            data["quality"],
            data["price"],
        )


def _opt_buy(data: dict | None) -> PeerBuy | None:
    return None if data is None else PeerBuy.from_payload(data)


def _opt_sell(data: dict | None) -> PeerSell | None:
    return None if data is None else PeerSell.from_payload(data)


# ---- the decision ----


@dataclass(frozen=True)
class Decision:
    day: int
    order_spot: int
    order_futures: int
    incoterm_tier: IncotermTier
    conserve_lots: tuple[str, ...]
    cooperate: bool
    peer_buy: PeerBuy | None = None
    peer_sell: PeerSell | None = None

    @classmethod
    def zero(cls, day: int) -> "Decision":
        return cls(day, 0, 0, IncotermTier.EXW, (), False, None, None)

    def to_payload(self) -> dict:
        return {
            "day": self.day,
            "order_spot": self.order_spot,
            "order_futures": self.order_futures,
            "incoterm_tier": self.incoterm_tier.value,
            "conserve_lots": list(self.conserve_lots),
            "cooperate": self.cooperate,
            "peer_buy": None if self.peer_buy is None else self.peer_buy.to_payload(),
            "peer_sell": (
                None if self.peer_sell is None else self.peer_sell.to_payload()
            ),
        }

    @classmethod
    def from_payload(cls, data: dict) -> "Decision":
        return cls(
            data["day"],
            data["order_spot"],
            data["order_futures"],
            IncotermTier(data["incoterm_tier"]),
            tuple(data["conserve_lots"]),
            data["cooperate"],
            _opt_buy(data["peer_buy"]),
            _opt_sell(data["peer_sell"]),
        )


_FIELDS = (
    "day",
    "order_spot",
    "order_futures",
    "incoterm_tier",
    "conserve_lots",
    "cooperate",
    "peer_buy",
    "peer_sell",
)


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _readable(data: object) -> bool:
    if not isinstance(data, dict) or any(k not in data for k in _FIELDS):
        return False
    if not (
        _is_int(data["day"])
        and _is_int(data["order_spot"])
        and _is_int(data["order_futures"])
        and isinstance(data["incoterm_tier"], str)
        and isinstance(data["conserve_lots"], list)
        and all(isinstance(x, str) for x in data["conserve_lots"])
        and isinstance(data["cooperate"], bool)
    ):
        return False
    buy, sell = data["peer_buy"], data["peer_sell"]
    if buy is not None and not (
        isinstance(buy, dict)
        and set(buy) == {"max_price", "min_quality"}
        and _is_num(buy["max_price"])
        and _is_int(buy["min_quality"])
    ):
        return False
    return sell is None or (
        isinstance(sell, dict)
        and set(sell) == {"price", "max_quality", "max_units"}
        and _is_num(sell["price"])
        and _is_int(sell["max_quality"])
        and _is_int(sell["max_units"])
    )


def _price_ok(x: float) -> bool:
    if not math.isfinite(x):
        return False
    cents = x * 100
    return abs(cents - round(cents)) < 1e-6 and 0.01 <= x <= MAX_PEER_PRICE


def parse_decision(data: object) -> Decision:
    """Parse untrusted wire input; the first failing check raises (spec §4.3)."""
    if not _readable(data):
        raise InvalidDecision(MSG_UNREADABLE)
    assert isinstance(data, dict)
    try:
        incoterm_tier = IncotermTier(data["incoterm_tier"])
    except ValueError:
        raise InvalidDecision(MSG_TIER_UNAVAILABLE) from None
    for key in ("order_spot", "order_futures"):
        if not 0 <= data[key] <= MAX_ORDER:
            raise InvalidDecision(msg_order_bounds())
    lots: list[str] = data["conserve_lots"]
    if len(lots) > MAX_CONSERVE_LOTS:
        raise InvalidDecision(msg_conserve_too_many())
    if len(set(lots)) != len(lots):
        raise InvalidDecision(MSG_CONSERVE_INVALID)
    buy, sell = data["peer_buy"], data["peer_sell"]
    prices = [p["max_price"] for p in (buy,) if p] + [p["price"] for p in (sell,) if p]
    if not all(_price_ok(p) for p in prices):
        raise InvalidDecision(msg_peer_price_bounds())
    qualities = [p["min_quality"] for p in (buy,) if p]
    qualities += [p["max_quality"] for p in (sell,) if p]
    if not all(0 <= q <= 100 for q in qualities):
        raise InvalidDecision(MSG_QUALITY_BOUNDS)
    if sell is not None and not 1 <= sell["max_units"] <= MAX_ORDER:
        raise InvalidDecision(msg_offer_units_bounds())
    return Decision(
        data["day"],
        data["order_spot"],
        data["order_futures"],
        incoterm_tier,
        tuple(lots),
        data["cooperate"],
        (
            None
            if buy is None
            else PeerBuy(round_cents(buy["max_price"]), buy["min_quality"])
        ),
        (
            None
            if sell is None
            else PeerSell(
                round_cents(sell["price"]), sell["max_quality"], sell["max_units"]
            )
        ),
    )


# ---- a shipment in a pipeline ----


@dataclass(frozen=True)
class Shipment:
    qty: int
    lost: int
    unit_cost: float
    incoterm_tier: IncotermTier
    shipped_day: int

    def to_payload(self) -> dict:
        return {
            "qty": self.qty,
            "lost": self.lost,
            "unit_cost": self.unit_cost,
            "incoterm_tier": self.incoterm_tier.value,
            "shipped_day": self.shipped_day,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "Shipment":
        return cls(
            data["qty"],
            data["lost"],
            data["unit_cost"],
            IncotermTier(data["incoterm_tier"]),
            data["shipped_day"],
        )


# ---- a player's private day ----


@dataclass(frozen=True)
class DayRecord:
    day: int
    weekday: Weekday
    order_spot: int
    order_futures: int
    incoterm_tier: IncotermTier
    conserve_lots: tuple[str, ...]
    lots_conserved: int
    cooperate: bool
    peer_buy: PeerBuy | None
    peer_sell: PeerSell | None
    outlay: float
    opening_stock: int
    futures_due: int
    futures_filled: int
    futures_unfilled: int
    allocated_spot: int
    shortfall_spot: int
    shipped: int
    lost_transport: int
    delivered: int
    peer_bought: int
    peer_sold: int
    peer_trades: tuple[PeerTrade, ...]
    demand: int
    backlog_open: int
    sold: int
    backlog_end: int
    spoiled: int
    closing_stock: int
    revenue_sales: float
    revenue_salvage: float
    revenue_peer: float
    refund_insurance: float
    refund_futures: float
    cost_spot: float
    cost_futures: float
    cost_peer: float
    cost_transport: float
    cost_insurance: float
    cost_conservation: float
    cost_holding: float
    cost_backlog: float
    profit: float
    cash_open: float
    cash_end: float
    futures_vs_spot: float
    fill: float
    waste: float
    reputation_open: float
    reputation_end: float
    market_share: float
    refrigeration_failure: bool

    def to_payload(self) -> dict:
        out: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, (Weekday, IncotermTier)):
                value = value.value
            elif name == "conserve_lots":
                value = list(value)
            elif name == "peer_trades":
                value = [t.to_payload() for t in value]
            elif isinstance(value, (PeerBuy, PeerSell)):
                value = value.to_payload()
            out[name] = value
        return out

    @classmethod
    def from_payload(cls, data: dict) -> "DayRecord":
        values = {name: data[name] for name in cls.__dataclass_fields__}
        values["weekday"] = Weekday(values["weekday"])
        values["incoterm_tier"] = IncotermTier(values["incoterm_tier"])
        values["conserve_lots"] = tuple(values["conserve_lots"])
        values["peer_buy"] = _opt_buy(values["peer_buy"])
        values["peer_sell"] = _opt_sell(values["peer_sell"])
        values["peer_trades"] = tuple(
            PeerTrade.from_payload(t) for t in values["peer_trades"]
        )
        return cls(**values)


# ---- the market's day ----


@dataclass(frozen=True)
class MarketDay:
    day: int
    weekday: Weekday
    weekday_multiplier: float
    expected_demand: float
    demand_total: int
    capacity: int
    disrupted: bool
    spot_price: float
    cooperation: bool
    cooperators: int
    refrigeration_failure: bool
    units_sold_total: int
    peer_units: int
    peer_avg_price: float | None

    def to_payload(self) -> dict:
        out: dict[str, Any] = {n: getattr(self, n) for n in self.__dataclass_fields__}
        out["weekday"] = self.weekday.value
        return out

    @classmethod
    def from_payload(cls, data: dict) -> "MarketDay":
        values = {name: data[name] for name in cls.__dataclass_fields__}
        values["weekday"] = Weekday(values["weekday"])
        return cls(**values)


# ---- the public day results ----


@dataclass(frozen=True)
class PublicPlayerDay:
    units_sold: int
    market_share: float
    reputation: float


@dataclass(frozen=True)
class PublicDayResults:
    day: int
    weekday: Weekday
    demand_total: int
    units_sold_total: int
    spot_price: float
    disrupted: bool
    cooperation: bool
    cooperators: int
    refrigeration_failure: bool
    peer_units: int
    peer_avg_price: float | None
    players: dict[str, PublicPlayerDay]

    def to_payload(self, display_names: dict[str, str] | None = None) -> dict:
        out: dict[str, Any] = {
            n: getattr(self, n) for n in self.__dataclass_fields__ if n != "players"
        }
        out["weekday"] = self.weekday.value
        players: dict[str, dict[str, Any]] = {}
        for alias, p in self.players.items():
            entry: dict[str, Any] = {
                "units_sold": p.units_sold,
                "market_share": p.market_share,
                "reputation": p.reputation,
            }
            if display_names is not None:
                entry["display_name"] = display_names[alias]
            players[alias] = entry
        out["players"] = players
        return out

    @classmethod
    def from_payload(cls, data: dict) -> "PublicDayResults":
        values = {n: data[n] for n in cls.__dataclass_fields__ if n != "players"}
        values["weekday"] = Weekday(values["weekday"])
        values["players"] = {
            alias: PublicPlayerDay(p["units_sold"], p["market_share"], p["reputation"])
            for alias, p in data["players"].items()
        }
        return cls(**values)


# ---- the end-of-game settlement ----


@dataclass(frozen=True)
class FinalSettlement:
    liquidated_units: int
    revenue_liquidation: float
    refund_insurance_in_transit: float
    futures_cancelled_units: int
    refund_futures_cancelled: float
    settlement_total: float

    def to_payload(self) -> dict:
        return {n: getattr(self, n) for n in self.__dataclass_fields__}

    @classmethod
    def from_payload(cls, data: dict) -> "FinalSettlement":
        return cls(**{n: data[n] for n in cls.__dataclass_fields__})
