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
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_session, init_db
from app.models import Account, FabricRoll, Product, SalesChannel

app = FastAPI(
    title="Bedding Franchise ERP/CRM",
    description="Unified production, multi-channel sales, and accounting for a bedding manufacturer.",
    version="0.2.0",
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
