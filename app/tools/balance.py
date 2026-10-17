"""Balance harness: scripted strategies played on the real engine (spec 13.14)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.calendar import weekday_of
from app.core.config_models import GameConfig
from app.core.engine import PulkyEngine
from app.core.enums import WEEKDAYS, IncotermTier, Phase
from app.core.kernels import weekday_multiplier
from app.core.limits import MAX_CONSERVE_LOTS, MAX_ORDER, MAX_PEER_PRICE
from app.core.money import round_cents
from app.core.records import Decision, PeerBuy, PeerSell
from app.core.sampling import halfup

# --------------------------------------------------------------------- helpers


def _own(engine: PulkyEngine, view: dict, day: int) -> float:
    """Expected own demand for ``day`` with today's reputations (spec 7.2, 7.3)."""
    cfg = engine.config
    reps = view["brief"]["reputation"]
    weights = {a: 0.5 + r for a, r in reps.items()}
    share = weights[view["alias"]] / sum(weights.values())
    mult = weekday_multiplier(
        weekday_of(day), cfg.demand.weekday_peak.from_, cfg.demand.weekday_peak.to
    )
    return cfg.demand.per_player_peak * len(reps) * mult * share


def _stock(view: dict) -> int:
    return sum(int(lot["qty"]) for lot in view["cellar"])


def _order_up_to_spot(engine: PulkyEngine, view: dict, safety: int = 0) -> int:
    if not view["order_windows"]["spot_open"]:
        return 0
    day = view["day"]
    lead = engine.config.supplier.lead_time_days
    target = sum(_own(engine, view, t) for t in range(day, day + lead + 1))
    target += view["backlog"]
    horizon = day + lead
    cover = _stock(view) - view["expected_spoil_tonight"]
    cover += sum(s["qty"] for s in view["in_transit"])
    cover += sum(f["qty"] for f in view["futures_book"] if f["delivery_day"] <= horizon)
    return min(MAX_ORDER, max(0, halfup(target + safety - cover)))


def _largest(
    ok: Callable[[int], bool],
    high: int,
) -> int:
    """Largest q in 0..high with ``ok(q)``, ``ok`` being monotone; -1 if none."""
    if ok(high):
        return high
    if not ok(0):
        return -1
    low = 0
    while high - low > 1:
        mid = (low + high) // 2
        if ok(mid):
            low = mid
        else:
            high = mid
    return low


def _trim(engine: PulkyEngine, alias: str, decision: Decision, cash: float) -> Decision:
    """Lower a decision until it is affordable (spec 8.3), in the harness order."""

    def affordable(d: Decision) -> bool:
        out = engine.outlay(alias, d)
        return out == 0 or out <= cash

    if affordable(decision):
        return decision
    q = _largest(
        lambda x: affordable(dataclasses.replace(decision, order_spot=x)),
        decision.order_spot,
    )
    decision = dataclasses.replace(decision, order_spot=max(q, 0))
    if affordable(decision):
        return decision
    q = _largest(
        lambda x: affordable(dataclasses.replace(decision, order_futures=x)),
        decision.order_futures,
    )
    decision = dataclasses.replace(decision, order_futures=max(q, 0))
    if affordable(decision):
        return decision
    decision = dataclasses.replace(decision, conserve_lots=())
    if affordable(decision):
        return decision
    decision = dataclasses.replace(decision, incoterm_tier=IncotermTier.EXW)
    if affordable(decision):
        return decision
    return dataclasses.replace(
        decision, order_spot=0, order_futures=0, conserve_lots=()
    )


def _peer_price(x: float) -> float:
    return float(min(MAX_PEER_PRICE, max(0.01, round_cents(x))))


# ------------------------------------------------------------------ strategies


class Strategy(Protocol):
    name: str

    def decide(self, engine: PulkyEngine, alias: str) -> Decision: ...


