"""Bedding Franchise ERP/CRM.

A small, working ERP unifying three functions that are commonly siloed at a
mid-sized manufacturer: Production (fabric -> finished bedding goods), Sales
(franchisee orders), and Accounting (invoices/payments) -- against one shared
system of record instead of three disconnected spreadsheets or systems.

Run locally:
    uvicorn app.main:app --reload
    python scripts/seed.py       # loads sample franchisees, products, fabric rolls

Then try, in order:
    POST /production-runs   -- turn a fabric roll into finished-goods stock
    POST /sales-orders      -- a franchisee order, deducting stock + raising an invoice
    POST /invoices/{id}/payments
    GET  /reports/summary   -- the cross-department view
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_session, init_db
from app.models import Franchisee, Product, FabricRoll

app = FastAPI(
    title="Bedding Franchise ERP/CRM",
    description="Unified production, franchise sales, and accounting for a bedding manufacturer.",
    version="0.1.0",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def db() -> Session:
    return get_session()


# ---- setup / master data -------------------------------------------------


class FranchiseeIn(BaseModel):
    name: str
    territory: str
    credit_limit: float = 0.0
    contact_email: str | None = None


@app.post("/franchisees")
def create_franchisee(payload: FranchiseeIn) -> dict:
    with db() as session:
        f = Franchisee(**payload.model_dump())
        session.add(f)
        session.commit()
        return {"id": f.id, "name": f.name}


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


# ---- sales / franchise orders ---------------------------------------------


class OrderLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class SalesOrderIn(BaseModel):
    franchisee_id: int
    lines: list[OrderLineIn]


@app.post("/sales-orders")
def create_sales_order(payload: SalesOrderIn) -> dict:
    with db() as session:
        lines = [services.OrderLineRequest(l.product_id, l.quantity) for l in payload.lines]
        try:
            order = services.place_franchise_order(session, payload.franchisee_id, lines)
        except services.InsufficientStockError as e:
            raise HTTPException(
                status_code=409,
                detail={"message": "Order rejected: insufficient finished-goods stock", "shortfalls": e.shortfalls},
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": order.id, "status": order.status.value}


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


@app.get("/franchisees/{franchisee_id}/outstanding-ar")
def franchisee_ar(franchisee_id: int) -> dict:
    with db() as session:
        return {"franchisee_id": franchisee_id, "outstanding_ar": services.outstanding_ar(session, franchisee_id)}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
