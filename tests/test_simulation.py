"""Tests for app/simulation.py -- the Monte Carlo reorder-point simulator.

Uses a small hand-written demand-history CSV (via monkeypatching
simulation.DATA_PATH) rather than depending on the real generated
data/demand_history.csv, so these tests are hermetic and don't require
scripts/generate_demand_history.py to have been run first.
"""
from __future__ import annotations

import csv

import numpy as np
import pytest

from app import simulation


def _write_demand_csv(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "product_id", "sku", "quantity_demanded"])
        writer.writerows(rows)


@pytest.fixture()
def steady_demand_csv(tmp_path, monkeypatch):
    """Product 1: low-variance demand around 3 units/day, 60 days."""
    path = tmp_path / "demand_history.csv"
    rng = np.random.default_rng(1)
    rows = [
        (f"2026-01-{d + 1:02d}" if d < 30 else f"2026-02-{d - 29:02d}", 1, "BED-001", int(rng.poisson(3)))
        for d in range(60)
    ]
    _write_demand_csv(path, rows)
    monkeypatch.setattr(simulation, "DATA_PATH", path)
    return path


@pytest.fixture()
def spiky_demand_csv(tmp_path, monkeypatch):
    """Product 2: mostly light demand (1-3/day) with occasional large spikes
    (20-30 units) -- deliberately right-skewed, unlike a Normal distribution,
    to demonstrate the formula-vs-simulation gap the module exists to show."""
    path = tmp_path / "demand_history.csv"
    rng = np.random.default_rng(2)
    rows = []
    for d in range(120):
        qty = int(rng.integers(20, 31)) if rng.random() < 0.06 else int(rng.integers(0, 4))
        rows.append((f"day-{d}", 2, "BED-002", qty))
    _write_demand_csv(path, rows)
    monkeypatch.setattr(simulation, "DATA_PATH", path)
    return path


def test_load_demand_history_filters_by_product_id(steady_demand_csv):
    history = simulation.load_demand_history(1)
    assert history.sku == "BED-001"
    assert history.num_days == 60
    assert len(history.daily_demand) == 60


def test_load_demand_history_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(simulation, "DATA_PATH", tmp_path / "does_not_exist.csv")
    with pytest.raises(FileNotFoundError):
        simulation.load_demand_history(1)


def test_load_demand_history_unknown_product_raises(steady_demand_csv):
    with pytest.raises(ValueError):
        simulation.load_demand_history(999)


def test_recommend_reorder_point_reproducible_with_fixed_seed(steady_demand_csv):
    first = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.95, seed=42)
    second = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.95, seed=42)
    assert first.simulated_reorder_point == second.simulated_reorder_point
    assert first.simulated_achieved_service_level == pytest.approx(second.simulated_achieved_service_level)


def test_simulated_reorder_point_achieves_at_least_the_target(steady_demand_csv):
    rec = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.95, num_trials=5000)
    # Empirical quantile with method="higher" guarantees achieved >= target
    # (up to sampling noise from the 5000-trial simulation) -- this is the
    # core correctness property, not just "the function returns a number."
    assert rec.simulated_achieved_service_level >= 0.95 - 0.01


def test_higher_target_service_level_yields_a_higher_or_equal_reorder_point(steady_demand_csv):
    lower = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.90)
    higher = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.99)
    assert higher.simulated_reorder_point >= lower.simulated_reorder_point


def test_safety_stock_equals_reorder_point_minus_mean_demand(steady_demand_csv):
    rec = simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=0.95)
    assert rec.simulated_safety_stock == pytest.approx(rec.simulated_reorder_point - rec.mean_lead_time_demand)


def test_formula_undercoverages_on_right_skewed_spiky_demand(spiky_demand_csv):
    """The core claim of the module: on demand with occasional large spikes
    (right-skewed, not Normal), the Normal-formula reorder point achieves a
    lower service level than the target -- i.e. it under-covers -- while the
    simulated recommendation, by construction, meets it."""
    rec = simulation.recommend_reorder_point(2, lead_time_days=7, target_service_level=0.95, num_trials=5000)
    assert rec.simulated_achieved_service_level >= 0.95 - 0.01
    assert rec.formula_achieved_service_level < rec.simulated_achieved_service_level
    assert rec.formula_undercoverage_pct > 0.0


def test_invalid_target_service_level_rejected(steady_demand_csv):
    with pytest.raises(ValueError):
        simulation.recommend_reorder_point(1, lead_time_days=7, target_service_level=1.5)
