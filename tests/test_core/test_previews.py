"""The host config screen's figures (spec 6.4, 13.9, 13.10): ``config_preview``.

Each figure is checked against the spec's worked examples, and a hidden default the
host left blank never reaches a figure (spec 9.3).
"""

import copy
import json
from typing import Any

import pytest

from app.core import defaults as defaults_mod
from app.core.config_models import host_default_config, validate_config
from app.core.defaults import HIDDEN_DEFAULT_KEYS
from app.core.lots import guide_rows, hazard
from app.core.previews import config_preview


def preview_of(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    result = validate_config(patch or {}, None)
    assert result.host_config is not None, result.errors
    return config_preview(result.host_config)


RISKS_TYPED = {"transport": {"failure_probability": 0.14, "loss_fraction": 0.30}}


def tier(preview: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in preview["calculator"]["tiers"] if t["tier"] == name)


# ---------------------------------------------------------- demand and calendar


def test_weekday_strip_and_market_demand_at_the_defaults() -> None:
    w = preview_of()["weekdays"]
    assert [d["multiplier"] for d in w["days"]] == [0.25, 0.5, 0.75, 1, 1, 1, 0.75]
    assert [d["in_peak"] for d in w["days"]] == [False] * 3 + [True] * 3 + [False]
    assert [d["market_demand"] for d in w["days"]] == [25, 50, 75, 100, 100, 100, 75]
    assert w["reach"] == 4
    assert w["average_multiplier"] == 0.75
    assert w["peak_demand_by_seats"] == [
        {"seats": 2, "units": 50.0},
        {"seats": 4, "units": 100.0},
    ]
    seven = preview_of({"max_players": 7})["weekdays"]["peak_demand_by_seats"]
    assert [r["seats"] for r in seven] == [2, 4, 7]


def test_game_examples() -> None:
    g = preview_of()["game"]
    assert g["weekly_cycles"] == 2.0 and g["last_weekday"] == "sun"
    assert g["own_demand_per_day"] == 18.75  # 25 x 0.75
    assert g["cost_per_day"] == 225.0  # x $12
    assert g["days_covered"] == pytest.approx(5000 / 225)


def test_calendar_examples_for_both_lead_times() -> None:
    s = preview_of()["supplier"]
    assert s["lead_time"] == {
        "arrival_day": 1,
        "arrival_weekday": "mon",
        "last_spot_day": 14,
    }
    assert s["initial_pipeline"] == {"used": False, "arrivals": []}
    assert s["futures"] == {"delivery_day": 8, "last_day_to_buy": 7}
    s = preview_of({"supplier": {"lead_time_days": 2, "initial_pipeline_qty": 9}})[
        "supplier"
    ]
    assert s["lead_time"] == {
        "arrival_day": 3,
        "arrival_weekday": "wed",
        "last_spot_day": 12,
    }
    assert s["initial_pipeline"] == {
        "used": True,
        "arrivals": [{"day": 1, "units": 9}, {"day": 2, "units": 9}],
    }


def test_prices_and_margins() -> None:
    p = preview_of()["prices"]
    assert (p["spot_low"], p["spot_high"]) == (9.0, 15.0)
    assert (p["margin_at_low"], p["margin_at_high"]) == (21.0, 15.0)
    assert p["backlog_cost"] == [
        {"days": 1, "cost": 1.5},
        {"days": 3, "cost": 4.5},
        {"days": 7, "cost": 10.5},
    ]


def test_reputation_examples() -> None:
    r = preview_of()["reputation"]
    assert [s["share"] for s in r["starting_shares"]] == [0.25] * 4
    assert r["clean_days"][0] == 0.5
    assert r["clean_days"][1] == pytest.approx(0.65)  # 0.7 x 0.5 + 0.3 x 1
    assert len(r["clean_days"]) == 8
    scores = {
        (row["fill"], s["waste"]): s["score"]
        for row in r["day_scores"]
        for s in row["scores"]
    }
    assert scores[(1.0, 0.0)] == 1.0
    assert scores[(0.8, 0.2)] == pytest.approx(0.7 * 0.8 + 0.3 * 0.8)
    # spec 7.3: weights 0.8 / 1.0 / 1.3 of 3.1; 75 units on a peak day for 3 seats
    assert [s["units"] for s in r["split"]] == [19, 24, 31]


# -------------------------------------------------------------- the supplier


def test_capacity_figures() -> None:
    s = preview_of({"supplier": {"capacity_sd_per_player": 5.0}})["supplier"]
    assert s["expected_capacity"] == 88.0
    assert s["disrupted_capacity"] == pytest.approx(35.2)
    # 88 +- 1.96 x 5 x 4, rounded half up
    assert s["capacity_band"] == {"low": 49, "high": 127}


def test_disruption_chance_when_typed() -> None:
    s = preview_of({"supplier": {"disruption_probability": 0.05}})["supplier"]
    assert s["disruptions"]["chance_any"] == pytest.approx(1 - 0.95**14)
    assert s["disruptions"]["expected"] == pytest.approx(0.7)


# --------------------------------------------------------------------- stock


def test_lot_tables_follow_the_hazard_and_stop_once_a_lot_is_gone() -> None:
    st = preview_of()["stock"]
    assert st["quality_step_per_night"] == pytest.approx(100 / 3)
    low, mid, high = st["quality_range"]
    assert (
        low["initial_quality"],
        mid["initial_quality"],
        high["initial_quality"],
    ) == (
        80,
        90,
        100,
    )
    assert [n["risk"] for n in high["nights"]] == [
        hazard(n, 0, 3, 100) for n in (1, 2, 3)
    ]
    plain, conserved = st["plain_vs_conserved"]
    assert [n["quality"] for n in plain["nights"]] == [67, 33, 0, None, None]
    assert [n["quality"] for n in conserved["nights"]] == [100, 100, 67, 33, 0]
    assert conserved["conserved"] is True
    assert len(st["spoil_risk"]) == 4
    assert st["quality_guide"] == guide_rows(1, 0, 3)


def test_cost_examples() -> None:
    st = preview_of()["stock"]
    assert st["holding"] == {"units": 50, "cost": 10.0}
    assert st["salvage"] == {"units": 10, "revenue": 20.0}
    c = st["conservation"]
    assert c["units"] == 30 and c["cost"] == 30.0
    assert c["risk_after_fresh"] == hazard(1, 0, 3, 100)
    assert c["units_saved"] == pytest.approx(10.0)
    t = preview_of()["transport"]
    assert t["shipping"] == {"units": 40, "cost": 40.0}


# ------------------------------------------------- transport and insurance


def test_loss_tables_match_spec_6_2() -> None:
    t = preview_of(RISKS_TYPED)["transport"]
    assert t["loss_table"]["rows"] == [
        {"alias": "P1", "shipped": 60, "alone": 18, "shared": 6},
        {"alias": "P2", "shipped": 30, "alone": 9, "shared": 3},
        {"alias": "P3", "shipped": 10, "alone": 3, "shared": 1},
    ]
    assert (t["loss_table"]["total_alone"], t["loss_table"]["total_shared"]) == (30, 10)
    assert t["loss_example"] == {"units": 20, "alone": 6, "shared": 1}
    assert t["reference_losses"] == {"units": 20, "alone": 6, "shared": 1}
    assert t["failures"]["expected"] == pytest.approx(0.14 * 14)


def test_calculator_matches_spec_6_4_defaults() -> None:
    c = preview_of(RISKS_TYPED)["calculator"]
    assert c["known"] is True and c["target_ratio"] == pytest.approx(1.1)
    breakeven = {t["tier"]: t["solo"]["break_even"] for t in c["tiers"]}
    assert breakeven == {"cip": 0.5, "dap": 0.5, "ddp": 1.26}
    margin = {t["tier"]: t["solo"]["fee_from_margin"] for t in c["tiers"]}
    assert margin == {"cip": 0.55, "dap": 0.55, "ddp": 1.39}
    shared = {t["tier"]: t["shared"]["break_even"] for t in c["tiers"]}
    assert shared == {"cip": 0.08, "dap": 0.08, "ddp": 0.21}
    for t in c["tiers"]:
        assert t["shared"]["badge"] == "not_worth_it"  # the trade-off sharing teaches
        assert t["in_sync"] is True  # the default fees come from the margin
    assert tier(preview_of(RISKS_TYPED), "ddp")["refund_value"] == 30.0
    assert tier(preview_of(RISKS_TYPED), "cip")["solo"]["worst_single_failure"] == 72.0


def test_a_fee_off_the_margin_is_out_of_sync() -> None:
    patch = copy.deepcopy(RISKS_TYPED)
    patch["insurance"] = {"tiers": {"cip": {"fee": 0.70}}}
    t = tier(preview_of(patch), "cip")
    assert t["in_sync"] is False
    assert t["current"]["ratio"] == pytest.approx(0.70 / (0.14 * 6 / 20 * 12))
    assert t["current"]["badge"] == "not_worth_it"


def test_badges_follow_spec_6_4_thresholds() -> None:
    for fee, badge in ((0.47, "worth_it"), (0.5, "break_even"), (0.53, "not_worth_it")):
        patch = copy.deepcopy(RISKS_TYPED)
        patch["insurance"] = {"tiers": {"cip": {"fee": fee}}}
        assert tier(preview_of(patch), "cip")["solo"]["badge"] == badge
    no_risk = {"transport": {"failure_probability": 0.0, "loss_fraction": 0.3}}
    assert tier(preview_of(no_risk), "cip")["solo"]["badge"] == "no_risk"


# --------------------------------------------------------- worked examples


def test_allocation_example_is_spec_5_4() -> None:
    a = preview_of()["examples"]["allocation"]
    assert a["largest_first"] == {"P1": 60, "P2": 40, "P3": 0}
    assert a["cooperative"] == {"P1": 43, "P2": 36, "P3": 21}


def test_peer_trades_example_is_spec_5_7() -> None:
    trades = preview_of()["examples"]["peer_trades"]
    assert [
        (t["buyer"], t["seller"], t["units"], t["price"], t["total"]) for t in trades
    ] == [
        ("P1", "P3", 10, 15.0, 150.0),
        ("P1", "P4", 6, 13.0, 78.0),
        ("P2", "P4", 2, 13.0, 26.0),
    ]


# ------------------------------------------------------------ hidden defaults


def test_hidden_defaults_leave_their_figures_null() -> None:
    p = preview_of()
    assert p["hidden"] == sorted(HIDDEN_DEFAULT_KEYS)
    assert p["supplier"]["capacity_band"] is None
    assert p["supplier"]["disruptions"] is None
    t = p["transport"]
    assert t["failures"] is None and t["loss_example"] is None
    assert t["loss_table"] is None and t["reference_losses"] is None
    c = p["calculator"]
    assert c["known"] is False
    for row in c["tiers"]:
        assert row["solo"] is None and row["shared"] is None
        assert row["current"] is None and row["in_sync"] is None


def test_a_preview_never_reads_a_hidden_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Failure mode: filling a blank risk with its default before computing a figure.
    # Change every hidden default: a preview of a config that leaves them blank
    # must not change at all.
    before = json.dumps(preview_of(), sort_keys=True)
    changed = copy.deepcopy(defaults_mod.DEFAULT_CONFIG)
    changed["supplier"]["capacity_sd_per_player"] = 7.77
    changed["supplier"]["disruption_probability"] = 0.333
    changed["transport"]["failure_probability"] = 0.444
    changed["transport"]["loss_fraction"] = 0.555
    monkeypatch.setitem(defaults_mod.DEFAULT_CONFIG, "supplier", changed["supplier"])
    monkeypatch.setitem(defaults_mod.DEFAULT_CONFIG, "transport", changed["transport"])
    host = host_default_config()
    assert json.dumps(config_preview(host), sort_keys=True) == before


def test_typed_risks_are_used() -> None:
    p = preview_of(RISKS_TYPED)
    assert p["hidden"] == [
        "supplier.capacity_sd_per_player",
        "supplier.disruption_probability",
    ]
    assert p["transport"]["failures"] is not None
    assert p["calculator"]["known"] is True
