"""Runs the Monte Carlo reorder-point simulation for every SKU with demand
history, saves a summary CSV, and renders a documentation chart for the
worst-case SKU -- the clearest illustration of where the textbook Normal
formula falls short of the simulated recommendation.

Run after scripts/generate_demand_history.py:
    python scripts/run_reorder_point_analysis.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session, init_db  # noqa: E402
from app.models import Product  # noqa: E402
from app.simulation import (  # noqa: E402
    load_demand_history,
    recommend_reorder_point,
    simulate_lead_time_demand,
)

ROOT = Path(__file__).resolve().parent.parent
LEAD_TIME_DAYS = 7
TARGET_SERVICE_LEVEL = 0.95
SUMMARY_PATH = ROOT / "data" / "reorder_point_recommendations.csv"
CHART_PATH = ROOT / "docs" / "reorder_point_simulation.png"


def main() -> None:
    init_db()
    with get_session() as session:
        products = session.query(Product).order_by(Product.id).all()

    results = []
    for product in products:
        try:
            rec = recommend_reorder_point(product.id, LEAD_TIME_DAYS, TARGET_SERVICE_LEVEL)
        except (FileNotFoundError, ValueError):
            continue
        results.append(rec)
        print(
            f"{rec.sku}: simulated R={rec.simulated_reorder_point} "
            f"(achieved {rec.simulated_achieved_service_level:.1%}) vs. "
            f"formula R={rec.formula_reorder_point:.1f} "
            f"(achieved {rec.formula_achieved_service_level:.1%}, "
            f"undercoverage {rec.formula_undercoverage_pct:.1f}pp)"
        )

    if not results:
        raise SystemExit("No results -- run scripts/generate_demand_history.py first.")

    SUMMARY_PATH.parent.mkdir(exist_ok=True)
    with SUMMARY_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "sku",
                "simulated_reorder_point",
                "simulated_achieved_service_level",
                "formula_reorder_point",
                "formula_achieved_service_level",
                "formula_undercoverage_pct",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    r.sku,
                    r.simulated_reorder_point,
                    f"{r.simulated_achieved_service_level:.4f}",
                    f"{r.formula_reorder_point:.2f}",
                    f"{r.formula_achieved_service_level:.4f}",
                    f"{r.formula_undercoverage_pct:.2f}",
                ]
            )
    print(f"\nWrote summary for {len(results)} SKUs to {SUMMARY_PATH}")

    avg_undercoverage = float(np.mean([r.formula_undercoverage_pct for r in results]))
    worst = max(results, key=lambda r: r.formula_undercoverage_pct)
    print(f"Average formula undercoverage across all SKUs: {avg_undercoverage:.2f} percentage points")
    print(f"Worst case (service-level gap): {worst.sku} ({worst.formula_undercoverage_pct:.2f}pp under target)")

    # Chart target: the SKU with the largest absolute gap between the two
    # recommended reorder points, so the two reference lines are visually
    # distinguishable (the worst service-level-gap SKU above is sometimes a
    # low-volume item where a ~0.5-unit difference in R already swings the
    # achieved service level a lot -- a real and correct finding, just a
    # cramped chart).
    chart_target = max(results, key=lambda r: r.simulated_reorder_point - r.formula_reorder_point)
    print(
        f"Chart SKU (largest R gap): {chart_target.sku} "
        f"(simulated {chart_target.simulated_reorder_point} vs. formula {chart_target.formula_reorder_point:.1f})"
    )

    history = load_demand_history(chart_target.product_id)
    rng = np.random.default_rng(42)
    lead_time_totals = simulate_lead_time_demand(history.daily_demand, LEAD_TIME_DAYS, 5000, rng)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(
        lead_time_totals,
        bins=30,
        color="#1F3864",
        alpha=0.75,
        label=f"Simulated {LEAD_TIME_DAYS}-day demand ({chart_target.sku})",
    )
    ax.axvline(
        chart_target.simulated_reorder_point,
        color="#2E7D32",
        linewidth=2,
        linestyle="-",
        label=(
            f"Simulated reorder point = {chart_target.simulated_reorder_point} "
            f"(achieves {chart_target.simulated_achieved_service_level:.1%})"
        ),
    )
    ax.axvline(
        chart_target.formula_reorder_point,
        color="#C62828",
        linewidth=2,
        linestyle="--",
        label=(
            f"Formula reorder point = {chart_target.formula_reorder_point:.1f} "
            f"(achieves {chart_target.formula_achieved_service_level:.1%})"
        ),
    )
    ax.set_xlabel(f"Total demand over a {LEAD_TIME_DAYS}-day lead time (units)")
    ax.set_ylabel("Simulated trials")
    ax.set_title(f"Reorder point: simulation vs. Normal-formula (target {TARGET_SERVICE_LEVEL:.0%} service level)")
    ax.legend(fontsize=8, loc="upper right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    CHART_PATH.parent.mkdir(exist_ok=True)
    fig.savefig(CHART_PATH, dpi=120)
    plt.close(fig)
    print(f"Saved chart to {CHART_PATH}")


if __name__ == "__main__":
    main()
