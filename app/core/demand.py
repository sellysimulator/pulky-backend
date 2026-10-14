"""Market demand, its split by reputation, and serving with backlog (spec §7.2-§7.7)."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.rng import stream
from app.core.sampling import poisson


def market_lambda(
    per_player_peak: float, n_players: int, weekday_multiplier: float
) -> float:
    return per_player_peak * n_players * weekday_multiplier


def shares(reputations: Mapping[str, float]) -> dict[str, float]:
    weights = {alias: 0.5 + rep for alias, rep in reputations.items()}
    total = sum(weights.values())
    return {alias: weight / total for alias, weight in weights.items()}


def draw_demands(seed: int, day: int, lambdas: Mapping[str, float]) -> dict[str, int]:
    return {
        alias: poisson(stream(seed, day, f"demand:{alias}"), lam)
        for alias, lam in lambdas.items()
    }


@dataclass(frozen=True)
class ServeResult:
    owed: int
    sold: int
    backlog_end: int


def serve(backlog_open: int, demand: int, stock: int) -> ServeResult:
    if backlog_open < 0 or demand < 0 or stock < 0:
        raise ValueError("backlog_open, demand and stock must not be negative")
    owed = backlog_open + demand
    sold = min(owed, stock)
    return ServeResult(owed=owed, sold=sold, backlog_end=owed - sold)


def market_shares(sold: Mapping[str, int]) -> dict[str, float]:
    total = sum(sold.values())
    if total <= 0:
        return {alias: 0.0 for alias in sold}
    return {alias: units / total for alias, units in sold.items()}