def _base(engine: PulkyEngine, alias: str, safety: int = 0) -> tuple[dict, Decision]:
    view = engine.player_view(alias, False)
    zero = Decision.zero(view["day"])
    spot = _order_up_to_spot(engine, view, safety)
    return view, dataclasses.replace(zero, order_spot=spot)


def _finish(
    engine: PulkyEngine, alias: str, view: dict, decision: Decision
) -> Decision:
    return _trim(engine, alias, decision, view["cash"])


class _Naive:
    name = "naive"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view = engine.player_view(alias, False)
        cfg = engine.config
        mults = [
            weekday_multiplier(
                w, cfg.demand.weekday_peak.from_, cfg.demand.weekday_peak.to
            )
            for w in WEEKDAYS
        ]
        spot = 0
        if view["order_windows"]["spot_open"]:
            spot = min(
                MAX_ORDER,
                halfup(cfg.demand.per_player_peak * sum(mults) / len(mults)),
            )
        decision = dataclasses.replace(Decision.zero(view["day"]), order_spot=spot)
        return _finish(engine, alias, view, decision)


class _OrderUpTo:
    name = "order_up_to"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias)
        return _finish(engine, alias, view, decision)


class _Hedger:
    name = "hedger"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias)
        cfg = engine.config
        arrival = view["day"] + cfg.futures.horizon_days
        window = cfg.demand.weekday_peak
        peak = weekday_multiplier(weekday_of(arrival), window.from_, window.to) == 1.0
        if view["order_windows"]["futures_open"] and peak:
            futures = min(MAX_ORDER, halfup(_own(engine, view, arrival)))
            decision = dataclasses.replace(decision, order_futures=futures)
        return _finish(engine, alias, view, decision)


class _Insured:
    name = "insured"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias)
        tiers = engine.config.insurance.tiers
        tier = IncotermTier.EXW
        for candidate, cfg in (
            (IncotermTier.DDP, tiers.ddp),
            (IncotermTier.DAP, tiers.dap),
            (IncotermTier.CIP, tiers.cip),
        ):
            if cfg.enabled:
                tier = candidate
                break
        decision = dataclasses.replace(decision, incoterm_tier=tier)
        return _finish(engine, alias, view, decision)


class _Conserver:
    name = "conserver"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias)
        cost = engine.config.stock.conservation_cost_per_unit
        lots = tuple(
            lot["id"]
            for lot in view["cellar"]
            if not lot["conserved"]
            and lot["expected_loss_tonight"] * lot["unit_cost"] > cost * lot["qty"]
        )[:MAX_CONSERVE_LOTS]
        decision = dataclasses.replace(decision, conserve_lots=lots)
        return _finish(engine, alias, view, decision)


class _Coop:
    name = "coop"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias)
        decision = dataclasses.replace(decision, cooperate=True)
        return _finish(engine, alias, view, decision)


TRADER_SAFETY_STOCK = 5


class _Trader:
    name = "trader"

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        view, decision = _base(engine, alias, TRADER_SAFETY_STOCK)
        decision = _finish(engine, alias, view, decision)
        cfg = engine.config
        if not cfg.peer_market.enabled:
            return decision
        buy = PeerBuy(_peer_price(cfg.demand.retail_price), 30)
        sell: PeerSell | None = None
        max_units = min(
            MAX_ORDER, _stock(view) - halfup(_own(engine, view, view["day"]))
        )
        if max_units >= 1:
            price = cfg.supplier.base_price * (1 + cfg.supplier.spot_spread)
            sell = PeerSell(_peer_price(price), 100, max_units)
        return dataclasses.replace(decision, peer_buy=buy, peer_sell=sell)


STRATEGIES: dict[str, Strategy] = {
    s.name: s
    for s in (
        _Naive(),
        _OrderUpTo(),
        _Hedger(),
        _Insured(),
        _Conserver(),
        _Coop(),
        _Trader(),
    )
}


