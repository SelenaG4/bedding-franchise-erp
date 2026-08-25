"""Synthetic daily demand history, product-level -- used to calibrate the
reorder-point Monte Carlo simulation (app/simulation.py) with an empirical
demand distribution instead of an assumed one.

No real sales history exists in this project (scripts/seed.py only creates
starting accounts/products/fabric rolls, not an order history), so this
generates one -- deliberately with the kind of realism a formula-only
approach would miss: weekday/weekend seasonality (most of this system's six
channels are wholesale/B2B, so demand is lighter on weekends) and occasional
bulk/promo order spikes (a franchisee or supermarket partner placing a much
larger-than-usual order). That makes the demand distribution right-skewed
rather than Normal -- see app/simulation.py's docstring for why that matters
for reorder-point sizing.

Output: data/demand_history.csv (date, product_id, sku, quantity_demanded),
one row per product per day, 180 days by default.

Run:
    python scripts/generate_demand_history.py
"""
from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session, init_db  # noqa: E402
from app.models import Product  # noqa: E402

NUM_DAYS = 180
SEED = 7
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "demand_history.csv"

SPIKE_PROBABILITY = 0.05
SPIKE_MULTIPLIER_RANGE = (2.5, 4.0)
WEEKEND_MULTIPLIER = 0.5


def _base_lambda_for(product: Product, rng: np.random.Generator) -> float:
    """Cheaper/smaller-size SKUs move in higher volume. Fixed per SKU via the
    seeded RNG, so re-running this script reproduces the same history."""
    scale = max(0.6, 4.0 - product.fabric_meters_per_unit)  # smaller sizes -> higher base rate
    return float(rng.uniform(0.8, 1.6) * scale)


def main() -> None:
    init_db()
    with get_session() as session:
        products = session.query(Product).order_by(Product.id).all()
    if not products:
        raise SystemExit("No products found -- run scripts/seed.py first.")

    rng = np.random.default_rng(SEED)
    start = date.today() - timedelta(days=NUM_DAYS)

    OUT_PATH.parent.mkdir(exist_ok=True)
    with OUT_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "product_id", "sku", "quantity_demanded"])

        for product in products:
            base_lambda = _base_lambda_for(product, rng)
            for d in range(NUM_DAYS):
                day = start + timedelta(days=d)
                lam = base_lambda
                if day.weekday() >= 5:  # Saturday/Sunday
                    lam *= WEEKEND_MULTIPLIER
                if rng.random() < SPIKE_PROBABILITY:
                    lam *= rng.uniform(*SPIKE_MULTIPLIER_RANGE)
                qty = int(rng.poisson(lam))
                writer.writerow([day.isoformat(), product.id, product.sku, qty])

    print(f"Wrote {NUM_DAYS} days x {len(products)} SKUs of synthetic demand history to {OUT_PATH}")


if __name__ == "__main__":
    main()
