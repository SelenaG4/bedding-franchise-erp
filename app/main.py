"""Bedding Franchise ERP/CRM.

A small, working ERP unifying three functions that are commonly siloed at a
mid-sized manufacturer: Production (fabric -> finished bedding goods), Sales
(across six distribution channels), and Accounting (invoices/payments) --
against one shared system of record instead of three disconnected
spreadsheets or systems.

Run locally:
    uvicorn app.main:app --reload
    python scripts/seed.py       # loads sample accounts, products, fabric rolls

Then try, in order:
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
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import optimization, services, simulation
from app.db import get_session, init_db
from app.models import Account, FabricRoll, Product, SalesChannel

app = FastAPI(
    title="Bedding Franchise ERP/CRM",
    description="Unified production, multi-channel sales, and accounting for a bedding manufacturer.",
    version="0.3.0",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def db() -> Session:
    return get_session()


# ---- setup / master data -------------------------------------------------


class AccountIn(BaseModel):
    name: str
    channel: SalesChannel
    location: str
    credit_limit: float = 0.0
    contact_email: str | None = None


@app.post("/accounts")
def create_account(payload: AccountIn) -> dict:
    with db() as session:
        a = Account(**payload.model_dump())
        session.add(a)
        session.commit()
        return {"id": a.id, "name": a.name, "channel": a.channel.value}


@app.get("/channels")
def list_channels() -> dict:
    """The checkout procedure and packaging for each sales channel."""
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
def create_product(payload: ProductIn) -> dict:
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
def create_fabric_roll(payload: FabricRollIn) -> dict:
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


# ---- production -----------------------------------------------------------


class ProductionRunIn(BaseModel):
    product_id: int
    units_to_produce: int
    fabric_roll_id: int | None = None


@app.post("/production-runs")
def create_production_run(payload: ProductionRunIn) -> dict:
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


# ---- sales across all channels --------------------------------------------


class OrderLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class SalesOrderIn(BaseModel):
    account_id: int
    lines: list[OrderLineIn]
    po_number: str | None = None
    customs_declaration: str | None = None


@app.post("/sales-orders")
def create_sales_order(payload: SalesOrderIn) -> dict:
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
        return {
            "id": order.id,
            "status": order.status.value,
            "channel": order.channel.value,
            "packaging": order.packaging,
            "total_value": order.total_value,
        }


# ---- accounting -------------------------------------------------------


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)


@app.post("/invoices/{invoice_id}/payments")
def pay_invoice(invoice_id: int, payload: PaymentIn) -> dict:
    with db() as session:
        try:
            invoice = services.record_payment(session, invoice_id, payload.amount)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"invoice_id": invoice.id, "balance": invoice.balance}


# ---- reporting: the cross-department view ---------------------------------


@app.get("/reports/summary")
def summary() -> dict:
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
def sales_by_channel() -> dict:
    with db() as session:
        return {"channels": services.sales_by_channel(session)}


@app.get("/accounts/{account_id}/outstanding-ar")
def account_ar(account_id: int) -> dict:
    with db() as session:
        return {"account_id": account_id, "outstanding_ar": services.outstanding_ar(session, account_id)}


# ---- optimization: batch production planning ------------------------------


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
def production_plan(payload: ProductionPlanIn) -> dict:
    """Compares the greedy one-at-a-time allocation heuristic against a
    jointly-optimal MILP assignment for the same batch of pending production
    runs against the current fabric-roll pool. See app/optimization.py for
    the full method and its stated scope."""
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


# ---- simulation: reorder-point / safety-stock recommendation --------------


class ReorderPointIn(BaseModel):
    product_id: int
    lead_time_days: int = Field(gt=0)
    target_service_level: float = Field(default=0.95, gt=0, lt=1)
    num_trials: int = Field(default=5000, gt=0, le=200_000)


@app.post("/simulation/reorder-point")
def reorder_point(payload: ReorderPointIn) -> dict:
    """Monte Carlo reorder-point/safety-stock recommendation, bootstrapped
    from data/demand_history.csv (see scripts/generate_demand_history.py),
    compared against the textbook Normal-formula answer on the same
    simulated trials. See app/simulation.py for the full method."""
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
