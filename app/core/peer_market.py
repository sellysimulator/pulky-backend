"""Peer market: priority matching at the asking price (spec 5.7)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Bid:
    shortfall: int
    max_price_cents: int
    min_quality: int
    cash_cents: int


@dataclass(frozen=True)
class OfferLot:
    lot_id: str
    qty: int
    quality: int
    days_left: int


@dataclass(frozen=True)
class Offer:
    price_cents: int
    max_units: int
    lots: tuple[OfferLot, ...]


@dataclass(frozen=True)
class Trade:
    buyer: str
    seller: str
    seller_lot: str
    qty: int
    quality: int
    price_cents: int


def _validate(
    bids: Mapping[str, Bid], offers: Mapping[str, Offer], aliases: Sequence[str]
) -> None:
    known = set(aliases)
    for a, bid in bids.items():
        if a not in known:
            raise ValueError("bid alias is not a seated alias")
        if (
            bid.shortfall <= 0
            or bid.max_price_cents <= 0
            or not 0 <= bid.min_quality <= 100
            or bid.cash_cents < 0
        ):
            raise ValueError("invalid bid")
    for a, offer in offers.items():
        if a not in known:
            raise ValueError("offer alias is not a seated alias")
        if offer.price_cents <= 0 or offer.max_units < 1:
            raise ValueError("invalid offer")
        for lot in offer.lots:
            if lot.qty <= 0 or not 0 <= lot.quality <= 100 or lot.days_left < 1:
                raise ValueError("invalid offer lot")


def clear_peer_market(
    bids: Mapping[str, Bid],
    offers: Mapping[str, Offer],
    aliases: Sequence[str],
    rng: random.Random,
) -> list[Trade]:
    tiebreak = {a: rng.random() for a in sorted(aliases)}
    _validate(bids, offers, aliases)
    lot_left = {(s, lot.lot_id): lot.qty for s, o in offers.items() for lot in o.lots}
    units_left = {s: o.max_units for s, o in offers.items()}
    trades: list[Trade] = []
    for b in sorted(
        bids, key=lambda a: (-bids[a].max_price_cents, -bids[a].shortfall, tiebreak[a])
    ):
        bid = bids[b]
        need, cash = bid.shortfall, bid.cash_cents
        candidates = [
            (s, i, lot)
            for s, o in offers.items()
            if s != b
            for i, lot in enumerate(o.lots)
            if lot.quality >= bid.min_quality and o.price_cents <= bid.max_price_cents
        ]
        candidates.sort(
            key=lambda c: (
                -c[2].quality,
                offers[c[0]].price_cents,
                -c[2].days_left,
                tiebreak[c[0]],
                c[1],
            )
        )
        for s, _, lot in candidates:
            price = offers[s].price_cents
            take = min(need, lot_left[(s, lot.lot_id)], units_left[s], cash // price)
            if take <= 0:
                continue
            lot_left[(s, lot.lot_id)] -= take
            units_left[s] -= take
            need -= take
            cash -= take * price
            trades.append(
                Trade(
                    buyer=b,
                    seller=s,
                    seller_lot=lot.lot_id,
                    qty=take,
                    quality=lot.quality,
                    price_cents=price,
                )
            )
            if need == 0:
                break
    return trades
