"""Black-box tests for app.core.insurance (plan 08, spec section 6.3-6.4)."""

import pytest

from app.core.enums import IncotermTier
from app.core.insurance import (
    INCOTERM_TIER_LABELS,
    INCOTERM_TIERS,
    REFUND_BASIS,
    InsuredTierTerms,
    break_even_fee,
    fee_from_margin,
    fee_ratio,
    plan_rows,
    ratio_badge,
    refund_value,
    tier_fee,
)

EXW, CIP, DAP, DDP = (
    IncotermTier.EXW,
    IncotermTier.CIP,
    IncotermTier.DAP,
    IncotermTier.DDP,
)

DEFAULT_TIERS = {
    CIP: InsuredTierTerms(enabled=True, fee=0.40),
    DAP: InsuredTierTerms(enabled=True, fee=0.40),
    DDP: InsuredTierTerms(enabled=True, fee=0.99),
}
KW = dict(
    failure_probability=0.1,
    loss_fraction=0.3,
    n_ref=4,
    reference_shipment=20,
    base_price=12,
    retail_price=30,
)
ROW_KEYS = {
    "tier",
    "label",
    "enabled",
    "refund_basis",
    "refund_value_today",
    "fee_per_unit",
}


# --- constants --------------------------------------------------------------


def test_tier_order():
    assert tuple(INCOTERM_TIERS) == (EXW, CIP, DAP, DDP)


def test_tier_labels_verbatim():
    assert INCOTERM_TIER_LABELS == {
        EXW: "No insurance — you carry the risk",
        CIP: "Paid-price insurance — refund what you paid",
        DAP: "Market-price insurance — refund at the arrival-day spot price",
        DDP: "Retail-price insurance — refund at the retail price",
    }


def test_refund_basis():
    assert REFUND_BASIS == {
        EXW: "none",
        CIP: "paid",
        DAP: "arrival_spot",
        DDP: "retail",
    }


# --- tier_fee ----------------------------------------------------------------


def test_tier_fee_exw_is_zero_and_not_in_tiers():
    assert tier_fee(EXW, DEFAULT_TIERS) == 0


def test_tier_fee_enabled_tiers():
    assert tier_fee(CIP, DEFAULT_TIERS) == 0.40
    assert tier_fee(DAP, DEFAULT_TIERS) == 0.40
    assert tier_fee(DDP, DEFAULT_TIERS) == 0.99


@pytest.mark.parametrize("tier", [CIP, DAP, DDP])
def test_tier_fee_disabled_raises(tier):
    tiers = dict(DEFAULT_TIERS)
    tiers[tier] = InsuredTierTerms(enabled=False, fee=0.5)
    with pytest.raises(ValueError):
        tier_fee(tier, tiers)


# --- refund_value ------------------------------------------------------------


def _refund(tier):
    return refund_value(tier, paid_unit_cost=13, arrival_spot_price=14, retail_price=30)


def test_refund_value_example():
    assert [15 * _refund(t) for t in (EXW, CIP, DAP, DDP)] == [0, 195, 210, 450]


def test_fm4_dap_refund_is_arrival_spot_not_paid():
    assert _refund(DAP) == 14
    assert _refund(DAP) != _refund(CIP)


# --- plan_rows ---------------------------------------------------------------


def test_plan_rows_default():
    rows = plan_rows(DEFAULT_TIERS, today_spot_price=12.5, retail_price=30)
    assert [r["tier"] for r in rows] == ["exw", "cip", "dap", "ddp"]
    assert all(set(r) == ROW_KEYS for r in rows)
    by = {r["tier"]: r for r in rows}
    for tier in INCOTERM_TIERS:
        r = by[str(tier)]
        assert r["label"] == INCOTERM_TIER_LABELS[tier]
        assert r["refund_basis"] == REFUND_BASIS[tier]
    assert by["exw"]["enabled"] is True
    assert by["exw"]["fee_per_unit"] == 0
    assert by["exw"]["refund_value_today"] is None
    assert by["cip"]["refund_value_today"] == 12.5
    assert by["dap"]["refund_value_today"] is None
    assert by["ddp"]["refund_value_today"] == 30
    assert by["cip"]["fee_per_unit"] == 0.40
    assert by["dap"]["fee_per_unit"] == 0.40
    assert by["ddp"]["fee_per_unit"] == 0.99


