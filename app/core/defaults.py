"""Every game default in one place (spec §9.1, §9.3)."""

from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "max_players": 4,
    "duration_days": 14,
    "starting_cash": 5000.0,
    "random_seed": None,
    "demand": {
        "per_player_peak": 25.0,
        "weekday_peak": {"from": "thu", "to": "sat"},
        "retail_price": 30.0,
        "backlog_penalty": 1.5,
    },
    "reputation": {
        "starting": 0.5,
        "smoothing": 0.3,
        "waste_weight": 0.3,
    },
    "supplier": {
        "base_price": 12.0,
        "spot_spread": 0.25,
        "capacity_mean_per_player": 22.0,
        "capacity_sd_per_player": 5.0,
        "disruption_probability": 0.05,
        "disruption_factor": 0.4,
        "lead_time_days": 1,
        "initial_pipeline_qty": 0,
        "quality_min": 80,
        "quality_max": 100,
    },
    "futures": {"horizon_days": 7},
    "stock": {
        "fresh_days": 0,
        "max_days": 3,
        "holding_cost": 0.2,
        "initial_stock": 30,
        "salvage_value": 2.0,
        "conservation_extra_fresh_days": 2,
        "conservation_cost_per_unit": 1.0,
    },
    "peer_market": {"enabled": True},
    "transport": {
        "unit_cost": 1.0,
        "shared": False,
        "failure_probability": 0.14,
        "loss_fraction": 0.30,
    },
    "insurance": {
        "tiers": {
            "cip": {"enabled": True, "fee": 0.55},
            "dap": {"enabled": True, "fee": 0.55},
            "ddp": {"enabled": True, "fee": 1.39},
        },
        "target_margin": 0.10,
        "reference_shipment": 20,
    },
}

PLAYER_CONFIG_KEYS: tuple[str, ...] = (
    "demand.retail_price",
    "stock.conservation_extra_fresh_days",
    "stock.conservation_cost_per_unit",
    "peer_market.enabled",
    "transport.unit_cost",
)
