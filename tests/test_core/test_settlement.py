"""Black-box tests for section 11: ``app/core/settlement.py`` (spec 8.2)."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.enums import Incoterm
from app.core.insurance import refund_value
from app.core.money import round_cents
from app.core.records import FinalSettlement, Shipment
from app.core.settlement import final_settlement

EXW, CIP, DAP, DDP = Incoterm.EXW, Incoterm.CIP, Incoterm.DAP, Incoterm.DDP
BASE, RETAIL, SALVAGE = 12.0, 30.0, 2.0


def settle(**over):
    kw = dict(
        cellar_units=0,
        in_transit=[],
        open_futures_units=0,
        ended_early=False,
        salvage_value=SALVAGE,
        base_price=BASE,
        retail_price=RETAIL,
    )
    kw.update(over)
    return final_settlement(**kw)


def ship(qty, lost, cost, tier, day=1):
    return Shipment(qty=qty, lost=lost, unit_cost=cost, incoterm=tier, shipped_day=day)


def test_returns_a_final_settlement():
    assert isinstance(settle(), FinalSettlement)


def test_natural_end_liquidates_only_the_cellar():
    s = settle(cellar_units=7)
    assert s == FinalSettlement(
        liquidated_units=7,
        revenue_liquidation=14.0,
        refund_insurance_in_transit=0.0,
        futures_cancelled_units=0,
        refund_futures_cancelled=0.0,
        settlement_total=14.0,
    )


def test_natural_end_ignores_in_transit_and_open_futures():
    s = settle(
        cellar_units=3,
        in_transit=[ship(10, 4, 12.0, DDP)],
        open_futures_units=5,
        ended_early=False,
    )
    assert s.liquidated_units == 3
    assert s.refund_insurance_in_transit == 0.0
    assert s.futures_cancelled_units == 0
    assert s.refund_futures_cancelled == 0.0
    assert s.settlement_total == 6.0


def test_early_end_adds_in_transit_units_and_cancelled_futures():
    s = settle(
        cellar_units=5,
        in_transit=[ship(10, 0, 12.0, EXW), ship(6, 0, 12.0, EXW, day=2)],
        open_futures_units=20,
        ended_early=True,
    )
    assert s.liquidated_units == 5 + 10 + 6
    assert s.revenue_liquidation == 42.0
    assert s.futures_cancelled_units == 20
    assert s.refund_futures_cancelled == 240.0
    assert s.refund_insurance_in_transit == 0.0
    assert s.settlement_total == 282.0


def test_early_end_in_transit_refunds_by_tier_with_dap_at_base_price():
    paid = 13.0
    s = settle(
        in_transit=[
            ship(10, 4, paid, EXW),
            ship(10, 3, paid, CIP),
            ship(10, 2, paid, DAP),
            ship(10, 1, paid, DDP),
        ],
        ended_early=True,
    )
    # EXW nothing; CIP 3 x paid; DAP 2 x base price (the arrival spot price is
    # never drawn); DDP 1 x retail price.
    assert s.refund_insurance_in_transit == 3 * 13.0 + 2 * BASE + RETAIL
    assert s.liquidated_units == 40
    assert s.settlement_total == s.revenue_liquidation + 93.0


def test_dap_refund_uses_base_price_not_the_price_paid():
    s = settle(in_transit=[ship(0, 5, 20.0, DAP)], ended_early=True)
    assert s.refund_insurance_in_transit == 5 * BASE


def test_salvage_price_zero_earns_nothing_on_liquidation():
    s = settle(cellar_units=9, in_transit=[ship(4, 0, 12.0, EXW)], ended_early=True)
    s0 = settle(
        cellar_units=9,
        in_transit=[ship(4, 0, 12.0, EXW)],
        ended_early=True,
        salvage_value=0.0,
    )
    assert s.revenue_liquidation == 26.0
    assert s0.liquidated_units == 13
    assert s0.revenue_liquidation == 0.0
    assert s0.settlement_total == 0.0


def test_every_term_is_rounded_before_the_total():
    # 0.125 rounds to 0.13 in each term; the total is the sum of the rounded
    # terms (0.26), not the rounded sum of the raw terms (0.25).
    s = settle(
        cellar_units=1,
        in_transit=[ship(0, 1, 0.125, CIP)],
        ended_early=True,
        salvage_value=0.125,
    )
    assert s.revenue_liquidation == 0.13
    assert s.refund_insurance_in_transit == 0.13
    assert s.settlement_total == 0.26


def test_money_values_are_whole_cents():
    s = settle(cellar_units=7, salvage_value=0.333)
    assert s.revenue_liquidation == 2.33
    assert s.settlement_total == 2.33


@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    cellar=st.integers(0, 500),
    shipments=st.lists(
        st.tuples(
            st.integers(0, 100),
            st.integers(0, 100),
            st.integers(1, 3000).map(lambda c: c / 100),
            st.sampled_from([EXW, CIP, DAP, DDP]),
        ),
        max_size=6,
    ),
    futures=st.integers(0, 300),
    early=st.booleans(),
    salvage_cents=st.integers(0, 1200),
)
def test_settlement_matches_the_spec_formula(
    cellar, shipments, futures, early, salvage_cents
):
    salvage = salvage_cents / 100
    transit = [
        ship(q, lo, c, t, day=i + 1) for i, (q, lo, c, t) in enumerate(shipments)
    ]
    s = settle(
        cellar_units=cellar,
        in_transit=transit,
        open_futures_units=futures,
        ended_early=early,
        salvage_value=salvage,
    )
    if early:
        units = cellar + sum(x.qty for x in transit)
        refund = round_cents(
            sum(
                x.lost
                * refund_value(
                    x.incoterm,
                    paid_unit_cost=x.unit_cost,
                    arrival_spot_price=BASE,
                    retail_price=RETAIL,
                )
                for x in transit
            )
        )
        cancelled = futures
    else:
        units, refund, cancelled = cellar, 0.0, 0
    assert s.liquidated_units == units
    assert s.futures_cancelled_units == cancelled
    assert s.revenue_liquidation == round_cents(salvage * units)
    assert s.refund_insurance_in_transit == pytest.approx(refund, abs=0.0051 * 6)
    assert s.refund_futures_cancelled == round_cents(BASE * cancelled)
    assert s.settlement_total == pytest.approx(
        s.revenue_liquidation
        + s.refund_insurance_in_transit
        + s.refund_futures_cancelled,
        abs=0.0051,
    )
