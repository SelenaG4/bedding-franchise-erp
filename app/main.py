"""Bedding Franchise ERP/CRM.

A small, working ERP unifying three functions that are commonly siloed at a
mid-sized manufacturer: Production (fabric -> finished bedding goods), Sales
(across six distribution channels), and Accounting (invoices/payments) --
against one shared system of record instead of three disconnected
spreadsheets or systems.

Run locally:
    export ADMIN_API_KEY=some-secret-you-choose   # see app/auth.py
    uvicorn app.main:app --reload
    python scripts/seed.py       # loads sample accounts, products, fabric rolls
                                  # -- prints each account's id + api_key

Then try, in order:
    POST /production-runs   -- turn a fabric roll into finished-goods stock
    POST /sales-orders      -- an order through any of the six channels
    POST /invoices/{id}/payments
    GET  /reports/summary   -- the cross-department view (admin only)
    GET  /reports/sales-by-channel (admin only)
    GET  /accounts/{id}/orders          -- an account's own orders
    GET  /accounts/{id}/outstanding-ar  -- an account's own AR

Every request except GET /health and GET /channels requires an X-API-Key
header -- either an account's own key (self-service, scoped to that account
only) or the admin key (internal staff, sees everything). See app/auth.py.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import auth, services
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


ApiKeyHeader = Header(default=None, alias="X-API-Key")


# ---- setup / master data (admin only -- internal ERP staff manage master data) --


class AccountIn(BaseModel):
    name: str
    channel: SalesChannel
    location: str
    credit_limit: float = 0.0
    contact_email: str | None = None


@app.post("/accounts")
def create_account(payload: AccountIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
        a = Account(**payload.model_dump())
        session.add(a)
        session.commit()
        # api_key is only ever returned in plaintext here, at creation --
        # same convention as any real API-key-issuing system.
        return {"id": a.id, "name": a.name, "channel": a.channel.value, "api_key": a.api_key}


@app.post("/accounts/individual/lookup-or-create")
def lookup_or_create_individual_account(
    contact_email: str, name: str, location: str, x_api_key: str | None = ApiKeyHeader
) -> dict:
    """The manual stand-in for a POS integration (see services.
    get_or_create_individual_account): a walk-in customer gets their own
    persistent account keyed on contact_email instead of one shared bucket.
    Admin-only for now -- in a real deployment, the POS terminal would call
    this with its own service credential rather than a customer's key, since
    a first-time customer doesn't have one yet."""
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
        account = services.get_or_create_individual_account(session, contact_email, name, location)
        return {
            "id": account.id,
            "name": account.name,
            "contact_email": account.contact_email,
            "api_key": account.api_key,
        }


@app.get("/channels")
def list_channels() -> dict:
    """The checkout procedure and packaging for each sales channel. Public --
    this is policy, not account data."""
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
def create_product(payload: ProductIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
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
def create_fabric_roll(payload: FabricRollIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
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
def create_production_run(payload: ProductionRunIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
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


# ---- sales across all channels ---------------------------------------------


class OrderLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class SalesOrderIn(BaseModel):
    account_id: int
    lines: list[OrderLineIn]
    po_number: str | None = None
    customs_declaration: str | None = None


@app.post("/sales-orders")
def create_sales_order(payload: SalesOrderIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        # Self-service: an account can place its own orders but not order on
        # behalf of another account. Admin can place an order for anyone
        # (e.g. staff taking a phone/walk-in order on a customer's behalf).
        principal = auth.get_principal(session, x_api_key)
        auth.ensure_account_access(principal, payload.account_id)

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


# ---- accounting (admin only -- staff reconcile incoming payments) ---------


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)


@app.post("/invoices/{invoice_id}/payments")
def pay_invoice(invoice_id: int, payload: PaymentIn, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
        try:
            invoice = services.record_payment(session, invoice_id, payload.amount)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"invoice_id": invoice.id, "balance": invoice.balance}


# ---- reporting: the cross-department view (admin only -- system-wide) -----


@app.get("/reports/summary")
def summary(x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
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
def sales_by_channel(x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        auth.ensure_admin(auth.get_principal(session, x_api_key))
        return {"channels": services.sales_by_channel(session)}


# ---- an account's own data -- the role-based-auth boundary ----------------


@app.get("/accounts/{account_id}/outstanding-ar")
def account_ar(account_id: int, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        principal = auth.get_principal(session, x_api_key)
        auth.ensure_account_access(principal, account_id)
        return {"account_id": account_id, "outstanding_ar": services.outstanding_ar(session, account_id)}


@app.get("/accounts/{account_id}/orders")
def account_orders(account_id: int, x_api_key: str | None = ApiKeyHeader) -> dict:
    with db() as session:
        principal = auth.get_principal(session, x_api_key)
        auth.ensure_account_access(principal, account_id)
        orders = services.orders_for_account(session, account_id)
        return {
            "account_id": account_id,
            "orders": [
                {
                    "id": o.id,
                    "status": o.status.value,
                    "channel": o.channel.value,
                    "order_date": o.order_date.isoformat(),
                    "total_value": o.total_value,
                }
                for o in orders
            ],
        }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