# ----------------------------------------------------------------------- games


def play_game(
    config: GameConfig, strategy: Strategy, n_players: int, seed: int
) -> PulkyEngine:
    seats = {f"P{i}": f"Bot {i}" for i in range(1, n_players + 1)}
    engine = PulkyEngine.start(config, seats, seed)
    while engine.phase != Phase.FINISHED:
        for alias in engine.aliases:
            engine.submit(alias, strategy.decide(engine, alias))
        engine.resolve_day()
    return engine


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    shared: bool
    games: int
    profit_mean: float
    profit_sd: float
    waste_rate_mean: float
    waste_rate_median: float
    fill_rate_mean: float
    market_share_mean: float
    backlog_days_mean: float
    disruption_fill_mean: float | None
    disruption_sold_mean: float | None
    disruption_trade_day_share: float | None
    normal_sold_mean: float
    peak_backlog_mean: float
    offpeak_backlog_mean: float


@dataclass(frozen=True)
class TargetResult:
    number: int
    passed: bool
    detail: str


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def evaluate(
    config: GameConfig, strategy: Strategy, n_players: int, seeds: Sequence[int]
) -> StrategyResult:
    window = config.demand.weekday_peak
    lead = config.supplier.lead_time_days
    profits: list[float] = []
    wastes: list[float] = []
    fills: list[float] = []
    shares: list[float] = []
    backlog_days: list[float] = []
    dis_fill: list[float] = []
    dis_sold: list[float] = []
    normal_sold: list[float] = []
    peak_backlog: list[float] = []
    off_backlog: list[float] = []
    dis_days = 0
    trade_days = 0
    for seed in seeds:
        engine = play_game(config, strategy, n_players, seed)
        players = engine.standings()["players"]
        for stats in players.values():
            profits.append(stats["total_profit"])
            if stats["waste_rate"] is not None:
                wastes.append(stats["waste_rate"])
            if stats["fill_rate"] is not None:
                fills.append(stats["fill_rate"])
            shares.append(stats["overall_market_share"])
            backlog_days.append(stats["days_with_backlog"])
        market = {m.day: m for m in engine.market_history()}
        disrupted = [m for m in market.values() if m.disrupted]
        dis_days += len(disrupted)
        trade_days += sum(1 for m in disrupted if m.peer_units > 0)
        last = max(market)
        arrivals = [
            m.day + lead
            for m in disrupted
            if m.day + lead <= last and not market[m.day + lead].disrupted
        ]
        arrival_weekdays = {market[t].weekday for t in arrivals}
        for alias in engine.aliases:
            for rec in engine.records(alias):
                if market[rec.day].disrupted:
                    dis_fill.append(rec.fill)
                else:
                    if rec.weekday in arrival_weekdays and rec.day not in arrivals:
                        normal_sold.append(rec.sold)
                dis_sold.extend([rec.sold] * arrivals.count(rec.day))
                peak = weekday_multiplier(rec.weekday, window.from_, window.to) == 1.0
                (peak_backlog if peak else off_backlog).append(rec.backlog_end)
    return StrategyResult(
        strategy=strategy.name,
        shared=config.transport.shared,
        games=len(seeds),
        profit_mean=_mean(profits),
        profit_sd=statistics.pstdev(profits) if profits else 0.0,
        waste_rate_mean=_mean(wastes),
        waste_rate_median=statistics.median(wastes) if wastes else 0.0,
        fill_rate_mean=_mean(fills),
        market_share_mean=_mean(shares),
        backlog_days_mean=_mean(backlog_days),
        disruption_fill_mean=_mean_or_none(dis_fill),
        disruption_sold_mean=_mean_or_none(dis_sold),
        disruption_trade_day_share=(trade_days / dis_days if dis_days else None),
        normal_sold_mean=_mean(normal_sold),
        peak_backlog_mean=_mean(peak_backlog),
        offpeak_backlog_mean=_mean(off_backlog),
    )