def test_plan_rows_disabled_tier_keeps_stored_fee():
    tiers = dict(DEFAULT_TIERS)
    tiers[DAP] = InsuredTierTerms(enabled=False, fee=0.77)
    rows = {
        r["tier"]: r for r in plan_rows(tiers, today_spot_price=12, retail_price=30)
    }
    assert len(rows) == 4
    assert rows["dap"]["enabled"] is False
    assert rows["dap"]["fee_per_unit"] == 0.77
    assert rows["cip"]["enabled"] is True


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def test_plan_rows_no_risk_keys():
    rows = plan_rows(DEFAULT_TIERS, today_spot_price=12, retail_price=30)
    for r in rows:
        for key in r:
            for banned in ("probability", "loss_fraction", "expected", "range"):
                assert banned not in key


def test_fm5_no_failure_odds_value_in_plan_rows():
    # spec 9.3: the secret p and L must never reach the player panel.
    p, loss = 0.137, 0.271
    tiers = {
        CIP: InsuredTierTerms(enabled=True, fee=0.45),
        DAP: InsuredTierTerms(enabled=False, fee=0.55),
        DDP: InsuredTierTerms(enabled=True, fee=1.05),
    }
    # The calculator inputs are the only place p and L exist; plan_rows takes
    # neither, so no value in any row may equal them, whatever else is passed.
    rows = plan_rows(tiers, today_spot_price=12.5, retail_price=30)
    values = [v for v in _walk(rows) if isinstance(v, (int, float))]
    assert p not in values
    assert loss not in values
    assert round(p * loss, 6) not in values
    assert round(p * (1 - loss), 6) not in values


# --- calculator --------------------------------------------------------------


def test_break_even_not_shared():
    got = [break_even_fee(t, shared=False, **KW) for t in (CIP, DAP, DDP)]
    assert got == pytest.approx([0.36, 0.36, 0.9], abs=1e-9)


def test_break_even_shared():
    got = [break_even_fee(t, shared=True, **KW) for t in (CIP, DAP, DDP)]
    assert got == pytest.approx([0.06, 0.06, 0.15], abs=1e-9)


@pytest.mark.parametrize("shared", [False, True])
def test_break_even_exw_is_zero(shared):
    assert break_even_fee(EXW, shared=shared, **KW) == 0


def test_break_even_float_noise_guard():
    # u_ref 20, L 0.3 not shared: 20 * 0.3 = 6.000000000000001 or 5.99..; either way 6.
    kw = dict(KW, failure_probability=1.0, base_price=10, n_ref=1)
    assert break_even_fee(CIP, shared=False, **kw) == pytest.approx(3.0, abs=1e-9)
    # shared n_ref=3, u_ref 60: 60 * (0.3 / 3) must floor to 6, not 5.
    kw = dict(
        KW, failure_probability=1.0, base_price=10, n_ref=3, reference_shipment=60
    )
    assert break_even_fee(CIP, shared=True, **kw) == pytest.approx(1.0, abs=1e-9)


def test_break_even_zero_when_no_risk():
    assert break_even_fee(DDP, shared=False, **dict(KW, failure_probability=0)) == 0
    assert break_even_fee(DDP, shared=False, **dict(KW, loss_fraction=0)) == 0


def test_fee_from_margin():
    got = [fee_from_margin(b, 0.10) for b in (0.36, 0.36, 0.9)]
    assert got == pytest.approx([0.40, 0.40, 0.99], abs=1e-9)


def test_fee_from_margin_rounds_to_cents():
    assert fee_from_margin(0.333, 0.10) == pytest.approx(0.37, abs=1e-12)
    assert fee_from_margin(0.0, 0.5) == 0


def test_fee_ratio():
    assert fee_ratio(0.40, 0.36) == pytest.approx(0.40 / 0.36)
    assert fee_ratio(0.40, 0.0) is None


def test_ratio_badge_examples():
    assert ratio_badge(0.94, 1.0) == "worth_it"
    assert ratio_badge(1.00, 1.0) == "break_even"
    assert ratio_badge(1.05, 1.0) == "break_even"
    assert ratio_badge(1.06, 1.0) == "not_worth_it"
    assert ratio_badge(0.40, 0.0) == "no_risk"


def test_fm6_badge_boundaries():
    assert ratio_badge(0.95, 1.0) == "break_even"
    assert ratio_badge(1.05, 1.0) == "break_even"
    assert ratio_badge(0.9499, 1.0) == "worth_it"
    assert ratio_badge(1.0501, 1.0) == "not_worth_it"


def test_ratio_badge_no_risk_even_with_zero_fee():
    assert ratio_badge(0.0, 0.0) == "no_risk"


def test_default_fees_badges():
    # spec 6.4: defaults are break-even-ish unshared, "not worth it" when shared.
    fee = 0.40
    shared = break_even_fee(CIP, shared=True, **KW)
    assert ratio_badge(fee, shared) == "not_worth_it"
    alone = break_even_fee(CIP, shared=False, **KW)
    assert ratio_badge(fee, alone) == "not_worth_it"  # 0.40 / 0.36 = 1.11
