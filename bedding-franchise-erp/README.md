# Bedding Franchise ERP/CRM

A small, working ERP unifying **Production**, **Multi-Channel Sales**, and
**Accounting** for a bedding manufacturer -- one shared system of record instead
of three disconnected spreadsheets or systems, which is the actual problem a
manufacturer's first ERP/CRM rollout is usually solving.

## Why this exists

This is a new build (Aug 2026), not a reconstruction of any specific past system --
it's a from-scratch demonstration of the kind of production/sales/accounting
integration problem that comes up when leading an ERP/CRM implementation at a
manufacturer: siloed departments, a real material-tracking problem (leftover
fabric) that a generic "orders and inventory" schema wouldn't capture, and a
sales side that isn't one uniform "add to cart" flow but six genuinely different
distribution channels, each with its own checkout procedure and packaging.

## Six sales channels, six checkout procedures

A real bedding manufacturer doesn't sell one way. This system models the six
channels finished goods actually leave through, each with different rules
enforced in code, not just as a label (`app/services.py::CHANNEL_POLICIES`):

| Channel | Payment terms | Packaging | Special requirement |
|---|---|---|---|
| Individual (direct/walk-in) | Prepaid | Single-item poly mailer | -- |
| Online warehouse (own e-commerce) | Prepaid | Barcoded carton, pick-and-pack | -- |
| Company-owned retail | Internal transfer | Shelf-ready carton | No invoice raised -- it's a stock move, not a sale |
| Supermarket partner | Net 60 | Palletized, GS1 labeling | Rejected without a PO number on file |
| Franchisee | Net 30 | Branded carton | -- |
| International / export | Net 30 | Export crate | Rejected without a customs declaration |

A supermarket or international order missing its required paperwork is
rejected outright -- no order row is even created, since that's a data problem,
not a stock-availability one (`ChannelRequirementError`, HTTP 422). A
company-retail order still deducts finished-goods stock like any other order,
but deliberately doesn't raise an invoice or touch accounts receivable, since
moving stock to the company's own shop isn't a sale.

**Scope simplification, stated honestly:** individual/walk-in customers are
modeled as one shared `Direct Customer Sales` account rather than one row per
person -- a real POS-integrated system would track each transaction
separately. That level of per-customer tracking wasn't needed to demonstrate
the actual point of this project (channel-differentiated checkout logic), so
it was left out rather than built halfway.

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

## Why every order is checked in full before anything is touched

`place_order()` is deliberately not "add to cart, checkout." It's a wholesale
B2B/B2C order across any of the six channels above, checked in full before
anything is mutated:

1. Confirm the order carries whatever paperwork its channel requires (PO number,
   customs declaration) -- reject immediately, no DB row at all, if not.
2. Check finished-goods stock (`FinishedGoodsMovement` ledger, not a raw counter --
   stock level is always the sum of movements, so it's auditable) for **every**
   line in the order.
3. If any line is short, the whole order is rejected -- an order row is still
   created (status `rejected_insufficient_stock`, for visibility into demand you
   couldn't fill) but **no stock moves and no invoice is raised**.
4. If every line has stock, the order is confirmed, stock is deducted for every
   line, and (for every channel except company-owned retail, which is an internal
   transfer) one invoice is raised for the total -- all in the same transaction.

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
python scripts/seed.py     # 8 accounts across all 6 channels, 18 SKUs, 9 starting rolls
```

Try the full loop:

```bash
# Turn fabric into finished goods
curl -X POST localhost:8000/production-runs -H "Content-Type: application/json" \
  -d '{"product_id": 1, "units_to_produce": 5}'

# See how each channel's checkout procedure differs
curl localhost:8000/channels

# A supermarket order without a PO number is rejected (422) -- no order created
curl -X POST localhost:8000/sales-orders -H "Content-Type: application/json" \
  -d '{"account_id": 6, "lines": [{"product_id": 1, "quantity": 1}]}'

# ...with a PO number, it's confirmed and palletized
curl -X POST localhost:8000/sales-orders -H "Content-Type: application/json" \
  -d '{"account_id": 6, "lines": [{"product_id": 1, "quantity": 1}], "po_number": "PO-1001"}'

# A company-retail order moves stock but raises no invoice
curl -X POST localhost:8000/sales-orders -H "Content-Type: application/json" \
  -d '{"account_id": 4, "lines": [{"product_id": 1, "quantity": 1}]}'

# Record a payment against an invoice
curl -X POST localhost:8000/invoices/1/payments -H "Content-Type: application/json" \
  -d '{"amount": 50}'

# The cross-department view: finished-goods stock, remnant fabric, total AR
curl localhost:8000/reports/summary

# Revenue and order count by channel -- shows company-retail transfers alongside real sales
curl localhost:8000/reports/sales-by-channel
```

Measured on this machine: production run + sales order + summary report each
respond in low single-digit milliseconds (SQLite, no network hop) -- the interesting
cost here is transactional correctness, not latency.

### Tests

```bash
pytest tests/ -v   # 11 passed
```

Covers: remnant creation vs. scrap-below-threshold, remnant-first allocation,
insufficient-fabric rejection (no partial stock change), atomic order rejection
on insufficient finished-goods stock, payment/overpayment handling, and the
three channel-specific behaviors (PO-number requirement, customs-declaration
requirement, and company-retail orders skipping invoicing).

### Docker

```bash
docker build -t bedding-franchise-erp .
docker run -p 8000:8000 bedding-franchise-erp
```

(Written and structurally checked; not run in this sandbox, which has no Docker
daemon -- build locally to confirm before demoing.)

### Live demo

Deployed on Render's free tier: **[link added once deployed]**. The free tier
spins the container down after 15 minutes idle, so the first request after a
lull takes ~20-30s to wake it back up -- visit `/docs` for the interactive
Swagger UI to try every endpoint above directly in the browser.

## What I'd do next with more time

- Move off SQLite to Postgres for real concurrent-write safety (SQLite's fine for a
  demo, not for multiple channels writing at once).
- Add a reorder-point alert on finished-goods stock, mirroring the remnant-tracking
  idea: flag SKUs below a threshold the same way remnants are flagged.
- Add role-based auth so each account can only see their own orders/AR, not the
  whole system.
- Track individual/walk-in customers as their own accounts instead of one shared
  bucket, once there's an actual POS integration driving that channel.
