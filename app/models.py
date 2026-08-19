"""Schema for a small bedding manufacturer selling through franchisees.

Modeled on a real domain: a textile/bedding producer (production lines, franchise
sales, finished-goods inventory) with one deliberately domain-specific feature --
fabric rolls track their own remnants. When a production run doesn't use a whole
roll, the leftover becomes a new roll (flagged as a remnant, linked back to its
parent) rather than disappearing into an untracked "waste" bucket. Below a minimum
usable length it's recorded as scrap instead. This is the actual operational problem
a cut-and-sew bedding line has that a generic ERP schema wouldn't surface.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Below this length, leftover fabric is recorded as scrap rather than kept as a
# usable remnant roll -- too short to cut another panel from for this product line.
REMNANT_MIN_LENGTH_M = 2.0


class Base(DeclarativeBase):
    pass


class Franchisee(Base):
    __tablename__ = "franchisees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    territory: Mapped[str] = mapped_column(String, nullable=False)
    credit_limit: Mapped[float] = mapped_column(Float, default=0.0)
    contact_email: Mapped[str] = mapped_column(String, nullable=True)

    sales_orders: Mapped[list["SalesOrder"]] = relationship(back_populates="franchisee")


class Product(Base):
    """A finished bedding SKU, e.g. 'Cotton Sateen Duvet Cover - Queen'."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    product_line: Mapped[str] = mapped_column(String, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    fabric_meters_per_unit: Mapped[float] = mapped_column(Float, nullable=False)


class FabricRoll(Base):
    """A roll of raw fabric. Remnant rolls (is_remnant=True) are leftovers from a
    prior production run, linked back to the roll they were cut from."""

    __tablename__ = "fabric_rolls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    roll_code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    fabric_type: Mapped[str] = mapped_column(String, nullable=False)
    color: Mapped[str] = mapped_column(String, nullable=False)
    total_length_m: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_length_m: Mapped[float] = mapped_column(Float, nullable=False)
    is_remnant: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_roll_id: Mapped[int | None] = mapped_column(
        ForeignKey("fabric_rolls.id"), nullable=True
    )


class ProductionRun(Base):
    """One production event: consumes fabric from a roll, produces finished units."""

    __tablename__ = "production_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    fabric_roll_id: Mapped[int] = mapped_column(ForeignKey("fabric_rolls.id"), nullable=False)
    units_produced: Mapped[int] = mapped_column(Integer, nullable=False)
    fabric_consumed_m: Mapped[float] = mapped_column(Float, nullable=False)
    leftover_m: Mapped[float] = mapped_column(Float, nullable=False)
    remnant_roll_id: Mapped[int | None] = mapped_column(
        ForeignKey("fabric_rolls.id"), nullable=True
    )
    scrapped_m: Mapped[float] = mapped_column(Float, default=0.0)
    run_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FinishedGoodsMovement(Base):
    """Ledger of finished-goods stock changes, not a raw stock counter -- so stock
    level is always derivable and auditable (sum of movements), matching the
    idempotent-migrations/transactions approach used elsewhere."""

    __tablename__ = "finished_goods_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)  # + production, - sale
    reason: Mapped[str] = mapped_column(String, nullable=False)
    ref_type: Mapped[str] = mapped_column(String, nullable=False)  # "production_run" | "sales_order"
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrderStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    REJECTED_INSUFFICIENT_STOCK = "rejected_insufficient_stock"


class SalesOrder(Base):
    __tablename__ = "sales_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    franchisee_id: Mapped[int] = mapped_column(ForeignKey("franchisees.id"), nullable=False)
    order_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), nullable=False)

    franchisee: Mapped["Franchisee"] = relationship(back_populates="sales_orders")
    lines: Mapped[list["SalesOrderLine"]] = relationship(back_populates="order")


class SalesOrderLine(Base):
    __tablename__ = "sales_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)

    order: Mapped["SalesOrder"] = relationship(back_populates="lines")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    issued_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
