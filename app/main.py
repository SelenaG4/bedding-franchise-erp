"""Bedding Franchise ERP/CRM.

A small, working ERP unifying three functions that are commonly siloed at a
mid-sized manufacturer: Production (fabric -> finished bedding goods), Sales
(across six distribution channels), and Accounting (invoices/payments) --
against one shared system of record instead of three disconnected
spreadsheets or systems.

Run locally:
    uvicorn app.main:app --reload
    python scripts/seed.py       # loads sample accounts, products, fabric rolls
                                  # -- also prints the admin key + each account's
                                  # own API key, needed for every request below

Then try, in order (every request needs an X-API-Key header -- see "Role-based
auth" below):
    POST /production-runs   -- turn a fabric roll into finished-goods stock
    POST /sales-orders      -- an order through any of the six channels
    POST /invoices/{id}/payments
    GET  /reports/summary   -- the cross-department view
    GET  /reports/sales-by-channel

Two planning tools, on top of the day-to-day operations above:
    POST /optimization/production-plan  -- batch fabric-cutting plan: greedy
                                            heuristic vs. MILP-optimal, compared
    POST /simulation/reorder-point      -- Monte Carlo reorder-point/safety-stock
                                            recommendation vs. the textbook formula

Role-based auth (app/auth.py): every request needs an `X-API-Key` header.
- The admin key (`ADMIN_API_KEY`) can do everything -- setup/master data,
  production, reports, and any account's orders/AR.
- Each sales account has its own key (`Account.api_key`, generated at
  creation and printed by scripts/seed.py) and can only place its own orders
  and see its own orders/AR -- never another account's, never system-wide
  reports. That scoping is enforced at this API boundary
  (app/auth.get_principal + ensure_account_access/ensure_admin), not inside
  app/services.py, which stays pure business logic with no notion of who's
  asking.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import auth, optimization, services, simulation
from app.db import get_session, init_db
from app.models import Account, FabricRoll, Product, SalesChannel, SalesOrder

app = FastAPI(
    title="Bedding Franchise ERP/CRM",
    description="Unified production, multi-channel sales, and accounting for a bedding manufacturer.",
    version="0.4.0",
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page() -> str:
    """Plain-language landing page for non-technical reviewers -- an API key
    field, a KPI dashboard, the roadmap and schema diagrams, and a form for
    every operation in the README, so nobody needs to know REST/JSON to try
    this. /docs (Swagger) is still there for anyone who wants the raw API."""
    return (_STATIC_DIR / "index.html").read_text()


def db() -> Session:
    return get_session()


def _current_principal(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> auth.Principal:
    """FastAPI dependency wrapping app.auth.get_principal(): opens its own
    short-lived session just to resolve the caller's identity from the
    X-API-Key header, separate from the session each route handler opens for
    its own work below. Two connections per request is a bit more than
    strictly necessary at this scale, but keeps auth resolution decoupled
    from each route's own transaction -- simpler to reason about than
    threading one shared session through FastAPI's dependency graph for a
    project this size."""
    with db() as session:
        return auth.get_principal(session, x_api_key)


def _account_out(a: Account) -> dict:
    return {"id": a.id, "name": a.name, "channel": a.channel.value, "api_key": a.api_key}


def _order_out(o: SalesOrder) -> dict:
    return {
        "id": o.id,
        "account_id": o.account_id,
        "status": o.status.value,
        "channel": o.channel.value,
        "packaging": o.packaging,
        "po_number": o.po_number,
        "customs_declaration": o.customs_declaration,
        "total_value": o.total_value,
        "order_date": o.order_date.isoformat(),
    }


# ---- setup / master data (admin only) --------------------------------------


class AccountIn(BaseModel):
    name: str
    channel: SalesChannel
    location: str
    credit_limit: float = 0.0
    contact_email: str | None = None


@app.post("/accounts")
def create_account(payload: AccountIn, principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        a = Account(**payload.model_dump())
        session.add(a)
        session.commit()
        return _account_out(a)


@app.post("/accounts/individual/lookup-or-create")
def lookup_or_create_individual_account(
    contact_email: str,
    name: str,
    location: str,
    principal: auth.Principal = Depends(_current_principal),
) -> dict:
    """The admin-facing counterpart to services.get_or_create_individual_account():
    looks up an existing individual/walk-in account by contact_email, or
    creates one, so a repeat walk-in customer gets their own persistent
    account (and their own AR/order history) rather than everyone sharing one
    bucket account. A real POS integration would call this at checkout time."""
    auth.ensure_admin(principal)
    with db() as session:
        account = services.get_or_create_individual_account(session, contact_email, name, location)
        return _account_out(account)


@app.get("/accounts")
def list_accounts(principal: auth.Principal = Depends(_current_principal)) -> dict:
    """Admin only -- listing every account (with its api_key) is exactly the
    kind of system-wide visibility the role-based auth model reserves for
    admin. Powers the demo landing page's account picker, which lets a
    visitor copy a specific account's key and see the access scoping
    enforced live rather than just described."""
    auth.ensure_admin(principal)
    with db() as session:
        accounts = session.query(Account).order_by(Account.id).all()
        return {"accounts": [_account_out(a) for a in accounts]}


@app.get("/channels")
def list_channels() -> dict:
    """The checkout procedure and packaging for each sales channel. Public --
    informational, not account-specific, no auth required."""
    return {
        channel.value: {
            "payment_terms": policy.payment_terms,
            "packaging": policy.packaging,
            "requires_po_number": policy.requires_po_number,
            "requires_customs_docs": policy.requires_customs_docs,
            "raises_invoice": policy.raises_invoice,
        }
        for channel, policy in services.CHANNEL_POLICIES.items()
    }


class ProductIn(BaseModel):
    sku: str
    name: str
    product_line: str
    unit_price: float
    fabric_meters_per_unit: float


@app.post("/products")
def create_product(payload: ProductIn, principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        p = Product(**payload.model_dump())
        session.add(p)
        session.commit()
        return {"id": p.id, "sku": p.sku}


class FabricRollIn(BaseModel):
    roll_code: str
    fabric_type: str
    color: str
    total_length_m: float


@app.post("/fabric-rolls")
def create_fabric_roll(payload: FabricRollIn, principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        roll = FabricRoll(
            roll_code=payload.roll_code,
            fabric_type=payload.fabric_type,
            color=payload.color,
            total_length_m=payload.total_length_m,
            remaining_length_m=payload.total_length_m,
            is_remnant=False,
        )
        session.add(roll)
        session.commit()
        return {"id": roll.id, "roll_code": roll.roll_code}


# ---- production (admin only) -----------------------------------------------


class ProductionRunIn(BaseModel):
    product_id: int
    units_to_produce: int
    fabric_roll_id: int | None = None


@app.post("/production-runs")
def create_production_run(
    payload: ProductionRunIn, principal: auth.Principal = Depends(_current_principal)
) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        try:
            run = services.run_production(
                session, payload.product_id, payload.units_to_produce, payload.fabric_roll_id
            )
        except (services.InsufficientFabricError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "id": run.id,
            "units_produced": run.units_produced,
            "fabric_consumed_m": run.fabric_consumed_m,
            "leftover_m": run.leftover_m,
            "remnant_roll_id": run.remnant_roll_id,
            "scrapped_m": run.scrapped_m,
        }


# ---- sales across all channels (any authenticated principal, scoped) ------


class OrderLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class SalesOrderIn(BaseModel):
    account_id: int
    lines: list[OrderLineIn]
    po_number: str | None = None
    customs_declaration: str | None = None


@app.post("/sales-orders")
def create_sales_order(
    payload: SalesOrderIn, principal: auth.Principal = Depends(_current_principal)
) -> dict:
    """Admin can place an order for any account. An account principal can
    only place an order for itself -- attempting to order on behalf of a
    different account_id is rejected (403) before anything is touched."""
    auth.ensure_account_access(principal, payload.account_id)
    with db() as session:
        lines = [services.OrderLineRequest(l.product_id, l.quantity) for l in payload.lines]
        try:
            order = services.place_order(
                session,
                payload.account_id,
                lines,
                po_number=payload.po_number,
                customs_declaration=payload.customs_declaration,
            )
        except services.ChannelRequirementError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except services.InsufficientStockError as e:
            raise HTTPException(
                status_code=409,
                detail={"message": "Order rejected: insufficient finished-goods stock", "shortfalls": e.shortfalls},
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _order_out(order)


# ---- accounting (admin only) -----------------------------------------------


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)


@app.post("/invoices/{invoice_id}/payments")
def pay_invoice(
    invoice_id: int, payload: PaymentIn, principal: auth.Principal = Depends(_current_principal)
) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        try:
            invoice = services.record_payment(session, invoice_id, payload.amount)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"invoice_id": invoice.id, "balance": invoice.balance}


# ---- reporting: the cross-department view (admin only) ---------------------


@app.get("/reports/summary")
def summary(principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        products = session.query(Product).all()
        finished_goods = {
            p.sku: services.finished_goods_stock(session, p.id) for p in products
        }
        return {
            "finished_goods_stock": finished_goods,
            "remnant_inventory": services.remnant_inventory_summary(session),
            "total_outstanding_ar": services.outstanding_ar(session),
        }


@app.get("/reports/sales-by-channel")
def sales_by_channel(principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_admin(principal)
    with db() as session:
        return {"channels": services.sales_by_channel(session)}


# ---- per-account views (admin, or that account's own key) -----------------


@app.get("/accounts/{account_id}/outstanding-ar")
def account_ar(account_id: int, principal: auth.Principal = Depends(_current_principal)) -> dict:
    auth.ensure_account_access(principal, account_id)
    with db() as session:
        return {"account_id": account_id, "outstanding_ar": services.outstanding_ar(session, account_id)}


@app.get("/accounts/{account_id}/orders")
def account_orders(account_id: int, principal: auth.Principal = Depends(_current_principal)) -> dict:
    """An account's own order history. Together with outstanding-ar above,
    this is the full "self-service" surface a sales account's own API key
    can reach -- everything else (setup, production, system-wide reports)
    stays admin-only."""
    auth.ensure_account_access(principal, account_id)
    with db() as session:
        orders = services.orders_for_account(session, account_id)
        return {"account_id": account_id, "orders": [_order_out(o) for o in orders]}


# ---- optimization: batch production planning (admin only) -----------------


class PendingRunIn(BaseModel):
    product_id: int
    units_to_produce: int = Field(gt=0)
    label: str | None = None


class ProductionPlanIn(BaseModel):
    pending_runs: list[PendingRunIn]


def _serialize_plan(plan: optimization.BatchPlanResult) -> dict:
    return {
        "method": plan.method,
        "assignments": [
            {
                "run_label": a.run_label,
                "product_id": a.product_id,
                "units_to_produce": a.units_to_produce,
                "meters_needed": a.meters_needed,
                "roll_id": a.roll_id,
                "roll_code": a.roll_code,
                "leftover_m": a.leftover_m,
                "is_scrap": a.is_scrap,
            }
            for a in plan.assignments
        ],
        "unassignable": plan.unassignable,
        "total_scrap_m": plan.total_scrap_m,
        "total_leftover_m": plan.total_leftover_m,
    }


@app.post("/optimization/production-plan")
def production_plan(
    payload: ProductionPlanIn, principal: auth.Principal = Depends(_current_principal)
) -> dict:
    """Compares the greedy one-at-a-time allocation heuristic against a
    jointly-optimal MILP assignment for the same batch of pending production
    runs against the current fabric-roll pool. See app/optimization.py for
    the full method and its stated scope. Admin-only, same as production
    and reporting -- this is an internal planning tool, not customer-facing."""
    auth.ensure_admin(principal)
    with db() as session:
        pending = [
            optimization.PendingRun(pr.product_id, pr.units_to_produce, pr.label or "")
            for pr in payload.pending_runs
        ]
        try:
            comparison = optimization.compare_plans(session, pending)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "greedy": _serialize_plan(comparison.greedy),
            "optimal": _serialize_plan(comparison.optimal),
            "scrap_reduction_m": comparison.scrap_reduction_m,
            "scrap_reduction_pct": comparison.scrap_reduction_pct,
        }


# ---- simulation: reorder-point / safety-stock recommendation (admin only) -


class ReorderPointIn(BaseModel):
    product_id: int
    lead_time_days: int = Field(gt=0)
    target_service_level: float = Field(default=0.95, gt=0, lt=1)
    num_trials: int = Field(default=5000, gt=0, le=200_000)


@app.post("/simulation/reorder-point")
def reorder_point(
    payload: ReorderPointIn, principal: auth.Principal = Depends(_current_principal)
) -> dict:
    """Monte Carlo reorder-point/safety-stock recommendation, bootstrapped
    from data/demand_history.csv (see scripts/generate_demand_history.py),
    compared against the textbook Normal-formula answer on the same
    simulated trials. See app/simulation.py for the full method. Admin-only,
    same reasoning as the production planner above."""
    auth.ensure_admin(principal)
    try:
        rec = simulation.recommend_reorder_point(
            payload.product_id, payload.lead_time_days, payload.target_service_level, payload.num_trials
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "sku": rec.sku,
        "lead_time_days": rec.lead_time_days,
        "target_service_level": rec.target_service_level,
        "num_historical_days": rec.num_historical_days,
        "num_trials": rec.num_trials,
        "mean_lead_time_demand": rec.mean_lead_time_demand,
        "std_lead_time_demand": rec.std_lead_time_demand,
        "simulated_reorder_point": rec.simulated_reorder_point,
        "simulated_safety_stock": rec.simulated_safety_stock,
        "simulated_achieved_service_level": rec.simulated_achieved_service_level,
        "formula_reorder_point": rec.formula_reorder_point,
        "formula_achieved_service_level": rec.formula_achieved_service_level,
        "formula_undercoverage_pct": rec.formula_undercoverage_pct,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
