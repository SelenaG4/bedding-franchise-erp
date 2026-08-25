"""Reorder-point / safety-stock recommendation via Monte Carlo simulation,
bootstrapped from historical daily demand -- with a direct, measured
comparison against the textbook closed-form formula (mean + z * std,
assuming Normally-distributed demand) a first-pass inventory policy would
typically reach for instead.

Why simulate at all instead of just using the formula: the formula is only
correct if lead-time demand really is Normally distributed. Real demand
often isn't -- this project's own synthetic demand history
(scripts/generate_demand_history.py) is deliberately generated with
weekday/weekend seasonality and occasional bulk/promo-order spikes (a
franchisee or supermarket partner placing a much larger-than-usual order),
which is realistic for this system's wholesale/B2B channel mix and produces
a right-skewed distribution the Normal formula underestimates the tail of.

The simulated recommendation is an empirical bootstrap quantile: resample
observed daily-demand values (with replacement), sum them over the lead
time, repeat thousands of times, and take the reorder point as the quantile
of that simulated distribution matching the target service level -- correct
by construction for whatever shape the real demand data has, no
distributional assumption required. The formula's reorder point is then
re-scored against the exact same simulated trials, so
"formula_undercoverage_pct" below is a measured finding on this data, not an
assumption -- see scripts/run_reorder_point_analysis.py for the numbers.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "demand_history.csv"

# Standard-normal inverse-CDF values for common service levels, so the
# formula-based comparison doesn't need scipy.stats just for a z-lookup.
_Z_TABLE = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263, 0.995: 2.5758}


def _z_for(target_service_level: float) -> float:
    closest = min(_Z_TABLE, key=lambda k: abs(k - target_service_level))
    return _Z_TABLE[closest]


@dataclass
class DemandHistory:
    product_id: int
    sku: str
    daily_demand: np.ndarray  # one observation per historical day
    num_days: int


def load_demand_history(product_id: int) -> DemandHistory:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"{DATA_PATH} not found -- run scripts/generate_demand_history.py first"
        )
    quantities: list[int] = []
    sku = None
    with DATA_PATH.open() as f:
        for row in csv.DictReader(f):
            if int(row["product_id"]) == product_id:
                quantities.append(int(row["quantity_demanded"]))
                sku = row["sku"]
    if not quantities:
        raise ValueError(f"No demand history found for product_id {product_id}")
    return DemandHistory(
        product_id=product_id, sku=sku, daily_demand=np.array(quantities), num_days=len(quantities)
    )


def simulate_lead_time_demand(
    daily_demand: np.ndarray, lead_time_days: int, num_trials: int, rng: np.random.Generator
) -> np.ndarray:
    """Bootstrap: resample `lead_time_days` historical daily-demand
    observations (with replacement) and sum them, repeated num_trials times.
    Makes no distributional assumption -- if the history is spiky, the
    bootstrap sums are spiky too."""
    draws = rng.choice(daily_demand, size=(num_trials, lead_time_days), replace=True)
    return draws.sum(axis=1)


@dataclass
class ReorderPointRecommendation:
    product_id: int
    sku: str
    lead_time_days: int
    target_service_level: float
    num_historical_days: int
    num_trials: int
    mean_lead_time_demand: float
    std_lead_time_demand: float
    simulated_reorder_point: int
    simulated_safety_stock: float
    simulated_achieved_service_level: float
    formula_reorder_point: float
    formula_achieved_service_level: float
    formula_undercoverage_pct: float


def recommend_reorder_point(
    product_id: int,
    lead_time_days: int,
    target_service_level: float = 0.95,
    num_trials: int = 5000,
    seed: int = 42,
) -> ReorderPointRecommendation:
    if not (0.0 < target_service_level < 1.0):
        raise ValueError("target_service_level must be between 0 and 1")

    history = load_demand_history(product_id)
    rng = np.random.default_rng(seed)
    lead_time_totals = simulate_lead_time_demand(history.daily_demand, lead_time_days, num_trials, rng)

    mean_ltd = float(lead_time_totals.mean())
    std_ltd = float(lead_time_totals.std(ddof=1))

    # Empirical quantile of the simulated distribution: correct by
    # construction for the target service level, whatever the shape.
    simulated_r = int(np.ceil(np.quantile(lead_time_totals, target_service_level, method="higher")))
    simulated_achieved = float((lead_time_totals <= simulated_r).mean())

    formula_r = max(0.0, mean_ltd + _z_for(target_service_level) * std_ltd)
    formula_achieved = float((lead_time_totals <= formula_r).mean())

    return ReorderPointRecommendation(
        product_id=product_id,
        sku=history.sku,
        lead_time_days=lead_time_days,
        target_service_level=target_service_level,
        num_historical_days=history.num_days,
        num_trials=num_trials,
        mean_lead_time_demand=mean_ltd,
        std_lead_time_demand=std_ltd,
        simulated_reorder_point=simulated_r,
        simulated_safety_stock=simulated_r - mean_ltd,
        simulated_achieved_service_level=simulated_achieved,
        formula_reorder_point=formula_r,
        formula_achieved_service_level=formula_achieved,
        formula_undercoverage_pct=max(0.0, (target_service_level - formula_achieved) * 100.0),
    )
