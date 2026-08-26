# Bedding Franchise ERP/CRM

A small, working ERP unifying **Production**, **Multi-Channel Sales**, and
**Accounting** for a bedding manufacturer -- one shared system of record instead
of three disconnected spreadsheets or systems, which is the actual problem a
manufacturer's first ERP/CRM rollout is usually solving.

On top of day-to-day operations, two planning tools apply real operations-
research methods to problems this same domain already surfaces: a
mixed-integer program that jointly plans a batch of fabric cuts instead of
committing to each one greedily (`app/optimization.py`), and a Monte Carlo
simulation that derives reorder points from actual demand history instead of
a textbook formula that assumes demand is Normally distributed
(`app/simulation.py`). Both are described in detail below, including what
each one is measured to actually improve.

## Why this exists

A bedding manufacturer that starts out selling through one or two channels
eventually needs to grow -- opening its own retail shops, signing supermarket
and franchise partners, and shipping internationally. That growth breaks a
single-channel "orders and inventory" system fast: production, sales, and
accounting stay siloed across separate spreadsheets or tools; a real
material-tracking problem (leftover fabric from cutting) goes untracked as
waste under a generic inventory schema; and six genuinely different
checkout procedures -- prepaid vs. net-30 vs. net-60, PO-number and
customs-declaration requirements, internal stock transfers to company-owned
stores -- can't be forced into one uniform "add to cart" flow.

This is the core database and business-logic system built for exactly that
expansion: one shared system of record spanning production, all six sales
channels, and accounting, with fabric-remnant tracking and each channel's
own rules built into the schema and logic itself, not bolted on after the
fact.

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

## Optimization: batch production planning (MILP)

