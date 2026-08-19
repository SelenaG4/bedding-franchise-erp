# Bedding Franchise ERP/CRM

A small, working ERP unifying **Production**, **Multi-Channel Sales**, and
**Accounting** for a bedding manufacturer -- one shared system of record instead
of three disconnected spreadsheets or systems, which is the actual problem a
manufacturer's first ERP/CRM rollout is usually solving.

## Why this exists

A reconstruction of bedding franchising sales, production and inventory system. --
it's a h demonstration of the kind of production/sales/accounting
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

Individual/walk-in customers each get their own `Account` row -- same as every
other channel, own AR, own order history, no shared bucket. What's still
missing is a real point-of-sale integration to create/look up that account
automatically at checkout; until that exists,
`services.get_or_create_individual_account()` (also exposed as
`POST /accounts/individual/lookup-or-create`) is the manual equivalent, keyed
on `contact_email` so a repeat customer doesn't get a duplicate account every
visit.

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

## Role-based auth

Every request except `GET /health` and `GET /channels` requires an
`X-API-Key` header. There are two roles, kept deliberately simple for what
this system actually is (a small internal ERP, not a public multi-tenant
product):

- **`account`** -- each `Account` gets its own generated API key (returned
  once, in plaintext, when the account is created). An account principal can
  place its own orders and see its own `/accounts/{id}/orders` and
  `/accounts/{id}/outstanding-ar` -- and gets a 403 on anyone else's, or on
  the system-wide `/reports/*` endpoints.
- **`admin`** -- one shared secret (`ADMIN_API_KEY` env var). Internal ERP
  staff: manages master data (accounts/products/fabric rolls), records
  production runs and payments, and can see everything, including on behalf
  of any account.

See `app/auth.py` for the full logic and `tests/test_auth.py` for the
behavior this is meant to guarantee (missing/invalid key, an account reading
its own vs. another's data, an account trying to place an order on another
account's behalf, admin-only reports).

This is API-key auth, not a full user/session system on purpose -- proportionate
to a project this size, and enough to demonstrate the actual access-control
requirement (each account is scoped to its own data). A real deployment would
still want the admin role backed by individual staff accounts rather than one
shared secret; see "What I'd do next."

## A bug I hit while building this

Two of the tests originally asked for more units than the seeded fabric roll could
supply (5 units at 2.1m/unit = 10.5m, against a 10m roll) -- the tests failed with
`InsufficientFabricError`, which was correct behavior catching wrong test data, not
a bug in the service layer. Fixed by correcting the test fixtures, not the logic.
Kept as `tests/test_erp.py::test_production_run_rejects_insufficient_fabric` and
the surrounding tests, which now pin this behavior deliberately.

A second, more consequential one: a repo-structure cleanup commit
("Fix repo structure: move project files to repo root") deleted the entire
multi-channel implementation (`Account`, `SalesChannel`, `CHANNEL_POLICIES`,
`ChannelRequirementError`, the whole six-channel `place_order()`) instead of
moving it -- the working tree silently reverted to the single-channel,
franchisee-only version from before that feature existed, while this README
kept describing the multi-channel system as if it were live. Found via `git
log`/`git show` against the object database (the multi-channel commit was
still there, just orphaned from the working tree) and restored from the
`Add multi-channel sales` commit rather than rewritten from scratch. Worth
noting because it's exactly the kind of regression that's invisible unless
you check the working tree against the docs, not just against the tests --
the single-channel tests still passed the whole time.

## Running it

```bash
pip install -r requirements.txt
export ADMIN_API_KEY=some-secret-you-choose   # see "Role-based auth" above
uvicorn app.main:app --reload
python scripts/seed.py     # 9 accounts across all 6 channels, 18 SKUs, 9 starting rolls
                            # -- prints each account's id + api_key, and the admin key
```

Try the full loop (swap in the api_key values `seed.py` printed for you):

```bash
ADMIN="X-API-Key: some-secret-you-choose"

# Turn fabric into finished goods (admin: production is a staff operation)
curl -X POST localhost:8000/production-runs -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"product_id": 1, "units_to_produce": 5}'

# See how each channel's checkout procedure differs -- no auth needed, this is policy not data
curl localhost:8000/channels

# A supermarket order without a PO number is rejected (422) -- no order created
curl -X POST localhost:8000/sales-orders -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"account_id": 6, "lines": [{"product_id": 1, "quantity": 1}]}'

# ...with a PO number, it's confirmed and palletized
curl -X POST localhost:8000/sales-orders -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"account_id": 6, "lines": [{"product_id": 1, "quantity": 1}], "po_number": "PO-1001"}'

# A company-retail order moves stock but raises no invoice
curl -X POST localhost:8000/sales-orders -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"account_id": 4, "lines": [{"product_id": 1, "quantity": 1}]}'

# Record a payment against an invoice
curl -X POST localhost:8000/invoices/1/payments -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"amount": 50}'

# The cross-department view: finished-goods stock, remnant fabric, total AR (admin only)
curl -H "$ADMIN" localhost:8000/reports/summary

# Revenue and order count by channel (admin only)
curl -H "$ADMIN" localhost:8000/reports/sales-by-channel

# A walk-in customer, looked up/created by email (the manual stand-in for a POS integration)
curl -X POST "localhost:8000/accounts/individual/lookup-or-create?contact_email=new.customer@example.com&name=Walk-in:%20New%20Customer&location=Zurich" \
  -H "$ADMIN"

# An account can see its own orders and AR -- but gets a 403 on another account's, or on /reports/*
curl -H "X-API-Key: <that account's own key>" localhost:8000/accounts/8/orders
curl -H "X-API-Key: <that account's own key>" localhost:8000/accounts/8/outstanding-ar
```

Measured on this machine: production run + sales order + summary report each
respond in low single-digit milliseconds (SQLite, no network hop) -- the interesting
cost here is transactional correctness, not latency.

### Tests

```bash
pytest tests/ -v   # 21 passed
```

`tests/test_erp.py` (11 tests, pure service-layer, no auth involved): remnant
creation vs. scrap-below-threshold, remnant-first allocation, insufficient-fabric
rejection (no partial stock change), atomic order rejection on insufficient
finished-goods stock, payment/overpayment handling, and the three
channel-specific behaviors (PO-number requirement, customs-declaration
requirement, and company-retail orders skipping invoicing).

`tests/test_auth.py` (10 tests, HTTP-level via FastAPI's TestClient): missing/
invalid API key rejected, an account reading its own vs. another account's
orders/AR (200 vs. 403), an account trying to place an order on another
account's behalf (403), admin seeing any account's data and the system-wide
reports, an account being blocked from those same reports, two individual/
walk-in accounts never sharing AR or order history, and
`lookup-or-create` being idempotent per `contact_email`.

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
- Replace the single shared `ADMIN_API_KEY` with a real staff/user table --
  right now every internal user is indistinguishable from every other admin,
  which is fine for a demo and not fine for a real deployment (no per-staff
  audit trail, no way to revoke one person's access).
- Wire `services.get_or_create_individual_account()` up to an actual
  point-of-sale system instead of the manual/API-triggered lookup -- the data
  model and access control are ready for it; the integration itself doesn't
  exist yet because there's no POS system in this project to integrate with.
- API keys are returned once in plaintext and stored in the DB as plain text
  columns -- fine for a demo, but a real system would hash them at rest
  (like passwords) and support rotation/revocation.
