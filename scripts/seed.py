"""Seeds sample accounts across all six sales channels, a bedding product line
(18 SKUs, matching real-world scale), and starting fabric rolls. Run directly
(not via the API) for a clean load.

Prints each account's id + api_key at the end -- that's what you need to
exercise the role-based auth from curl/Swagger (X-API-Key header). It's only
ever visible in plaintext here (or in the /accounts and
/accounts/individual/lookup-or-create API responses at creation time), same
as any real API-key-issuing system.

Usage:
    python scripts/seed.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import ADMIN_API_KEY
from app.db import get_session, init_db
from app.models import Account, FabricRoll, Product, SalesChannel

PRODUCT_LINES = ["Cotton Sateen", "Bamboo Weave", "Linen Blend"]
SIZES = [("Single", 1.6), ("Queen", 2.1), ("King", 2.6), ("Super King", 3.0)]
COLORS = ["Ivory", "Slate", "Blush"]


def main() -> None:
    init_db()
    with get_session() as session:
        accounts = [
            # Franchisees -- independently owned stores, net-30 terms
            Account(name="Alpine Home Textiles", channel=SalesChannel.FRANCHISEE, location="Zurich", credit_limit=25000),
            Account(name="Lakeside Linens", channel=SalesChannel.FRANCHISEE, location="Geneva", credit_limit=18000),
            Account(name="Nordic Bedding Co", channel=SalesChannel.FRANCHISEE, location="Basel", credit_limit=20000),
            # Company-owned retail -- internal stock transfer, no invoice
            Account(name="Company Flagship Store", channel=SalesChannel.COMPANY_RETAIL, location="Zurich Oerlikon"),
            # Own e-commerce fulfillment warehouse -- prepaid, ships direct to consumer
            Account(name="EU Fulfillment Center", channel=SalesChannel.ONLINE_WAREHOUSE, location="Basel Logistics Park"),
            # Supermarket wholesale partner -- requires a PO number, net-60
            Account(name="Migros Distribution AG", channel=SalesChannel.SUPERMARKET_PARTNER, location="Zurich", credit_limit=50000),
            # International / export account -- requires a customs declaration
            Account(name="Nordic Home Textiles AS", channel=SalesChannel.INTERNATIONAL, location="Oslo, Norway", credit_limit=30000),
            # Individual/walk-in customers -- each gets their own account (own
            # AR, own order history), the same as every other channel. In
            # production these would be created on the fly by
            # services.get_or_create_individual_account() at checkout, once a
            # POS integration exists to call it; seeded directly here for demo
            # purposes.
            Account(
                name="Walk-in: M. Herzog", channel=SalesChannel.INDIVIDUAL, location="Zurich",
                contact_email="m.herzog@example.com",
            ),
            Account(
                name="Walk-in: A. Keller", channel=SalesChannel.INDIVIDUAL, location="Zurich",
                contact_email="a.keller@example.com",
            ),
        ]
        session.add_all(accounts)
        session.flush()

        sku_n = 0
        products = []
        for line in PRODUCT_LINES:
            for size_name, meters in SIZES:
                for color in COLORS[:2]:  # keep it to ~24 SKUs across 3 lines
                    sku_n += 1
                    if sku_n > 18:
                        break
                    products.append(
                        Product(
                            sku=f"BED-{sku_n:03d}",
                            name=f"{line} Duvet Set - {size_name} ({color})",
                            product_line=line,
                            unit_price=round(45 + meters * 20, 2),
                            fabric_meters_per_unit=meters,
                        )
                    )
        session.add_all(products)

        rolls = []
        for i, line in enumerate(PRODUCT_LINES):
            for j in range(3):
                rolls.append(
                    FabricRoll(
                        roll_code=f"ROLL-{line[:3].upper()}-{j+1:02d}",
                        fabric_type=line,
                        color=COLORS[j % len(COLORS)],
                        total_length_m=80.0,
                        remaining_length_m=80.0,
                        is_remnant=False,
                    )
                )
        session.add_all(rolls)

        session.commit()
        print(
            f"Seeded {len(accounts)} accounts across "
            f"{len({a.channel for a in accounts})} channels, "
            f"{len(products)} SKUs, {len(rolls)} fabric rolls."
        )
        print(f"\nAdmin API key (X-API-Key): {ADMIN_API_KEY}")
        print("\nAccount API keys -- each can only see its own orders/AR:")
        for a in accounts:
            print(f"  [{a.id}] {a.name} ({a.channel.value}): {a.api_key}")


if __name__ == "__main__":
    main()