`allocate_fabric_roll()` (used by `run_production()`, above) makes the best
*local* choice for one production run at a time -- exactly what happens if a
planner submits runs one by one. In practice a planner queues a batch (a
day's worth of pending runs) before committing to cuts, and assigning a batch
jointly is a different, harder problem: N pending runs, M available rolls,
one roll per run, minimizing total scrap. A sequence of locally-best picks
can leave more scrap on the table than a plan that considers the whole batch
at once.

`app/optimization.py` solves the batch version as a 0/1 integer program --
binary variables for every feasible (run, roll) pairing, one-roll-per-run and
one-run-per-roll constraints, minimize total scrap -- via `scipy.optimize.milp`
(the HiGHS solver), and separately reproduces the greedy heuristic's behavior
on the identical input so the two can be compared directly, not just asserted
to differ.

**Proven, not just claimed:** `tests/test_optimization.py::test_greedy_is_myopic_optimal_beats_it`
constructs a concrete two-run, two-roll scenario (roll lengths 5.0m and
5.2m; runs needing 3.1m and 3.0m) where the greedy heuristic's first pick
blocks the pairing that would leave zero scrap: greedy produces 1.9m of
scrap, the MILP finds the zero-scrap pairing, a 100% reduction on that batch.
A second test runs a larger 5-run/5-roll batch and asserts the general
invariant that must always hold: the optimal plan's scrap total is never
worse than greedy's, since greedy is itself one specific assignment the MILP
is also free to choose.

**Scope, stated honestly:** both planners work off a single static snapshot
of the roll pool as it exists right now. Neither models a remnant created by
run A becoming available to run B *within the same batch* -- that's a harder,
multi-stage cutting-stock problem (see "What I'd do next"). Because both
planners share that same assumption, the comparison between them is fair
even though neither captures that further optimization opportunity. The
objective is also "true waste" in this domain's own terms, not raw leftover
meters: a leftover at or above `REMNANT_MIN_LENGTH_M` becomes a usable
remnant roll (costs nothing in the objective); only leftover below that
threshold -- genuine scrap -- is counted, matching the distinction
`run_production()` already draws.

```bash
curl -X POST localhost:8000/optimization/production-plan -H "Content-Type: application/json" \
  -d '{"pending_runs": [{"product_id": 1, "units_to_produce": 3, "label": "queue-1"},
                          {"product_id": 2, "units_to_produce": 4, "label": "queue-2"}]}'
# Returns both plans (assignments + total scrap) side by side, plus
# scrap_reduction_m / scrap_reduction_pct. On the freshly-seeded 80m rolls
# there's enough slack that both plans already hit zero scrap -- the gap only
# shows up when the roll pool is tighter, which is exactly what the test
# above is built to demonstrate deterministically.
```

## Simulation: reorder-point / safety-stock (Monte Carlo)

The original "what I'd do next" list for this project said "add a
reorder-point alert on finished-goods stock." A naive version of that is a
single hand-picked number. `app/simulation.py` instead derives it: given a
product's historical daily demand, it bootstraps thousands of simulated
lead-time windows (resample `lead_time_days` historical daily observations
with replacement and sum them, repeated `num_trials` times) and takes the
reorder point as the quantile of that simulated distribution matching the
target service level -- correct by construction for whatever shape the real
demand data has, no assumption that demand is Normally distributed required.

No real order history existed to derive this from (`scripts/seed.py` only
creates starting accounts/products/rolls, not a sales history), so
`scripts/generate_demand_history.py` generates one: 180 days x 18 SKUs of
synthetic daily demand with weekday/weekend seasonality (most of this
system's six channels are wholesale/B2B, so weekend demand is lighter) and a
5% daily chance of a bulk/promo-order spike (2.5x-4x demand, the kind of
thing a franchisee or supermarket partner placing a much larger-than-usual
order actually looks like). That makes the demand distribution right-skewed
-- not Normal -- on purpose, to test the thing this feature is actually
for: does the simulation-based recommendation hold up where a textbook
formula wouldn't?

**Measured, on this generated history:** `scripts/run_reorder_point_analysis.py`
runs the simulation for all 18 SKUs and also scores the textbook closed-form
answer (`mean + z * std`, assuming Normal demand) against the *same*
simulated trials. Every one of the 18 SKUs shows the formula falling short of
the 95% target -- average **1.61 percentage points** of undercoverage, worst
case **3.18pp** (a low-volume SKU where the discreteness of small-count
demand makes a fraction-of-a-unit difference in the reorder point swing the
achieved service level noticeably). `tests/test_simulation.py::test_formula_undercoverages_on_right_skewed_spiky_demand`
pins this as a regression test on a synthetic spiky demand series, so it's
not just true of one dataset generated once.

![Reorder point: simulated recommendation (green) vs. the Normal-formula recommendation (red, dashed), against the actual simulated 7-day demand distribution for BED-009 -- the formula sits inside the distribution's body while the simulated point sits at the true 95th percentile](docs/reorder_point_simulation.png)

```bash
python scripts/generate_demand_history.py       # writes data/demand_history.csv
python scripts/run_reorder_point_analysis.py     # writes data/reorder_point_recommendations.csv + the chart above

curl -X POST localhost:8000/simulation/reorder-point -H "Content-Type: application/json" \
  -d '{"product_id": 9, "lead_time_days": 7, "target_service_level": 0.95}'
```

## Running it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
python scripts/seed.py     # 8 accounts across all 6 channels, 18 SKUs, 9 starting rolls
python scripts/generate_demand_history.py   # synthetic demand history, needed for /simulation/reorder-point
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
pytest tests/ -v   # 24 passed
```

Covers: remnant creation vs. scrap-below-threshold, remnant-first allocation,
insufficient-fabric rejection (no partial stock change), atomic order rejection
on insufficient finished-goods stock, payment/overpayment handling, and the
three channel-specific behaviors (PO-number requirement, customs-declaration
requirement, and company-retail orders skipping invoicing) -- plus, for the two
planning tools: the constructed scenario proving the MILP batch plan beats
greedy, the invariant that it's never worse across a larger randomized batch,
correct scrap accounting at the remnant-threshold boundary, reproducibility of
the Monte Carlo simulation under a fixed seed, the achieved-service-level
correctness property of the simulated reorder point, and the formula-vs-
simulation undercoverage finding on a synthetic right-skewed demand series.

### Docker

```bash
docker build -t bedding-franchise-erp .
docker run -p 8000:8000 bedding-franchise-erp
```

(Written and structurally checked; not run in this sandbox, which has no Docker
daemon -- build locally to confirm before demoing.)

### Live demo

Deployed on Render's free tier: **https://bedding-franchise-erp.onrender.com/**.
The free tier spins the container down after 15 minutes idle, so the first
request after a lull takes ~20-30s to wake it back up -- visit
[`/docs`](https://bedding-franchise-erp.onrender.com/docs) for the interactive
Swagger UI to try every endpoint above directly in the browser, including the
two planning tools (`/optimization/production-plan`, `/simulation/reorder-point`).

### CI

GitHub Actions runs the test suite, a seed-script smoke test, and the full
reorder-point simulation pipeline on every push to `main` (see
`.github/workflows/ci.yml`), uploading the simulation chart and
recommendations CSV as build artifacts each run.

## A CI bug I hit (and how it was diagnosed)

The first CI run passed, but that was luck, not correctness: `pytest tests/`
run bare (as CI does) failed with `ModuleNotFoundError: No module named 'app'`
the moment I re-tested it in a clean virtualenv, even though `pytest tests/ -v`
had worked fine for me locally throughout development.

The cause is a pytest import-mode detail. By default, pytest walks up from
each test file looking for `__init__.py` files, and adds the first directory
*without* one to `sys.path`. This repo's `tests/` has no `__init__.py`, so
that directory is `tests/` itself -- `app/`, one level up, never lands on
`sys.path`, and `from app import services` fails. It only ever worked on my
machine because I'd been invoking pytest in a way (`python -m pytest`, which
also inserts the current directory) that papered over it. Fixed with a
`pytest.ini` that pins `pythonpath = .`, so the repo root is on `sys.path`
regardless of how pytest is invoked -- verified with a fresh virtualenv and
the bare `pytest tests/ -v` command CI actually runs (24 passed).

## What I'd do next with more time

- Move off SQLite to Postgres for real concurrent-write safety (SQLite's fine for a
  demo, not for multiple channels writing at once).
- Extend the batch production planner to model intra-batch remnant chaining
  (a remnant created by one run in the batch becoming available to a later
  run in the same batch) -- a genuinely harder multi-stage cutting-stock
  problem, deliberately out of scope for the current static-snapshot MILP
  (see "Optimization" above).
- Replace the synthetic demand history with real order history once there is
  any, and wire the reorder-point recommendation into an actual low-stock
  alert on `/reports/summary` instead of a separate on-demand endpoint.
- Add role-based auth so each account can only see their own orders/AR, not the
  whole system.
- Track individual/walk-in customers as their own accounts instead of one shared
  bucket, once there's an actual POS integration driving that channel.