# --------------------------------------------------------------------- targets


def _fmt(x: float | None) -> str:
    return "none" if x is None else f"{x:.4g}"


def check_targets(
    results: Mapping[tuple[str, bool], StrategyResult],
) -> list[TargetResult]:
    def get(name: str, shared: bool = False) -> StrategyResult | None:
        return results.get((name, shared))

    def missing(number: int, what: str) -> TargetResult:
        return TargetResult(number, False, f"missing input: {what}")

    out: list[TargetResult] = []
    naive, base = get("naive"), get("order_up_to")
    hedger, insured = get("hedger"), get("insured")
    conserver, trader = get("conserver"), get("trader")

    # 1
    if naive is None or base is None:
        out.append(missing(1, "naive and order_up_to"))
    else:
        ok = base.profit_mean >= 1.10 * naive.profit_mean
        out.append(
            TargetResult(
                1,
                ok,
                f"order_up_to profit {_fmt(base.profit_mean)} vs naive "
                f"{_fmt(naive.profit_mean)} (need >= 1.10x)",
            )
        )
    # 2
    if base is None:
        out.append(missing(2, "order_up_to"))
    else:
        m = base.waste_rate_median
        out.append(
            TargetResult(
                2, 0.05 <= m <= 0.20, f"order_up_to median waste rate {_fmt(m)}"
            )
        )
    # 3
    if hedger is None or base is None:
        out.append(missing(3, "hedger and order_up_to"))
    elif hedger.disruption_fill_mean is None or base.disruption_fill_mean is None:
        out.append(missing(3, "disruption fill (no disruption day occurred)"))
    else:
        gap = abs(hedger.profit_mean - base.profit_mean)
        ok = (
            gap <= 0.05 * abs(base.profit_mean)
            and hedger.profit_sd < base.profit_sd
            and hedger.disruption_fill_mean > base.disruption_fill_mean
        )
        out.append(
            TargetResult(
                3,
                ok,
                f"hedger profit {_fmt(hedger.profit_mean)} vs "
                f"{_fmt(base.profit_mean)}, "
                f"sd {_fmt(hedger.profit_sd)} vs {_fmt(base.profit_sd)}, disruption "
                f"fill {_fmt(hedger.disruption_fill_mean)} vs "
                f"{_fmt(base.disruption_fill_mean)}",
            )
        )
    # 4
    ins_shared, base_shared = get("insured", True), get("order_up_to", True)
    if insured is None or base is None:
        out.append(missing(4, "insured and order_up_to (not shared)"))
    elif ins_shared is None or base_shared is None:
        out.append(missing(4, "insured and order_up_to with shared transport"))
    else:
        m = base.profit_mean
        ok_plain = 0.95 * m <= insured.profit_mean <= m and (
            insured.profit_sd < base.profit_sd
        )
        ok_shared = ins_shared.profit_mean < 0.95 * base_shared.profit_mean
        out.append(
            TargetResult(
                4,
                ok_plain and ok_shared,
                f"not shared: insured {_fmt(insured.profit_mean)} vs {_fmt(m)}, sd "
                f"{_fmt(insured.profit_sd)} vs {_fmt(base.profit_sd)}; shared: "
                f"insured {_fmt(ins_shared.profit_mean)} vs "
                f"{_fmt(base_shared.profit_mean)}",
            )
        )
    # 5
    if conserver is None or base is None:
        out.append(missing(5, "conserver and order_up_to"))
    else:
        ok = (
            conserver.waste_rate_mean < base.waste_rate_mean
            and conserver.profit_mean >= base.profit_mean
        )
        out.append(
            TargetResult(
                5,
                ok,
                f"conserver waste {_fmt(conserver.waste_rate_mean)} vs "
                f"{_fmt(base.waste_rate_mean)}, profit {_fmt(conserver.profit_mean)} "
                f"vs {_fmt(base.profit_mean)}",
            )
        )
    # 6
    if base is None or naive is None:
        out.append(missing(6, "order_up_to and naive"))
    elif base.disruption_sold_mean is None:
        out.append(missing(6, "order_up_to disruption sales (no disruption day)"))
    else:
        ok = (
            0 < base.disruption_sold_mean < 0.9 * base.normal_sold_mean
            and naive.peak_backlog_mean > naive.offpeak_backlog_mean
        )
        out.append(
            TargetResult(
                6,
                ok,
                f"order_up_to sold {_fmt(base.disruption_sold_mean)} on disruption "
                f"days vs {_fmt(base.normal_sold_mean)} normal; naive backlog peak "
                f"{_fmt(naive.peak_backlog_mean)} vs off-peak "
                f"{_fmt(naive.offpeak_backlog_mean)}",
            )
        )
    # 7
    if trader is None or base is None:
        out.append(missing(7, "trader and order_up_to"))
    elif (
        trader.disruption_trade_day_share is None
        or trader.disruption_fill_mean is None
        or base.disruption_fill_mean is None
    ):
        out.append(missing(7, "trade share or disruption fill (no disruption day)"))
    else:
        ok = (
            trader.disruption_trade_day_share >= 0.05
            and trader.disruption_fill_mean > base.disruption_fill_mean
        )
        out.append(
            TargetResult(
                7,
                ok,
                f"trader traded on {_fmt(trader.disruption_trade_day_share)} of "
                f"disruption days; disruption fill {_fmt(trader.disruption_fill_mean)} "
                f"vs {_fmt(base.disruption_fill_mean)}",
            )
        )
    return out


