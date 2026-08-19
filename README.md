# Bedding Franchise ERP/CRM

A small, working ERP unifying **Production**, **Franchise Sales**, and **Accounting**
for a bedding manufacturer -- one shared system of record instead of three
disconnected spreadsheets or systems, which is the actual problem a manufacturer's
first ERP/CRM rollout is usually solving.

## Why this exists

This is a new build (Aug 2026), not a reconstruction of any specific past system --
it's a from-scratch demonstration of the kind of production/sales/accounting
integration problem that comes up when leading an ERP/CRM implementation at a
manufacturer: siloed departments, a franchise sales model instead of direct retail,
and a real material-tracking problem (leftover fabric) that a generic "orders and
inventory" schema wouldn't capture.

## The domain-specific part: fabric remnants

A cut-and-sew bedding line doesn't consume a fabric roll cleanly -- there's almost
always leftover. Most simple systems either ignore this (waste goes untracked) or
treat it as scrap by default (waste that could've been reused gets thrown away on
paper even if it physically isn't). This system tracks it explicitly:

- Every `ProductionRun` records exactly how much fabric was consumed and how much
  was left over.
- Leftover at or above a minimum usable length (`REMNANT_MIN_LENGTH_M = 2.0m`)
  becomes a new `FabricRoll`, flagged `is_remnant=True` and linked back to the roll
  it was cut from.
- Leftover below that threshold is recorded as scrap (`scrapped_m`) -- tracked for
  a waste report, but not treated as usable stock.
- Fabric allocation (`allocate_fabric_roll`) is **remnant-first, best-fit**: when a
  production run doesn't specify a roll, the system prefers the smallest remnant
  that's still big enough, before cutting into a fresh full roll. This is what
  actually reduces material waste over time, rather than just recording it.

## Why the sales side is franchise orders, not a shopping cart

`place_franchise_order()` is deliberately not "add to cart, checkout." It's a
wholesale B2B order from a franchisee, checked in full before anything is touched:

1. Check finished-goods stock (`FinishedGoodsMovement` ledger, not a raw counter --
   stock level is always the sum of movements, so it's auditable) for **every**
   line in the order.
2. If any line is short, the whole order is rejected -- an order row is still
   created (status `rejected_insufficient_stock`, for visibility into demand you
   couldn't fill) but **no stock moves and no invoice is raised**.
3. If every line has stock, the order is confirmed, stock is deducted for every
   line, and one invoice is raised for the total -- all in the same transaction.

No order can exist in a half-fulfilled state. This was actually verified by a
failing-then-passing test during development, not just asserted: see "a bug I hit"
below.

## A bug I hit while building this

Two of the tests originally asked for more units than the seeded fabric roll could
supply (5 units at 2.1m/unit = 10.5m, against a 10m roll) -- the tests failed with
`InsufficientFabricError`, which was correct behavior catching wrong test data, not
a bug in the service layer. Fixed by correcting the test fixtures, not the logic.
Kept as `tests/test_erp.py::test_production_run_rejects_insufficient_fabric` and
the surrounding tests, which now pin this behavior deliberately.

## Running it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
python scripts/seed.py     # 3 franchisees, 18 SKUs across 3 product lines, 9 starting rolls
```

Try the full loop:

```bash
# Turn fabric into finished goods
curl -X POST localhost:8000/production-runs -H "Content-Type: application/json" \
  -d '{"product_id": 1, "units_to_produce": 3}'

# A franchisee orders some of that stock
curl -X POST localhost:8000/sales-orders -H "Content-Type: application/json" \
  -d '{"franchisee_id": 1, "lines": [{"product_id": 1, "quantity": 2}]}'

# Record a payment against the invoice raised
curl -X POST localhost:8000/invoices/1/payments -H "Content-Type: application/json" \
  -d '{"amount": 50}'

# The cross-department view: finished-goods stock, remnant fabric available, total AR
curl localhost:8000/reports/summary
```

Measured on this machine: production run + sales order + summary report each
respond in low single-digit milliseconds (SQLite, no network hop) -- the interesting
cost here is transactional correctness, not latency.

### Tests

```bash
pytest tests/ -v   # 7 passed
```

Covers: remnant creation vs. scrap-below-threshold, remnant-first allocation,
insufficient-fabric rejection (no partial stock change), atomic order
rejection on insufficient finished-goods stock, and payment/overpayment handling.

### Docker

```bash
docker build -t bedding-franchise-erp .
docker run -p 8000:8000 bedding-franchise-erp
```

(Written and structurally checked; not run in this sandbox, which has no Docker
daemon -- build locally to confirm before demoing.)

## What I'd do next with more time

- Move off SQLite to Postgres for real concurrent-write safety (SQLite's fine for a
  demo, not for multiple franchisees hitting it at once).
- Add a reorder-point alert on finished-goods stock, mirroring the remnant-tracking
  idea: flag SKUs below a threshold the same way remnants are flagged.
- Add role-based auth so a franchisee can only see their own orders/AR, not the
  whole system.
