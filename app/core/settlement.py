"""Final settlement (spec §8.2)."""

from collections.abc import Sequence

from app.core.insurance import refund_value
from app.core.money import round_cents
from app.core.records import FinalSettlement, Shipment


def final_settlement(
    *,
    cellar_units: int,
    in_transit: Sequence[Shipment],
    open_futures_units: int,
    ended_early: bool,
    salvage_value: float,
    base_price: float,
    retail_price: float,
) -> FinalSettlement:
    """Liquidate one player's remaining position.

    A natural end liquidates the cellar only. An early end also liquidates the
    units in transit, pays the insurance owed on them (DAP at the base price,
    the arrival day's spot price never being drawn) and refunds unshipped
    futures contracts at the base price.
    """
    liquidated = cellar_units
    refund_insurance = 0.0
    cancelled_units = 0
    refund_futures = 0.0
    if ended_early:
        liquidated += sum(shipment.qty for shipment in in_transit)
        refund_insurance = round_cents(
            sum(
                shipment.lost
                * refund_value(
                    shipment.incoterm_tier,
                    paid_unit_cost=shipment.unit_cost,
                    arrival_spot_price=base_price,
                    retail_price=retail_price,
                )
                for shipment in in_transit
            )
        )
        cancelled_units = open_futures_units
        refund_futures = round_cents(base_price * open_futures_units)
    revenue_liquidation = round_cents(salvage_value * liquidated)
    return FinalSettlement(
        liquidated_units=liquidated,
        revenue_liquidation=revenue_liquidation,
        refund_insurance_in_transit=refund_insurance,
        futures_cancelled_units=cancelled_units,
        refund_futures_cancelled=refund_futures,
        settlement_total=round_cents(
            revenue_liquidation + refund_insurance + refund_futures
        ),
    )