# ------------------------------------------------------------------------- CLI


def _shared_config(config: GameConfig) -> GameConfig:
    data = config.to_dict()
    data["transport"]["shared"] = True
    return GameConfig.from_dict(data)


def _table(results: Sequence[StrategyResult]) -> str:
    head = (
        f"{'strategy':<12}{'shared':<7}{'games':>6}{'profit':>10}{'sd':>9}"
        f"{'waste':>8}{'w.med':>8}{'fill':>7}{'share':>7}{'backlog':>8}"
    )
    lines = [head]
    for r in results:
        lines.append(
            f"{r.strategy:<12}{str(r.shared):<7}{r.games:>6}{r.profit_mean:>10.2f}"
            f"{r.profit_sd:>9.2f}{r.waste_rate_mean:>8.3f}{r.waste_rate_median:>8.3f}"
            f"{r.fill_rate_mean:>7.3f}{r.market_share_mean:>7.3f}"
            f"{r.backlog_days_mean:>8.2f}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools.balance")
    parser.add_argument("--seeds", type=int, default=200)
    parser.add_argument("--players", type=int, default=4)
    parser.add_argument("--strategy", nargs="+", action="extend", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    names = args.strategy or list(STRATEGIES)
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        parser.error(f"unknown strategy: {', '.join(unknown)}")
    seeds = range(args.seeds)
    base = GameConfig.default()
    shared = _shared_config(base)
    results: dict[tuple[str, bool], StrategyResult] = {}
    for name in names:
        results[(name, False)] = evaluate(base, STRATEGIES[name], args.players, seeds)
        if name in ("insured", "order_up_to"):
            results[(name, True)] = evaluate(
                shared, STRATEGIES[name], args.players, seeds
            )
    targets = check_targets(results)
    if args.json:
        payload: dict[str, Any] = {
            "results": [dataclasses.asdict(r) for r in results.values()],
            "targets": [dataclasses.asdict(t) for t in targets],
        }
        print(json.dumps(payload))
    else:
        print(_table(list(results.values())))
        for t in targets:
            print(f"target {t.number}: {'PASS' if t.passed else 'FAIL'}  {t.detail}")
    return 0 if all(t.passed for t in targets) else 1


if __name__ == "__main__":
    sys.exit(main())
